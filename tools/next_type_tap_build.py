#!/usr/bin/env python3
"""Build headless CGEvent tap binary used by next_type capture.

This replaces the GUI `cgeventtap-example` app to avoid window popups.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

DEFAULT_SOURCE = Path("~/code/seq/tools/cgeventtap_headless.m").expanduser()
DEFAULT_OUT = Path("~/code/seq/cli/cpp/out/bin/seq-cgeventtap-headless").expanduser()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build seq headless CGEvent tap helper.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Source .m file path.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output binary path.")
    parser.add_argument("--verbose", action="store_true", help="Print full compiler command.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()

    if not source.exists():
        raise SystemExit(f"missing source: {source}")

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "clang",
        "-O2",
        "-Wall",
        "-Wextra",
        str(source),
        "-framework",
        "ApplicationServices",
        "-framework",
        "CoreFoundation",
        "-o",
        str(out),
    ]
    if args.verbose:
        print(" ".join(cmd))

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="")
        raise SystemExit(proc.returncode)

    os.chmod(out, 0o755)
    print(f"built: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

