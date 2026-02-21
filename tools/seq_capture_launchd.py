#!/usr/bin/env python3
"""Manage always-on launchd supervision for seq capture daemons."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LABEL_PREFIX = "dev.nikiv.seq-capture"
DEFAULT_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LOG_DIR = Path.home() / "code" / "seq" / "cli" / "cpp" / "out" / "logs"
DEFAULT_REPO_ROOT = Path.home() / "code" / "seq"


@dataclass(frozen=True)
class ServiceDef:
    key: str
    suffix: str
    tool: str
    pid_env: str
    pid_default: str


SERVICES: tuple[ServiceDef, ...] = (
    ServiceDef(
        key="next_type",
        suffix="next-type",
        tool="next_type_key_capture_daemon.py",
        pid_env="SEQ_NEXT_TYPE_PIDFILE",
        pid_default="~/.local/state/seq/next_type_key_capture.pid",
    ),
    ServiceDef(
        key="kar_signal",
        suffix="kar-signal",
        tool="kar_signal_capture.py",
        pid_env="SEQ_KAR_SIGNAL_PIDFILE",
        pid_default="~/.local/state/seq/kar_signal.pid",
    ),
    ServiceDef(
        key="agent_qa",
        suffix="agent-qa",
        tool="agent_qa_ingest.py",
        pid_env="SEQ_AGENT_QA_PIDFILE",
        pid_default="~/.local/state/seq/agent_qa_ingest.pid",
    ),
    ServiceDef(
        key="watchdog",
        suffix="watchdog",
        tool="seq_signal_watchdog.py",
        pid_env="SEQ_SIGNAL_WATCHDOG_PIDFILE",
        pid_default="~/.local/state/seq/signal_watchdog.pid",
    ),
    ServiceDef(
        key="next_type_predictor",
        suffix="next-type-predictor",
        tool="next_type_predictor_daemon.py",
        pid_env="SEQ_NEXT_TYPE_PREDICTOR_PIDFILE",
        pid_default="~/.local/state/seq/next_type_predictor.pid",
    ),
)


PASS_ENV_KEYS = (
    "PATH",
    "HOME",
    "SEQ_CH_MODE",
    "SEQ_CH_MEM_PATH",
    "SEQ_CH_LOG_PATH",
    "SEQ_NEXT_TYPE_TAP_LOG",
    "SEQ_NEXT_TYPE_TAP_BIN",
    "SEQ_NEXT_TYPE_OUT",
    "SEQ_NEXT_TYPE_STATE",
    "SEQ_NEXT_TYPE_PIDFILE",
    "SEQ_NEXT_TYPE_LOG",
    "SEQ_NEXT_TYPE_POLL_SECONDS",
    "SEQ_NEXT_TYPE_BATCH_SIZE",
    "SEQ_NEXT_TYPE_FLUSH_MS",
    "SEQ_NEXT_TYPE_SOURCE",
    "SEQ_NEXT_TYPE_LAUNCH_TAP",
    "SEQ_NEXT_TYPE_SESSION_ID",
    "SEQ_NEXT_TYPE_PROJECT_PATH",
    "SEQ_NEXT_TYPE_PREDICTOR_INBOX",
    "SEQ_NEXT_TYPE_PREDICTOR_STATE",
    "SEQ_NEXT_TYPE_PREDICTOR_MODEL",
    "SEQ_NEXT_TYPE_PREDICTOR_PIDFILE",
    "SEQ_NEXT_TYPE_PREDICTOR_LOG",
    "SEQ_NEXT_TYPE_PREDICTOR_POLL_SECONDS",
    "SEQ_NEXT_TYPE_PREDICTOR_SAVE_INTERVAL_SECONDS",
    "SEQ_NEXT_TYPE_PREDICTOR_COOLDOWN_MS",
    "SEQ_NEXT_TYPE_PREDICTOR_TTL_MS",
    "SEQ_NEXT_TYPE_PREDICTOR_MIN_PREFIX",
    "SEQ_NEXT_TYPE_PREDICTOR_MIN_TOKEN_COUNT",
    "SEQ_NEXT_TYPE_PREDICTOR_MIN_BIGRAM_COUNT",
    "SEQ_NEXT_TYPE_PREDICTOR_MAX_VOCAB",
    "SEQ_NEXT_TYPE_PREDICTOR_EMIT_SEQ_EVENTS",
    "SEQ_KAR_SIGNAL_STATE",
    "SEQ_KAR_SIGNAL_PIDFILE",
    "SEQ_KAR_SIGNAL_LOG",
    "SEQ_KAR_SIGNAL_POLL_SECONDS",
    "SEQ_KAR_SIGNAL_OUTCOME_WINDOW_MS",
    "SEQ_KAR_SIGNAL_OVERRIDE_WINDOW_MS",
    "SEQ_AGENT_QA_CLAUDE_DIR",
    "SEQ_AGENT_QA_CODEX_DIR",
    "SEQ_AGENT_QA_STATE",
    "SEQ_AGENT_QA_PIDFILE",
    "SEQ_AGENT_QA_LOG",
    "SEQ_AGENT_QA_ZVEC_JSONL",
    "SEQ_AGENT_QA_POLL_SECONDS",
    "SEQ_AGENT_QA_RESCAN_SECONDS",
    "SEQ_AGENT_QA_FLUSH_EVERY",
    "SEQ_AGENT_QA_MAX_TEXT_CHARS",
    "SEQ_AGENT_QA_INCLUDE_TEXT",
    "SEQ_SIGNAL_WATCHDOG_STATE",
    "SEQ_SIGNAL_WATCHDOG_PIDFILE",
    "SEQ_SIGNAL_WATCHDOG_LOG",
    "SEQ_SIGNAL_WATCHDOG_REPORT",
    "SEQ_SIGNAL_WATCHDOG_INTERVAL_SECONDS",
    "SEQ_SIGNAL_WATCHDOG_LOOKBACK_HOURS",
    "SEQ_SIGNAL_WATCHDOG_TAIL_BYTES",
    "SEQ_SIGNAL_WATCHDOG_MIN_KAR_INTENTS",
    "SEQ_SIGNAL_WATCHDOG_MIN_KAR_LINK_RATE",
    "SEQ_SIGNAL_WATCHDOG_EMIT_EVENT",
    "SEQ_SIGNAL_WATCHDOG_AUTO_REMEDIATE",
    "SEQ_SIGNAL_WATCHDOG_REMEDIATE_WITH_LAUNCHD",
    "SEQ_SIGNAL_WATCHDOG_REMEDIATE_COOLDOWN_SECONDS",
    "SEQ_SIGNAL_WATCHDOG_LABEL_NEXT_TYPE",
    "SEQ_SIGNAL_WATCHDOG_LABEL_KAR_SIGNAL",
    "SEQ_SIGNAL_WATCHDOG_LABEL_AGENT_QA",
    "SEQ_SIGNAL_WATCHDOG_SNAPSHOT_ENABLED",
    "SEQ_SIGNAL_WATCHDOG_SNAPSHOT_DIR",
    "SEQ_SIGNAL_WATCHDOG_SNAPSHOT_INTERVAL_MINUTES",
    "SEQ_SIGNAL_WATCHDOG_SNAPSHOT_KEEP",
    "SEQ_SIGNAL_WATCHDOG_SNAPSHOT_TAIL_BYTES",
    "SEQ_SIGNAL_WATCHDOG_SEQ_TRACE",
    "PYTHONUNBUFFERED",
)


def _service_by_key(key: str) -> ServiceDef:
    for svc in SERVICES:
        if svc.key == key:
            return svc
    raise KeyError(key)


def _selected_services(value: str) -> list[ServiceDef]:
    if value == "all":
        return list(SERVICES)
    return [_service_by_key(value)]


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def _launchd_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _label(prefix: str, svc: ServiceDef) -> str:
    return f"{prefix}.{svc.suffix}"


def _pidfile_for_service(svc: ServiceDef) -> Path:
    raw = os.environ.get(svc.pid_env, svc.pid_default)
    return Path(raw).expanduser().resolve()


def _capture_env() -> dict[str, str]:
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"),
        "PYTHONUNBUFFERED": os.environ.get("PYTHONUNBUFFERED", "1"),
    }
    for key in PASS_ENV_KEYS:
        if key in os.environ and os.environ[key] != "":
            env[key] = os.environ[key]
    if "SEQ_CH_MEM_PATH" not in env:
        env["SEQ_CH_MEM_PATH"] = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
    if "SEQ_CH_LOG_PATH" not in env:
        env["SEQ_CH_LOG_PATH"] = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl").expanduser())
    return env


def _build_service_command(python_bin: str, repo_root: Path, svc: ServiceDef, wrapper: Path) -> list[str]:
    pidfile = _pidfile_for_service(svc)
    tool_path = repo_root / "tools" / svc.tool
    child = [python_bin, str(tool_path), "run"]
    # Watchdog should remediate + checkpoint automatically in supervised mode.
    if svc.key == "watchdog":
        child.extend(["--auto-remediate", "--snapshot-enabled"])
    return [python_bin, str(wrapper), "--pidfile", str(pidfile), "--", *child]


def _build_plist(
    label: str,
    command: list[str],
    stdout_log: Path,
    stderr_log: Path,
    env: dict[str, str],
) -> dict:
    return {
        "Label": label,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "EnvironmentVariables": env,
        "StandardOutPath": str(stdout_log),
        "StandardErrorPath": str(stderr_log),
        "SoftResourceLimits": {"NumberOfFiles": 65536},
    }


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(payload, f, sort_keys=False)


def _bootout(label: str, plist_path: Path) -> None:
    target = _launchd_target(label)
    _run(["launchctl", "bootout", target], check=False)
    _run(["launchctl", "bootout", target, str(plist_path)], check=False)


def _bootstrap(plist_path: Path) -> None:
    target = f"gui/{os.getuid()}"
    last_err = ""
    for attempt in range(1, 4):
        res = _run(["launchctl", "bootstrap", target, str(plist_path)], check=False)
        if res.returncode == 0:
            return
        last_err = (res.stderr or res.stdout or "").strip()
        if "Input/output error" not in last_err or attempt == 3:
            break
        time.sleep(0.5 * attempt)
    raise RuntimeError(f"launchctl bootstrap failed: {last_err}")


def _kickstart(label: str) -> None:
    res = _run(["launchctl", "kickstart", "-k", _launchd_target(label)], check=False)
    if res.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed: {res.stdout}\n{res.stderr}")


def _status_one(label: str) -> tuple[bool, str]:
    res = _run(["launchctl", "print", _launchd_target(label)], check=False)
    if res.returncode == 0:
        return True, res.stdout
    return False, (res.stderr or res.stdout)


def _stop_legacy_daemon(python_bin: str, repo_root: Path, svc: ServiceDef) -> None:
    tool = repo_root / "tools" / svc.tool
    _run([python_bin, str(tool), "stop"], check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage launchd supervision for seq capture daemons")
    sub = parser.add_subparsers(dest="action", required=True)

    for action in ("install", "restart", "stop", "status", "uninstall", "print-plist"):
        p = sub.add_parser(action)
        p.add_argument(
            "--service",
            choices=["all", "next_type", "kar_signal", "agent_qa", "watchdog", "next_type_predictor"],
            default="all",
        )
        p.add_argument("--label-prefix", default=os.environ.get("SEQ_CAPTURE_LAUNCHD_LABEL_PREFIX", DEFAULT_LABEL_PREFIX))
        p.add_argument("--plist-dir", type=Path, default=DEFAULT_PLIST_DIR)
        p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
        p.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
        p.add_argument("--python", default=sys.executable)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    services = _selected_services(args.service)
    prefix = str(args.label_prefix)
    plist_dir = args.plist_dir.expanduser().resolve()
    log_dir = args.log_dir.expanduser().resolve()
    repo_root = args.repo_root.expanduser().resolve()
    python_bin = str(args.python)
    wrapper = repo_root / "tools" / "launchd_keepalive_wrapper.py"

    if not wrapper.exists():
        raise FileNotFoundError(f"missing wrapper: {wrapper}")

    env = _capture_env()
    log_dir.mkdir(parents=True, exist_ok=True)

    if args.action in {"install", "restart", "print-plist"}:
        exit_code = 0
        for svc in services:
            label = _label(prefix, svc)
            plist_path = plist_dir / f"{label}.plist"
            stdout_log = log_dir / f"{svc.key}.launchd.stdout.log"
            stderr_log = log_dir / f"{svc.key}.launchd.stderr.log"
            command = _build_service_command(python_bin, repo_root, svc, wrapper)
            payload = _build_plist(label, command, stdout_log, stderr_log, env)

            if args.action == "print-plist":
                plistlib.dump(payload, sys.stdout.buffer, sort_keys=False)
                continue

            _stop_legacy_daemon(python_bin, repo_root, svc)
            _write_plist(plist_path, payload)
            _bootout(label, plist_path)
            try:
                _bootstrap(plist_path)
                if args.action == "restart":
                    _kickstart(label)
            except Exception as exc:
                print(f"FAIL {svc.key}: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            print(f"loaded {svc.key}: {_launchd_target(label)}")
            print(f"  plist:  {plist_path}")
            print(f"  stdout: {stdout_log}")
            print(f"  stderr: {stderr_log}")
        return exit_code

    if args.action == "status":
        exit_code = 0
        for svc in services:
            label = _label(prefix, svc)
            ok, detail = _status_one(label)
            print(f"[{svc.key}] {_launchd_target(label)}: {'loaded' if ok else 'not_loaded'}")
            if ok:
                # Show compact status lines.
                for line in detail.splitlines():
                    if any(k in line for k in ("state =", "pid =", "path =", "program =", "last exit code =")):
                        print(f"  {line.strip()}")
            else:
                msg = detail.strip()
                if msg:
                    print(f"  {msg.splitlines()[-1]}")
                exit_code = 1
        return exit_code

    if args.action in {"stop", "uninstall"}:
        for svc in services:
            label = _label(prefix, svc)
            plist_path = plist_dir / f"{label}.plist"
            _bootout(label, plist_path)
            print(f"stopped {svc.key}: {_launchd_target(label)}")
            if args.action == "uninstall" and plist_path.exists():
                plist_path.unlink()
                print(f"  removed: {plist_path}")
        return 0

    raise RuntimeError(f"unknown action: {args.action}")


if __name__ == "__main__":
    raise SystemExit(main())
