#!/usr/bin/env python3
"""Enable remote-first seq capture using Flow personal env values."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ENV_FILE = Path.home() / ".config" / "flow" / "env-local" / "personal" / "production.env"
DEFAULT_MEM_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = raw.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def run_checked(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(cmd, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enable remote-first seq capture.")
    parser.add_argument(
        "--install",
        action="store_true",
        help="Also build preflight + install launchd services.",
    )
    args = parser.parse_args()

    env_file = read_env_file(ENV_FILE)
    ch_host = env_file.get("SEQ_CH_HOST", "").strip()
    if not ch_host:
        print("Set SEQ_CH_HOST first (f env set --personal SEQ_CH_HOST=<host>)", file=sys.stderr)
        return 1

    ch_port = int(env_file.get("SEQ_CH_PORT", "9000") or "9000")
    ch_http_port = int(env_file.get("SEQ_CH_HTTP_PORT", "8123") or "8123")
    ch_db = env_file.get("SEQ_CH_DATABASE", "seq") or "seq"
    remote_url = env_file.get("SEQ_MEM_REMOTE_URL", "").strip() or f"http://{ch_host}:{ch_http_port}"

    run_checked(["f", "env", "set", "--personal", f"SEQ_MEM_REMOTE_URL={remote_url}"])
    run_checked(
        [
            "python3",
            "tools/ch_mode.py",
            "native",
            "--host",
            ch_host,
            "--port",
            str(ch_port),
            "--database",
            ch_db,
        ]
    )
    run_checked(["f", "env", "set", "--personal", "SEQ_MEM_SINK_MODE=remote"])

    if args.install:
        run_checked(["python3", "tools/next_type_tap_build.py"])
        run_checked(["python3", "tools/next_type_key_capture_daemon.py", "preflight"])
        run_checked(["python3", "tools/seq_capture_launchd.py", "install", "--service", "all"])

    # Show sink status with env loaded from Flow personal env file.
    merged = os.environ.copy()
    merged.update(env_file)
    merged["SEQ_MEM_REMOTE_URL"] = remote_url
    merged["SEQ_MEM_SINK_MODE"] = "remote"
    local_path = env_file.get("SEQ_CH_MEM_PATH", DEFAULT_MEM_PATH) or DEFAULT_MEM_PATH
    run_checked(["python3", "tools/seq_mem_sink.py", "status", "--local-path", local_path], env=merged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
