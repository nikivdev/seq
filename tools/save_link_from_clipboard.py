#!/usr/bin/env python3
"""Save the clipboard URL under a ## Links section in an MD/MDX file."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def read_clipboard() -> str:
    proc = subprocess.run(["/usr/bin/pbpaste"], text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("pbpaste failed")
    return proc.stdout


def extract_url(text: str) -> str:
    match = URL_RE.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(").,;]>")


def wait_for_clipboard_url(timeout_ms: int, poll_ms: int, initial_delay_ms: int) -> str:
    if initial_delay_ms > 0:
        time.sleep(initial_delay_ms / 1000.0)

    deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
    last = ""
    while True:
        try:
            current = read_clipboard()
        except RuntimeError:
            current = ""
        url = extract_url(current)
        if url:
            return url
        last = current
        if time.monotonic() >= deadline:
            break
        time.sleep(max(1, poll_ms) / 1000.0)

    # Best-effort one more pass on the last clipboard snapshot.
    return extract_url(last)


def find_h2(lines: list[str], title: str) -> int:
    needle = f"## {title}".strip().lower()
    for i, line in enumerate(lines):
        if line.strip().lower() == needle:
            return i
    return -1


def next_h2(lines: list[str], start: int) -> int:
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("## "):
            return i
    return len(lines)


def save_url(path: Path, section: str, url: str) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []

    section_idx = find_h2(lines, section)
    if section_idx < 0:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"## {section}\n")
        lines.append("\n")
        section_idx = len(lines) - 2

    section_end = next_h2(lines, section_idx)
    section_text = "".join(lines[section_idx + 1 : section_end])
    if url in section_text:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    date_heading = f"### {today}"
    date_idx = -1
    for i in range(section_idx + 1, section_end):
        if lines[i].strip() == date_heading:
            date_idx = i
            break

    bullet = f"- {url}\n"
    if date_idx >= 0:
        insert_at = date_idx + 1
        while insert_at < section_end and not lines[insert_at].lstrip().startswith("### ") and not lines[
            insert_at
        ].lstrip().startswith("## "):
            insert_at += 1
        while insert_at > date_idx + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, bullet)
    else:
        insert_at = section_end
        block: list[str] = []
        if insert_at > 0 and lines[insert_at - 1].strip():
            block.append("\n")
        block.append(f"{date_heading}\n")
        block.append(f"{bullet}")
        block.append("\n")
        lines[insert_at:insert_at] = block

    content = "".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Save the clipboard URL under ## Links.")
    parser.add_argument(
        "--file",
        default="~/docs/nice-urls.mdx",
        help="Target markdown/MDX file path (default: ~/docs/nice-urls.mdx).",
    )
    parser.add_argument("--section", default="Links", help="Section name to append under (default: Links).")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=1200,
        help="Max wait for clipboard URL after copy (default: 1200).",
    )
    parser.add_argument(
        "--poll-ms",
        type=int,
        default=50,
        help="Clipboard poll interval in ms (default: 50).",
    )
    parser.add_argument(
        "--initial-delay-ms",
        type=int,
        default=150,
        help="Initial delay after trigger in ms (default: 150).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.file).expanduser()

    url = wait_for_clipboard_url(args.timeout_ms, args.poll_ms, args.initial_delay_ms)
    if not url:
        sys.stderr.write("No http(s) URL found in clipboard.\n")
        return 1

    wrote = save_url(path, args.section, url)
    if wrote:
        print(f"Saved URL to {path}: {url}")
    else:
        print(f"URL already present in {path}: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
