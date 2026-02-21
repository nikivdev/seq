#!/usr/bin/env python3
"""Batch-ingest key events (JSONL from stdin) into seq mem spool for RL pipelines.

Designed for osx-event-observer style streams:
- non-blocking to user typing path
- batched local writes
- stable schema envelope
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


_NON_ALNUM = re.compile(r"[^a-z0-9_]+")


def canonical_event_type(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "key_event"
    cleaned = _NON_ALNUM.sub("_", raw.strip().lower()).strip("_")
    return cleaned or "key_event"


def to_event(line: str, source: str, session_id: str | None, project_path: str | None) -> dict[str, Any] | None:
    raw = line.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    ts = payload.get("timestamp_ms")
    if not isinstance(ts, int):
        ts = now_ms()

    event_type = canonical_event_type(payload.get("event_type"))
    effective_source = payload.get("source") or source
    effective_session = payload.get("session_id") or session_id or "next-type"
    effective_project = payload.get("project_path") or project_path or ""
    subject_obj: dict[str, Any] = {
        "schema_version": "next_type_v1",
        "event_type": event_type,
        "source": effective_source,
        "project_path": effective_project,
        "app_id": payload.get("app_id") or "",
        "payload": payload,
    }
    return {
        "ts_ms": ts,
        "dur_us": 0,
        "ok": True,
        "session_id": effective_session,
        "name": f"next_type.{event_type}",
        "subject": json.dumps(subject_obj, ensure_ascii=True),
    }


def flush(path: Path, batch: list[dict[str, Any]]) -> None:
    if not batch:
        return
    lines = [json.dumps(row, ensure_ascii=True) for row in batch]
    blob = "\n".join(lines) + "\n"
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(blob)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch key-event ingest to seq mem JSONL.")
    parser.add_argument(
        "--out",
        default=str(
            os.getenv(
                "SEQ_CH_MEM_PATH",
                str(Path.home() / "repos" / "ClickHouse" / "ClickHouse" / "user_files" / "seq_mem.jsonl"),
            )
        ),
        help="Output seq mem JSONL path.",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--flush-ms", type=int, default=2000)
    parser.add_argument("--source", default=os.getenv("NEXT_TYPE_SOURCE", "zed"))
    parser.add_argument("--session-id", default=os.getenv("NEXT_TYPE_SESSION_ID"))
    parser.add_argument("--project-path", default=os.getenv("NEXT_TYPE_PROJECT_PATH"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_path = Path(args.out).expanduser()
    batch: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    stop = False

    def handle_signal(_signum: int, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not stop:
        line = sys.stdin.readline()
        if not line:
            break
        event = to_event(
            line=line,
            source=args.source,
            session_id=args.session_id,
            project_path=args.project_path,
        )
        if event is None:
            continue
        batch.append(event)

        now = time.monotonic()
        if len(batch) >= args.batch_size or (now - last_flush) * 1000 >= args.flush_ms:
            flush(out_path, batch)
            batch.clear()
            last_flush = now

    flush(out_path, batch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
