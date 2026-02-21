#!/usr/bin/env python3
"""Audit exported Kar RL signal dataset quality gates."""

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
    parser = argparse.ArgumentParser(description="Audit Kar RL signal export summary")
    parser.add_argument("--summary", required=True, help="summary.json from kar_signal_export")
    parser.add_argument("--min-intents", type=int, default=300)
    parser.add_argument("--min-joined", type=int, default=250)
    parser.add_argument("--min-link-rate", type=float, default=0.80)
    parser.add_argument("--max-action-dominance", type=float, default=0.65)
    parser.add_argument("--min-overrides", type=int, default=10)
    parser.add_argument("--min-failureish", type=int, default=30, help="minimum partial+failure+wasted rows")
    parser.add_argument("--report-out", help="optional audit report path")
    args = parser.parse_args()

    summary_path = Path(args.summary).expanduser().resolve()
    s = _load(summary_path)

    counts = s.get("counts") if isinstance(s.get("counts"), dict) else {}
    quality = s.get("quality") if isinstance(s.get("quality"), dict) else {}
    dist = s.get("distributions") if isinstance(s.get("distributions"), dict) else {}
    outcome_dist = dist.get("outcomes") if isinstance(dist.get("outcomes"), dict) else {}

    intents = int(counts.get("intents") or 0)
    joined = int(counts.get("joined_rows") or 0)
    overrides = int(counts.get("overrides") or 0)
    link_rate = float(quality.get("intent_outcome_link_rate") or 0.0)
    action_dom = float(quality.get("action_dominance") or 1.0)

    failureish = 0
    for k in ("partial", "failure", "wasted"):
        v = outcome_dist.get(k)
        if isinstance(v, int):
            failureish += v

    gates = {
        "min_intents": {
            "actual": intents,
            "threshold": args.min_intents,
            "pass": intents >= args.min_intents,
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

    ok = all(g["pass"] for g in gates.values())
    report = {
        "schema_version": "kar_signal_audit_v1",
        "summary": str(summary_path),
        "pass": ok,
        "gates": gates,
    }

    report_path = Path(args.report_out).expanduser().resolve() if args.report_out else summary_path.parent / "audit.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    print(f"Kar signal audit: {'PASS' if ok else 'FAIL'}")
    print(f"  summary: {summary_path}")
    print(f"  report:  {report_path}")
    if not ok:
        for name, gate in gates.items():
            if not gate["pass"]:
                print(f"  fail {name}: actual={gate['actual']} threshold={gate['threshold']}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
