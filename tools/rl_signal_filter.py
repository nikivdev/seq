#!/usr/bin/env python3
"""Filter/summarize seq events used for RL pipelines."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Default is router-ground-truth only.
ROUTER_PATTERNS = [
    r"^flow\.router\.decision\.v1$",
    r"^flow\.router\.outcome\.v1$",
    r"^flow\.router\.override\.v1$",
]

AGENT_QA_PATTERNS = [
    r"^agent\.qa\.",
]

OPS_PATTERNS = [
    r"^seqd\.request$",
    r"^seqd\.run(\.|$)",
    r"^cli\.run(\.|$)",
    r"^cli\.agent$",
]

UI_PATTERNS = [
    r"^cli\.open_app_toggle(\.|$)",
    r"^seq\.sequence\.",
    r"^menu\.select\.",
    r"^open_url(\.|$)",
    r"^app\.activate$",
    r"^actions\.",
    r"^AX_(STATUS|PROMPT)$",
]


def _compile(include_agent_qa: bool, include_ops: bool, include_ui: bool) -> re.Pattern[str]:
    pats = list(ROUTER_PATTERNS)
    if include_agent_qa:
        pats.extend(AGENT_QA_PATTERNS)
    if include_ops:
        pats.extend(OPS_PATTERNS)
    if include_ui:
        pats.extend(UI_PATTERNS)
    return re.compile("|".join(f"(?:{p})" for p in pats))


def event_name(row: dict) -> str:
    for key in ("name", "event", "kind"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def iter_lines(file_path: str | None):
    if file_path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"missing file: {path}")
        yield from path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        for line in sys.stdin:
            yield line.rstrip("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter seq events for RL")
    parser.add_argument("--file", help="Read JSONEachRow from file instead of stdin")
    parser.add_argument("--summary", action="store_true", help="Print summary counts instead of rows")
    parser.add_argument("--last", type=int, default=0, help="Only process last N lines from file input")
    parser.add_argument("--include-agent-qa", action="store_true", help="Include agent.qa.* events")
    parser.add_argument("--include-ops", action="store_true", help="Include operational seqd/cli traces")
    parser.add_argument("--include-ui", action="store_true", help="Include UI/open-app traces (usually noise for router RL)")
    args = parser.parse_args()

    re_sig = _compile(args.include_agent_qa, args.include_ops, args.include_ui)

    lines = list(iter_lines(args.file))
    if args.last > 0:
        lines = lines[-args.last :]

    total = 0
    matched = 0
    counts = Counter()

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        total += 1
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        name = event_name(row)
        if not name or not re_sig.search(name):
            continue

        matched += 1
        counts[name] += 1
        if not args.summary:
            print(raw)

    if args.summary:
        ratio = (matched / total * 100.0) if total else 0.0
        print(f"rows_total={total}")
        print(f"rows_matched={matched}")
        print(f"match_ratio_pct={ratio:.2f}")
        print("top_events:")
        for name, count in counts.most_common(30):
            print(f"  {name}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
