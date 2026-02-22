#!/usr/bin/env python3
"""Claude Code → Codex handoff: saves plan/prompt to .ai/handoff.md.

Reads the Claude Code transcript and extracts the original prompt,
plan, key decisions, and progress. Writes structured markdown that
Codex (or a new Claude Code session) can pick up.

Usage (called by hooks, not directly):
  python3 tools/codex_handoff.py --mode save    # PreCompact hook (stdin: hook JSON)
  python3 tools/codex_handoff.py --mode clear   # Manual cleanup

Stdin (save mode): JSON from Claude Code PreCompact hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HANDOFF_FILENAME = ".ai/handoff.md"
MAX_PROMPT_CHARS = 4_000
MAX_PLAN_CHARS = 8_000
MAX_DECISION_CHARS = 4_000
MAX_DIFF_CHARS = 2_000


def _read_stdin_json() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _extract_claude_text(message: Any) -> str:
    """Extract text from a Claude Code message object."""
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _parse_transcript(transcript_path: str) -> dict[str, Any]:
    """Parse a Claude Code transcript JSONL to extract key context.

    Returns dict with: original_prompt, plan, decisions, assistant_summaries.
    """
    result: dict[str, Any] = {
        "original_prompt": "",
        "plan": "",
        "decisions": [],
        "changes_summary": [],
    }

    path = Path(transcript_path)
    if not path.exists():
        return result

    PLAN_MARKERS = [
        "## plan", "# plan", "implementation plan",
        "step 1:", "## approach", "## architecture",
        "## part 1:", "## files to create", "## context",
    ]
    DECISION_MARKERS = [
        "decided to", "choosing", "trade-off",
        "instead of", "approach:", "going with",
    ]

    first_user = True
    plan_found = False

    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                record_type = obj.get("type")

                if record_type == "user":
                    text = _extract_claude_text(obj.get("message"))
                    if not text:
                        continue

                    # Capture original user prompt (first user message)
                    if first_user:
                        result["original_prompt"] = text[:MAX_PROMPT_CHARS]
                        first_user = False

                    # Plans are often pasted in user messages
                    # (e.g. "Implement the following plan: ...")
                    if not plan_found:
                        lower = text.lower()
                        if any(marker in lower for marker in PLAN_MARKERS):
                            result["plan"] = text[:MAX_PLAN_CHARS]
                            plan_found = True
                    continue

                if record_type == "assistant":
                    text = _extract_claude_text(obj.get("message"))
                    if not text:
                        continue

                    # Detect plan-like content in assistant messages too
                    if not plan_found:
                        lower = text.lower()
                        if any(marker in lower for marker in PLAN_MARKERS):
                            result["plan"] = text[:MAX_PLAN_CHARS]
                            plan_found = True
                            continue

                    # Detect decision-like content
                    lower = text.lower()
                    if any(marker in lower for marker in DECISION_MARKERS):
                        summary = text[:300]
                        if len(text) > 300:
                            summary += "..."
                        result["decisions"].append(summary)

    except Exception:
        pass

    return result


def _get_git_diff_summary(project_dir: str) -> str:
    """Get a brief git diff stat (tracked changes + new untracked files)."""
    parts: list[str] = []
    try:
        # Tracked changes (staged + unstaged) vs HEAD
        proc = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parts.append(proc.stdout.strip())

        # New untracked files
        proc2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc2.returncode == 0 and proc2.stdout.strip():
            new_files = proc2.stdout.strip().splitlines()
            if new_files:
                parts.append("New files:\n" + "\n".join(f"  {f}" for f in new_files[:20]))
    except Exception:
        pass

    diff = "\n".join(parts)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n..."
    return diff


def _write_handoff(project_dir: str, context: dict[str, Any]) -> str:
    """Write .ai/handoff.md and return its path."""
    handoff_path = Path(project_dir) / HANDOFF_FILENAME
    handoff_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    lines.append("# Handoff: Claude Code → Codex")
    lines.append(f"Updated: {now}")

    # Original prompt
    prompt = context.get("original_prompt", "")
    if prompt:
        lines.append("")
        lines.append("## Original Prompt")
        lines.append(prompt)

    # Plan
    plan = context.get("plan", "")
    if plan:
        lines.append("")
        lines.append("## Plan")
        lines.append(plan)

    # Key decisions
    decisions = context.get("decisions", [])
    if decisions:
        lines.append("")
        lines.append("## Key Decisions")
        for d in decisions[:10]:
            lines.append(f"- {d}")

    # Changes made so far
    diff = context.get("diff_summary", "")
    if diff:
        lines.append("")
        lines.append("## Changes Made So Far")
        lines.append("```")
        lines.append(diff)
        lines.append("```")

    content = "\n".join(lines) + "\n"
    handoff_path.write_text(content, encoding="utf-8")
    return str(handoff_path)


def mode_save(hook_input: dict[str, Any]) -> None:
    """PreCompact: save current Claude Code context to .ai/handoff.md."""
    project_dir = hook_input.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    transcript_path = hook_input.get("transcript_path", "")

    if not project_dir:
        return

    context = _parse_transcript(transcript_path) if transcript_path else {
        "original_prompt": "",
        "plan": "",
        "decisions": [],
    }

    # Add git diff summary
    context["diff_summary"] = _get_git_diff_summary(project_dir)

    # Only write if there's meaningful content
    has_content = any([
        context.get("original_prompt"),
        context.get("plan"),
        context.get("decisions"),
        context.get("diff_summary"),
    ])

    if has_content:
        handoff_path = _write_handoff(project_dir, context)
        print(f"[codex-handoff] saved: {handoff_path}", file=sys.stderr)


def mode_clear(project_dir: str | None = None) -> None:
    """Clear the handoff file."""
    if not project_dir:
        project_dir = os.getcwd()
    handoff_path = Path(project_dir) / HANDOFF_FILENAME
    if handoff_path.exists():
        handoff_path.unlink()
        print(f"[codex-handoff] cleared: {handoff_path}", file=sys.stderr)
    else:
        print(f"[codex-handoff] no handoff file at: {handoff_path}", file=sys.stderr)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Claude Code → Codex handoff")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["save", "clear"],
        help="Operation mode",
    )
    parser.add_argument(
        "--project-dir",
        default=None,
        help="Project directory (default: cwd from hook stdin or $CLAUDE_PROJECT_DIR)",
    )
    args = parser.parse_args()

    if args.mode == "save":
        hook_input = _read_stdin_json()
        if args.project_dir:
            hook_input["cwd"] = args.project_dir
        mode_save(hook_input)
    elif args.mode == "clear":
        mode_clear(args.project_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
