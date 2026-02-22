#!/usr/bin/env python3
"""Enable remote-first seq capture using Flow personal env values."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path


ENV_FILE = Path.home() / ".config" / "flow" / "env-local" / "personal" / "production.env"
DEFAULT_MEM_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_TRACE_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl").expanduser())
SEQD_LABEL = "dev.nikiv.seqd"
SEQD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{SEQD_LABEL}.plist"


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


def run_best_effort(cmd: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    out = (proc.stderr or proc.stdout or "").strip()
    return False, out


def _default_seqd_plist_payload() -> dict[str, object]:
    seq_bin = (Path(__file__).resolve().parent.parent / "cli" / "cpp" / "out" / "bin" / "seq").resolve()
    log_dir = (Path(__file__).resolve().parent.parent / "cli" / "cpp" / "out" / "logs").resolve()
    return {
        "Label": SEQD_LABEL,
        "ProgramArguments": [str(seq_bin), "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "seqd.launchd.stdout.log"),
        "StandardErrorPath": str(log_dir / "seqd.launchd.stderr.log"),
        "ProcessType": "Interactive",
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def configure_seqd_launchd(
    *,
    ch_host: str,
    ch_port: int,
    ch_http_port: int,
    ch_db: str,
    env_file: dict[str, str],
) -> None:
    if not SEQD_PLIST.exists():
        print(f"WARN: seqd launchd plist not found at {SEQD_PLIST}; skipping seqd remote wiring.")
        return

    try:
        with SEQD_PLIST.open("rb") as fh:
            payload = plistlib.load(fh)
    except Exception as exc:
        print(f"WARN: failed to parse {SEQD_PLIST}: {exc}")
        print("INFO: rebuilding seqd launchd plist with defaults.")
        payload = _default_seqd_plist_payload()

    env = dict(payload.get("EnvironmentVariables") or {})
    env["PATH"] = env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    env["SEQ_CH_MODE"] = "native"
    env["SEQ_CH_HOST"] = ch_host
    env["SEQ_CH_PORT"] = str(ch_port)
    env["SEQ_CH_HTTP_PORT"] = str(ch_http_port)
    env["SEQ_CH_DATABASE"] = ch_db
    env["SEQ_CH_MEM_PATH"] = env_file.get("SEQ_CH_MEM_PATH", DEFAULT_MEM_PATH) or DEFAULT_MEM_PATH
    env["SEQ_CH_LOG_PATH"] = env_file.get("SEQ_CH_LOG_PATH", DEFAULT_TRACE_PATH) or DEFAULT_TRACE_PATH
    payload["EnvironmentVariables"] = env

    with SEQD_PLIST.open("wb") as fh:
        plistlib.dump(payload, fh, sort_keys=False)

    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{SEQD_LABEL}"
    run_best_effort(["launchctl", "bootout", target])
    run_best_effort(["launchctl", "bootout", domain, str(SEQD_PLIST)])

    ok_bootstrap = False
    err_bootstrap = ""
    for _ in range(3):
        ok_bootstrap, err_bootstrap = run_best_effort(["launchctl", "bootstrap", domain, str(SEQD_PLIST)])
        if ok_bootstrap:
            break
        if "already loaded" in err_bootstrap.lower():
            ok_bootstrap = True
            break
        time.sleep(0.25)
    if not ok_bootstrap:
        print(f"WARN: launchctl bootstrap failed for seqd: {err_bootstrap}")
    ok_kick, err_kick = run_best_effort(["launchctl", "kickstart", "-k", target])
    if not ok_kick:
        print(f"WARN: launchctl kickstart failed for seqd: {err_kick}")
    print("seqd launchd configured for remote ClickHouse:")
    print(f"  label={SEQD_LABEL}")
    print(f"  host={ch_host} port={ch_port} db={ch_db}")


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
    configure_seqd_launchd(
        ch_host=ch_host,
        ch_port=ch_port,
        ch_http_port=ch_http_port,
        ch_db=ch_db,
        env_file=env_file,
    )

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
