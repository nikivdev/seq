#!/usr/bin/env python3
"""Periodic health/validity checks for seq RL data capture stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_SEQ_TRACE_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl").expanduser())
DEFAULT_STATE_PATH = str(Path("~/.local/state/seq/signal_watchdog_state.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/signal_watchdog.pid").expanduser())
DEFAULT_LOG_PATH = str(Path("~/code/seq/cli/cpp/out/logs/signal_watchdog.log").expanduser())
DEFAULT_REPORT_PATH = str(Path("~/.local/state/seq/signal_watchdog_report.json").expanduser())
DEFAULT_SNAPSHOT_DIR = str(Path("~/.local/state/seq/checkpoints").expanduser())

DEFAULT_NEXT_TYPE_PID = str(Path("~/.local/state/seq/next_type_key_capture.pid").expanduser())
DEFAULT_KAR_SIGNAL_PID = str(Path("~/.local/state/seq/kar_signal.pid").expanduser())
DEFAULT_AGENT_QA_PID = str(Path("~/.local/state/seq/agent_qa_ingest.pid").expanduser())
DEFAULT_LABEL_NEXT_TYPE = "dev.nikiv.seq-capture.next-type"
DEFAULT_LABEL_KAR_SIGNAL = "dev.nikiv.seq-capture.kar-signal"
DEFAULT_LABEL_AGENT_QA = "dev.nikiv.seq-capture.agent-qa"


@dataclass
class Config:
    seq_mem_path: Path
    seq_trace_path: Path
    state_path: Path
    pidfile: Path
    log_path: Path
    report_path: Path
    interval_seconds: float
    lookback_hours: int
    tail_bytes: int
    min_kar_intents: int
    min_kar_link_rate: float
    emit_event: bool
    next_type_pidfile: Path
    kar_signal_pidfile: Path
    agent_qa_pidfile: Path
    auto_remediate: bool
    remediate_with_launchd: bool
    remediate_cooldown_seconds: float
    launchd_label_next_type: str
    launchd_label_kar_signal: str
    launchd_label_agent_qa: str
    snapshot_enabled: bool
    snapshot_dir: Path
    snapshot_interval_minutes: int
    snapshot_keep: int
    snapshot_tail_bytes: int


class Watchdog:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.iteration = 0
        self.last_remediation_ms: dict[str, int] = {}
        self.last_snapshot_ms = 0
        self._load_previous_state()

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] {message}", flush=True)

    def _safe_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)

    def _sha(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _read_pid(self, pidfile: Path) -> int:
        if not pidfile.exists():
            return 0
        try:
            return int(pidfile.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _daemon_status(self, pidfile: Path) -> dict[str, Any]:
        pid = self._read_pid(pidfile)
        return {
            "pidfile": str(pidfile),
            "pid": pid,
            "running": self._pid_alive(pid),
        }

    def _read_tail_lines(self, path: Path, max_bytes: int) -> list[str]:
        if not path.exists():
            return []
        size = path.stat().st_size
        if size <= 0:
            return []
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                _ = f.readline()  # discard partial
            data = f.read()
        if not data:
            return []
        text = data.decode("utf-8", errors="replace")
        return [ln for ln in text.splitlines() if ln.strip()]

    def _load_previous_state(self) -> None:
        if not self.cfg.state_path.exists():
            return
        try:
            payload = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                rem = payload.get("last_remediation_ms")
                if isinstance(rem, dict):
                    for key, value in rem.items():
                        if isinstance(key, str):
                            try:
                                self.last_remediation_ms[key] = int(value)
                            except Exception:
                                pass
                last_snap = payload.get("last_snapshot_ms")
                if last_snap is not None:
                    try:
                        self.last_snapshot_ms = int(last_snap)
                    except Exception:
                        self.last_snapshot_ms = 0
        except Exception:
            pass

    def _run_cmd(self, cmd: list[str], timeout_s: float = 8.0) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout_s,
            )
            if proc.returncode == 0:
                return True, (proc.stdout or "ok").strip()
            return False, (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        except Exception as exc:
            return False, str(exc)

    def _can_remediate(self, daemon_key: str, now_ms: int) -> bool:
        last = int(self.last_remediation_ms.get(daemon_key, 0))
        return (now_ms - last) >= int(self.cfg.remediate_cooldown_seconds * 1000)

    def _kickstart_label(self, label: str) -> tuple[bool, str]:
        target = f"gui/{os.getuid()}/{label}"
        return self._run_cmd(["launchctl", "kickstart", "-k", target], timeout_s=10.0)

    def _start_daemon_direct(self, daemon_key: str) -> tuple[bool, str]:
        tool_map = {
            "next_type": "next_type_key_capture_daemon.py",
            "kar_signal": "kar_signal_capture.py",
            "agent_qa": "agent_qa_ingest.py",
        }
        tool = tool_map.get(daemon_key)
        if not tool:
            return False, "unknown_daemon"
        script = Path(__file__).resolve().parent / tool
        return self._run_cmd([sys.executable, str(script), "start"], timeout_s=12.0)

    def _restart_launchd_service(self, daemon_key: str) -> tuple[bool, str]:
        script = Path(__file__).resolve().parent / "seq_capture_launchd.py"
        if not script.exists():
            return False, f"missing {script}"
        return self._run_cmd(
            [sys.executable, str(script), "restart", "--service", daemon_key],
            timeout_s=20.0,
        )

    def _daemon_label(self, daemon_key: str) -> str:
        if daemon_key == "next_type":
            return self.cfg.launchd_label_next_type
        if daemon_key == "kar_signal":
            return self.cfg.launchd_label_kar_signal
        if daemon_key == "agent_qa":
            return self.cfg.launchd_label_agent_qa
        return ""

    def _tail_hash(self, path: Path, max_bytes: int) -> str:
        if not path.exists():
            return ""
        size = path.stat().st_size
        if size <= 0:
            return ""
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return self._sha(data.decode("utf-8", errors="replace"))

    def _snapshot_file_stats(self, path: Path, tail_bytes: int) -> dict[str, Any]:
        if not path.exists():
            return {"path": str(path), "exists": False}
        st = path.stat()
        return {
            "path": str(path),
            "exists": True,
            "size": int(st.st_size),
            "mtime_ms": int(st.st_mtime * 1000),
            "tail_hash": self._tail_hash(path, tail_bytes),
        }

    def _copy_if_exists(self, src: Path, dst_dir: Path) -> str:
        if not src.exists():
            return ""
        dst = dst_dir / src.name
        shutil.copy2(src, dst)
        return str(dst)

    def _maybe_write_snapshot(self, now_ms: int, report: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.snapshot_enabled:
            return {"enabled": False}
        interval_ms = max(1, self.cfg.snapshot_interval_minutes) * 60 * 1000
        if self.last_snapshot_ms > 0 and (now_ms - self.last_snapshot_ms) < interval_ms:
            return {"enabled": True, "created": False, "reason": "interval_not_elapsed"}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snap_dir = self.cfg.snapshot_dir / f"checkpoint_{stamp}"
        snap_dir.mkdir(parents=True, exist_ok=True)

        next_state = Path(os.environ.get("SEQ_NEXT_TYPE_STATE", "~/.local/state/seq/next_type_key_capture_state.json")).expanduser().resolve()
        kar_state = Path(os.environ.get("SEQ_KAR_SIGNAL_STATE", "~/.local/state/seq/kar_signal_state.json")).expanduser().resolve()
        agent_state = Path(os.environ.get("SEQ_AGENT_QA_STATE", "~/.local/state/seq/agent_qa_ingest_state.json")).expanduser().resolve()
        copied = {
            "next_type_state": self._copy_if_exists(next_state, snap_dir),
            "kar_signal_state": self._copy_if_exists(kar_state, snap_dir),
            "agent_qa_state": self._copy_if_exists(agent_state, snap_dir),
            "watchdog_state": self._copy_if_exists(self.cfg.state_path, snap_dir),
            "watchdog_report": self._copy_if_exists(self.cfg.report_path, snap_dir),
        }

        manifest = {
            "schema_version": "seq_capture_checkpoint_v1",
            "created_at_ms": now_ms,
            "snapshot_dir": str(snap_dir),
            "seq_mem": self._snapshot_file_stats(self.cfg.seq_mem_path, self.cfg.snapshot_tail_bytes),
            "seq_trace": self._snapshot_file_stats(self.cfg.seq_trace_path, self.cfg.snapshot_tail_bytes),
            "daemons": report.get("daemons"),
            "signals": report.get("signals"),
            "copied_files": copied,
        }
        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

        snapshots = sorted(
            [p for p in self.cfg.snapshot_dir.glob("checkpoint_*") if p.is_dir()],
            key=lambda p: p.name,
        )
        keep = max(1, int(self.cfg.snapshot_keep))
        if len(snapshots) > keep:
            for old in snapshots[: len(snapshots) - keep]:
                shutil.rmtree(old, ignore_errors=True)

        self.last_snapshot_ms = now_ms
        return {"enabled": True, "created": True, "path": str(snap_dir)}

    def _emit_health_event(self, report: dict[str, Any]) -> None:
        ts_ms = int(time.time() * 1000)
        subject_obj = {
            "schema_version": "seq_signal_health_v1",
            "overall_pass": bool(report.get("overall_pass")),
            "daemons": report.get("daemons"),
            "signals": report.get("signals"),
            "validity": report.get("validity"),
        }
        row = {
            "ts_ms": ts_ms,
            "dur_us": 0,
            "ok": bool(report.get("overall_pass")),
            "session_id": "seq-signal-watchdog",
            "event_id": self._sha(f"seq.signal.health.v1|{ts_ms}|{report.get('overall_pass')}"),
            "content_hash": self._sha(self._safe_json(subject_obj)),
            "name": "seq.signal.health.v1",
            "subject": self._safe_json(subject_obj),
        }
        self.cfg.seq_mem_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg.seq_mem_path.open("a", encoding="utf-8") as f:
            f.write(self._safe_json(row))
            f.write("\n")

    def run_once(self) -> tuple[bool, dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - max(1, int(self.cfg.lookback_hours)) * 3600 * 1000

        daemons = {
            "next_type": self._daemon_status(self.cfg.next_type_pidfile),
            "kar_signal": self._daemon_status(self.cfg.kar_signal_pidfile),
            "agent_qa": self._daemon_status(self.cfg.agent_qa_pidfile),
        }
        remediations: list[dict[str, Any]] = []

        if self.cfg.auto_remediate:
            for daemon_key, status in list(daemons.items()):
                if status.get("running"):
                    continue
                if not self._can_remediate(daemon_key, now_ms):
                    remediations.append(
                        {
                            "daemon": daemon_key,
                            "attempted": False,
                            "reason": "cooldown_active",
                        }
                    )
                    continue

                ok = False
                detail = ""
                method = "direct_start"
                if self.cfg.remediate_with_launchd:
                    label = self._daemon_label(daemon_key)
                    if label:
                        method = "launchd_kickstart"
                        ok, detail = self._kickstart_label(label)
                        if not ok:
                            method = "launchd_restart"
                            ok, detail = self._restart_launchd_service(daemon_key)
                if not ok:
                    method = "direct_start"
                    ok, detail = self._start_daemon_direct(daemon_key)

                self.last_remediation_ms[daemon_key] = now_ms
                # brief settle before re-check
                time.sleep(0.3)
                daemons[daemon_key] = self._daemon_status(
                    self.cfg.next_type_pidfile
                    if daemon_key == "next_type"
                    else self.cfg.kar_signal_pidfile
                    if daemon_key == "kar_signal"
                    else self.cfg.agent_qa_pidfile
                )
                remediations.append(
                    {
                        "daemon": daemon_key,
                        "attempted": True,
                        "method": method,
                        "ok": ok,
                        "detail": detail,
                        "running_after": bool(daemons[daemon_key].get("running")),
                    }
                )

        lines = self._read_tail_lines(self.cfg.seq_mem_path, self.cfg.tail_bytes)
        parse_errors = 0
        latest_ts: dict[str, int] = {}
        counts = {
            "next_type": 0,
            "kar_intent": 0,
            "kar_outcome": 0,
            "kar_override": 0,
            "agent_qa": 0,
        }

        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                parse_errors += 1
                continue
            if not isinstance(row, dict):
                parse_errors += 1
                continue
            name = str(row.get("name") or "")
            ts_ms = int(row.get("ts_ms") or 0)

            if name.startswith("next_type."):
                latest_ts["next_type"] = max(latest_ts.get("next_type", 0), ts_ms)
                if ts_ms >= cutoff_ms:
                    counts["next_type"] += 1
            elif name == "kar.intent.v1":
                latest_ts["kar_intent"] = max(latest_ts.get("kar_intent", 0), ts_ms)
                if ts_ms >= cutoff_ms:
                    counts["kar_intent"] += 1
            elif name == "kar.outcome.v1":
                latest_ts["kar_outcome"] = max(latest_ts.get("kar_outcome", 0), ts_ms)
                if ts_ms >= cutoff_ms:
                    counts["kar_outcome"] += 1
            elif name == "kar.override.v1":
                latest_ts["kar_override"] = max(latest_ts.get("kar_override", 0), ts_ms)
                if ts_ms >= cutoff_ms:
                    counts["kar_override"] += 1
            elif name == "agent.qa.pair":
                latest_ts["agent_qa"] = max(latest_ts.get("agent_qa", 0), ts_ms)
                if ts_ms >= cutoff_ms:
                    counts["agent_qa"] += 1

        kar_intents = counts["kar_intent"]
        kar_outcomes = counts["kar_outcome"]
        kar_link_rate = (kar_outcomes / kar_intents) if kar_intents > 0 else None

        kar_link_gate = True
        if kar_intents >= self.cfg.min_kar_intents:
            kar_link_gate = (kar_link_rate is not None) and (kar_link_rate >= self.cfg.min_kar_link_rate)

        all_running = all(d["running"] for d in daemons.values())
        seq_mem_ok = self.cfg.seq_mem_path.exists()

        warnings: list[str] = []
        if counts["next_type"] == 0:
            warnings.append("no_next_type_events_in_lookback")
        if counts["agent_qa"] == 0:
            warnings.append("no_agent_qa_events_in_lookback")
        if kar_intents == 0:
            warnings.append("no_kar_intent_events_in_lookback")

        overall_pass = bool(seq_mem_ok and all_running and kar_link_gate)

        report = {
            "schema_version": "seq_signal_watchdog_report_v1",
            "generated_at_ms": now_ms,
            "inputs": {
                "seq_mem": str(self.cfg.seq_mem_path),
                "seq_trace": str(self.cfg.seq_trace_path),
                "tail_bytes": self.cfg.tail_bytes,
                "lookback_hours": self.cfg.lookback_hours,
                "min_kar_intents": self.cfg.min_kar_intents,
                "min_kar_link_rate": self.cfg.min_kar_link_rate,
                "auto_remediate": self.cfg.auto_remediate,
                "remediate_with_launchd": self.cfg.remediate_with_launchd,
            },
            "daemons": daemons,
            "signals": {
                "counts_in_lookback": counts,
                "latest_ts_ms": latest_ts,
            },
            "validity": {
                "kar_link_rate": kar_link_rate,
                "kar_link_gate_pass": kar_link_gate,
                "parse_errors": parse_errors,
                "warnings": warnings,
            },
            "remediations": remediations,
            "overall_pass": overall_pass,
        }
        report["snapshot"] = self._maybe_write_snapshot(now_ms, report)

        self.cfg.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

        state_payload = {
            "schema_version": "seq_signal_watchdog_state_v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "iteration": self.iteration,
            "last_overall_pass": overall_pass,
            "last_report_path": str(self.cfg.report_path),
            "last_remediation_ms": self.last_remediation_ms,
            "last_snapshot_ms": self.last_snapshot_ms,
        }
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state_payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

        if self.cfg.emit_event:
            try:
                self._emit_health_event(report)
            except Exception as exc:
                self.log(f"failed to emit seq.signal.health.v1: {exc}")

        return overall_pass, report

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self.log(
            f"signal watchdog started (interval={self.cfg.interval_seconds}s, "
            f"lookback={self.cfg.lookback_hours}h, report={self.cfg.report_path})"
        )

        while not self.stop_requested:
            self.iteration += 1
            passed, report = self.run_once()
            self.log(
                "check "
                f"iter={self.iteration} pass={passed} "
                f"kar_intent={report['signals']['counts_in_lookback']['kar_intent']} "
                f"kar_outcome={report['signals']['counts_in_lookback']['kar_outcome']} "
                f"next_type={report['signals']['counts_in_lookback']['next_type']} "
                f"remediations={len(report.get('remediations') or [])}"
            )
            deadline = time.time() + self.cfg.interval_seconds
            while not self.stop_requested and time.time() < deadline:
                time.sleep(0.2)

        self.log("signal watchdog stopping")
        return 0


def _read_pid(pidfile: Path) -> int:
    if not pidfile.exists():
        return 0
    try:
        return int(pidfile.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_pid(pidfile: Path, pid: int) -> None:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(f"{pid}\n", encoding="utf-8")


def _drop_pid(pidfile: Path) -> None:
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def cmd_start(cfg: Config) -> int:
    existing = _read_pid(cfg.pidfile)
    if _is_pid_alive(existing):
        print(f"already running: pid={existing}")
        return 0
    _drop_pid(cfg.pidfile)

    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--seq-mem",
        str(cfg.seq_mem_path),
        "--state-path",
        str(cfg.state_path),
        "--pidfile",
        str(cfg.pidfile),
        "--log-path",
        str(cfg.log_path),
        "--report-path",
        str(cfg.report_path),
        "--interval-seconds",
        str(cfg.interval_seconds),
        "--lookback-hours",
        str(cfg.lookback_hours),
        "--tail-bytes",
        str(cfg.tail_bytes),
        "--min-kar-intents",
        str(cfg.min_kar_intents),
        "--min-kar-link-rate",
        str(cfg.min_kar_link_rate),
        "--next-type-pidfile",
        str(cfg.next_type_pidfile),
        "--kar-signal-pidfile",
        str(cfg.kar_signal_pidfile),
        "--agent-qa-pidfile",
        str(cfg.agent_qa_pidfile),
    ]
    if not cfg.emit_event:
        run_cmd.append("--no-emit-event")

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _write_pid(cfg.pidfile, proc.pid)
    print(f"started signal watchdog: pid={proc.pid}")
    print(f"log: {cfg.log_path}")
    return 0


def cmd_stop(cfg: Config) -> int:
    pid = _read_pid(cfg.pidfile)
    if pid <= 0:
        print("not running")
        _drop_pid(cfg.pidfile)
        return 0
    if not _is_pid_alive(pid):
        print("not running (stale pidfile)")
        _drop_pid(cfg.pidfile)
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            break
        time.sleep(0.1)

    if _is_pid_alive(pid):
        os.kill(pid, signal.SIGKILL)

    _drop_pid(cfg.pidfile)
    print(f"stopped signal watchdog: pid={pid}")
    return 0


def cmd_status(cfg: Config) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)
    print(f"pidfile: {cfg.pidfile}")
    print(f"log: {cfg.log_path}")
    print(f"state: {cfg.state_path}")
    print(f"report: {cfg.report_path}")
    print(f"seq_mem: {cfg.seq_mem_path}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    return 0 if alive else 1


def cmd_preflight(cfg: Config) -> int:
    ok = True
    print("seq signal watchdog preflight")
    print(f"seq_mem: {cfg.seq_mem_path}")
    print(f"seq_trace: {cfg.seq_trace_path}")
    print(f"report: {cfg.report_path}")
    print(f"next_type_pid: {cfg.next_type_pidfile}")
    print(f"kar_signal_pid: {cfg.kar_signal_pidfile}")
    print(f"agent_qa_pid: {cfg.agent_qa_pidfile}")
    print(f"auto_remediate: {cfg.auto_remediate}")
    print(f"snapshot_enabled: {cfg.snapshot_enabled}")
    print(f"snapshot_dir: {cfg.snapshot_dir}")

    if not cfg.seq_mem_path.exists():
        print("- FAIL: seq_mem path missing")
        ok = False
    else:
        print("- OK: seq_mem path exists")

    if not cfg.seq_trace_path.exists():
        print("- WARN: seq_trace path missing")
    else:
        print("- OK: seq_trace path exists")

    cfg.report_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.snapshot_dir.mkdir(parents=True, exist_ok=True)
    print("- OK: report/state/log dirs writable")
    return 0 if ok else 1


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        seq_mem_path=Path(args.seq_mem).expanduser().resolve(),
        seq_trace_path=Path(args.seq_trace).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        report_path=Path(args.report_path).expanduser().resolve(),
        interval_seconds=max(5.0, float(args.interval_seconds)),
        lookback_hours=max(1, int(args.lookback_hours)),
        tail_bytes=max(1024 * 1024, int(args.tail_bytes)),
        min_kar_intents=max(1, int(args.min_kar_intents)),
        min_kar_link_rate=max(0.0, min(1.0, float(args.min_kar_link_rate))),
        emit_event=bool(args.emit_event),
        next_type_pidfile=Path(args.next_type_pidfile).expanduser().resolve(),
        kar_signal_pidfile=Path(args.kar_signal_pidfile).expanduser().resolve(),
        agent_qa_pidfile=Path(args.agent_qa_pidfile).expanduser().resolve(),
        auto_remediate=bool(args.auto_remediate),
        remediate_with_launchd=bool(args.remediate_with_launchd),
        remediate_cooldown_seconds=max(10.0, float(args.remediate_cooldown_seconds)),
        launchd_label_next_type=str(args.launchd_label_next_type),
        launchd_label_kar_signal=str(args.launchd_label_kar_signal),
        launchd_label_agent_qa=str(args.launchd_label_agent_qa),
        snapshot_enabled=bool(args.snapshot_enabled),
        snapshot_dir=Path(args.snapshot_dir).expanduser().resolve(),
        snapshot_interval_minutes=max(1, int(args.snapshot_interval_minutes)),
        snapshot_keep=max(1, int(args.snapshot_keep)),
        snapshot_tail_bytes=max(4096, int(args.snapshot_tail_bytes)),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seq-mem", default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM_PATH))
    parser.add_argument("--seq-trace", default=os.environ.get("SEQ_SIGNAL_WATCHDOG_SEQ_TRACE", os.environ.get("SEQ_CH_LOG_PATH", DEFAULT_SEQ_TRACE_PATH)))
    parser.add_argument("--state-path", default=os.environ.get("SEQ_SIGNAL_WATCHDOG_STATE", DEFAULT_STATE_PATH))
    parser.add_argument("--pidfile", default=os.environ.get("SEQ_SIGNAL_WATCHDOG_PIDFILE", DEFAULT_PIDFILE))
    parser.add_argument("--log-path", default=os.environ.get("SEQ_SIGNAL_WATCHDOG_LOG", DEFAULT_LOG_PATH))
    parser.add_argument("--report-path", default=os.environ.get("SEQ_SIGNAL_WATCHDOG_REPORT", DEFAULT_REPORT_PATH))
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=float(os.environ.get("SEQ_SIGNAL_WATCHDOG_INTERVAL_SECONDS", "900")),
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=int(os.environ.get("SEQ_SIGNAL_WATCHDOG_LOOKBACK_HOURS", "24")),
    )
    parser.add_argument(
        "--tail-bytes",
        type=int,
        default=int(os.environ.get("SEQ_SIGNAL_WATCHDOG_TAIL_BYTES", str(20 * 1024 * 1024))),
    )
    parser.add_argument(
        "--min-kar-intents",
        type=int,
        default=int(os.environ.get("SEQ_SIGNAL_WATCHDOG_MIN_KAR_INTENTS", "20")),
    )
    parser.add_argument(
        "--min-kar-link-rate",
        type=float,
        default=float(os.environ.get("SEQ_SIGNAL_WATCHDOG_MIN_KAR_LINK_RATE", "0.75")),
    )
    parser.add_argument(
        "--emit-event",
        action=argparse.BooleanOptionalAction,
        default=(os.environ.get("SEQ_SIGNAL_WATCHDOG_EMIT_EVENT", "true").lower() in {"1", "true", "yes", "on"}),
    )
    parser.add_argument(
        "--auto-remediate",
        action=argparse.BooleanOptionalAction,
        default=(os.environ.get("SEQ_SIGNAL_WATCHDOG_AUTO_REMEDIATE", "true").lower() in {"1", "true", "yes", "on"}),
    )
    parser.add_argument(
        "--remediate-with-launchd",
        action=argparse.BooleanOptionalAction,
        default=(os.environ.get("SEQ_SIGNAL_WATCHDOG_REMEDIATE_WITH_LAUNCHD", "true").lower() in {"1", "true", "yes", "on"}),
    )
    parser.add_argument(
        "--remediate-cooldown-seconds",
        type=float,
        default=float(os.environ.get("SEQ_SIGNAL_WATCHDOG_REMEDIATE_COOLDOWN_SECONDS", "120")),
    )
    parser.add_argument(
        "--launchd-label-next-type",
        default=os.environ.get("SEQ_SIGNAL_WATCHDOG_LABEL_NEXT_TYPE", DEFAULT_LABEL_NEXT_TYPE),
    )
    parser.add_argument(
        "--launchd-label-kar-signal",
        default=os.environ.get("SEQ_SIGNAL_WATCHDOG_LABEL_KAR_SIGNAL", DEFAULT_LABEL_KAR_SIGNAL),
    )
    parser.add_argument(
        "--launchd-label-agent-qa",
        default=os.environ.get("SEQ_SIGNAL_WATCHDOG_LABEL_AGENT_QA", DEFAULT_LABEL_AGENT_QA),
    )
    parser.add_argument(
        "--snapshot-enabled",
        action=argparse.BooleanOptionalAction,
        default=(os.environ.get("SEQ_SIGNAL_WATCHDOG_SNAPSHOT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}),
    )
    parser.add_argument(
        "--snapshot-dir",
        default=os.environ.get("SEQ_SIGNAL_WATCHDOG_SNAPSHOT_DIR", DEFAULT_SNAPSHOT_DIR),
    )
    parser.add_argument(
        "--snapshot-interval-minutes",
        type=int,
        default=int(os.environ.get("SEQ_SIGNAL_WATCHDOG_SNAPSHOT_INTERVAL_MINUTES", "30")),
    )
    parser.add_argument(
        "--snapshot-keep",
        type=int,
        default=int(os.environ.get("SEQ_SIGNAL_WATCHDOG_SNAPSHOT_KEEP", "48")),
    )
    parser.add_argument(
        "--snapshot-tail-bytes",
        type=int,
        default=int(os.environ.get("SEQ_SIGNAL_WATCHDOG_SNAPSHOT_TAIL_BYTES", str(1024 * 1024))),
    )
    parser.add_argument("--next-type-pidfile", default=os.environ.get("SEQ_NEXT_TYPE_PIDFILE", DEFAULT_NEXT_TYPE_PID))
    parser.add_argument("--kar-signal-pidfile", default=os.environ.get("SEQ_KAR_SIGNAL_PIDFILE", DEFAULT_KAR_SIGNAL_PID))
    parser.add_argument("--agent-qa-pidfile", default=os.environ.get("SEQ_AGENT_QA_PIDFILE", DEFAULT_AGENT_QA_PID))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seq signal watchdog")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run watchdog loop in foreground.")
    add_common_args(p_run)

    p_once = sub.add_parser("once", help="Run one health/validity check.")
    add_common_args(p_once)

    p_start = sub.add_parser("start", help="Start watchdog daemon.")
    add_common_args(p_start)

    p_stop = sub.add_parser("stop", help="Stop watchdog daemon.")
    add_common_args(p_stop)

    p_status = sub.add_parser("status", help="Show watchdog daemon status.")
    add_common_args(p_status)

    p_preflight = sub.add_parser("preflight", help="Check watchdog prerequisites.")
    add_common_args(p_preflight)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cfg = build_config(args)

    if args.command == "start":
        return cmd_start(cfg)
    if args.command == "stop":
        return cmd_stop(cfg)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "preflight":
        return cmd_preflight(cfg)

    wd = Watchdog(cfg)
    if args.command == "once":
        ok, report = wd.run_once()
        print(f"overall_pass={ok}")
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if ok else 1
    if args.command == "run":
        return wd.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
