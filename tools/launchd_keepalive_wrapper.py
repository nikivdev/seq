#!/usr/bin/env python3
"""Launchd wrapper that keeps a child process supervised and pidfile-managed."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


class Wrapper:
    def __init__(self, pidfile: Path, command: list[str]) -> None:
        self.pidfile = pidfile
        self.command = command
        self.child: Optional[subprocess.Popen[str]] = None
        self.stop_requested = False

    def _write_pid(self, pid: int) -> None:
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(f"{pid}\n", encoding="utf-8")

    def _drop_pid(self) -> None:
        try:
            self.pidfile.unlink()
        except FileNotFoundError:
            pass

    def _handle_signal(self, signum: int, _frame: object) -> None:
        self.stop_requested = True
        if self.child and self.child.poll() is None:
            try:
                os.kill(self.child.pid, signum)
            except ProcessLookupError:
                pass

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_signal)

        self.child = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        self._write_pid(self.child.pid)

        rc = 0
        try:
            while True:
                ret = self.child.poll()
                if ret is not None:
                    rc = int(ret)
                    break
                time.sleep(0.25)
        finally:
            self._drop_pid()

        return rc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launchd wrapper with pidfile management")
    parser.add_argument("--pidfile", required=True, help="Pidfile path for child process")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after -- separator, e.g. -- python3 tool.py run",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("error: missing command (use -- <cmd> ...)", file=sys.stderr)
        return 2

    wrapper = Wrapper(pidfile=Path(args.pidfile).expanduser().resolve(), command=cmd)
    return wrapper.run()


if __name__ == "__main__":
    raise SystemExit(main())
