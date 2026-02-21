#!/usr/bin/env python3
"""Preflight and dataset prep for the next RL training run.

Runs strict health gates and exports audited datasets for router + kar signals.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_ROUTER_OUT = str(Path("~/repos/laude-institute/harbor/data/router_signal/latest").expanduser())
DEFAULT_KAR_OUT = str(Path("~/repos/laude-institute/harbor/data/kar_signal/latest").expanduser())
DEFAULT_REPORT = str(Path("~/.local/state/seq/rl_next_run_prep_report.json").expanduser())


def run_step(name: str, cmd: list[str]) -> tuple[bool, float]:
    print(f"\n==> {name}")
    print("$ " + " ".join(cmd))
    t0 = time.time()
    proc = subprocess.run(cmd)
    dt = time.time() - t0
    if proc.returncode != 0:
        print(f"FAIL {name} (exit={proc.returncode}, {dt:.2f}s)")
        return False, dt
    print(f"OK   {name} ({dt:.2f}s)")
    return True, dt


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prepare seq signals for next RL training run")
    p.add_argument("--seq-mem", default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM))
    p.add_argument("--router-out", default=os.environ.get("ROUTER_SIGNAL_OUT", DEFAULT_ROUTER_OUT))
    p.add_argument("--kar-out", default=os.environ.get("KAR_SIGNAL_OUT", DEFAULT_KAR_OUT))
    p.add_argument("--report", default=os.environ.get("SEQ_RL_NEXT_RUN_REPORT", DEFAULT_REPORT))
    p.add_argument("--signal-summary-last", type=int, default=20000)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    seq_mem = Path(args.seq_mem).expanduser().resolve()
    router_out = Path(args.router_out).expanduser().resolve()
    kar_out = Path(args.kar_out).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()

    router_summary = router_out / "summary.json"
    kar_summary = kar_out / "summary.json"

    steps: list[tuple[str, list[str]]] = [
        ("seq_health", [sys.executable, "tools/seq_health.py"]),
        (
            "router_export",
            [
                sys.executable,
                "tools/router_signal_export.py",
                "--seq-mem",
                str(seq_mem),
                "--output-dir",
                str(router_out),
                "--max-age-hours",
                str(os.environ.get("ROUTER_SIGNAL_MAX_AGE_HOURS", "168")),
            ],
        ),
        (
            "router_audit",
            [
                sys.executable,
                "tools/router_signal_audit.py",
                "--summary",
                str(router_summary),
                "--min-decisions",
                str(os.environ.get("ROUTER_SIGNAL_MIN_DECISIONS", "500")),
                "--min-joined",
                str(os.environ.get("ROUTER_SIGNAL_MIN_JOINED", "400")),
                "--min-link-rate",
                str(os.environ.get("ROUTER_SIGNAL_MIN_LINK_RATE", "0.80")),
                "--max-task-dominance",
                str(os.environ.get("ROUTER_SIGNAL_MAX_TASK_DOMINANCE", "0.55")),
                "--min-overrides",
                str(os.environ.get("ROUTER_SIGNAL_MIN_OVERRIDES", "20")),
                "--min-failureish",
                str(os.environ.get("ROUTER_SIGNAL_MIN_FAILUREISH", "60")),
            ],
        ),
        (
            "kar_export",
            [
                sys.executable,
                "tools/kar_signal_export.py",
                "--seq-mem",
                str(seq_mem),
                "--output-dir",
                str(kar_out),
                "--max-age-hours",
                str(os.environ.get("KAR_SIGNAL_MAX_AGE_HOURS", "168")),
            ],
        ),
        (
            "kar_audit",
            [
                sys.executable,
                "tools/kar_signal_audit.py",
                "--summary",
                str(kar_summary),
                "--min-intents",
                str(os.environ.get("KAR_SIGNAL_MIN_INTENTS", "300")),
                "--min-joined",
                str(os.environ.get("KAR_SIGNAL_MIN_JOINED", "250")),
                "--min-link-rate",
                str(os.environ.get("KAR_SIGNAL_MIN_LINK_RATE", "0.80")),
                "--max-action-dominance",
                str(os.environ.get("KAR_SIGNAL_MAX_ACTION_DOMINANCE", "0.65")),
                "--min-overrides",
                str(os.environ.get("KAR_SIGNAL_MIN_OVERRIDES", "10")),
                "--min-failureish",
                str(os.environ.get("KAR_SIGNAL_MIN_FAILUREISH", "30")),
            ],
        ),
        (
            "signal_summary",
            [
                sys.executable,
                "tools/rl_signal_filter.py",
                "--file",
                str(seq_mem),
                "--summary",
                "--include-agent-qa",
                "--last",
                str(max(1000, int(args.signal_summary_last))),
            ],
        ),
    ]

    results: list[dict[str, Any]] = []
    ok_all = True
    for name, cmd in steps:
        ok, seconds = run_step(name, cmd)
        results.append({"step": name, "ok": ok, "seconds": round(seconds, 3), "cmd": cmd})
        if not ok:
            ok_all = False
            break

    summary = {
        "schema_version": "rl_next_run_prep_v1",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok_all,
        "seq_mem": str(seq_mem),
        "router_out": str(router_out),
        "router_summary": str(router_summary),
        "kar_out": str(kar_out),
        "kar_summary": str(kar_summary),
        "router_metrics": load_json(router_summary),
        "kar_metrics": load_json(kar_summary),
        "steps": results,
        "next_commands": [
            "cd ~/repos/PrimeIntellect-ai/prime-rl",
            "f router-env-push",
            "f router-run-qwen3-30b-a3b-erl-reflect",
            "f router-early-stop-watch",
            "f router-gate",
        ],
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {report_path}")
    print(f"status: {'READY' if ok_all else 'BLOCKED'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
