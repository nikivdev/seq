#!/usr/bin/env python3
"""Emit a Lin widget suggestion for "next thing to type".

Writes a JSONL entry into Lin's intent inbox.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def default_inbox() -> Path:
    return Path.home() / "Library" / "Application Support" / "Lin" / "intent-inbox.jsonl"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit Lin next-type suggestion widget")
    p.add_argument("text", help="Suggested text to type")
    p.add_argument("--title", default="Next to Type", help="Widget title")
    p.add_argument("--message", default="", help="Optional widget body")
    p.add_argument("--ttl-ms", type=int, default=90_000, help="Expiry TTL in milliseconds")
    p.add_argument("--inbox", default=str(default_inbox()), help="Override Lin intent inbox path")
    p.add_argument("--id", default="", help="Optional stable id for de-dup")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    now_ms = int(time.time() * 1000)
    entry_id = args.id.strip() or f"seq-next-{now_ms}"
    msg = args.message.strip() or f"Accept to type: {args.text[:120]}"

    entry = {
        "id": entry_id,
        "kind": "widget",
        "title": args.title,
        "message": msg,
        "createdAt": now_ms,
        "expiresAt": now_ms + max(args.ttl_ms, 1),
        "action": "paste",
        "actionTitle": "Type",
        "value": args.text,
    }

    inbox = Path(os.path.expanduser(args.inbox)).resolve()
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"Wrote widget intent: {entry_id}")
    print(f"Inbox: {inbox}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
