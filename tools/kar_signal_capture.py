#!/usr/bin/env python3
"""Derive RL-grade Kar signal events from seq mem stream.

Source events consumed:
- seqd.run
- cli.open_app_toggle.action
- app.activate

Derived events emitted:
- kar.intent.v1
- kar.outcome.v1
- kar.override.v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seq_mem_sink import append_seq_mem_rows

DEFAULT_SEQ_MEM_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_STATE_PATH = str(Path("~/.local/state/seq/kar_signal_state.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/kar_signal.pid").expanduser())
DEFAULT_LOG_PATH = str(Path("~/code/seq/cli/cpp/out/logs/kar_signal_capture.log").expanduser())

SOURCE_NAMES = {
    "seqd.run",
    "cli.run.local",
    "seqd.open_app_toggle",
    "cli.open_app_toggle.action",
    "app.activate",
}


@dataclass
class PendingIntent:
    decision_id: str
    session_id: str
    ts_ms: int
    action_type: str
    action_name: str
    target_app: str
    source_event_id: str
    expired_at_ms: int
    resolved: bool = False
    overridden_by: str = ""


@dataclass
class Config:
    seq_mem_path: Path
    state_path: Path
    pidfile: Path
    log_path: Path
    poll_seconds: float
    outcome_window_ms: int
    override_window_ms: int
    reset_state: bool


class KarSignalCapture:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.offset = 0
        self.inode = 0
        self.pending: dict[str, PendingIntent] = {}

        self.rows_seen = 0
        self.rows_emitted = 0
        self.rows_skipped = 0
        self.last_state_save = 0.0
        self.state_loaded = False

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] {message}", flush=True)

    def _sha(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _safe_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)

    def _subject(self, row: dict[str, Any]) -> dict[str, Any]:
        subj = row.get("subject")
        if isinstance(subj, dict):
            return subj
        if isinstance(subj, str):
            try:
                parsed = json.loads(subj)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {}

    def _parse_open_toggle_subject(self, raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in raw.split("\t"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        return out

    def _mk_row(self, *, ts_ms: int, session_id: str, name: str, subject_obj: dict[str, Any], ok: bool) -> dict[str, Any]:
        event_id = self._sha(
            f"{name}|{session_id}|{subject_obj.get('decision_id','')}|"
            f"{subject_obj.get('source_event_id','')}|{subject_obj.get('override_decision_id','')}"
        )
        return {
            "ts_ms": int(ts_ms),
            "dur_us": 0,
            "ok": bool(ok),
            "session_id": session_id,
            "event_id": event_id,
            "content_hash": self._sha(self._safe_json(subject_obj)),
            "name": name,
            "subject": self._safe_json(subject_obj),
        }

    def _append_row(self, row: dict[str, Any]) -> None:
        append_seq_mem_rows([row], local_path=self.cfg.seq_mem_path)
        self.rows_emitted += 1

    def _emit_intent(
        self,
        *,
        ts_ms: int,
        session_id: str,
        decision_id: str,
        source_event_id: str,
        source_event_name: str,
        action_type: str,
        action_name: str,
        macro_name: str,
        target_app: str,
        front_app: str,
        prev_app: str,
        decision: str,
    ) -> None:
        subj = {
            "schema_version": "kar_intent_v1",
            "source": "kar",
            "decision_id": decision_id,
            "source_event_id": source_event_id,
            "source_event_name": source_event_name,
            "session_id": session_id,
            "action_type": action_type,
            "action_name": action_name,
            "macro_name": macro_name,
            "target_app": target_app,
            "front_app": front_app,
            "prev_app": prev_app,
            "decision": decision,
        }
        self._append_row(
            self._mk_row(
                ts_ms=ts_ms,
                session_id=session_id,
                name="kar.intent.v1",
                subject_obj=subj,
                ok=True,
            )
        )

    def _emit_outcome(
        self,
        *,
        ts_ms: int,
        session_id: str,
        decision_id: str,
        source_event_id: str,
        action_type: str,
        action_name: str,
        target_app: str,
        outcome: str,
        latency_ms: int,
        observed_app: str,
        reason: str,
    ) -> None:
        subj = {
            "schema_version": "kar_outcome_v1",
            "source": "kar",
            "decision_id": decision_id,
            "source_event_id": source_event_id,
            "session_id": session_id,
            "action_type": action_type,
            "action_name": action_name,
            "target_app": target_app,
            "outcome": outcome,
            "latency_ms": int(max(0, latency_ms)),
            "observed_app": observed_app,
            "reason": reason,
        }
        ok = outcome in {"success", "partial"}
        self._append_row(
            self._mk_row(
                ts_ms=ts_ms,
                session_id=session_id,
                name="kar.outcome.v1",
                subject_obj=subj,
                ok=ok,
            )
        )

    def _emit_override(
        self,
        *,
        ts_ms: int,
        session_id: str,
        decision_id: str,
        override_decision_id: str,
        action_name: str,
        override_action_name: str,
        ms_since_decision: int,
        reason: str,
    ) -> None:
        subj = {
            "schema_version": "kar_override_v1",
            "source": "kar",
            "session_id": session_id,
            "decision_id": decision_id,
            "override_decision_id": override_decision_id,
            "action_name": action_name,
            "override_action_name": override_action_name,
            "ms_since_decision": int(max(0, ms_since_decision)),
            "reason": reason,
        }
        self._append_row(
            self._mk_row(
                ts_ms=ts_ms,
                session_id=session_id,
                name="kar.override.v1",
                subject_obj=subj,
                ok=True,
            )
        )

    def load_state(self) -> None:
        if self.cfg.reset_state:
            self.offset = 0
            self.inode = 0
            self.state_loaded = False
            return
        if not self.cfg.state_path.exists():
            return
        try:
            payload = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        offset = payload.get("offset")
        inode = payload.get("inode")
        if isinstance(offset, int) and offset >= 0:
            self.offset = offset
        if isinstance(inode, int) and inode >= 0:
            self.inode = inode
        self.state_loaded = True

    def save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_state_save) < 1.0:
            return
        payload = {
            "schema_version": "kar_signal_state_v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "offset": int(self.offset),
            "inode": int(self.inode),
            "rows_seen": int(self.rows_seen),
            "rows_emitted": int(self.rows_emitted),
            "rows_skipped": int(self.rows_skipped),
        }
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        self.last_state_save = now

    def _expire_pending(self, now_ts_ms: int) -> None:
        expired_ids: list[str] = []
        for decision_id, p in self.pending.items():
            if p.resolved:
                expired_ids.append(decision_id)
                continue
            if now_ts_ms >= p.expired_at_ms:
                self._emit_outcome(
                    ts_ms=now_ts_ms,
                    session_id=p.session_id,
                    decision_id=p.decision_id,
                    source_event_id=p.source_event_id,
                    action_type=p.action_type,
                    action_name=p.action_name,
                    target_app=p.target_app,
                    outcome="wasted",
                    latency_ms=now_ts_ms - p.ts_ms,
                    observed_app="",
                    reason="no_matching_app_activate_within_window",
                )
                p.resolved = True
                expired_ids.append(decision_id)
        for d in expired_ids:
            self.pending.pop(d, None)

    def _handle_override(self, *, ts_ms: int, session_id: str, new_decision_id: str, new_action_name: str) -> None:
        latest: PendingIntent | None = None
        for p in self.pending.values():
            if p.session_id != session_id or p.resolved:
                continue
            if latest is None or p.ts_ms > latest.ts_ms:
                latest = p
        if latest is None:
            return
        if latest.decision_id == new_decision_id:
            return
        delta = ts_ms - latest.ts_ms
        if delta < 0 or delta > self.cfg.override_window_ms:
            return

        self._emit_override(
            ts_ms=ts_ms,
            session_id=session_id,
            decision_id=latest.decision_id,
            override_decision_id=new_decision_id,
            action_name=latest.action_name,
            override_action_name=new_action_name,
            ms_since_decision=delta,
            reason="superseded_by_new_kar_intent",
        )
        self._emit_outcome(
            ts_ms=ts_ms,
            session_id=session_id,
            decision_id=latest.decision_id,
            source_event_id=latest.source_event_id,
            action_type=latest.action_type,
            action_name=latest.action_name,
            target_app=latest.target_app,
            outcome="wasted",
            latency_ms=delta,
            observed_app="",
            reason="superseded_by_new_kar_intent",
        )
        latest.resolved = True
        latest.overridden_by = new_decision_id

    def _handle_row(self, row: dict[str, Any]) -> None:
        name = str(row.get("name") or "")
        if not name or name.startswith("kar."):
            self.rows_skipped += 1
            return
        if name not in SOURCE_NAMES:
            self.rows_skipped += 1
            return

        ts_ms = int(row.get("ts_ms") or 0)
        if ts_ms <= 0:
            ts_ms = int(time.time() * 1000)
        session_id = str(row.get("session_id") or "") or "kar"
        source_event_id = str(row.get("event_id") or self._sha(self._safe_json(row)))

        self._expire_pending(ts_ms)

        if name in {"seqd.run", "cli.run.local"}:
            macro_name = str(row.get("subject") or "").strip()
            decision_id = self._sha(f"kar.intent|{name}|{source_event_id}")
            self._emit_intent(
                ts_ms=ts_ms,
                session_id=session_id,
                decision_id=decision_id,
                source_event_id=source_event_id,
                source_event_name=name,
                action_type="run_macro",
                action_name=macro_name,
                macro_name=macro_name,
                target_app="",
                front_app="",
                prev_app="",
                decision="run",
            )

            ok = bool(row.get("ok"))
            self._emit_outcome(
                ts_ms=ts_ms,
                session_id=session_id,
                decision_id=decision_id,
                source_event_id=source_event_id,
                action_type="run_macro",
                action_name=macro_name,
                target_app="",
                outcome="partial" if ok else "failure",
                latency_ms=int((row.get("dur_us") or 0) / 1000),
                observed_app="",
                reason=f"{name}_returned_ok" if ok else f"{name}_failed",
            )
            return

        if name == "seqd.open_app_toggle":
            target = str(row.get("subject") or "").strip()
            decision = "open_target"
            action_name = f"open_app_toggle:{decision}:{target or 'unknown'}"
            decision_id = self._sha(f"kar.intent|seqd.open_app_toggle|{source_event_id}|{target}")

            self._handle_override(
                ts_ms=ts_ms,
                session_id=session_id,
                new_decision_id=decision_id,
                new_action_name=action_name,
            )

            self._emit_intent(
                ts_ms=ts_ms,
                session_id=session_id,
                decision_id=decision_id,
                source_event_id=source_event_id,
                source_event_name=name,
                action_type="open_app_toggle",
                action_name=action_name,
                macro_name="",
                target_app=target,
                front_app="",
                prev_app="",
                decision=decision,
            )

            if target:
                self.pending[decision_id] = PendingIntent(
                    decision_id=decision_id,
                    session_id=session_id,
                    ts_ms=ts_ms,
                    action_type="open_app_toggle",
                    action_name=action_name,
                    target_app=target,
                    source_event_id=source_event_id,
                    expired_at_ms=ts_ms + self.cfg.outcome_window_ms,
                )
            else:
                self._emit_outcome(
                    ts_ms=ts_ms,
                    session_id=session_id,
                    decision_id=decision_id,
                    source_event_id=source_event_id,
                    action_type="open_app_toggle",
                    action_name=action_name,
                    target_app="",
                    outcome="failure",
                    latency_ms=0,
                    observed_app="",
                    reason="missing_target_in_seqd_open_app_toggle",
                )
            return

        if name == "cli.open_app_toggle.action":
            raw_subj = str(row.get("subject") or "")
            parsed = self._parse_open_toggle_subject(raw_subj)
            target = parsed.get("target", "")
            front = parsed.get("front", "")
            prev = parsed.get("prev", "")
            decision = parsed.get("decision", "")
            expected_app = prev if (decision == "open_prev" and prev) else target
            action_name = f"open_app_toggle:{decision or 'unknown'}:{target or 'unknown'}"
            decision_id = self._sha(f"kar.intent|open_toggle|{source_event_id}|{expected_app}|{decision}")

            self._handle_override(
                ts_ms=ts_ms,
                session_id=session_id,
                new_decision_id=decision_id,
                new_action_name=action_name,
            )

            self._emit_intent(
                ts_ms=ts_ms,
                session_id=session_id,
                decision_id=decision_id,
                source_event_id=source_event_id,
                source_event_name=name,
                action_type="open_app_toggle",
                action_name=action_name,
                macro_name="",
                target_app=expected_app,
                front_app=front,
                prev_app=prev,
                decision=decision,
            )

            if expected_app:
                self.pending[decision_id] = PendingIntent(
                    decision_id=decision_id,
                    session_id=session_id,
                    ts_ms=ts_ms,
                    action_type="open_app_toggle",
                    action_name=action_name,
                    target_app=expected_app,
                    source_event_id=source_event_id,
                    expired_at_ms=ts_ms + self.cfg.outcome_window_ms,
                )
            else:
                self._emit_outcome(
                    ts_ms=ts_ms,
                    session_id=session_id,
                    decision_id=decision_id,
                    source_event_id=source_event_id,
                    action_type="open_app_toggle",
                    action_name=action_name,
                    target_app="",
                    outcome="failure",
                    latency_ms=0,
                    observed_app="",
                    reason="missing_expected_target_app",
                )
            return

        if name == "app.activate":
            app_name = str(row.get("subject") or "")
            resolved_ids: list[str] = []
            for decision_id, p in self.pending.items():
                if p.session_id != session_id or p.resolved:
                    continue
                if ts_ms < p.ts_ms or ts_ms > p.expired_at_ms:
                    continue
                if app_name == p.target_app:
                    self._emit_outcome(
                        ts_ms=ts_ms,
                        session_id=session_id,
                        decision_id=decision_id,
                        source_event_id=p.source_event_id,
                        action_type=p.action_type,
                        action_name=p.action_name,
                        target_app=p.target_app,
                        outcome="success",
                        latency_ms=ts_ms - p.ts_ms,
                        observed_app=app_name,
                        reason="target_app_activated",
                    )
                    p.resolved = True
                    resolved_ids.append(decision_id)
            for d in resolved_ids:
                self.pending.pop(d, None)
            return

    def process_from_offset_once(self) -> int:
        self.load_state()
        if not self.cfg.seq_mem_path.exists():
            self.log(f"seq_mem path missing: {self.cfg.seq_mem_path}")
            return 1

        stat = self.cfg.seq_mem_path.stat()
        inode = int(getattr(stat, "st_ino", 0))
        if self.inode and self.inode != inode:
            self.offset = 0
        self.inode = inode

        with self.cfg.seq_mem_path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            while True:
                line = fh.readline()
                if not line:
                    break
                self.offset = fh.tell()
                self.rows_seen += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    self.rows_skipped += 1
                    continue
                if not isinstance(obj, dict):
                    self.rows_skipped += 1
                    continue
                self._handle_row(obj)

        self._expire_pending(int(time.time() * 1000))
        self.save_state(force=True)
        self.log(f"once complete: seen={self.rows_seen} emitted={self.rows_emitted} skipped={self.rows_skipped}")
        return 0

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self.load_state()
        if not self.state_loaded and self.cfg.seq_mem_path.exists():
            try:
                stat0 = self.cfg.seq_mem_path.stat()
                self.offset = int(stat0.st_size)
                self.inode = int(getattr(stat0, "st_ino", 0))
                self.log(
                    f"no prior state; tailing from EOF offset={self.offset} "
                    f"(use 'once' for historical backfill)"
                )
            except Exception:
                pass
        self.log(
            f"kar signal capture started (seq_mem={self.cfg.seq_mem_path}, "
            f"outcome_window_ms={self.cfg.outcome_window_ms}, override_window_ms={self.cfg.override_window_ms})"
        )

        while not self.stop_requested:
            if not self.cfg.seq_mem_path.exists():
                time.sleep(self.cfg.poll_seconds)
                continue

            try:
                stat = self.cfg.seq_mem_path.stat()
            except FileNotFoundError:
                time.sleep(self.cfg.poll_seconds)
                continue

            inode = int(getattr(stat, "st_ino", 0))
            if self.inode and self.inode != inode:
                self.log("seq_mem rotated/recreated; resetting offset")
                self.offset = 0
            self.inode = inode

            if stat.st_size < self.offset:
                self.log("seq_mem truncated; resetting offset")
                self.offset = 0

            with self.cfg.seq_mem_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.offset)
                while not self.stop_requested:
                    line = fh.readline()
                    if line:
                        self.offset = fh.tell()
                        self.rows_seen += 1
                        try:
                            obj = json.loads(line)
                        except Exception:
                            self.rows_skipped += 1
                            continue
                        if not isinstance(obj, dict):
                            self.rows_skipped += 1
                            continue
                        self._handle_row(obj)
                        if self.rows_seen % 100 == 0:
                            self.save_state(force=False)
                        continue

                    self._expire_pending(int(time.time() * 1000))
                    self.save_state(force=False)
                    time.sleep(self.cfg.poll_seconds)

                    try:
                        stat2 = self.cfg.seq_mem_path.stat()
                    except FileNotFoundError:
                        break
                    inode2 = int(getattr(stat2, "st_ino", 0))
                    if inode2 != self.inode or stat2.st_size < self.offset:
                        break

        self._expire_pending(int(time.time() * 1000))
        self.save_state(force=True)
        self.log(f"stopping: seen={self.rows_seen} emitted={self.rows_emitted} skipped={self.rows_skipped}")
        return 0


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False

    proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True)
    if proc.returncode != 0:
        return False
    cmd = proc.stdout.strip()
    return "kar_signal_capture.py" in cmd and (" run " in cmd or cmd.endswith(" run"))


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
        "--poll-seconds",
        str(cfg.poll_seconds),
        "--outcome-window-ms",
        str(cfg.outcome_window_ms),
        "--override-window-ms",
        str(cfg.override_window_ms),
    ]
    if cfg.reset_state:
        run_cmd.append("--reset-state")

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _write_pid(cfg.pidfile, proc.pid)
    print(f"started kar signal capture: pid={proc.pid}")
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
    print(f"stopped kar signal capture: pid={pid}")
    return 0


def cmd_status(cfg: Config) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)
    print(f"pidfile: {cfg.pidfile}")
    print(f"log: {cfg.log_path}")
    print(f"state: {cfg.state_path}")
    print(f"seq_mem: {cfg.seq_mem_path}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    return 0 if alive else 1


def cmd_preflight(cfg: Config) -> int:
    ok = True
    print("kar signal capture preflight")
    print(f"seq_mem: {cfg.seq_mem_path}")
    print(f"state: {cfg.state_path}")
    print(f"log: {cfg.log_path}")

    if not cfg.seq_mem_path.exists():
        print("- FAIL: seq_mem path does not exist")
        ok = False
    else:
        print("- OK: seq_mem path exists")

    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    print("- OK: state/log dirs writable")
    return 0 if ok else 1


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        seq_mem_path=Path(args.seq_mem).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        outcome_window_ms=max(100, int(args.outcome_window_ms)),
        override_window_ms=max(50, int(args.override_window_ms)),
        reset_state=bool(args.reset_state),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seq-mem", default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM_PATH))
    parser.add_argument("--state-path", default=os.environ.get("SEQ_KAR_SIGNAL_STATE", DEFAULT_STATE_PATH))
    parser.add_argument("--pidfile", default=os.environ.get("SEQ_KAR_SIGNAL_PIDFILE", DEFAULT_PIDFILE))
    parser.add_argument("--log-path", default=os.environ.get("SEQ_KAR_SIGNAL_LOG", DEFAULT_LOG_PATH))
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("SEQ_KAR_SIGNAL_POLL_SECONDS", "0.5")),
    )
    parser.add_argument(
        "--outcome-window-ms",
        type=int,
        default=int(os.environ.get("SEQ_KAR_SIGNAL_OUTCOME_WINDOW_MS", "2200")),
    )
    parser.add_argument(
        "--override-window-ms",
        type=int,
        default=int(os.environ.get("SEQ_KAR_SIGNAL_OVERRIDE_WINDOW_MS", "1200")),
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore saved state and derive from start of seq_mem.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kar RL signal capture daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run in foreground.")
    add_common_args(p_run)

    p_once = sub.add_parser("once", help="Process current seq_mem rows from saved offset and exit.")
    add_common_args(p_once)

    p_start = sub.add_parser("start", help="Start background daemon.")
    add_common_args(p_start)

    p_stop = sub.add_parser("stop", help="Stop background daemon.")
    add_common_args(p_stop)

    p_status = sub.add_parser("status", help="Show daemon status.")
    add_common_args(p_status)

    p_preflight = sub.add_parser("preflight", help="Check prerequisites.")
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

    cap = KarSignalCapture(cfg)
    if args.command == "once":
        return cap.process_from_offset_once()
    if args.command == "run":
        return cap.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
