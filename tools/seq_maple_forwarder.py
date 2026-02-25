#!/usr/bin/env python3
"""Forward seq mem/trace JSONL rows to Maple OTLP traces endpoint.

This daemon tails:
- SEQ_CH_MEM_PATH   (seq mem events)
- SEQ_CH_LOG_PATH   (seq trace events)

Rows are converted to OTLP spans and sent to Maple ingest endpoint using
`x-maple-ingest-key`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_SEQ_TRACE = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl").expanduser())
DEFAULT_ENDPOINT = "https://ingest.maple.dev/v1/traces"
DEFAULT_SERVICE_NAME = "seq"
DEFAULT_ENV = "local"
DEFAULT_SCOPE_NAME = "seq_forwarder"
DEFAULT_STATE_PATH = str(Path("~/.local/state/seq/maple_forwarder_state.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/maple_forwarder.pid").expanduser())
DEFAULT_LOG_PATH = str(Path("~/code/seq/cli/cpp/out/logs/maple_forwarder.log").expanduser())
FLOW_PERSONAL_ENV = Path.home() / ".config" / "flow" / "env-local" / "personal" / "production.env"
_FLOW_ENV_CACHE: dict[str, str] | None = None


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    flow = _read_flow_personal_env()
    return flow.get(name, default)


def _read_flow_personal_env() -> dict[str, str]:
    global _FLOW_ENV_CACHE
    if _FLOW_ENV_CACHE is not None:
        return _FLOW_ENV_CACHE
    out: dict[str, str] = {}
    if not FLOW_PERSONAL_ENV.exists():
        _FLOW_ENV_CACHE = out
        return out
    try:
        for line in FLOW_PERSONAL_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
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
    except Exception:
        pass
    _FLOW_ENV_CACHE = out
    return out


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_hex(seed: str, chars: int) -> str:
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()
    return digest[:chars]


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except Exception:
            return default
    return default


@dataclass
class Config:
    seq_mem_path: Path
    seq_trace_path: Path
    endpoint: str
    ingest_key: str
    service_name: str
    service_version: str
    deployment_environment: str
    scope_name: str
    state_path: Path
    pidfile: Path
    log_path: Path
    poll_seconds: float
    batch_size: int
    max_batches: int
    timeout_s: float
    verify_tls: bool
    ca_bundle: str
    max_line_bytes: int
    reset_state: bool


@dataclass
class Cursor:
    path: Path
    inode: int = 0
    offset: int = 0


@dataclass
class SourceBatch:
    source: str
    inode: int
    next_offset: int
    lines_read: int
    spans: list[dict[str, Any]]


class MapleForwarder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.cursors: dict[str, Cursor] = {
            "mem": Cursor(path=cfg.seq_mem_path),
            "trace": Cursor(path=cfg.seq_trace_path),
        }
        self.rows_sent = 0
        self.rows_failed = 0
        self.rows_skipped = 0
        self._prefer_trace_first = False

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def log(self, msg: str) -> None:
        print(f"[{_iso_now()}] {msg}", flush=True)

    def load_state(self) -> None:
        if self.cfg.reset_state or not self.cfg.state_path.exists():
            return
        try:
            payload = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            return
        for name, cursor in self.cursors.items():
            node = sources.get(name)
            if not isinstance(node, dict):
                continue
            cursor.inode = max(0, _as_int(node.get("inode"), 0))
            cursor.offset = max(0, _as_int(node.get("offset"), 0))

    def save_state(self) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "seq_maple_forwarder_state_v1",
            "updated_at": _iso_now(),
            "sources": {
                name: {
                    "path": str(cursor.path),
                    "inode": int(cursor.inode),
                    "offset": int(cursor.offset),
                }
                for name, cursor in self.cursors.items()
            },
            "stats": {
                "rows_sent": int(self.rows_sent),
                "rows_failed": int(self.rows_failed),
                "rows_skipped": int(self.rows_skipped),
            },
        }
        self.cfg.state_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _resource_attrs(self) -> list[dict[str, Any]]:
        attrs = [
            {
                "key": "service.name",
                "value": {"stringValue": self.cfg.service_name},
            },
            {
                "key": "deployment.environment",
                "value": {"stringValue": self.cfg.deployment_environment},
            },
        ]
        if self.cfg.service_version.strip():
            attrs.append(
                {
                    "key": "service.version",
                    "value": {"stringValue": self.cfg.service_version.strip()},
                }
            )
        return attrs

    def _attr(self, key: str, value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        return {"key": key, "value": {"stringValue": rendered}}

    def _make_span(
        self,
        *,
        source: str,
        span_name: str,
        seed: str,
        start_ns: int,
        end_ns: int,
        ok: bool,
        attributes: list[dict[str, Any]],
        status_message: str = "",
    ) -> dict[str, Any]:
        trace_id = _stable_hex(f"trace:{seed}", 32)
        span_id = _stable_hex(f"span:{seed}", 16)
        return {
            "traceId": trace_id,
            "spanId": span_id,
            "parentSpanId": "",
            "name": span_name,
            "kind": 1,
            "startTimeUnixNano": str(max(1, int(start_ns))),
            "endTimeUnixNano": str(max(2, int(end_ns))),
            "attributes": [self._attr("source", source), *attributes],
            "status": {
                "code": 1 if ok else 2,
                "message": status_message,
            },
        }

    def _span_from_mem_row(self, row: dict[str, Any], line_offset: int) -> dict[str, Any]:
        ts_ms = _as_int(row.get("ts_ms"), int(time.time() * 1000))
        dur_us = max(1, _as_int(row.get("dur_us"), 0))
        start_ns = ts_ms * 1_000_000
        end_ns = start_ns + dur_us * 1_000

        session_id = str(row.get("session_id") or "")
        event_id = str(row.get("event_id") or "")
        name = str(row.get("name") or "event")
        ok = bool(row.get("ok", True))

        subject = row.get("subject")
        if isinstance(subject, (dict, list)):
            subject_raw = json.dumps(subject, ensure_ascii=True)
        else:
            subject_raw = str(subject or "")
        subject_len = len(subject_raw)

        attrs = [
            self._attr("seq.source", "mem"),
            self._attr("seq.mem.name", name),
            self._attr("seq.mem.session_id", session_id),
            self._attr("seq.mem.event_id", event_id),
            self._attr("seq.mem.content_hash", str(row.get("content_hash") or "")),
            self._attr("seq.mem.dur_us", dur_us),
            self._attr("seq.mem.ok", ok),
            self._attr("seq.mem.subject_len", subject_len),
        ]

        span_name = f"seq.mem.{name}"
        seed = "|".join(
            [
                "mem",
                session_id,
                event_id,
                name,
                str(ts_ms),
                str(line_offset),
            ]
        )
        status_message = "" if ok else "seq mem row marked not ok"
        return self._make_span(
            source="seq_mem",
            span_name=span_name,
            seed=seed,
            start_ns=start_ns,
            end_ns=end_ns,
            ok=ok,
            attributes=attrs,
            status_message=status_message,
        )

    def _span_from_trace_row(self, row: dict[str, Any], line_offset: int) -> dict[str, Any]:
        ts_us = _as_int(row.get("ts_us"), int(time.time() * 1_000_000))
        dur_us = max(1, _as_int(row.get("dur_us"), 0))
        start_ns = ts_us * 1_000
        end_ns = start_ns + dur_us * 1_000

        level = str(row.get("level") or "info")
        kind = str(row.get("kind") or "event")
        name = str(row.get("name") or "event")
        msg = str(row.get("message") or "")
        msg_len = len(msg)

        ok = level.lower() not in {"error", "fatal"}
        attrs = [
            self._attr("seq.source", "trace"),
            self._attr("seq.trace.level", level),
            self._attr("seq.trace.kind", kind),
            self._attr("seq.trace.name", name),
            self._attr("seq.trace.app", str(row.get("app") or "")),
            self._attr("seq.trace.pid", _as_int(row.get("pid"), 0)),
            self._attr("seq.trace.tid", _as_int(row.get("tid"), 0)),
            self._attr("seq.trace.dur_us", dur_us),
            self._attr("seq.trace.message_len", msg_len),
        ]

        safe_kind = kind if kind else "event"
        safe_name = name if name else "event"
        span_name = f"seq.trace.{safe_kind}.{safe_name}"
        seed = "|".join(["trace", safe_kind, safe_name, str(ts_us), str(line_offset)])
        status_message = "" if ok else f"level={level}"
        return self._make_span(
            source="seq_trace",
            span_name=span_name,
            seed=seed,
            start_ns=start_ns,
            end_ns=end_ns,
            ok=ok,
            attributes=attrs,
            status_message=status_message,
        )

    def _row_to_span(self, source: str, row: dict[str, Any], line_offset: int) -> dict[str, Any]:
        if source == "mem":
            return self._span_from_mem_row(row, line_offset)
        return self._span_from_trace_row(row, line_offset)

    def _read_source_batch(self, source: str, cursor: Cursor, limit: int) -> SourceBatch:
        if limit <= 0:
            return SourceBatch(source=source, inode=cursor.inode, next_offset=cursor.offset, lines_read=0, spans=[])

        path = cursor.path
        try:
            st = path.stat()
        except FileNotFoundError:
            return SourceBatch(source=source, inode=cursor.inode, next_offset=cursor.offset, lines_read=0, spans=[])
        except OSError:
            return SourceBatch(source=source, inode=cursor.inode, next_offset=cursor.offset, lines_read=0, spans=[])

        inode = int(st.st_ino)
        size = int(st.st_size)
        offset = int(cursor.offset)
        if cursor.inode and cursor.inode != inode:
            offset = 0
        if size < offset:
            offset = 0

        spans: list[dict[str, Any]] = []
        lines_read = 0
        next_offset = offset

        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                while lines_read < limit:
                    line_offset = fh.tell()
                    raw = fh.readline()
                    if not raw:
                        break
                    next_offset = fh.tell()
                    lines_read += 1

                    if len(raw) > self.cfg.max_line_bytes:
                        self.rows_skipped += 1
                        continue

                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        self.rows_skipped += 1
                        continue
                    if not isinstance(obj, dict):
                        self.rows_skipped += 1
                        continue
                    spans.append(self._row_to_span(source, obj, line_offset))
        except OSError:
            return SourceBatch(source=source, inode=cursor.inode, next_offset=cursor.offset, lines_read=0, spans=[])

        return SourceBatch(
            source=source,
            inode=inode,
            next_offset=next_offset,
            lines_read=lines_read,
            spans=spans,
        )

    def _collect_batch(self) -> tuple[dict[str, SourceBatch], list[dict[str, Any]]]:
        order = ("trace", "mem") if self._prefer_trace_first else ("mem", "trace")
        self._prefer_trace_first = not self._prefer_trace_first

        remaining = max(1, int(self.cfg.batch_size))
        batches: dict[str, SourceBatch] = {}
        merged: list[dict[str, Any]] = []

        for source in order:
            cursor = self.cursors[source]
            batch = self._read_source_batch(source, cursor, remaining)
            batches[source] = batch
            if batch.spans:
                merged.extend(batch.spans)
                remaining = max(0, remaining - len(batch.spans))
                if remaining == 0:
                    break

        # Ensure both entries exist so commit logic can always run safely.
        for source in ("mem", "trace"):
            if source not in batches:
                cursor = self.cursors[source]
                batches[source] = SourceBatch(
                    source=source,
                    inode=cursor.inode,
                    next_offset=cursor.offset,
                    lines_read=0,
                    spans=[],
                )

        return batches, merged

    def _send_batch(self, spans: list[dict[str, Any]]) -> bool:
        if not spans:
            return True
        if not self.cfg.endpoint.strip():
            self.log("maple forwarder skipped: endpoint is empty")
            return False
        if not self.cfg.ingest_key.strip():
            self.log("maple forwarder skipped: ingest key is empty")
            return False

        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": self._resource_attrs()},
                    "scopeSpans": [
                        {
                            "scope": {"name": self.cfg.scope_name},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.endpoint,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-maple-ingest-key": self.cfg.ingest_key,
                "user-agent": "seq-maple-forwarder/1.0",
            },
        )
        ssl_ctx = self._build_ssl_context()
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s, context=ssl_ctx) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                if 200 <= status < 300:
                    return True
                self.log(f"maple forwarder send failed: status={status}")
                return False
        except urllib.error.HTTPError as exc:
            self.log(f"maple forwarder send failed: http={exc.code}")
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.log(f"maple forwarder send failed: {exc}")
            return False

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not self.cfg.verify_tls:
            return ssl._create_unverified_context()
        if self.cfg.ca_bundle.strip():
            try:
                return ssl.create_default_context(cafile=self.cfg.ca_bundle.strip())
            except Exception as exc:
                self.log(f"WARN invalid SEQ_MAPLE_FORWARDER_CA_BUNDLE ({exc}), falling back")
        # Python.framework installs can miss macOS roots; certifi bundle is more reliable.
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return None

    def _commit_batch(self, batches: dict[str, SourceBatch]) -> None:
        for source, batch in batches.items():
            cursor = self.cursors[source]
            cursor.inode = int(batch.inode)
            cursor.offset = int(batch.next_offset)
        self.save_state()

    def run_once(self) -> int:
        batches, spans = self._collect_batch()
        if not spans:
            return 0
        ok = self._send_batch(spans)
        if ok:
            self._commit_batch(batches)
            self.rows_sent += len(spans)
            return len(spans)
        self.rows_failed += len(spans)
        return -len(spans)

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.load_state()
        self.log(
            "maple forwarder started "
            f"endpoint={self.cfg.endpoint} mem={self.cfg.seq_mem_path} trace={self.cfg.seq_trace_path}"
        )

        while not self.stop_requested:
            sent_any = 0
            failed_any = False
            for _ in range(max(1, self.cfg.max_batches)):
                result = self.run_once()
                if result > 0:
                    sent_any += result
                    continue
                if result < 0:
                    failed_any = True
                break

            if sent_any > 0:
                self.log(f"maple forwarder flushed spans={sent_any}")
                continue
            if failed_any:
                time.sleep(max(1.0, self.cfg.poll_seconds))
                continue
            time.sleep(self.cfg.poll_seconds)

        self.save_state()
        self.log("maple forwarder stopped")
        return 0


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True)
    if proc.returncode != 0:
        return False
    cmd = proc.stdout.strip()
    return "seq_maple_forwarder.py" in cmd and (" run " in cmd or cmd.endswith(" run"))


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
        "--seq-trace",
        str(cfg.seq_trace_path),
        "--endpoint",
        cfg.endpoint,
        "--service-name",
        cfg.service_name,
        "--service-version",
        cfg.service_version,
        "--deployment-env",
        cfg.deployment_environment,
        "--scope-name",
        cfg.scope_name,
        "--state-path",
        str(cfg.state_path),
        "--poll-seconds",
        str(cfg.poll_seconds),
        "--batch-size",
        str(cfg.batch_size),
        "--max-batches",
        str(cfg.max_batches),
        "--timeout-s",
        str(cfg.timeout_s),
        "--max-line-bytes",
        str(cfg.max_line_bytes),
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
    print(f"started maple forwarder: pid={proc.pid}")
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
    print(f"stopped maple forwarder: pid={pid}")
    return 0


def cmd_status(cfg: Config) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)
    print(f"pidfile: {cfg.pidfile}")
    print(f"log: {cfg.log_path}")
    print(f"state: {cfg.state_path}")
    print(f"seq_mem: {cfg.seq_mem_path}")
    print(f"seq_trace: {cfg.seq_trace_path}")
    print(f"endpoint: {cfg.endpoint}")
    print(f"ingest_key: {'<set>' if cfg.ingest_key.strip() else '<unset>'}")
    print(f"verify_tls: {cfg.verify_tls}")
    print(f"ca_bundle: {cfg.ca_bundle or '<auto>'}")
    print(f"batch_size: {cfg.batch_size}")
    print(f"poll_seconds: {cfg.poll_seconds}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    return 0 if alive else 1


def cmd_preflight(cfg: Config) -> int:
    ok = True
    print("maple forwarder preflight")
    print(f"  endpoint={cfg.endpoint or '<unset>'}")
    print(f"  ingest_key={'<set>' if cfg.ingest_key.strip() else '<unset>'}")
    print(f"  verify_tls={cfg.verify_tls}")
    print(f"  ca_bundle={cfg.ca_bundle or '<auto>'}")
    print(f"  seq_mem={cfg.seq_mem_path}")
    print(f"  seq_trace={cfg.seq_trace_path}")
    print(f"  state={cfg.state_path}")
    if not cfg.endpoint.strip():
        ok = False
        print("  ERR endpoint is empty")
    if not cfg.ingest_key.strip():
        ok = False
        print("  ERR ingest key is empty")
    if not cfg.seq_mem_path.exists():
        print("  WARN seq_mem path does not exist yet")
    if not cfg.seq_trace_path.exists():
        print("  WARN seq_trace path does not exist yet")
    return 0 if ok else 1


def build_config(args: argparse.Namespace) -> Config:
    endpoint = args.endpoint.strip()
    if not endpoint:
        endpoint = _env("SEQ_EVERRUNS_MAPLE_HOSTED_ENDPOINT", DEFAULT_ENDPOINT).strip()
    ingest_key = args.ingest_key.strip()
    if not ingest_key:
        ingest_key = _env("SEQ_EVERRUNS_MAPLE_HOSTED_INGEST_KEY", "").strip()

    deployment_environment = args.deployment_env.strip()
    if not deployment_environment:
        deployment_environment = _env("SEQ_EVERRUNS_MAPLE_ENV", DEFAULT_ENV).strip() or DEFAULT_ENV

    return Config(
        seq_mem_path=Path(args.seq_mem).expanduser().resolve(),
        seq_trace_path=Path(args.seq_trace).expanduser().resolve(),
        endpoint=endpoint,
        ingest_key=ingest_key,
        service_name=(args.service_name.strip() or DEFAULT_SERVICE_NAME),
        service_version=args.service_version.strip(),
        deployment_environment=(deployment_environment or DEFAULT_ENV),
        scope_name=(args.scope_name.strip() or DEFAULT_SCOPE_NAME),
        state_path=Path(args.state_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        poll_seconds=max(0.1, float(args.poll_seconds)),
        batch_size=max(1, int(args.batch_size)),
        max_batches=max(1, int(args.max_batches)),
        timeout_s=max(0.2, float(args.timeout_s)),
        verify_tls=bool(args.verify_tls),
        ca_bundle=args.ca_bundle.strip(),
        max_line_bytes=max(1024, int(args.max_line_bytes)),
        reset_state=bool(args.reset_state),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seq-mem", default=_env("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM))
    parser.add_argument("--seq-trace", default=_env("SEQ_CH_LOG_PATH", DEFAULT_SEQ_TRACE))
    parser.add_argument(
        "--endpoint",
        default=_env(
            "SEQ_MAPLE_FORWARDER_ENDPOINT",
            _env("SEQ_EVERRUNS_MAPLE_HOSTED_ENDPOINT", DEFAULT_ENDPOINT),
        ),
    )
    parser.add_argument(
        "--ingest-key",
        default=_env(
            "SEQ_MAPLE_FORWARDER_INGEST_KEY",
            _env("SEQ_EVERRUNS_MAPLE_HOSTED_INGEST_KEY", ""),
        ),
    )
    parser.add_argument("--service-name", default=_env("SEQ_MAPLE_FORWARDER_SERVICE_NAME", DEFAULT_SERVICE_NAME))
    parser.add_argument("--service-version", default=_env("SEQ_MAPLE_FORWARDER_SERVICE_VERSION", ""))
    parser.add_argument(
        "--deployment-env",
        default=_env("SEQ_MAPLE_FORWARDER_ENV", _env("SEQ_EVERRUNS_MAPLE_ENV", DEFAULT_ENV)),
    )
    parser.add_argument("--scope-name", default=_env("SEQ_MAPLE_FORWARDER_SCOPE_NAME", DEFAULT_SCOPE_NAME))
    parser.add_argument("--state-path", default=_env("SEQ_MAPLE_FORWARDER_STATE", DEFAULT_STATE_PATH))
    parser.add_argument("--pidfile", default=_env("SEQ_MAPLE_FORWARDER_PIDFILE", DEFAULT_PIDFILE))
    parser.add_argument("--log-path", default=_env("SEQ_MAPLE_FORWARDER_LOG", DEFAULT_LOG_PATH))
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(_env("SEQ_MAPLE_FORWARDER_POLL_SECONDS", "0.5")),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(_env("SEQ_MAPLE_FORWARDER_BATCH_SIZE", "128")),
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=int(_env("SEQ_MAPLE_FORWARDER_MAX_BATCHES", "8")),
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=float(_env("SEQ_MAPLE_FORWARDER_TIMEOUT_S", "2.0")),
    )
    parser.add_argument(
        "--verify-tls",
        action=argparse.BooleanOptionalAction,
        default=_env("SEQ_MAPLE_FORWARDER_VERIFY_TLS", "true").strip().lower() in {"1", "true", "yes", "on"},
    )
    parser.add_argument("--ca-bundle", default=_env("SEQ_MAPLE_FORWARDER_CA_BUNDLE", ""))
    parser.add_argument(
        "--max-line-bytes",
        type=int,
        default=int(_env("SEQ_MAPLE_FORWARDER_MAX_LINE_BYTES", "262144")),
    )
    parser.add_argument("--reset-state", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward seq mem/trace rows to Maple OTLP endpoint.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run in foreground.")
    add_common_args(p_run)

    p_once = sub.add_parser("once", help="Run one or more batches and exit.")
    add_common_args(p_once)

    p_start = sub.add_parser("start", help="Start background daemon.")
    add_common_args(p_start)

    p_stop = sub.add_parser("stop", help="Stop background daemon.")
    add_common_args(p_stop)

    p_status = sub.add_parser("status", help="Show daemon status.")
    add_common_args(p_status)

    p_preflight = sub.add_parser("preflight", help="Validate endpoint/key/config.")
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

    forwarder = MapleForwarder(cfg)
    if args.command == "once":
        forwarder.load_state()
        total = 0
        for _ in range(max(1, cfg.max_batches)):
            result = forwarder.run_once()
            if result <= 0:
                break
            total += result
        print(f"spans_sent={total} rows_failed={forwarder.rows_failed} rows_skipped={forwarder.rows_skipped}")
        return 0 if forwarder.rows_failed == 0 else 1

    if args.command == "run":
        return forwarder.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
