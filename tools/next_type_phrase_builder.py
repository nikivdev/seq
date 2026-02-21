#!/usr/bin/env python3
"""Join text_burst events with editor context to produce (context, target_phrase) training pairs.

Reads seq_mem JSONL, matches `next_type.text_burst.v1` events with the most recent
`next_type.context.v1` within a configurable window, and outputs training-ready JSONL.

Usage:
    python3 next_type_phrase_builder.py --seq-mem ~/.../seq_mem.jsonl --out phrases.jsonl
    python3 next_type_phrase_builder.py --seq-mem ~/.../seq_mem.jsonl --out phrases.jsonl --min-chars 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_OUT = str(Path("~/.local/state/seq/next_type_phrases.jsonl").expanduser())

# Context association window: max ms between a context event and a burst
CONTEXT_WINDOW_MS = 10_000

# Minimum burst length to include (skip navigation / short shortcuts)
MIN_BURST_CHARS = 3

# Target app filter — Zed only initially
TARGET_APP_IDS = {"dev.zed.Zed"}


def _parse_subject(row: dict[str, Any]) -> dict[str, Any]:
    """Extract subject dict from a seq_mem row."""
    subject = row.get("subject")
    if isinstance(subject, dict):
        return subject
    if isinstance(subject, str):
        try:
            parsed = json.loads(subject)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _burst_id(burst: dict[str, Any]) -> str:
    """Deterministic ID for a burst based on its content and timestamp."""
    key = f"{burst.get('start_ts_ms', 0)}:{burst.get('text', '')}"
    return "burst_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _is_whitespace_only(text: str) -> bool:
    return not text.strip()


def build_training_pair(
    burst: dict[str, Any],
    context: dict[str, Any] | None,
    recent_bursts: list[str],
    *,
    min_chars: int = MIN_BURST_CHARS,
) -> dict[str, Any] | None:
    """Build a training pair from a burst and its associated context.

    Returns None if the pair should be filtered out.
    """
    text = burst.get("text", "")
    if len(text) < min_chars:
        return None
    if _is_whitespace_only(text):
        return None

    # Build context fields
    file_path = ""
    file_ext = ""
    language = ""
    project_name = ""
    git_branch = ""

    if context:
        file_path = context.get("file_path", "")
        file_ext = context.get("file_ext", "")
        language = context.get("language", "")
        project_name = context.get("project_name", "")
        git_branch = context.get("git_branch", "")

    # Build prefix from recent bursts (last few chars of previous typing)
    prefix = ""
    if recent_bursts:
        combined = "".join(recent_bursts)
        prefix = combined[-40:]  # last 40 chars for prefix context

    # Build the prompt in a format suitable for RL training
    context_block = ""
    if file_path or language or project_name:
        parts = []
        if file_path:
            parts.append(f"File: {file_path}")
        if language:
            parts.append(f"({language})")
        if project_name:
            parts.append(f"Project: {project_name}")
        if git_branch:
            parts.append(f"Branch: {git_branch}")
        context_block = " ".join(parts)

    recent_block = ""
    if recent_bursts:
        recent_items = [repr(b) for b in recent_bursts[-5:]]
        recent_block = f"Recent: [{', '.join(recent_items)}]"

    prompt_parts = ["<context>"]
    if context_block:
        prompt_parts.append(context_block)
    if recent_block:
        prompt_parts.append(recent_block)
    prompt_parts.append("</context>")
    if prefix:
        prompt_parts.append(f"<prefix>{prefix}</prefix>")

    prompt = "\n".join(prompt_parts)

    return {
        "id": _burst_id(burst),
        "prompt": prompt,
        "answer": text,
        "language": language,
        "file_ext": file_ext,
        "project_name": project_name,
        "session_id": burst.get("session_id") or (context.get("session_id", "") if context else ""),
        "burst_wpm": burst.get("wpm_estimate", 0),
        "burst_duration_ms": burst.get("duration_ms", 0),
        "burst_char_count": burst.get("char_count", 0),
        "start_ts_ms": burst.get("start_ts_ms", 0),
        "trigger": burst.get("trigger", ""),
    }


def process_seq_mem(
    seq_mem_path: Path,
    *,
    min_chars: int = MIN_BURST_CHARS,
    context_window_ms: int = CONTEXT_WINDOW_MS,
    app_filter: set[str] | None = None,
    require_context: bool = True,
) -> list[dict[str, Any]]:
    """Read seq_mem and produce training pairs."""
    if app_filter is None:
        app_filter = TARGET_APP_IDS

    # Collect all burst and context events in one pass
    bursts: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []

    with seq_mem_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            name = row.get("name", "")
            subject = _parse_subject(row)

            if name == "next_type.text_burst.v1":
                bursts.append(subject)
            elif name == "next_type.context.v1":
                contexts.append(subject)

    # Sort by timestamp
    bursts.sort(key=lambda b: b.get("start_ts_ms", 0))
    contexts.sort(key=lambda c: c.get("ts_ms", 0))

    # Build training pairs
    pairs: list[dict[str, Any]] = []
    recent_texts: list[str] = []
    ctx_idx = 0

    for burst in bursts:
        burst_ts = burst.get("start_ts_ms", 0)
        text = burst.get("text", "")

        if len(text) < min_chars:
            continue
        if _is_whitespace_only(text):
            continue

        # Find most recent context within window
        best_context: dict[str, Any] | None = None
        while ctx_idx < len(contexts) and contexts[ctx_idx].get("ts_ms", 0) <= burst_ts:
            ctx_idx += 1

        # Look backwards from ctx_idx for closest context within window
        for i in range(ctx_idx - 1, -1, -1):
            ctx = contexts[i]
            ctx_ts = ctx.get("ts_ms", 0)
            if burst_ts - ctx_ts > context_window_ms:
                break
            if app_filter:
                ctx_app = ctx.get("app_id", "")
                if ctx_app and ctx_app not in app_filter:
                    continue
            best_context = ctx
            break

        # For code-aware prediction we require a recent context event.
        if require_context and best_context is None:
            continue
        if app_filter and best_context is not None:
            ctx_app = best_context.get("app_id", "")
            if ctx_app and ctx_app not in app_filter:
                continue

        pair = build_training_pair(
            burst,
            best_context,
            list(recent_texts[-5:]),
            min_chars=min_chars,
        )
        if pair is not None:
            pairs.append(pair)

        # Track recent bursts for prefix context
        recent_texts.append(text)
        if len(recent_texts) > 10:
            recent_texts = recent_texts[-10:]

    return pairs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build (context, phrase) training pairs from seq_mem events."
    )
    parser.add_argument(
        "--seq-mem",
        default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM),
        help="Path to seq_mem JSONL.",
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("SEQ_NEXT_TYPE_PHRASES_OUT", DEFAULT_OUT),
        help="Output JSONL path for training pairs.",
    )
    parser.add_argument(
        "--min-chars", type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_MIN_BURST_CHARS", str(MIN_BURST_CHARS))),
        help="Minimum burst character count to include.",
    )
    parser.add_argument(
        "--context-window-ms", type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_CONTEXT_WINDOW_MS", str(CONTEXT_WINDOW_MS))),
        help="Max ms between context event and burst for association.",
    )
    parser.add_argument(
        "--no-app-filter", action="store_true",
        help="Disable Zed-only app filter (include all apps).",
    )
    parser.add_argument(
        "--allow-missing-context", action="store_true",
        help="Include bursts without a nearby context event.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    seq_mem_path = Path(args.seq_mem).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if not seq_mem_path.exists():
        print(f"seq_mem not found: {seq_mem_path}", file=sys.stderr)
        return 1

    app_filter = None if args.no_app_filter else TARGET_APP_IDS

    pairs = process_seq_mem(
        seq_mem_path,
        min_chars=args.min_chars,
        context_window_ms=args.context_window_ms,
        app_filter=app_filter,
        require_context=not args.allow_missing_context,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(p, ensure_ascii=True) for p in pairs]
    out_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")

    # Summary
    languages: dict[str, int] = {}
    projects: dict[str, int] = {}
    for p in pairs:
        lang = p.get("language", "") or "unknown"
        proj = p.get("project_name", "") or "unknown"
        languages[lang] = languages.get(lang, 0) + 1
        projects[proj] = projects.get(proj, 0) + 1

    print(f"training pairs: {len(pairs)}")
    print(f"output: {out_path}")
    if languages:
        print(f"languages: {json.dumps(languages, indent=2)}")
    if projects:
        print(f"projects: {json.dumps(projects, indent=2)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
