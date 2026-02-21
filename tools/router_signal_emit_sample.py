#!/usr/bin/env python3
"""Emit one linked sample decision/outcome pair for router signal smoke tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def main() -> int:
    rid = f"sample-{int(time.time())}"
    base = [sys.executable, "tools/router_signal_emit.py"]

    decision_cmd = base + [
        "decision",
        "--decision-id",
        rid,
        "--chosen-task",
        "ai:flow/dev-check",
        "--confidence",
        "0.74",
        "--user-intent",
        "sample-intent",
        "--context-json",
        '{"changed_files":3,"ci_status":"failing"}',
    ]
    outcome_cmd = base + [
        "outcome",
        "--decision-id",
        rid,
        "--outcome",
        "partial",
        "--task-executed",
        "ai:flow/dev-check",
        "--time-to-resolution-ms",
        "120000",
    ]

    for cmd in (decision_cmd, outcome_cmd):
        proc = subprocess.run(cmd, text=True)
        if proc.returncode != 0:
            return proc.returncode
    print(f"sample decision_id={rid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
