#!/usr/bin/env python3
"""Audit exported z-mode RL signal dataset quality gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit z-mode RL signal export summary")
    parser.add_argument("--summary", required=True, help="summary.json from zmode_signal_export")
    parser.add_argument("--min-decisions", type=int, default=500)
    parser.add_argument("--min-applies", type=int, default=200)
    parser.add_argument("--min-apply-rate", type=float, default=0.40)
    parser.add_argument("--min-joined", type=int, default=400)
    parser.add_argument("--min-link-rate", type=float, default=0.80)
    parser.add_argument("--max-action-dominance", type=float, default=0.55)
    parser.add_argument("--min-overrides", type=int, default=20)
    parser.add_argument("--min-failureish", type=int, default=60, help="minimum partial+failure+wasted rows")
    parser.add_argument("--report-out", help="optional audit report path")
    args = parser.parse_args()

    summary_path = Path(args.summary).expanduser().resolve()
    s = _load(summary_path)

    counts = s.get("counts") if isinstance(s.get("counts"), dict) else {}
    quality = s.get("quality") if isinstance(s.get("quality"), dict) else {}
    dist = s.get("distributions") if isinstance(s.get("distributions"), dict) else {}
    outcome_dist = dist.get("outcomes") if isinstance(dist.get("outcomes"), dict) else {}

    decisions = int(counts.get("zmode_decisions") or 0)
    applies = int(counts.get("zmode_applies") or 0)
    joined = int(counts.get("joined_rows") or 0)
    overrides = int(counts.get("override_links") or 0)

    apply_rate = float(quality.get("apply_rate") or 0.0)
    link_rate = float(quality.get("intent_outcome_link_rate") or 0.0)
    action_dom = float(quality.get("action_dominance") or 1.0)

    failureish = 0
    for key in ("partial", "failure", "wasted"):
        value = outcome_dist.get(key)
        if isinstance(value, int):
            failureish += value

    gates = {
        "min_decisions": {
            "actual": decisions,
            "threshold": args.min_decisions,
            "pass": decisions >= args.min_decisions,
        },
        "min_applies": {
            "actual": applies,
            "threshold": args.min_applies,
            "pass": applies >= args.min_applies,
        },
        "min_apply_rate": {
            "actual": apply_rate,
            "threshold": args.min_apply_rate,
            "pass": apply_rate >= args.min_apply_rate,
        },
        "min_joined": {
            "actual": joined,
            "threshold": args.min_joined,
            "pass": joined >= args.min_joined,
        },
        "min_link_rate": {
            "actual": link_rate,
            "threshold": args.min_link_rate,
            "pass": link_rate >= args.min_link_rate,
        },
        "max_action_dominance": {
            "actual": action_dom,
            "threshold": args.max_action_dominance,
            "pass": action_dom <= args.max_action_dominance,
        },
        "min_overrides": {
            "actual": overrides,
            "threshold": args.min_overrides,
            "pass": overrides >= args.min_overrides,
        },
        "min_failureish": {
            "actual": failureish,
            "threshold": args.min_failureish,
            "pass": failureish >= args.min_failureish,
        },
    }

    ok = all(gate["pass"] for gate in gates.values())
    report = {
        "schema_version": "zmode_signal_audit_v1",
        "summary": str(summary_path),
        "pass": ok,
        "gates": gates,
    }

    report_path = (
        Path(args.report_out).expanduser().resolve()
        if args.report_out
        else summary_path.parent / "audit.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    print(f"z-mode signal audit: {'PASS' if ok else 'FAIL'}")
    print(f"  summary: {summary_path}")
    print(f"  report:  {report_path}")
    if not ok:
        for name, gate in gates.items():
            if not gate["pass"]:
                print(
                    f"  fail {name}: actual={gate['actual']} threshold={gate['threshold']}",
                    file=sys.stderr,
                )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
