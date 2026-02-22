#!/usr/bin/env python3
"""Low-latency local bridge for seq BMQ_ENQ jobs.

Hot path:
- seqd receives `BMQ_ENQ <payload>`.
- seqd tries AF_UNIX datagram send to this bridge socket and returns immediately.
- if bridge is down, seqd appends to spool file (ts_ms\tpayload).

Bridge path:
- consume datagrams + seqd spool fallback.
- persist to local SQLite queue.
- asynchronously dispatch with configured backend.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOCKET = str(Path("~/Library/Application Support/seq/bmq_bridge.sock").expanduser())
DEFAULT_SPOOL = str(Path("~/Library/Application Support/seq/bmq_bridge_spool.tsv").expanduser())
DEFAULT_DB = str(Path("~/.local/state/seq/bmq_bridge.sqlite3").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/bmq_bridge.pid").expanduser())
DEFAULT_LOG = str(Path("~/code/seq/cli/cpp/out/logs/seq_bmq_bridge.log").expanduser())
DEFAULT_OUT = str(Path("~/.local/state/seq/bmq_bridge_out.jsonl").expanduser())


@dataclass
class Config:
    socket_path: Path
    spool_path: Path
    db_path: Path
    pidfile: Path
    log_path: Path
    out_path: Path
    backend: str
    dispatch_cmd: str
    dispatch_timeout_s: float
    poll_seconds: float
    spool_poll_seconds: float
    dispatch_batch: int


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.rx_count = 0
        self.dispatch_ok = 0
        self.dispatch_err = 0

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] {message}", flush=True)

    def request_stop(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True

    def _db(self) -> sqlite3.Connection:
        self.cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.cfg.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                payload TEXT NOT NULL,
                enqueued_at_ms INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_id ON queue(id)")
        conn.commit()
        return conn

    def enqueue(self, conn: sqlite3.Connection, payload: str, ts_ms: int | None = None) -> None:
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO queue(ts_ms, payload, enqueued_at_ms, attempts, last_error) VALUES(?, ?, ?, 0, NULL)",
            (int(ts_ms or now_ms), payload, now_ms),
        )
        conn.commit()
        self.rx_count += 1

    def import_spool(self, conn: sqlite3.Connection) -> int:
        src = self.cfg.spool_path
        if not src.exists():
            return 0
        draining = src.with_suffix(src.suffix + ".drain")
        try:
            os.replace(src, draining)
        except FileNotFoundError:
            return 0
        imported = 0
        try:
            with draining.open("r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.rstrip("\n")
                    if not line:
                        continue
                    ts_ms = int(time.time() * 1000)
                    payload = line
                    if "\t" in line:
                        left, right = line.split("\t", 1)
                        if left.isdigit():
                            ts_ms = int(left)
                            payload = right
                    if not payload:
                        continue
                    self.enqueue(conn, payload, ts_ms=ts_ms)
                    imported += 1
        finally:
            try:
                draining.unlink()
            except FileNotFoundError:
                pass
        if imported:
            self.log(f"imported {imported} spool rows")
        return imported

    def _dispatch_jsonl(self, payload: str) -> tuple[bool, str]:
        self.cfg.out_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts_ms": int(time.time() * 1000),
            "payload": payload,
        }
        with self.cfg.out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
        return True, ""

    def _dispatch_command(self, payload: str) -> tuple[bool, str]:
        if not self.cfg.dispatch_cmd.strip():
            return False, "dispatch_cmd_not_set"
        try:
            proc = subprocess.run(
                ["/bin/sh", "-lc", self.cfg.dispatch_cmd],
                input=payload,
                text=True,
                capture_output=True,
                timeout=max(0.05, self.cfg.dispatch_timeout_s),
                check=False,
            )
        except Exception as exc:
            return False, f"dispatch_exec_failed:{exc}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "dispatch_failed").strip().replace("\n", " | ")
            return False, f"dispatch_rc_{proc.returncode}:{err[:500]}"
        return True, ""

    def dispatch_one(self, payload: str) -> tuple[bool, str]:
        if self.cfg.backend == "jsonl":
            return self._dispatch_jsonl(payload)
        if self.cfg.backend == "command":
            return self._dispatch_command(payload)
        return False, f"unsupported_backend:{self.cfg.backend}"

    def drain_once(self, conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            "SELECT id, payload, attempts FROM queue ORDER BY id ASC LIMIT ?",
            (max(1, self.cfg.dispatch_batch),),
        ).fetchall()
        if not rows:
            return 0
        drained = 0
        for row_id, payload, attempts in rows:
            ok, err = self.dispatch_one(str(payload))
            if ok:
                conn.execute("DELETE FROM queue WHERE id = ?", (int(row_id),))
                conn.commit()
                self.dispatch_ok += 1
                drained += 1
                continue
            conn.execute(
                "UPDATE queue SET attempts = ?, last_error = ? WHERE id = ?",
                (int(attempts) + 1, err, int(row_id)),
            )
            conn.commit()
            self.dispatch_err += 1
            self.log(f"dispatch error row={row_id} err={err}")
            break
        return drained

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        conn = self._db()
        self.import_spool(conn)

        self.cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.cfg.socket_path.unlink()
        except FileNotFoundError:
            pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(str(self.cfg.socket_path))
        sock.settimeout(max(0.01, self.cfg.poll_seconds))

        self.log(f"bridge listening: {self.cfg.socket_path}")
        self.log(f"backend={self.cfg.backend} db={self.cfg.db_path} spool={self.cfg.spool_path}")

        last_spool_poll = 0.0
        last_stats = 0.0
        try:
            while not self.stop_requested:
                now = time.monotonic()
                if now - last_spool_poll >= max(0.05, self.cfg.spool_poll_seconds):
                    self.import_spool(conn)
                    last_spool_poll = now

                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    data = b""
                except Exception as exc:
                    self.log(f"socket_recv_error: {exc}")
                    data = b""

                if data:
                    payload = data.decode("utf-8", errors="replace").strip()
                    if payload:
                        self.enqueue(conn, payload)

                self.drain_once(conn)

                if now - last_stats >= 10.0:
                    queued = int(conn.execute("SELECT COUNT(1) FROM queue").fetchone()[0])
                    self.log(
                        f"stats queued={queued} rx={self.rx_count} ok={self.dispatch_ok} err={self.dispatch_err}"
                    )
                    last_stats = now
        finally:
            try:
                sock.close()
            except Exception:
                pass
            try:
                self.cfg.socket_path.unlink()
            except FileNotFoundError:
                pass
            conn.close()
        self.log("bridge stopped")
        return 0


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
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
        "--socket",
        str(cfg.socket_path),
        "--spool",
        str(cfg.spool_path),
        "--db",
        str(cfg.db_path),
        "--pidfile",
        str(cfg.pidfile),
        "--log-path",
        str(cfg.log_path),
        "--out",
        str(cfg.out_path),
        "--backend",
        cfg.backend,
        "--dispatch-timeout-s",
        str(cfg.dispatch_timeout_s),
        "--poll-seconds",
        str(cfg.poll_seconds),
        "--spool-poll-seconds",
        str(cfg.spool_poll_seconds),
        "--dispatch-batch",
        str(cfg.dispatch_batch),
    ]
    if cfg.dispatch_cmd:
        run_cmd.extend(["--dispatch-cmd", cfg.dispatch_cmd])

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _write_pid(cfg.pidfile, proc.pid)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if cfg.socket_path.exists():
            break
        if not _is_pid_alive(proc.pid):
            break
        time.sleep(0.05)
    print(f"started seq-bmq-bridge: pid={proc.pid}")
    print(f"socket: {cfg.socket_path}")
    print(f"log: {cfg.log_path}")
    if not cfg.socket_path.exists():
        print("warn: socket not ready yet (check log if first start is slow)")
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
    print(f"stopped seq-bmq-bridge: pid={pid}")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)
    queued = -1
    last_error = ""
    if cfg.db_path.exists():
        conn = sqlite3.connect(str(cfg.db_path))
        try:
            queued = int(conn.execute("SELECT COUNT(1) FROM queue").fetchone()[0])
            row = conn.execute(
                "SELECT last_error FROM queue WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                last_error = str(row[0])
        finally:
            conn.close()

    print(f"pidfile: {cfg.pidfile}")
    print(f"socket: {cfg.socket_path}")
    print(f"spool: {cfg.spool_path}")
    print(f"db: {cfg.db_path}")
    print(f"backend: {cfg.backend}")
    if cfg.backend == "command":
        print(f"dispatch_cmd: {cfg.dispatch_cmd or '(unset)'}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    if queued >= 0:
        print(f"queued: {queued}")
    if last_error:
        print(f"last_error: {last_error}")
    return 0 if alive else 1


def cmd_preflight(cfg: Config, _args: argparse.Namespace) -> int:
    cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.spool_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.pidfile.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(cfg.db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, payload TEXT NOT NULL, enqueued_at_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
    conn.close()

    print(f"ok: socket parent {cfg.socket_path.parent}")
    print(f"ok: db {cfg.db_path}")
    print(f"ok: backend={cfg.backend}")
    if cfg.backend == "command" and not cfg.dispatch_cmd.strip():
        print("warn: backend=command but dispatch cmd is empty")
    return 0


def cmd_once(cfg: Config, _args: argparse.Namespace) -> int:
    bridge = Bridge(cfg)
    conn = bridge._db()
    imported = bridge.import_spool(conn)
    drained = bridge.drain_once(conn)
    queued = int(conn.execute("SELECT COUNT(1) FROM queue").fetchone()[0])
    conn.close()
    print(f"imported={imported} drained={drained} queued={queued}")
    return 0


def cmd_enqueue(cfg: Config, args: argparse.Namespace) -> int:
    payload = args.payload.strip()
    if not payload:
        print("error: enqueue requires payload", file=sys.stderr)
        return 1
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload.encode("utf-8"), str(cfg.socket_path))
    except Exception as exc:
        print(f"error: sendto failed: {exc}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    print("OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="seq BMQ bridge daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--socket", default=os.environ.get("SEQ_BMQ_BRIDGE_SOCKET", DEFAULT_SOCKET))
        p.add_argument("--spool", default=os.environ.get("SEQ_BMQ_BRIDGE_SPOOL", DEFAULT_SPOOL))
        p.add_argument("--db", default=os.environ.get("SEQ_BMQ_BRIDGE_DB", DEFAULT_DB))
        p.add_argument("--pidfile", default=os.environ.get("SEQ_BMQ_BRIDGE_PIDFILE", DEFAULT_PIDFILE))
        p.add_argument("--log-path", default=os.environ.get("SEQ_BMQ_BRIDGE_LOG", DEFAULT_LOG))
        p.add_argument("--out", default=os.environ.get("SEQ_BMQ_BRIDGE_OUT", DEFAULT_OUT))
        p.add_argument(
            "--backend",
            choices=["jsonl", "command"],
            default=os.environ.get("SEQ_BMQ_BACKEND", "jsonl"),
        )
        p.add_argument("--dispatch-cmd", default=os.environ.get("SEQ_BMQ_DISPATCH_CMD", ""))
        p.add_argument(
            "--dispatch-timeout-s",
            type=float,
            default=float(os.environ.get("SEQ_BMQ_DISPATCH_TIMEOUT_S", "3.0")),
        )
        p.add_argument(
            "--poll-seconds",
            type=float,
            default=float(os.environ.get("SEQ_BMQ_POLL_SECONDS", "0.02")),
        )
        p.add_argument(
            "--spool-poll-seconds",
            type=float,
            default=float(os.environ.get("SEQ_BMQ_SPOOL_POLL_SECONDS", "0.25")),
        )
        p.add_argument(
            "--dispatch-batch",
            type=int,
            default=int(os.environ.get("SEQ_BMQ_DISPATCH_BATCH", "64")),
        )

    p_preflight = sub.add_parser("preflight", help="Check bridge paths and DB")
    add_common(p_preflight)

    p_run = sub.add_parser("run", help="Run bridge in foreground")
    add_common(p_run)

    p_start = sub.add_parser("start", help="Start bridge in background")
    add_common(p_start)

    p_stop = sub.add_parser("stop", help="Stop bridge")
    add_common(p_stop)

    p_status = sub.add_parser("status", help="Show bridge status")
    add_common(p_status)

    p_once = sub.add_parser("once", help="Import spool + dispatch one batch")
    add_common(p_once)

    p_enqueue = sub.add_parser("enqueue", help="Send a test datagram directly to bridge")
    add_common(p_enqueue)
    p_enqueue.add_argument("payload")

    return parser


def make_config(args: argparse.Namespace) -> Config:
    return Config(
        socket_path=Path(args.socket).expanduser().resolve(),
        spool_path=Path(args.spool).expanduser().resolve(),
        db_path=Path(args.db).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        out_path=Path(args.out).expanduser().resolve(),
        backend=str(args.backend),
        dispatch_cmd=str(args.dispatch_cmd),
        dispatch_timeout_s=max(0.05, float(args.dispatch_timeout_s)),
        poll_seconds=max(0.01, float(args.poll_seconds)),
        spool_poll_seconds=max(0.05, float(args.spool_poll_seconds)),
        dispatch_batch=max(1, int(args.dispatch_batch)),
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cfg = make_config(args)

    if args.command == "preflight":
        return cmd_preflight(cfg, args)
    if args.command == "start":
        return cmd_start(cfg, args)
    if args.command == "stop":
        return cmd_stop(cfg, args)
    if args.command == "status":
        return cmd_status(cfg, args)
    if args.command == "once":
        return cmd_once(cfg, args)
    if args.command == "enqueue":
        return cmd_enqueue(cfg, args)
    if args.command == "run":
        bridge = Bridge(cfg)
        return bridge.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
