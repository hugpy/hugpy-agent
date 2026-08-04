#!/usr/bin/env python3
"""Phase 5 shadow evaluation — full-context baseline vs MCT-curated vs faults.

Deterministic and offline (no billed calls). Proves the design §22 Phase-5 exit
condition: token savings are material where MCT is designed to help, and quality
(omission, adherence, selection recall, pull recovery, fault recovery) holds.

Usage:  PYTHONPATH=src python3 tools/phase5_eval.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hugpy_agent.mct.evaluation import ShadowEvaluator


def bar(x, width=28):
    return "█" * int(round(x * width)) + "·" * (width - int(round(x * width)))


def main() -> int:
    ev = ShadowEvaluator(use_model=True)
    rep = ev.run_suite_offline()
    rows, scaling, s = rep["rows"], rep["scaling"], rep["summary"]

    print("\n\033[1mPer-task (baseline full-context  vs  MCT curated)\033[0m")
    print(f"  {'task':<20}{'family':<20}{'baseline':>9}{'mct':>7}{'reduce':>8}  notes")
    for r in rows:
        if "token_reduction" in r:
            note = []
            if r.get("pull_recovered"): note.append("pull-recovered")
            if r.get("answerable_no_pull"): note.append("in-context")
            if "adherence" in r: note.append(f"adherence={r['adherence']}")
            if "selection_recall" in r:
                note.append(f"P/R={r['selection_precision']}/{r['selection_recall']}")
            print(f"  {r['id']:<20}{r['family']:<20}{r['baseline_tokens']:>9}"
                  f"{r['mct_tokens']:>7}{r['token_reduction']*100:>7.1f}%  {', '.join(note)}")
        else:
            print(f"  {r['id']:<20}{'fault':<20}{'—':>9}{'—':>7}{'—':>8}  "
                  f"recovered={r['fault_recovered']}")

    print("\n\033[1mBounded working set (memory query as history grows)\033[0m")
    print(f"  {'history':>8}{'baseline':>10}{'mct':>7}{'reduction':>11}")
    for r in scaling:
        print(f"  {r['history']:>8}{r['baseline_tokens']:>10}{r['mct_tokens']:>7}"
              f"   {bar(max(0, r['token_reduction']))} {r['token_reduction']*100:>5.1f}%")

    print("\n\033[1mExit-condition checks\033[0m")
    checks = [
        ("large-context token reduction >= 50%", s["mean_reduction_large_context"] >= 0.50,
         f"{s['mean_reduction_large_context']*100:.1f}%"),
        ("bounded working set: reduction grows with history >= 50%",
         s["scaling_reduction_at_max_history"] >= 0.50,
         f"{s['scaling_reduction_at_max_history']*100:.1f}% @ max history"),
        ("no omission errors (curation never loses needed evidence)", s["omission_errors"] == 0,
         f"{s['omission_errors']} errors"),
        ("latest-instruction adherence", s["adherence_pass"], str(s["adherence_pass"])),
        ("selection recall == 1.0 (all relevant retrieved)", s["selection_recall"] == 1.0,
         str(s["selection_recall"])),
        ("fault recovery (epoch reset + source change)", s["fault_recovered"],
         str(s["fault_recovered"])),
    ]
    ok = True
    for label, cond, detail in checks:
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}  ({detail})")

    print("\n" + ("\033[1mPHASE-5 SHADOW EVALUATION: PASSED\033[0m" if ok
                  else "\033[1mPHASE-5: SOME CHECKS FAILED\033[0m"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
