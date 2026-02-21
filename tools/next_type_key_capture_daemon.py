#!/usr/bin/env python3
"""Continuously capture macOS key events into seq mem via cgeventtap log tail.

Pipeline:
  cgeventtap-example (/tmp/cgeventtap.log) -> parser -> next_type_key_event_ingest.py -> seq_mem.jsonl

Design constraints:
- zero impact on typing path (listen-only event tap + async file tail)
- batched writes via ingest helper
- restart-safe offsets via state file
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TAP_LOG = "/tmp/cgeventtap.log"
DEFAULT_TAP_BIN = str(
    Path(
        "~/repos/pqrs-org/osx-event-observer-examples/cgeventtap-example/build_xcode/Release/"
        "cgeventtap-example.app/Contents/MacOS/cgeventtap-example"
    ).expanduser()
)
DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_STATE = str(Path("~/.local/state/seq/next_type_key_capture_state.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/next_type_key_capture.pid").expanduser())
DEFAULT_LOG = str(Path("~/code/seq/cli/cpp/out/logs/next_type_key_capture.log").expanduser())

KEY_DOWN_RE = re.compile(r"^\s*(\d+)\s+keyDown\s+(\d+)\s*$")
KEY_UP_RE = re.compile(r"^\s*(\d+)\s+keyUp\s+(\d+)\s*$")
FLAGS_RE = re.compile(r"^\s*(\d+)\s+flagsChanged\s+0x([0-9A-Fa-f]+)\s*$")


@dataclass
class Config:
    tap_log: Path
    tap_bin: Path
    out_path: Path
    state_path: Path
    pidfile: Path
    log_path: Path
    poll_seconds: float
    batch_size: int
    flush_ms: int
    source: str
    session_id: str | None
    project_path: str | None
    launch_tap: bool
    reset_state: bool


class CaptureDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.state_offset = 0
        self.state_inode = 0
        self.state_last_counter = 0
        self.lines_seen = 0
        self.lines_emitted = 0
        self.lines_skipped = 0
        self.last_state_save = 0.0

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] {message}", flush=True)

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def load_state(self) -> None:
        if self.cfg.reset_state:
            self.state_offset = 0
            self.state_inode = 0
            self.state_last_counter = 0
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
        last_counter = payload.get("last_counter")
        if isinstance(offset, int) and offset >= 0:
            self.state_offset = offset
        if isinstance(inode, int) and inode >= 0:
            self.state_inode = inode
        if isinstance(last_counter, int) and last_counter >= 0:
            self.state_last_counter = last_counter

    def save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_state_save) < 1.0:
            return
        payload = {
            "schema_version": "next_type_key_capture_state_v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "offset": int(self.state_offset),
            "inode": int(self.state_inode),
            "last_counter": int(self.state_last_counter),
            "lines_seen": int(self.lines_seen),
            "lines_emitted": int(self.lines_emitted),
            "lines_skipped": int(self.lines_skipped),
        }
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        self.last_state_save = now

    def parse_line(self, raw_line: str) -> dict[str, Any] | None:
        line = raw_line.strip()
        if not line:
            return None

        match = KEY_DOWN_RE.match(line)
        if match:
            counter = int(match.group(1))
            if counter <= self.state_last_counter:
                return None
            self.state_last_counter = counter
            return {
                "timestamp_ms": int(time.time() * 1000),
                "event_type": "key_down",
                "counter": counter,
                "key_code": int(match.group(2)),
            }

        match = KEY_UP_RE.match(line)
        if match:
            counter = int(match.group(1))
            if counter <= self.state_last_counter:
                return None
            self.state_last_counter = counter
            return {
                "timestamp_ms": int(time.time() * 1000),
                "event_type": "key_up",
                "counter": counter,
                "key_code": int(match.group(2)),
            }

        match = FLAGS_RE.match(line)
        if match:
            counter = int(match.group(1))
            if counter <= self.state_last_counter:
                return None
            self.state_last_counter = counter
            return {
                "timestamp_ms": int(time.time() * 1000),
                "event_type": "flags_changed",
                "counter": counter,
                "flags_hex": f"0x{match.group(2).lower()}",
            }

        return None

    def ensure_tap_running(self) -> None:
        if not self.cfg.launch_tap:
            return
        if not self.cfg.tap_bin.exists():
            self.log(f"tap binary missing: {self.cfg.tap_bin}")
            return

        proc = subprocess.run(
            ["pgrep", "-f", "cgeventtap-example"],
            text=True,
            capture_output=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return

        try:
            subprocess.Popen(
                [str(self.cfg.tap_bin)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.log("launched cgeventtap-example")
        except Exception as exc:
            self.log(f"failed to launch cgeventtap-example: {exc}")

    def start_ingest_subprocess(self) -> subprocess.Popen[str]:
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "next_type_key_event_ingest.py"),
            "--out",
            str(self.cfg.out_path),
            "--batch-size",
            str(self.cfg.batch_size),
            "--flush-ms",
            str(self.cfg.flush_ms),
            "--source",
            self.cfg.source,
        ]
        if self.cfg.session_id:
            cmd.extend(["--session-id", self.cfg.session_id])
        if self.cfg.project_path:
            cmd.extend(["--project-path", self.cfg.project_path])

        return subprocess.Popen(
            cmd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=1,
        )

    def send_event(self, proc: subprocess.Popen[str], event: dict[str, Any]) -> bool:
        if proc.poll() is not None:
            return False
        if proc.stdin is None:
            return False
        try:
            proc.stdin.write(json.dumps(event, ensure_ascii=True) + "\n")
            proc.stdin.flush()
            return True
        except BrokenPipeError:
            return False
        except Exception:
            return False

    def process_existing_once(self) -> int:
        self.load_state()
        if not self.cfg.tap_log.exists():
            self.log(f"tap log not found: {self.cfg.tap_log}")
            return 1

        stat = self.cfg.tap_log.stat()
        inode = int(getattr(stat, "st_ino", 0))
        if self.state_inode and self.state_inode != inode:
            self.state_offset = 0
            self.state_last_counter = 0
        self.state_inode = inode

        proc = self.start_ingest_subprocess()
        try:
            with self.cfg.tap_log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.state_offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    self.lines_seen += 1
                    self.state_offset = fh.tell()
                    event = self.parse_line(line)
                    if event is None:
                        self.lines_skipped += 1
                        continue
                    if self.send_event(proc, event):
                        self.lines_emitted += 1
                    else:
                        self.lines_skipped += 1
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
            except Exception:
                proc.kill()

        self.save_state(force=True)
        self.log(f"once complete: seen={self.lines_seen} emitted={self.lines_emitted} skipped={self.lines_skipped}")
        return 0

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self.load_state()
        self.ensure_tap_running()
        proc = self.start_ingest_subprocess()
        self.log(
            f"capture loop started (tap_log={self.cfg.tap_log}, out={self.cfg.out_path}, "
            f"batch_size={self.cfg.batch_size}, flush_ms={self.cfg.flush_ms})"
        )

        while not self.stop_requested:
            if proc.poll() is not None:
                self.log("ingest subprocess exited; restarting")
                proc = self.start_ingest_subprocess()

            if not self.cfg.tap_log.exists():
                time.sleep(self.cfg.poll_seconds)
                continue

            try:
                stat = self.cfg.tap_log.stat()
            except FileNotFoundError:
                time.sleep(self.cfg.poll_seconds)
                continue

            inode = int(getattr(stat, "st_ino", 0))
            if self.state_inode and self.state_inode != inode:
                self.log("tap log rotated/recreated; resetting offset")
                self.state_offset = 0
                self.state_last_counter = 0
            self.state_inode = inode

            if stat.st_size < self.state_offset:
                self.log("tap log truncated; resetting offset")
                self.state_offset = 0
                self.state_last_counter = 0

            with self.cfg.tap_log.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.state_offset)
                while not self.stop_requested:
                    line = fh.readline()
                    if line:
                        self.lines_seen += 1
                        self.state_offset = fh.tell()
                        event = self.parse_line(line)
                        if event is None:
                            self.lines_skipped += 1
                        elif self.send_event(proc, event):
                            self.lines_emitted += 1
                        else:
                            self.lines_skipped += 1
                        if self.lines_seen % 100 == 0:
                            self.save_state(force=False)
                        continue

                    time.sleep(self.cfg.poll_seconds)
                    self.save_state(force=False)

                    try:
                        stat2 = self.cfg.tap_log.stat()
                    except FileNotFoundError:
                        break
                    inode2 = int(getattr(stat2, "st_ino", 0))
                    if inode2 != self.state_inode or stat2.st_size < self.state_offset:
                        break

        self.save_state(force=True)
        self.log(
            f"capture loop stopping (seen={self.lines_seen}, emitted={self.lines_emitted}, "
            f"skipped={self.lines_skipped})"
        )
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except Exception:
                proc.kill()
        except Exception:
            proc.kill()
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
    return "next_type_key_capture_daemon.py" in cmd and (" run " in cmd or cmd.endswith(" run"))


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


def cmd_preflight(cfg: Config, _args: argparse.Namespace) -> int:
    ok = True
    print("next-type capture preflight")
    print(f"tap_bin: {cfg.tap_bin}")
    print(f"tap_log: {cfg.tap_log}")
    print(f"out_path: {cfg.out_path}")
    print(f"state_path: {cfg.state_path}")
    print(f"log_path: {cfg.log_path}")

    if not cfg.tap_bin.exists() or not os.access(cfg.tap_bin, os.X_OK):
        ok = False
        print("- FAIL: cgeventtap binary missing or not executable")
        print(
            "  Build it in ~/repos/pqrs-org/osx-event-observer-examples/cgeventtap-example "
            "(Xcode Release build)."
        )
    else:
        print("- OK: cgeventtap binary present")

    ingest_script = Path(__file__).resolve().parent / "next_type_key_event_ingest.py"
    if not ingest_script.exists():
        ok = False
        print(f"- FAIL: ingest script missing: {ingest_script}")
    else:
        print("- OK: ingest script present")

    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.out_path.parent.mkdir(parents=True, exist_ok=True)
    print("- OK: output/state/log directories are writable")

    print("- NOTE: grant Accessibility/Input Monitoring if macOS prompts for cgeventtap-example")
    return 0 if ok else 1


def cmd_start(cfg: Config, _args: argparse.Namespace) -> int:
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
        "--tap-log",
        str(cfg.tap_log),
        "--tap-bin",
        str(cfg.tap_bin),
        "--out",
        str(cfg.out_path),
        "--state-path",
        str(cfg.state_path),
        "--pidfile",
        str(cfg.pidfile),
        "--log-path",
        str(cfg.log_path),
        "--poll-seconds",
        str(cfg.poll_seconds),
        "--batch-size",
        str(cfg.batch_size),
        "--flush-ms",
        str(cfg.flush_ms),
        "--source",
        cfg.source,
    ]
    if cfg.session_id:
        run_cmd.extend(["--session-id", cfg.session_id])
    if cfg.project_path:
        run_cmd.extend(["--project-path", cfg.project_path])
    if cfg.launch_tap:
        run_cmd.append("--launch-tap")
    else:
        run_cmd.append("--no-launch-tap")

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    _write_pid(cfg.pidfile, proc.pid)
    print(f"started next-type key capture: pid={proc.pid}")
    print(f"log: {cfg.log_path}")
    return 0


def cmd_stop(cfg: Config, _args: argparse.Namespace) -> int:
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
    print(f"stopped next-type key capture: pid={pid}")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)
    print(f"pidfile: {cfg.pidfile}")
    print(f"log: {cfg.log_path}")
    print(f"state: {cfg.state_path}")
    print(f"tap_log: {cfg.tap_log}")
    print(f"out_path: {cfg.out_path}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    return 0 if alive else 1


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tap-log", default=os.environ.get("SEQ_NEXT_TYPE_TAP_LOG", DEFAULT_TAP_LOG))
    parser.add_argument("--tap-bin", default=os.environ.get("SEQ_NEXT_TYPE_TAP_BIN", DEFAULT_TAP_BIN))
    parser.add_argument(
        "--out",
        default=os.environ.get("SEQ_NEXT_TYPE_OUT", os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM)),
    )
    parser.add_argument("--state-path", default=os.environ.get("SEQ_NEXT_TYPE_STATE", DEFAULT_STATE))
    parser.add_argument("--pidfile", default=os.environ.get("SEQ_NEXT_TYPE_PIDFILE", DEFAULT_PIDFILE))
    parser.add_argument("--log-path", default=os.environ.get("SEQ_NEXT_TYPE_LOG", DEFAULT_LOG))
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("SEQ_NEXT_TYPE_POLL_SECONDS", "0.25")),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_BATCH_SIZE", "64")),
    )
    parser.add_argument(
        "--flush-ms",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_FLUSH_MS", "1500")),
    )
    parser.add_argument("--source", default=os.environ.get("SEQ_NEXT_TYPE_SOURCE", "cgeventtap"))
    parser.add_argument("--session-id", default=os.environ.get("SEQ_NEXT_TYPE_SESSION_ID"))
    parser.add_argument("--project-path", default=os.environ.get("SEQ_NEXT_TYPE_PROJECT_PATH"))
    parser.add_argument(
        "--launch-tap",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SEQ_NEXT_TYPE_LAUNCH_TAP", True),
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore saved offset and parse from start of tap log.",
    )


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        tap_log=Path(args.tap_log).expanduser().resolve(),
        tap_bin=Path(args.tap_bin).expanduser().resolve(),
        out_path=Path(args.out).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        batch_size=max(1, int(args.batch_size)),
        flush_ms=max(100, int(args.flush_ms)),
        source=str(args.source),
        session_id=str(args.session_id) if args.session_id else None,
        project_path=str(args.project_path) if args.project_path else None,
        launch_tap=bool(args.launch_tap),
        reset_state=bool(args.reset_state),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuous key-event capture daemon for seq")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run in foreground.")
    add_common_args(p_run)

    p_once = sub.add_parser("once", help="Process current tap log from saved offset and exit.")
    add_common_args(p_once)

    p_preflight = sub.add_parser("preflight", help="Check capture prerequisites.")
    add_common_args(p_preflight)

    p_start = sub.add_parser("start", help="Start background daemon.")
    add_common_args(p_start)

    p_stop = sub.add_parser("stop", help="Stop background daemon.")
    add_common_args(p_stop)

    p_status = sub.add_parser("status", help="Show daemon status.")
    add_common_args(p_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cfg = build_config(args)

    if args.command == "preflight":
        return cmd_preflight(cfg, args)
    if args.command == "start":
        return cmd_start(cfg, args)
    if args.command == "stop":
        return cmd_stop(cfg, args)
    if args.command == "status":
        return cmd_status(cfg, args)

    daemon = CaptureDaemon(cfg)
    if args.command == "once":
        return daemon.process_existing_once()
    if args.command == "run":
        return daemon.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
