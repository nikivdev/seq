#!/usr/bin/env python3
"""Health checks for seq remote-first capture on ClickHouse."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ENV_FILE = Path.home() / ".config" / "flow" / "env-local" / "personal" / "production.env"
DEFAULT_FALLBACK = Path("~/.local/state/seq/remote_fallback/seq_mem_fallback.jsonl").expanduser()
DEFAULT_SEQ_BIN = (Path(__file__).resolve().parent.parent / "cli" / "cpp" / "out" / "bin" / "seq").resolve()
SEQD_LABEL = "dev.nikiv.seqd"


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


def merged_env() -> dict[str, str]:
    env = read_env_file(ENV_FILE)
    out = dict(os.environ)
    out.update(env)
    return out


def ch_query(base_url: str, query: str, timeout_s: float) -> str:
    url = base_url.rstrip("/") + "/?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()


def launchctl_env(label: str) -> str:
    target = f"gui/{os.getuid()}/{label}"
    proc = subprocess.run(
        ["launchctl", "print", target],
        text=True,
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"launchctl print failed: {target}")
    return proc.stdout


def probe_seqd_trace(seq_bin: Path, timeout_s: float) -> tuple[bool, str]:
    if not seq_bin.exists():
        return False, f"missing seq binary: {seq_bin}"
    try:
        proc = subprocess.run(
            [str(seq_bin), "app-state"],
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
        if proc.returncode == 0:
            return True, "seq app-state probe ok"
        detail = (proc.stderr or proc.stdout).strip()
        return False, detail or f"exit={proc.returncode}"
    except Exception as exc:
        return False, str(exc)


def run_once(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    env = merged_env()
    sink_mode = (env.get("SEQ_MEM_SINK_MODE") or "auto").strip().lower()
    remote_url = (env.get("SEQ_MEM_REMOTE_URL") or "").strip()
    fallback_path = Path(env.get("SEQ_MEM_REMOTE_FALLBACK_PATH", str(DEFAULT_FALLBACK))).expanduser()
    max_age_ms = int(args.max_age_s * 1000)
    now_ms = int(time.time() * 1000)
    seq_bin = Path(args.seq_bin).expanduser().resolve()

    checks: list[dict[str, Any]] = []
    ok = True

    def add(name: str, passed: bool, detail: str, severity: str = "critical") -> None:
        nonlocal ok
        checks.append(
            {
                "name": name,
                "ok": passed,
                "severity": severity,
                "detail": detail,
            }
        )
        if not passed and severity == "critical":
            ok = False

    add("remote_url_set", bool(remote_url), remote_url or "<unset>")
    add("sink_mode_remote", sink_mode in {"remote", "dual"}, f"sink_mode={sink_mode}")

    if remote_url:
        try:
            pong = ch_query(remote_url, "SELECT 1", args.timeout_s)
            add("remote_ping", pong.strip() == "1", f"response={pong!r}")
        except Exception as exc:
            add("remote_ping", False, str(exc))

        try:
            raw = ch_query(
                remote_url,
                (
                    "SELECT max(ts_ms) AS max_ts_ms, count() AS rows_5m "
                    "FROM seq.mem_events "
                    "WHERE ts_ms > toUInt64(toUnixTimestamp64Milli(now64(3)) - 300000) "
                    "FORMAT TabSeparated"
                ),
                args.timeout_s,
            )
            max_ts_s, rows_s = (raw.split("\t", 1) + ["0"])[:2]
            max_ts_ms = int(max_ts_s or "0")
            rows_5m = int(rows_s or "0")
            age_ms = now_ms - max_ts_ms if max_ts_ms > 0 else 10**12
            add("remote_mem_recent", age_ms <= max_age_ms and rows_5m > 0, f"age_ms={age_ms} rows_5m={rows_5m}")
        except Exception as exc:
            add("remote_mem_recent", False, str(exc))

        if args.probe_seqd:
            probe_ok, probe_detail = probe_seqd_trace(seq_bin, timeout_s=max(0.5, args.timeout_s))
            add("seqd_trace_probe", probe_ok, probe_detail)

        try:
            raw = ch_query(
                remote_url,
                (
                    "SELECT max(toInt64(ts_us / 1000)) AS max_ts_ms, count() AS rows_5m "
                    "FROM seq.trace_events "
                    "WHERE ts_us > toInt64(toUnixTimestamp64Micro(now64(6)) - 300000000) "
                    "FORMAT TabSeparated"
                ),
                args.timeout_s,
            )
            max_ts_s, rows_s = (raw.split("\t", 1) + ["0"])[:2]
            max_ts_ms = int(max_ts_s or "0")
            rows_5m = int(rows_s or "0")
            age_ms = now_ms - max_ts_ms if max_ts_ms > 0 else 10**12
            add("remote_trace_recent", age_ms <= max_age_ms and rows_5m > 0, f"age_ms={age_ms} rows_5m={rows_5m}")
        except Exception as exc:
            add("remote_trace_recent", False, str(exc))

    try:
        raw = launchctl_env(SEQD_LABEL)
        has_mode = "SEQ_CH_MODE => native" in raw
        has_host = "SEQ_CH_HOST => " in raw and env.get("SEQ_CH_HOST", "") in raw
        has_db = "SEQ_CH_DATABASE => " in raw and env.get("SEQ_CH_DATABASE", "seq") in raw
        has_port = "SEQ_CH_PORT => " in raw and str(env.get("SEQ_CH_PORT", "9000")) in raw
        add("seqd_launchd_mode", has_mode, "SEQ_CH_MODE=native in launchctl env")
        add("seqd_launchd_host", has_host, f"SEQ_CH_HOST={env.get('SEQ_CH_HOST', '<unset>')}")
        add("seqd_launchd_db", has_db, f"SEQ_CH_DATABASE={env.get('SEQ_CH_DATABASE', '<unset>')}")
        add("seqd_launchd_port", has_port, f"SEQ_CH_PORT={env.get('SEQ_CH_PORT', '<unset>')}")
    except Exception as exc:
        add("seqd_launchd_env", False, str(exc))

    try:
        size = fallback_path.stat().st_size if fallback_path.exists() else 0
        add("fallback_queue_size", size <= args.max_fallback_bytes, f"bytes={size} path={fallback_path}")
    except Exception as exc:
        add("fallback_queue_size", False, str(exc), severity="warning")

    report = {
        "ts_ms": now_ms,
        "ok": ok,
        "checks": checks,
        "config": {
            "remote_url": remote_url,
            "sink_mode": sink_mode,
            "max_age_s": args.max_age_s,
            "max_fallback_bytes": args.max_fallback_bytes,
        },
    }
    return ok, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check seq remote capture health.")
    parser.add_argument("--max-age-s", type=int, default=90, help="Max allowed freshness age for remote mem/trace.")
    parser.add_argument("--max-fallback-bytes", type=int, default=1_000_000, help="Max tolerated fallback queue size.")
    parser.add_argument("--timeout-s", type=float, default=3.0, help="Remote CH HTTP timeout.")
    parser.add_argument("--seq-bin", default=str(DEFAULT_SEQ_BIN), help="Path to seq binary used for trace probe.")
    parser.add_argument("--probe-seqd", action=argparse.BooleanOptionalAction, default=True, help="Trigger lightweight seqd probe before trace freshness check.")
    parser.add_argument("--watch", type=int, default=0, help="Repeat every N seconds.")
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        ok, report = run_once(args)
        if args.json:
            print(json.dumps(report, ensure_ascii=True))
        else:
            print(f"remote_ok={report['ok']} sink_mode={report['config']['sink_mode']} remote_url={report['config']['remote_url'] or '<unset>'}")
            for check in report["checks"]:
                status = "OK" if check["ok"] else "FAIL"
                print(f"  [{status}] {check['name']}: {check['detail']}")
        if args.watch <= 0:
            return 0 if ok else 1
        time.sleep(max(1, args.watch))


if __name__ == "__main__":
    raise SystemExit(main())
