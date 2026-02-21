#!/usr/bin/env python3
"""Quick wrapper for switching seq ClickHouse emission mode via Flow env."""

from __future__ import annotations

import argparse
import subprocess
import sys


VALID_MODES = ("native", "mirror", "file", "off")
STATUS_KEYS = (
    "SEQ_CH_MODE",
    "SEQ_CH_HOST",
    "SEQ_CH_PORT",
    "SEQ_CH_DATABASE",
    "SEQ_CH_MEM_PATH",
    "SEQ_CH_LOG_PATH",
)


def run_checked(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        elif proc.stdout:
            sys.stderr.write(proc.stdout)
        raise SystemExit(proc.returncode)
    return proc.stdout.strip()


def set_key(key: str, value: str) -> None:
    run_checked(["f", "env", "set", "--personal", f"{key}={value}"])


def get_key(key: str) -> str:
    proc = subprocess.run(
        ["f", "env", "get", "--personal", "-f", "value", key],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return "<unset>"
    value = proc.stdout.strip()
    return value if value else "<empty>"


def print_status() -> None:
    print("seq clickhouse mode env")
    for key in STATUS_KEYS:
        print(f"  {key}={get_key(key)}")


def apply_mode(mode: str, host: str | None, port: int | None, database: str | None) -> None:
    set_key("SEQ_CH_MODE", mode)
    if host is not None:
        set_key("SEQ_CH_HOST", host)
    if port is not None:
        set_key("SEQ_CH_PORT", str(port))
    if database is not None:
        set_key("SEQ_CH_DATABASE", database)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Switch seq ClickHouse emission mode using Flow env store."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status",) + VALID_MODES,
        help="Mode to set, or 'status' to show current values.",
    )
    parser.add_argument("--host", help="Set SEQ_CH_HOST together with mode.")
    parser.add_argument("--port", type=int, help="Set SEQ_CH_PORT together with mode.")
    parser.add_argument("--database", help="Set SEQ_CH_DATABASE together with mode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "status":
        print_status()
        return 0

    apply_mode(args.mode, args.host, args.port, args.database)
    print(f"Updated SEQ_CH_MODE={args.mode}")
    print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
