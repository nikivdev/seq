#!/usr/bin/env python3
"""
Migrate `km("...")` callsites in /Users/nikiv/config/i/kar/config.ts to `seqSocket("...")`
when the macro can be represented as a pure `open_url` in seq.

This preserves performance (socket_command avoids spawning a `seq` process per hotkey) and
reduces reliance on Keyboard Maestro for simple URL openers.

Dry-run by default. Use `--apply` to write changes.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path

_GEN = Path(__file__).resolve().parent / "gen_macros.py"
_spec = importlib.util.spec_from_file_location("gen_macros", _GEN)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[attr-defined]
classify = _mod.classify


CONFIG = Path("/Users/nikiv/config/i/kar/config.ts")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--apply", action="store_true", help="write changes to config.ts")
    ap.add_argument("--limit", type=int, default=50, help="max replacements to print")
    args = ap.parse_args()

    path = Path(args.config)
    text = path.read_text()

    # Replace km("...") / km('...') where classify(name) == open_url.
    pat = re.compile(r"\bkm\(\s*([\"'])(.*?)(?<!\\)\1\s*\)", re.DOTALL)

    replaced = 0
    samples: list[str] = []

    def repl(m: re.Match[str]) -> str:
        nonlocal replaced, samples
        # Ignore commented-out occurrences on the same line.
        line_start = text.rfind("\n", 0, m.start()) + 1
        if "//" in text[line_start:m.start()]:
            return m.group(0)

        q = m.group(1)
        raw = m.group(2)
        # Minimal unescape (same spirit as gen_macros usage).
        name = raw.replace(r"\\", "\\").replace(r"\'", "'").replace(r"\"", "\"")
        action, _, _ = classify(name)
        if action != "open_url":
            return m.group(0)
        replaced += 1
        if len(samples) < args.limit:
            samples.append(name)
        # Keep original quoting style.
        return f"seqSocket({q}{raw}{q})"

    out = pat.sub(repl, text)

    print(f"replacements: {replaced}")
    if samples:
        print("sample:")
        for s in samples:
            print(f"  - {s}")

    if args.apply and replaced:
        path.write_text(out)
        print(f"wrote: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
