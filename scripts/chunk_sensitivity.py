#!/usr/bin/env python3
"""Sensitivity analysis of the chunk size (e.g. 256 / 512 / 1024 characters).

This analysis was not part of the thesis validation campaigns: it is listed
among the future developments (thesis §3.4.1 and §6.5), and this script is
the tooling prepared to run it.

The full procedure is in docs/validation/chunk_sensitivity.md. In short, for
each chunk size: set CHUNK_SIZE/CHUNK_OVERLAP in .env, recreate the
services, then

  1. prepare  (inside the AS-1 container) - make sure the ISO index matches
     the chunk configuration in effect, rebuilding it if needed, and delete
     every ORG-DOCS chunk of the experiment's org_id, so that no chunk of a
     previous configuration survives;
  2. run      (on the host) - check that AS-1, AS-2 and AS-3 all report the
     expected chunk size, then run the three agents K times on the same
     documents and save the evaluation cards.

Finally compare all configurations with the ground truth:

  3. compare  (on the host) - one row per configuration: coverage, verdict
     distribution, Gamma, agreement with the ground truth, AUTO-FLAG
     interventions, reproducibility and verdicts changed against the
     baseline.

Examples:
    docker compose exec as1 python scripts/chunk_sensitivity.py prepare --org-id chunksens-aerarium
    python scripts/chunk_sensitivity.py run --org-id chunksens-aerarium \\
        --docs-dir ./corpus/aerarium --label 256 --runs 1 --out results/chunks
    python scripts/chunk_sensitivity.py compare --out results/chunks \\
        --ground-truth ./ground_truth/aerarium.csv --baseline 512

Use a dedicated org_id (e.g. chunksens-<company>): prepare deletes all of
its company-document chunks. The agents are called directly, without the
AGA (it does not change the verdicts) and without writing to ORG-HISTORY.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDICTS = ["CONFORME", "PARZIALMENTE_CONFORME", "NON_CONFORME", "NON_APPLICABILE"]
SHORT = {"CONFORME": "C", "PARZIALMENTE_CONFORME": "PC", "NON_CONFORME": "NC", "NON_APPLICABILE": "NA"}
# Ordinal scale used for the automatic classification of the disagreements
RANK = {"NON_CONFORME": 0, "PARZIALMENTE_CONFORME": 1, "CONFORME": 2}
WEIGHT = {"CONFORME": 1.0, "PARZIALMENTE_CONFORME": 0.5, "NON_CONFORME": 0.0}
# Gaps written by the agents when a card is a controlled degradation
DEGRADED_MARKERS = ("LLM output parsing failed", "LLM invocation failed")
AGENTS = [("AS-1", "AS1_URL", "http://localhost:8001"),
          ("AS-2", "AS2_URL", "http://localhost:8002"),
          ("AS-3", "AS3_URL", "http://localhost:8003")]


# ---------------------------------------------------------------------------
# prepare (runs inside the AS-1 container)
# ---------------------------------------------------------------------------

def cmd_prepare(args: argparse.Namespace) -> None:
    from rag.collections import COLLECTION_ORG_DOCS, delete_documents_by_metadata, initialize_collections
    from rag.indexer import CHUNK_OVERLAP, CHUNK_SIZE
    from rag.iso_indexing import STATE_INDEXING, index_directory, iso_index_status

    current = {"chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP}
    print(f"Chunk configuration in effect: {current}")
    initialize_collections()

    # AS-1 rebuilds a stale index on its own at startup: wait for it
    deadline = time.time() + args.wait_minutes * 60
    while iso_index_status()["state"] == STATE_INDEXING:
        if time.time() > deadline:
            sys.exit("ISO indexing still running after the waiting time; retry later.")
        print("ISO indexing in progress, waiting...")
        time.sleep(15)

    status = iso_index_status()
    recorded = {key: status.get(key) for key in current}
    if status["state"] == "ready" and recorded == current:
        print("ISO index already built with this configuration.")
    else:
        print(f"ISO index not built with this configuration (status: {status['state']}, "
              f"recorded: {recorded}): rebuilding from {args.iso_docs_dir}...")
        index_directory(args.iso_docs_dir, reset=True)
        status = iso_index_status()
        recorded = {key: status.get(key) for key in current}
        if status["state"] != "ready" or recorded != current:
            sys.exit(f"ISO index rebuild failed: {status}")
    print(f"ISO index ready: {status.get('chunks')}")

    removed = delete_documents_by_metadata(COLLECTION_ORG_DOCS, {"org_id": args.org_id})
    print(f"Removed {removed} ORG-DOCS chunks of org_id={args.org_id!r}.")
    print("Ready: run the 'run' step for this configuration.")


# ---------------------------------------------------------------------------
# run (runs on the host)
# ---------------------------------------------------------------------------

def _agent_urls() -> List[tuple]:
    return [(name, os.getenv(env, default)) for name, env, default in AGENTS]


def cmd_run(args: argparse.Namespace) -> None:
    import httpx
    from dotenv import load_dotenv

    from scripts.run_evaluation import _load_documents

    load_dotenv()
    expected = args.expect_chunk_size or int(args.label)

    configs = {}
    for name, url in _agent_urls():
        health = httpx.get(f"{url}/health", timeout=10).json()
        config = health.get("config") or {}
        if config.get("chunk_size") != expected:
            sys.exit(f"{name} reports chunk_size={config.get('chunk_size')}, expected {expected}: "
                     "update .env and recreate the services, then run 'prepare' again.")
        configs[name] = config
    print(f"All agents report chunk_size={expected}: {configs['AS-1']}")

    documents = _load_documents(args.docs_dir, args.org_id)
    if not documents:
        sys.exit(f"No documents found in {args.docs_dir}")
    print(f"{len(documents)} documents loaded from {args.docs_dir}")

    out_dir = Path(args.out) / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"org_id": args.org_id, "documents": documents}

    for run in range(1, args.runs + 1):
        cards: List[Dict[str, Any]] = []
        timings = {}
        for name, url in _agent_urls():
            started = time.time()
            print(f"[{args.label}] run {run}/{args.runs}: {name}...", flush=True)
            response = httpx.post(f"{url}/analyze", json=payload, timeout=args.timeout)
            response.raise_for_status()
            agent_cards = response.json()
            timings[name] = round(time.time() - started, 1)
            print(f"  {len(agent_cards)} cards in {timings[name]} s")
            cards.extend(agent_cards)
        result = {
            "label": args.label,
            "run": run,
            "org_id": args.org_id,
            "docs_dir": str(args.docs_dir),
            "agent_config": configs,
            "seconds_per_agent": timings,
            "evaluation_cards": cards,
        }
        path = out_dir / f"run_{run}.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"Saved {path}")


# ---------------------------------------------------------------------------
# compare (runs on the host)
# ---------------------------------------------------------------------------

def _is_degraded(card: Dict[str, Any]) -> bool:
    return any(gap.startswith(DEGRADED_MARKERS) for gap in card.get("gaps", []))


def _auto_flags(card: Dict[str, Any]) -> Counter:
    flags: Counter = Counter()
    for gap in card.get("gaps", []):
        if not gap.startswith("AUTO-FLAG"):
            continue
        if "declassato" in gap:
            flags["downgrade"] += 1
        else:
            flags["evidence_dropped"] += 1
    return flags


def _classify(gt: str, system: str) -> str:
    """Automatic approximation of the reviewer's scoring: exact match is
    correct, adjacent levels (or an applicability disagreement) are partial,
    C vs NC is an error. The thesis scoring was done by a human reviewer:
    check the cases listed by the comparison before reporting them."""
    if gt == system:
        return "correct"
    if gt in RANK and system in RANK and abs(RANK[gt] - RANK[system]) == 2:
        return "error"
    return "partial"


def _gamma(verdicts: List[str]) -> Optional[float]:
    applicable = [v for v in verdicts if v != "NON_APPLICABILE"]
    if not applicable:
        return None
    return 100 * sum(WEIGHT[v] for v in applicable) / len(applicable)


def _load_runs(directory: Path) -> List[Dict[str, str]]:
    runs = []
    for path in sorted(directory.glob("run_*.json"), key=lambda p: int(p.stem.split("_")[1])):
        data = json.loads(path.read_text())
        runs.append(data)
    return runs


def _label_key(label: str):
    return (0, int(label)) if label.isdigit() else (1, label)


def cmd_compare(args: argparse.Namespace) -> None:
    with open(args.ground_truth, newline="") as f:
        truth = {row["requirement_id"]: row["gt_verdict"].strip() for row in csv.DictReader(f)}
    n = len(truth)
    out = Path(args.out)
    labels = sorted((d.name for d in out.iterdir() if d.is_dir() and any(d.glob("run_*.json"))), key=_label_key)
    if not labels:
        sys.exit(f"No results in {out}")

    rows, first_runs = [], {}
    for label in labels:
        runs = _load_runs(out / label)
        per_run = [{c["requirement_id"]: c for c in r["evaluation_cards"]} for r in runs]
        first = per_run[0]
        first_runs[label] = first
        verdicts = {rid: c["verdict"] for rid, c in first.items()}
        valid = [rid for rid in truth if rid in first]
        degraded = [rid for rid in valid if _is_degraded(first[rid])]
        classes = Counter(_classify(truth[rid], verdicts[rid]) for rid in valid)
        flags: Counter = Counter()
        evidences = 0
        for rid in valid:
            flags += _auto_flags(first[rid])
            evidences += len(first[rid].get("evidences", []))
        stable = sum(1 for rid in truth if len({r.get(rid, {}).get("verdict") for r in per_run}) == 1)
        dist = Counter(verdicts[rid] for rid in valid)
        gamma = _gamma([verdicts[rid] for rid in valid if rid not in degraded])
        rows.append({
            "chunk": label,
            "runs": len(runs),
            "TCN %": round(100 * len(valid) / n, 2),
            "EEC %": round(100 * (len(valid) - len(degraded)) / n, 2),
            "exact %": round(100 * classes["correct"] / max(len(valid), 1), 2),
            "TA auto %": round(100 * (classes["correct"] + classes["partial"]) / max(len(valid), 1), 2),
            "errors": classes["error"],
            "C/PC/NC/NA": "/".join(str(dist[v]) for v in VERDICTS),
            "Gamma": None if gamma is None else round(gamma, 2),
            "downgrades": flags["downgrade"],
            "evid. dropped": flags["evidence_dropped"],
            "evid./card": round(evidences / max(len(valid), 1), 2),
            "IR %": round(100 * stable / n, 2) if len(runs) > 1 else "-",
        })

    headers = list(rows[0])
    print("\n| " + " | ".join(headers) + " |")
    print("|" + "---|" * len(headers))
    for row in rows:
        print("| " + " | ".join(str(row[h]) for h in headers) + " |")
    print("\nexact = same verdict as the ground truth; TA auto = automatic approximation "
          "of the reviewer's scoring (exact + adjacent levels), to be checked by hand.")

    with open(out / "summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    changes = {}
    if args.baseline in first_runs:
        base = first_runs[args.baseline]
        for label in labels:
            if label == args.baseline:
                continue
            diff = []
            for rid in truth:
                b = base.get(rid, {}).get("verdict")
                o = first_runs[label].get(rid, {}).get("verdict")
                if b != o:
                    diff.append({"requirement_id": rid, "ground_truth": truth[rid],
                                 args.baseline: b, label: o})
            changes[label] = diff
            print(f"\nVerdicts changed {args.baseline} -> {label}: {len(diff)}")
            for d in diff:
                print(f"  {d['requirement_id']:<10} GT={SHORT.get(d['ground_truth'], d['ground_truth']):<3} "
                      f"{SHORT.get(d[args.baseline], d[args.baseline])} -> {SHORT.get(d[label], d[label])}")
    else:
        print(f"\nBaseline {args.baseline!r} not found: no per-requirement comparison.")

    (out / "summary.json").write_text(json.dumps({"rows": rows, "changes_vs_baseline": changes},
                                                  indent=2, ensure_ascii=False))
    print(f"\nSaved {out / 'summary.csv'} and {out / 'summary.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="inside the AS-1 container: align the index with the chunk configuration")
    p.add_argument("--org-id", required=True, help="dedicated org_id of the experiment")
    p.add_argument("--iso-docs-dir", default=os.getenv("ISO_DOCS_DIR", "/app/iso_docs"))
    p.add_argument("--wait-minutes", type=float, default=30)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("run", help="on the host: run the agents and save the cards")
    p.add_argument("--org-id", required=True)
    p.add_argument("--docs-dir", required=True)
    p.add_argument("--label", required=True, help="name of the configuration, e.g. the chunk size")
    p.add_argument("--expect-chunk-size", type=int, help="defaults to the label when it is a number")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--out", default="results/chunk_sensitivity")
    p.add_argument("--timeout", type=float, default=float(os.getenv("AS_TIMEOUT", "3600")))
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="on the host: compare all configurations with the ground truth")
    p.add_argument("--out", default="results/chunk_sensitivity")
    p.add_argument("--ground-truth", required=True, help="CSV with columns requirement_id, gt_verdict")
    p.add_argument("--baseline", default="512")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
