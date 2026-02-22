#!/usr/bin/env python3
"""Codex ↔ Claude Code context bridge.

Reads recent Codex session history and injects it as additionalContext
via Claude Code hooks (SessionStart / UserPromptSubmit).

Usage (called by hooks, not directly):
  python3 tools/codex_bridge.py --mode session-start   # SessionStart hook
  python3 tools/codex_bridge.py --mode prompt-submit    # UserPromptSubmit hook

Stdin: JSON from Claude Code hook system.
Stdout: JSON with hookSpecificOutput.additionalContext (or empty on no-op).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CODEX_DIR = Path("~/.codex/sessions").expanduser()
DEFAULT_STATE_PATH = Path("~/.local/state/seq/codex_bridge_state.json").expanduser()
HANDOFF_FILENAME = ".ai/handoff.md"
MAX_CONTEXT_CHARS = 12_000
MAX_EXCHANGES = 10
MAX_ANSWER_CHARS = 800


def _read_stdin_json() -> dict[str, Any]:
    """Read the hook's stdin JSON payload."""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(
        json.dumps(state, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _discover_codex_sessions(codex_dir: Path) -> list[Path]:
    """Find all Codex session JSONL files, sorted newest-first by mtime."""
    if not codex_dir.exists():
        return []
    files = [p for p in codex_dir.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _extract_text(obj: dict[str, Any]) -> str:
    """Extract text content from a Codex message object."""
    content = obj.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _extract_codex_cwd(obj: dict[str, Any]) -> str:
    """Extract project directory from a Codex session_meta/header object."""
    git_obj = obj.get("git")
    if isinstance(git_obj, dict):
        cwd = git_obj.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    instructions = obj.get("instructions")
    if isinstance(instructions, str):
        marker = "Current working directory:"
        if marker in instructions:
            try:
                return instructions.split(marker, 1)[1].split("\n", 1)[0].strip()
            except Exception:
                pass
    return ""


def _parse_session(path: Path, project_dir: str | None, from_offset: int = 0) -> tuple[list[dict[str, str]], str, int]:
    """Parse a Codex session JSONL file.

    Returns (exchanges, session_cwd, end_offset).
    Each exchange is {"user": "...", "assistant": "..."}.
    If project_dir is set, skips sessions not matching that directory.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], "", 0

    if size <= from_offset:
        return [], "", size

    exchanges: list[dict[str, str]] = []
    session_cwd = ""
    pending_user: str | None = None

    with path.open("rb") as fh:
        if from_offset > 0:
            fh.seek(from_offset)
        for line_bytes in fh:
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            # Session header (no "type" field, has "id" or "git")
            if obj.get("type") is None and ("id" in obj or "git" in obj):
                session_cwd = _extract_codex_cwd(obj)
                if project_dir and session_cwd and not session_cwd.startswith(project_dir):
                    return [], session_cwd, size
                continue

            if obj.get("type") != "message":
                continue

            role = obj.get("role")
            text = _extract_text(obj)
            if not text:
                continue

            if role == "user":
                pending_user = text
            elif role == "assistant" and pending_user:
                # Truncate long assistant responses for context injection
                answer = text
                if len(answer) > MAX_ANSWER_CHARS:
                    answer = answer[:MAX_ANSWER_CHARS] + "..."
                exchanges.append({"user": pending_user, "assistant": answer})
                pending_user = None

    return exchanges, session_cwd, size


def _find_matching_sessions(codex_dir: Path, project_dir: str | None) -> list[tuple[Path, list[dict[str, str]]]]:
    """Find Codex sessions matching the current project, newest first."""
    results: list[tuple[Path, list[dict[str, str]]]] = []
    for path in _discover_codex_sessions(codex_dir):
        exchanges, cwd, _ = _parse_session(path, project_dir)
        if exchanges:
            results.append((path, exchanges))
        if len(results) >= 3:  # At most 3 recent sessions
            break
    return results


def _format_exchanges(exchanges: list[dict[str, str]], header: str) -> str:
    """Format exchanges as readable context."""
    if not exchanges:
        return ""
    lines = [header]
    for i, ex in enumerate(exchanges[-MAX_EXCHANGES:], 1):
        lines.append(f"\n### Exchange {i}")
        lines.append(f"**User:** {ex['user']}")
        lines.append(f"**Assistant:** {ex['assistant']}")
    return "\n".join(lines)


def _read_handoff(project_dir: str) -> str:
    """Read .ai/handoff.md if it exists."""
    handoff_path = Path(project_dir) / HANDOFF_FILENAME
    if not handoff_path.exists():
        return ""
    try:
        content = handoff_path.read_text(encoding="utf-8").strip()
        if content:
            return f"\n---\n## Previous Handoff Context\n{content}"
        return ""
    except Exception:
        return ""


def _output_context(context: str, hook_event: str) -> None:
    """Write hook JSON response to stdout."""
    if not context.strip():
        return
    # Truncate if too long
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n[... truncated]"
    output = {
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "additionalContext": context,
        }
    }
    print(json.dumps(output, ensure_ascii=True))


def mode_session_start(hook_input: dict[str, Any]) -> None:
    """SessionStart: inject recent Codex history + handoff file."""
    project_dir = hook_input.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    codex_dir = Path(os.environ.get("SEQ_CODEX_DIR", str(DEFAULT_CODEX_DIR)))

    parts: list[str] = []

    # 1. Read Codex session history
    sessions = _find_matching_sessions(codex_dir, project_dir or None)
    if sessions:
        for path, exchanges in sessions:
            session_name = path.stem
            header = f"## Recent Codex Session ({session_name})"
            formatted = _format_exchanges(exchanges, header)
            if formatted:
                parts.append(formatted)

    # 2. Read handoff file
    if project_dir:
        handoff = _read_handoff(project_dir)
        if handoff:
            parts.append(handoff)

    if parts:
        context = "# Context from Codex\n" + "\n\n".join(parts)
        _output_context(context, "SessionStart")


def mode_prompt_submit(hook_input: dict[str, Any]) -> None:
    """UserPromptSubmit: inject only NEW Codex exchanges since last check."""
    project_dir = hook_input.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    codex_dir = Path(os.environ.get("SEQ_CODEX_DIR", str(DEFAULT_CODEX_DIR)))
    state_path = Path(os.environ.get("SEQ_CODEX_BRIDGE_STATE", str(DEFAULT_STATE_PATH)))

    state = _load_state(state_path)
    offsets: dict[str, int] = state.get("offsets", {})

    sessions = _discover_codex_sessions(codex_dir)
    new_exchanges: list[dict[str, str]] = []

    for path in sessions[:5]:  # Only check recent files
        key = str(path)
        prev_offset = offsets.get(key, 0)

        exchanges, cwd, end_offset = _parse_session(path, project_dir or None, from_offset=prev_offset)
        offsets[key] = end_offset

        if exchanges:
            new_exchanges.extend(exchanges)

    # Save updated offsets
    state["offsets"] = offsets
    _save_state(state_path, state)

    if new_exchanges:
        context = _format_exchanges(
            new_exchanges[-MAX_EXCHANGES:],
            "# New Codex Activity\nThe following exchanges happened in Codex since last check:",
        )
        _output_context(context, "UserPromptSubmit")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Codex ↔ Claude Code context bridge")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["session-start", "prompt-submit"],
        help="Hook mode to run",
    )
    args = parser.parse_args()

    hook_input = _read_stdin_json()

    if args.mode == "session-start":
        mode_session_start(hook_input)
    elif args.mode == "prompt-submit":
        mode_prompt_submit(hook_input)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
