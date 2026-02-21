#!/usr/bin/env python3
"""Audit exported flow router signal quality gates."""

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
    parser = argparse.ArgumentParser(description="Audit flow router signal export summary")
    parser.add_argument("--summary", required=True, help="summary.json from router_signal_export")
    parser.add_argument("--min-decisions", type=int, default=500)
    parser.add_argument("--min-joined", type=int, default=400)
    parser.add_argument("--min-link-rate", type=float, default=0.80)
    parser.add_argument("--max-task-dominance", type=float, default=0.55)
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

    decisions = int(counts.get("decisions") or 0)
    joined = int(counts.get("joined_rows") or 0)
    overrides = int(counts.get("overrides") or 0)
    link_rate = float(quality.get("decision_outcome_link_rate") or 0.0)
    task_dom = float(quality.get("task_dominance") or 1.0)

    failureish = 0
    for k in ("partial", "failure", "wasted"):
        v = outcome_dist.get(k)
        if isinstance(v, int):
            failureish += v

    gates = {
        "min_decisions": {
            "actual": decisions,
            "threshold": args.min_decisions,
            "pass": decisions >= args.min_decisions,
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
        "max_task_dominance": {
            "actual": task_dom,
            "threshold": args.max_task_dominance,
            "pass": task_dom <= args.max_task_dominance,
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
        "schema_version": "flow_router_signal_audit_v1",
        "summary": str(summary_path),
        "pass": ok,
        "gates": gates,
    }

    report_path = Path(args.report_out).expanduser().resolve() if args.report_out else summary_path.parent / "audit.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Flow router signal audit: {'PASS' if ok else 'FAIL'}")
    print(f"  summary: {summary_path}")
    print(f"  report:  {report_path}")
    if not ok:
        for name, gate in gates.items():
            if not gate["pass"]:
                print(f"  fail {name}: actual={gate['actual']} threshold={gate['threshold']}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
