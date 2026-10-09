#!/usr/bin/env python3
"""Extended reproducibility test (thesis §3.6.2, results in §5.2.2).

The standard procedure runs the pipeline K = 3 times; the extended test
repeats it with K = 10 independent runs on a single unchanged corpus and,
by default, evaluates the WHOLE requirement set R (N = 70, i.e. N x K = 700
verdicts) rather than a random subset, so that no requirement is left out
and the estimate does not depend on the sample drawn.

Two figures are reported:
  - IR_esteso (binary):     % of requirements where all K runs agree,
                             directly comparable to the 3-run IR defined in
                             §3.2.3 (eq. IR).
  - Modal agreement rate:   per requirement, % of runs matching the most
                             common verdict, averaged over the requirements —
                             a finer-grained variance estimate.

Prerequisite: run scripts/run_evaluation.py with --save-all-runs so every
run's report is kept, e.g.:

    python scripts/run_evaluation.py --org-id aerarium --docs-dir /path/to/docs \\
        --runs 10 --save-all-runs results/aerarium_runs

Then, on the full requirement set (the configuration used in the thesis):

    python scripts/extended_reproducibility.py \\
        --reports-dir results/aerarium_runs \\
        --output-csv results/aerarium_ir_extended.csv

Optionally, --sample-size n restricts IR_esteso to n randomly drawn
requirements (--seed for a reproducible draw), and --include adds
requirement IDs as a targeted retest reported outside the sample.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _load_runs(reports_dir: str) -> List[Dict[str, Any]]:
    paths = sorted(Path(reports_dir).glob("run_*.json"))
    if not paths:
        raise FileNotFoundError(
            f"No run_*.json files in {reports_dir!r}. "
            "Re-run run_evaluation.py with --save-all-runs."
        )
    reports = []
    for path in paths:
        data = json.loads(path.read_text())
        # Accept either a raw GapReport or the {"gap_report": {...}} wrapper
        # written by run_evaluation.py's --output-json.
        report = data.get("gap_report", data)
        reports.append(report)
    return reports


def _verdict_maps(reports: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    maps = []
    for report in reports:
        cards = report.get("evaluation_cards", [])
        maps.append({c["requirement_id"]: c["verdict"] for c in cards})
    return maps


def _row_for(rid: str, group: str, maps: List[Dict[str, str]]) -> Dict[str, Any]:
    verdicts = [vm.get(rid) for vm in maps]
    counts = Counter(v for v in verdicts if v is not None)
    if not counts:
        raise ValueError(f"Requirement {rid!r} not found in any run's cards")
    modal_verdict, modal_count = counts.most_common(1)[0]
    return {
        "group": group,
        "requirement_id": rid,
        "verdicts": verdicts,
        "modal_verdict": modal_verdict,
        "modal_agreement_pct": round(modal_count / len(verdicts) * 100, 1),
        "concordant": len(counts) == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports-dir", required=True, help="Directory of run_<n>.json files from --save-all-runs")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=None,
        help="Restrict IR_esteso to n randomly sampled requirements "
        "(default: all requirements, as in the thesis)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible sampling (default: 42)")
    parser.add_argument(
        "--include",
        nargs="*",
        default=[],
        help="With --sample-size: requirement IDs to add as a targeted retest, outside "
        "the random sample (reported separately, not counted in IR_esteso)",
    )
    parser.add_argument("--output-csv", default=None, help="Optional path to save the per-requirement results as CSV")
    args = parser.parse_args()

    reports = _load_runs(args.reports_dir)
    print(f"Loaded {len(reports)} run(s) from {args.reports_dir}")
    if len(reports) < 2:
        sys.exit("Need at least 2 runs to compute an idempotency rate.")

    maps = _verdict_maps(reports)
    all_ids = sorted(set(maps[0].keys()))

    if args.sample_size is None:
        sample = all_ids
        targeted: List[str] = []
    else:
        if len(all_ids) < args.sample_size:
            sys.exit(f"Only {len(all_ids)} requirement IDs available, cannot sample {args.sample_size}.")
        random.seed(args.seed)
        sample = random.sample(all_ids, args.sample_size)
        targeted = [rid for rid in args.include if rid not in sample]

    rows = [_row_for(rid, "sample", maps) for rid in sample]
    rows += [_row_for(rid, "targeted", maps) for rid in targeted]

    sample_rows = [r for r in rows if r["group"] == "sample"]
    n_concordant = sum(1 for r in sample_rows if r["concordant"])
    ir_esteso = round(n_concordant / len(sample) * 100, 2)
    modal_mean = round(sum(r["modal_agreement_pct"] for r in sample_rows) / len(sample_rows), 2)

    if args.sample_size is None:
        print(f"\nFull requirement set: {len(sample)} requirements")
    else:
        print(f"\nRandom sample (seed={args.seed}, n={args.sample_size}): {sample}")
        if targeted:
            print(f"Targeted retest (excluded from IR_esteso): {targeted}")

    print(f"\nIR_esteso (binary, {len(reports)} runs) = {n_concordant}/{len(sample)} = {ir_esteso}%")
    print(f"Mean modal agreement = {modal_mean}%\n")

    header = f"{'group':<9} {'req_id':<12} {'modal verdict':<24} {'agree%':>7}  {'concordant':<10} verdicts"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['group']:<9} {r['requirement_id']:<12} {r['modal_verdict']:<24} "
            f"{r['modal_agreement_pct']:>6.1f}%  {'YES' if r['concordant'] else 'NO':<10} "
            f"{r['verdicts']}"
        )

    if args.output_csv:
        out_path = Path(args.output_csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["group", "requirement_id", "modal_verdict", "modal_agreement_pct", "concordant"]
                + [f"run_{i + 1}" for i in range(len(reports))]
            )
            for r in rows:
                writer.writerow(
                    [
                        r["group"],
                        r["requirement_id"],
                        r["modal_verdict"],
                        r["modal_agreement_pct"],
                        "YES" if r["concordant"] else "NO",
                    ]
                    + r["verdicts"]
                )
        print(f"\nSaved CSV to {out_path}")


if __name__ == "__main__":
    main()
