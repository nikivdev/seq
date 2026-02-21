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

# macOS ANSI keycodes (US layout) — unshifted characters
KEYCODE_TO_CHAR: dict[int, str] = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v",
    11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t", 18: "1", 19: "2", 20: "3",
    21: "4", 22: "6", 23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0", 30: "]",
    31: "o", 32: "u", 33: "[", 34: "i", 35: "p", 37: "l", 38: "j", 39: "'", 40: "k", 41: ";",
    42: "\\", 43: ",", 44: "/", 45: "n", 46: "m", 47: ".", 50: "`",
}

# Shifted variants of ANSI keycodes (US layout)
SHIFTED_KEYCODE_TO_CHAR: dict[int, str] = {
    0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X", 8: "C", 9: "V",
    11: "B", 12: "Q", 13: "W", 14: "E", 15: "R", 16: "Y", 17: "T", 18: "!", 19: "@", 20: "#",
    21: "$", 22: "^", 23: "%", 24: "+", 25: "(", 26: "&", 27: "_", 28: "*", 29: ")", 30: "}",
    31: "O", 32: "U", 33: "{", 34: "I", 35: "P", 37: "L", 38: "J", 39: '"', 40: "K", 41: ":",
    42: "|", 43: "<", 44: "?", 45: "N", 46: "M", 47: ">", 50: "~",
}

SPACE_CODE = 49
ENTER_CODES = {36, 76}
TAB_CODE = 48
DELETE_CODES = {51, 117}

# macOS modifier flag masks
_SHIFT_MASK = 0x020000
_CTRL_MASK = 0x040000
_ALT_MASK = 0x080000
_CMD_MASK = 0x100000

# Burst segmentation: pause threshold in ms between keystrokes before emitting a burst
BURST_PAUSE_MS = 500
DELIMITER_CHARS = {".", ",", ";", ":", ")", "]", "}", "?"}


class ModifierState:
    """Track current modifier key state from flagsChanged events."""

    __slots__ = ("shift", "ctrl", "alt", "cmd")

    def __init__(self) -> None:
        self.shift = False
        self.ctrl = False
        self.alt = False
        self.cmd = False

    def update(self, flags_hex: str) -> None:
        try:
            flags = int(flags_hex, 16) if isinstance(flags_hex, str) else int(flags_hex)
        except (ValueError, TypeError):
            return
        self.shift = bool(flags & _SHIFT_MASK)
        self.ctrl = bool(flags & _CTRL_MASK)
        self.alt = bool(flags & _ALT_MASK)
        self.cmd = bool(flags & _CMD_MASK)

    @property
    def has_command_modifier(self) -> bool:
        """True when Cmd or Ctrl is held — indicates a shortcut, not typed text."""
        return self.cmd or self.ctrl


class TextBurstAccumulator:
    """Accumulate decoded characters into text bursts, emitting on pause or delimiter."""

    def __init__(self, pause_ms: int = BURST_PAUSE_MS) -> None:
        self.pause_ms = pause_ms
        self.chars: list[str] = []
        self.start_ts_ms: int = 0
        self.end_ts_ms: int = 0
        self.app_id: str = ""

    def _reset(self) -> None:
        self.chars.clear()
        self.start_ts_ms = 0
        self.end_ts_ms = 0
        self.app_id = ""

    def add_char(self, ch: str, ts_ms: int, app_id: str = "") -> dict[str, Any] | None:
        """Add a decoded character. Returns a burst dict if a pause triggered emission."""
        burst = None
        if self.chars:
            if app_id and self.app_id and app_id != self.app_id:
                burst = self._flush("app_switch", ts_ms)
            elif ts_ms - self.end_ts_ms > self.pause_ms:
                burst = self._flush("pause", ts_ms)

        if not self.chars:
            self.start_ts_ms = ts_ms
            self.app_id = app_id
        self.end_ts_ms = ts_ms
        self.chars.append(ch)
        return burst

    def add_enter(self, ts_ms: int) -> dict[str, Any] | None:
        """Newline acts as a delimiter — flush current burst."""
        if not self.chars:
            return None
        return self._flush("enter", ts_ms)

    def add_special(self, trigger: str, ts_ms: int) -> dict[str, Any] | None:
        """App switch or other non-char event — flush if pending."""
        if not self.chars:
            return None
        return self._flush(trigger, ts_ms)

    def flush_pending(self, ts_ms: int) -> dict[str, Any] | None:
        """Force-flush any remaining chars (e.g., at shutdown)."""
        if not self.chars:
            return None
        return self._flush("flush", ts_ms)

    def _flush(self, trigger: str, ts_ms: int) -> dict[str, Any]:
        text = "".join(self.chars)
        duration_ms = max(0, self.end_ts_ms - self.start_ts_ms)
        char_count = len(text)
        wpm = int(char_count / 5 / max(duration_ms / 60000, 0.001)) if duration_ms > 0 else 0
        burst = {
            "schema_version": "next_type_text_burst_v1",
            "text": text,
            "char_count": char_count,
            "duration_ms": duration_ms,
            "wpm_estimate": wpm,
            "start_ts_ms": self.start_ts_ms,
            "end_ts_ms": self.end_ts_ms,
            "trigger": trigger,
            "app_id": self.app_id,
        }
        self._reset()
        return burst


def decode_key_event(key_code: int, modifiers: ModifierState) -> str | None:
    """Decode a key_code + modifier state into a character, or None for non-text."""
    if modifiers.has_command_modifier:
        return None  # Cmd+S, Ctrl+C etc. are shortcuts, not typed text

    if key_code == SPACE_CODE:
        return " "
    if key_code == TAB_CODE:
        return "\t"
    if key_code in ENTER_CODES:
        return "\n"
    if key_code in DELETE_CODES:
        return None  # delete is not a typed character

    if modifiers.shift:
        return SHIFTED_KEYCODE_TO_CHAR.get(key_code)
    return KEYCODE_TO_CHAR.get(key_code)


def canonical_event_type(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return "key_event"
    cleaned = _NON_ALNUM.sub("_", raw.strip().lower()).strip("_")
    aliases = {
        "keydown": "key_down",
        "keyup": "key_up",
        "flagschanged": "flags_changed",
    }
    cleaned = aliases.get(cleaned, cleaned)
    return cleaned or "key_event"


# Module-level state shared across events within one ingest process lifetime
_modifier_state = ModifierState()
_burst_accumulator = TextBurstAccumulator()


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

    # Update modifier state from flagsChanged events
    if event_type == "flags_changed":
        flags_hex = payload.get("flags_hex", "0x0")
        _modifier_state.update(flags_hex)

    # Decode character for key_down events
    decoded_char: str | None = None
    if event_type == "key_down":
        key_code = payload.get("key_code")
        if isinstance(key_code, int):
            decoded_char = decode_key_event(key_code, _modifier_state)
            if decoded_char is not None:
                payload["decoded_char"] = decoded_char

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


def _make_burst_event(burst: dict[str, Any], session_id: str, source: str) -> dict[str, Any]:
    """Convert a text burst dict into a seq_mem row."""
    payload = dict(burst)
    payload["source"] = source
    payload["session_id"] = session_id
    return {
        "ts_ms": payload.get("start_ts_ms", now_ms()),
        "dur_us": payload.get("duration_ms", 0) * 1000,
        "ok": True,
        "session_id": session_id,
        "name": "next_type.text_burst.v1",
        "subject": json.dumps(payload, ensure_ascii=True),
    }


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
    effective_session = args.session_id or "next-type"

    def handle_signal(_signum: int, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not stop:
        line = sys.stdin.readline()
        if not line:
            break

        # Parse raw payload to feed burst accumulator before creating the event
        raw = line.strip()
        raw_payload: dict[str, Any] | None = None
        if raw:
            try:
                raw_payload = json.loads(raw)
            except json.JSONDecodeError:
                pass

        event = to_event(
            line=line,
            source=args.source,
            session_id=args.session_id,
            project_path=args.project_path,
        )
        if event is None:
            continue
        batch.append(event)

        # Feed decoded characters into burst accumulator
        if raw_payload and canonical_event_type(raw_payload.get("event_type")) == "key_down":
            decoded = raw_payload.get("decoded_char")
            if decoded is None:
                key_code = raw_payload.get("key_code")
                if isinstance(key_code, int):
                    decoded = decode_key_event(key_code, _modifier_state)
            ts = raw_payload.get("timestamp_ms") or now_ms()
            app_id = raw_payload.get("app_id", "")
            if decoded is not None:
                emitted_bursts: list[dict[str, Any]] = []
                if decoded == "\n":
                    burst = _burst_accumulator.add_enter(ts)
                    if burst is not None:
                        emitted_bursts.append(burst)
                else:
                    burst = _burst_accumulator.add_char(decoded, ts, app_id=app_id)
                    if burst is not None:
                        emitted_bursts.append(burst)
                    if decoded in DELIMITER_CHARS:
                        delimiter_burst = _burst_accumulator.add_special("delimiter", ts)
                        if delimiter_burst is not None:
                            emitted_bursts.append(delimiter_burst)
                for emitted in emitted_bursts:
                    batch.append(_make_burst_event(emitted, effective_session, args.source))

        now = time.monotonic()
        if len(batch) >= args.batch_size or (now - last_flush) * 1000 >= args.flush_ms:
            flush(out_path, batch)
            batch.clear()
            last_flush = now

    # Flush any pending burst at shutdown
    final_burst = _burst_accumulator.flush_pending(now_ms())
    if final_burst is not None:
        batch.append(_make_burst_event(final_burst, effective_session, args.source))

    flush(out_path, batch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
