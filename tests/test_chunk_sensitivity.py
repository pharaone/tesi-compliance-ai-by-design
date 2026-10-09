"""Unit tests for scripts/chunk_sensitivity.py (comparison logic only)."""

from __future__ import annotations

import csv
import json
from types import SimpleNamespace

from scripts import chunk_sensitivity as cs


def _card(rid, verdict, gaps=(), evidences=1):
    return {"requirement_id": rid, "verdict": verdict, "gaps": list(gaps),
            "evidences": [{"excerpt": "x"}] * evidences}


def test_classification_mirrors_the_scoring_rules():
    assert cs._classify("CONFORME", "CONFORME") == "correct"
    assert cs._classify("NON_CONFORME", "PARZIALMENTE_CONFORME") == "partial"
    assert cs._classify("NON_APPLICABILE", "PARZIALMENTE_CONFORME") == "partial"
    assert cs._classify("CONFORME", "NON_CONFORME") == "error"


def test_degraded_cards_and_auto_flags_are_recognised():
    degraded = _card("A.4.3", "NON_APPLICABILE", ["LLM output parsing failed — manual review required"])
    assert cs._is_degraded(degraded)
    flagged = _card("cl-4.3", "NON_CONFORME", [
        "AUTO-FLAG: verdetto PARZIALMENTE_CONFORME declassato a NON_CONFORME — evidenze non sostanziali.",
        "AUTO-FLAG: 1 evidenza/e non è stata trovata nella documentazione recuperata.",
    ])
    assert cs._auto_flags(flagged) == {"downgrade": 1, "evidence_dropped": 1}


def test_gamma_ignores_non_applicable():
    assert cs._gamma(["CONFORME", "PARZIALMENTE_CONFORME", "NON_APPLICABILE"]) == 75.0
    assert cs._gamma(["NON_APPLICABILE"]) is None


def test_compare_builds_the_table_and_the_changes(tmp_path, capsys):
    gt = tmp_path / "gt.csv"
    with open(gt, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["requirement_id", "gt_verdict"])
        w.writerows([["cl-4.1", "CONFORME"], ["cl-4.2", "NON_CONFORME"], ["A.4.3", "PARZIALMENTE_CONFORME"]])
    runs = {
        "512": [_card("cl-4.1", "CONFORME"), _card("cl-4.2", "PARZIALMENTE_CONFORME"),
                _card("A.4.3", "NON_APPLICABILE", ["LLM output parsing failed — manual review required"])],
        "256": [_card("cl-4.1", "NON_CONFORME"), _card("cl-4.2", "NON_CONFORME"),
                _card("A.4.3", "PARZIALMENTE_CONFORME")],
    }
    for label, cards in runs.items():
        d = tmp_path / "out" / label
        d.mkdir(parents=True)
        (d / "run_1.json").write_text(json.dumps({"evaluation_cards": cards}))

    cs.cmd_compare(SimpleNamespace(out=str(tmp_path / "out"), ground_truth=str(gt), baseline="512"))

    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    rows = {r["chunk"]: r for r in summary["rows"]}
    assert [r["chunk"] for r in summary["rows"]] == ["256", "512"]
    assert rows["512"]["EEC %"] == round(100 * 2 / 3, 2)
    assert rows["512"]["exact %"] == round(100 / 3, 2)
    assert rows["256"]["errors"] == 1
    changed = {c["requirement_id"] for c in summary["changes_vs_baseline"]["256"]}
    assert changed == {"cl-4.1", "cl-4.2", "A.4.3"}
