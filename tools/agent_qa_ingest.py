#!/usr/bin/env python3
"""Continuously ingest Claude/Codex Q/A pairs into seq ClickHouse spool files.

The daemon tails JSONL sessions from:
- ~/.claude/projects/**/*.jsonl
- ~/.codex/sessions/**/*.jsonl

It emits normalized seq.mem_events rows (`name=agent.qa.pair`) to `SEQ_CH_MEM_PATH`.
Each row's `subject` is compact JSON with anonymized training payload.
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

DEFAULT_CLAUDE_DIR = str(Path("~/.claude/projects").expanduser())
DEFAULT_CODEX_DIR = str(Path("~/.codex/sessions").expanduser())
DEFAULT_SEQ_MEM_PATH = str(
    Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser()
)
DEFAULT_STATE_PATH = str(Path("~/.local/state/seq/agent_qa_ingest_state.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/agent_qa_ingest.pid").expanduser())
DEFAULT_LOG_PATH = str(Path("~/code/seq/cli/cpp/out/logs/agent_qa_ingest.log").expanduser())
DEFAULT_ZVEC_JSONL = str(Path("~/repos/alibaba/zvec/data/agent_qa.jsonl").expanduser())


@dataclass
class Config:
    claude_dir: Path
    codex_dir: Path
    seq_mem_path: Path
    state_path: Path
    pidfile: Path
    log_path: Path
    zvec_jsonl: Path | None
    poll_seconds: float
    rescan_seconds: float
    flush_every: int
    max_text_chars: int
    backfill: bool
    include_text: bool
    reset_state: bool


class Ingestor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.offsets: dict[str, int] = {}
        self.file_meta: dict[str, dict[str, Any]] = {}
        self.pending_user: dict[str, dict[str, Any]] = {}
        self.rows_emitted = 0
        self.rows_skipped = 0
        self.files_seen = 0
        self.last_rescan = 0.0
        self.watched_files: list[Path] = []

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def load_state(self) -> None:
        path = self.cfg.state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        offsets = payload.get("offsets")
        if isinstance(offsets, dict):
            clean: dict[str, int] = {}
            for key, value in offsets.items():
                if isinstance(key, str) and isinstance(value, int) and value >= 0:
                    clean[key] = value
            self.offsets = clean

    def save_state(self) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "agent_qa_ingest_state_v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "offsets": self.offsets,
        }
        self.cfg.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    def discover_files(self) -> list[Path]:
        out: list[Path] = []
        for root in (self.cfg.claude_dir, self.cfg.codex_dir):
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                if path.is_file():
                    out.append(path)
        out.sort()
        return out

    def rescan_if_needed(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_rescan < self.cfg.rescan_seconds:
            return
        self.watched_files = self.discover_files()
        self.files_seen = len(self.watched_files)
        self.last_rescan = now

    def parse_timestamp_ms(self, value: Any) -> int:
        if isinstance(value, bool):
            return int(time.time() * 1000)
        if isinstance(value, int):
            if value > 10_000_000_000:
                return value
            return value * 1000
        if isinstance(value, float):
            if value > 10_000_000_000:
                return int(value)
            return int(value * 1000)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            try:
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                dt = datetime.fromisoformat(raw)
                return int(dt.timestamp() * 1000)
            except Exception:
                return int(time.time() * 1000)
        return int(time.time() * 1000)

    def sanitize_text(self, text: str) -> str:
        value = " ".join(text.replace("\r", "\n").splitlines()).strip()
        if not value:
            return ""
        if len(value) > self.cfg.max_text_chars:
            value = value[: self.cfg.max_text_chars]
        return value

    def _extract_codex_text(self, obj: dict[str, Any]) -> str:
        content = obj.get("content")
        if isinstance(content, str):
            return self.sanitize_text(content)
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind in {"input_text", "output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        return self.sanitize_text("\n".join(parts))

    def _extract_claude_text(self, obj: dict[str, Any]) -> str:
        message = obj.get("message")
        if isinstance(message, str):
            return self.sanitize_text(message)
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return self.sanitize_text(content)
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return self.sanitize_text("\n".join(parts))

    def _extract_codex_meta(self, obj: dict[str, Any], fallback_session_id: str) -> dict[str, Any]:
        session_id = fallback_session_id
        model = ""
        project_path = ""

        root_id = obj.get("id")
        if isinstance(root_id, str) and root_id.strip():
            session_id = root_id.strip()

        git_obj = obj.get("git")
        if isinstance(git_obj, dict):
            cwd = git_obj.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                project_path = cwd.strip()

        instructions = obj.get("instructions")
        if isinstance(instructions, str):
            marker = "Current working directory:"
            if marker in instructions and not project_path:
                try:
                    project_path = instructions.split(marker, 1)[1].split("\n", 1)[0].strip()
                except Exception:
                    project_path = ""

        return {
            "session_id": session_id,
            "model": model,
            "project_path": project_path,
        }

    def _session_key(self, agent: str, session_id: str) -> str:
        return f"{agent}:{session_id}"

    def _make_event_id(self, agent: str, source_path: str, source_offset: int, session_id: str) -> str:
        raw = f"{agent}|{source_path}|{source_offset}|{session_id}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _make_content_hash(self, question: str, answer: str) -> str:
        raw = f"q:{question}\na:{answer}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _emit_pair(
        self,
        *,
        ts_ms: int,
        agent: str,
        session_id: str,
        project_path: str,
        source_path: str,
        source_offset: int,
        question: str,
        answer: str,
        model: str,
    ) -> dict[str, Any]:
        event_id = self._make_event_id(agent, source_path, source_offset, session_id)
        content_hash = self._make_content_hash(question, answer)
        subject_obj = {
            "agent": agent,
            "session_id": session_id,
            "project_path": project_path,
            "source_path": source_path,
            "offset": source_offset,
            "model": model,
            "question": question if self.cfg.include_text else "",
            "answer": answer if self.cfg.include_text else "",
            "question_chars": len(question),
            "answer_chars": len(answer),
        }
        row = {
            "ts_ms": int(ts_ms),
            "dur_us": 0,
            "ok": True,
            "session_id": session_id,
            "event_id": event_id,
            "content_hash": content_hash,
            "name": "agent.qa.pair",
            "subject": json.dumps(subject_obj, ensure_ascii=True),
        }
        return row

    def _parse_line(self, path: Path, offset: int, line: bytes) -> list[dict[str, Any]]:
        raw = line.decode("utf-8", errors="replace").strip()
        if not raw:
            return []
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            self.rows_skipped += 1
            return []
        if not isinstance(obj, dict):
            self.rows_skipped += 1
            return []

        source_path = str(path)
        claude_root = str(self.cfg.claude_dir)
        is_claude = "/.claude/" in source_path or source_path.startswith(claude_root)
        agent = "claude" if is_claude else "codex"
        file_key = source_path
        meta = self.file_meta.get(file_key, {})
        out_rows: list[dict[str, Any]] = []

        default_session_id = path.stem

        if not is_claude and obj.get("type") is None and ("id" in obj or "git" in obj):
            parsed_meta = self._extract_codex_meta(obj, default_session_id)
            meta.update(parsed_meta)
            self.file_meta[file_key] = meta
            return out_rows

        if is_claude:
            record_type = obj.get("type")
            if record_type not in {"user", "assistant"}:
                return out_rows
            session_id = str(obj.get("sessionId") or meta.get("session_id") or default_session_id)
            project_path = str(obj.get("cwd") or meta.get("project_path") or "")
            model = str(meta.get("model") or "")
            text = self._extract_claude_text(obj)
            ts_ms = self.parse_timestamp_ms(obj.get("timestamp"))
            role = "user" if record_type == "user" else "assistant"
        else:
            if obj.get("type") != "message":
                return out_rows
            role = str(obj.get("role") or "")
            if role not in {"user", "assistant"}:
                return out_rows
            session_id = str(meta.get("session_id") or default_session_id)
            project_path = str(meta.get("project_path") or "")
            model = str(meta.get("model") or "")
            text = self._extract_codex_text(obj)
            ts_ms = self.parse_timestamp_ms(obj.get("timestamp"))

        if not session_id:
            session_id = default_session_id
        if not text:
            return out_rows

        key = self._session_key(agent, session_id)
        if role == "user":
            self.pending_user[key] = {
                "question": text,
                "ts_ms": ts_ms,
                "project_path": project_path,
                "model": model,
            }
            if len(self.pending_user) > 100_000:
                # Bound memory in pathological cases.
                for stale_key in list(self.pending_user.keys())[:10_000]:
                    self.pending_user.pop(stale_key, None)
            return out_rows

        pending = self.pending_user.pop(key, None)
        if not pending:
            return out_rows

        question = self.sanitize_text(str(pending.get("question") or ""))
        answer = self.sanitize_text(text)
        if not question or not answer:
            return out_rows

        pair_ts = max(int(pending.get("ts_ms") or 0), ts_ms)
        row = self._emit_pair(
            ts_ms=pair_ts,
            agent=agent,
            session_id=session_id,
            project_path=project_path or str(pending.get("project_path") or ""),
            source_path=source_path,
            source_offset=offset,
            question=question,
            answer=answer,
            model=model or str(pending.get("model") or ""),
        )
        out_rows.append(row)
        return out_rows

    def _append_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if path.resolve() == self.cfg.seq_mem_path.resolve():
            append_seq_mem_rows(rows, local_path=path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=True))
                fh.write("\n")

    def _append_zvec_jsonl(self, rows: list[dict[str, Any]]) -> None:
        if not rows or not self.cfg.zvec_jsonl:
            return
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("name") != "agent.qa.pair":
                continue
            try:
                subject = json.loads(str(row.get("subject") or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(subject, dict):
                continue
            q = str(subject.get("question") or "")
            a = str(subject.get("answer") or "")
            if not q or not a:
                continue
            out.append(
                {
                    "id": str(row.get("event_id") or ""),
                    "text": f"Question: {q}\n\nAnswer: {a}",
                    "metadata": {
                        "agent": subject.get("agent", ""),
                        "session_id": subject.get("session_id", ""),
                        "project_path": subject.get("project_path", ""),
                        "source_path": subject.get("source_path", ""),
                        "ts_ms": row.get("ts_ms", 0),
                    },
                }
            )
        if out:
            self._append_jsonl(self.cfg.zvec_jsonl, out)

    def _process_file(self, path: Path) -> int:
        key = str(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self.offsets.pop(key, None)
            return 0

        if key not in self.offsets:
            if self.cfg.backfill:
                self.offsets[key] = 0
            else:
                self.offsets[key] = size
                return 0

        offset = self.offsets.get(key, 0)
        if size < offset:
            offset = 0

        if size == offset:
            return 0

        emitted_rows: list[dict[str, Any]] = []
        new_offset = offset

        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()

        if not chunk:
            self.offsets[key] = size
            return 0

        parts = chunk.split(b"\n")
        # Always drop the trailing split fragment:
        # - if chunk ends with newline, it's an empty sentinel
        # - otherwise it's an incomplete partial line
        complete = parts[:-1]
        cursor = offset
        for line in complete:
            line_offset = cursor
            cursor += len(line) + 1
            if not line:
                continue
            rows = self._parse_line(path, line_offset, line)
            if rows:
                emitted_rows.extend(rows)

        new_offset = cursor
        self.offsets[key] = new_offset

        if emitted_rows:
            self._append_jsonl(self.cfg.seq_mem_path, emitted_rows)
            self._append_zvec_jsonl(emitted_rows)
            self.rows_emitted += len(emitted_rows)

        return len(emitted_rows)

    def run_once(self) -> int:
        self.rescan_if_needed(force=True)
        emitted = 0
        for path in self.watched_files:
            emitted += self._process_file(path)
        self.save_state()
        return emitted

    def run_forever(self) -> int:
        self.load_state()
        if self.cfg.reset_state:
            self.offsets = {}
            print(
                f"[agent-qa-ingest] reset-state enabled: {self.cfg.state_path}",
                flush=True,
            )
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        print(
            f"[agent-qa-ingest] starting: claude_dir={self.cfg.claude_dir} codex_dir={self.cfg.codex_dir}",
            flush=True,
        )
        print(
            f"[agent-qa-ingest] sink={self.cfg.seq_mem_path} state={self.cfg.state_path}",
            flush=True,
        )
        if self.cfg.zvec_jsonl:
            print(f"[agent-qa-ingest] zvec_jsonl={self.cfg.zvec_jsonl}", flush=True)

        ticks = 0
        while not self.stop_requested:
            self.rescan_if_needed(force=False)
            emitted_this_tick = 0
            for path in self.watched_files:
                emitted_this_tick += self._process_file(path)

            ticks += 1
            if ticks % max(1, self.cfg.flush_every) == 0:
                self.save_state()

            if emitted_this_tick > 0:
                print(
                    "[agent-qa-ingest] "
                    f"emitted={emitted_this_tick} total={self.rows_emitted} files={self.files_seen}",
                    flush=True,
                )

            time.sleep(max(0.2, self.cfg.poll_seconds))

        self.save_state()
        print(
            f"[agent-qa-ingest] stopped: emitted_total={self.rows_emitted} skipped={self.rows_skipped}",
            flush=True,
        )
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

    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return False
    cmd = proc.stdout.strip()
    return "agent_qa_ingest.py" in cmd and (" run " in cmd or cmd.endswith(" run"))


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
        "--poll-seconds",
        str(cfg.poll_seconds),
        "--rescan-seconds",
        str(cfg.rescan_seconds),
        "--flush-every",
        str(cfg.flush_every),
        "--max-text-chars",
        str(cfg.max_text_chars),
        "--claude-dir",
        str(cfg.claude_dir),
        "--codex-dir",
        str(cfg.codex_dir),
        "--seq-mem",
        str(cfg.seq_mem_path),
        "--state-path",
        str(cfg.state_path),
    ]
    if cfg.backfill:
        run_cmd.append("--backfill")
    if cfg.reset_state:
        run_cmd.append("--reset-state")
    if not cfg.include_text:
        run_cmd.append("--no-include-text")
    if cfg.zvec_jsonl:
        run_cmd.extend(["--zvec-jsonl", str(cfg.zvec_jsonl)])

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _write_pid(cfg.pidfile, proc.pid)
    print(f"started agent-qa ingest: pid={proc.pid}")
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
    print(f"stopped agent-qa ingest: pid={pid}")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
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


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_config(args: argparse.Namespace) -> Config:
    zvec_raw = args.zvec_jsonl
    if zvec_raw is None:
        zvec_raw = os.environ.get("SEQ_AGENT_QA_ZVEC_JSONL", DEFAULT_ZVEC_JSONL)
    zvec_path = Path(zvec_raw).expanduser().resolve() if zvec_raw else None

    return Config(
        claude_dir=Path(args.claude_dir).expanduser().resolve(),
        codex_dir=Path(args.codex_dir).expanduser().resolve(),
        seq_mem_path=Path(args.seq_mem).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        zvec_jsonl=zvec_path,
        poll_seconds=max(0.2, float(args.poll_seconds)),
        rescan_seconds=max(2.0, float(args.rescan_seconds)),
        flush_every=max(1, int(args.flush_every)),
        max_text_chars=max(256, min(100_000, int(args.max_text_chars))),
        backfill=bool(args.backfill),
        include_text=bool(args.include_text),
        reset_state=bool(args.reset_state),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--claude-dir",
        default=os.environ.get("SEQ_AGENT_QA_CLAUDE_DIR", DEFAULT_CLAUDE_DIR),
    )
    parser.add_argument(
        "--codex-dir",
        default=os.environ.get("SEQ_AGENT_QA_CODEX_DIR", DEFAULT_CODEX_DIR),
    )
    parser.add_argument(
        "--seq-mem",
        default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM_PATH),
    )
    parser.add_argument(
        "--state-path",
        default=os.environ.get("SEQ_AGENT_QA_STATE", DEFAULT_STATE_PATH),
    )
    parser.add_argument(
        "--pidfile",
        default=os.environ.get("SEQ_AGENT_QA_PIDFILE", DEFAULT_PIDFILE),
    )
    parser.add_argument(
        "--log-path",
        default=os.environ.get("SEQ_AGENT_QA_LOG", DEFAULT_LOG_PATH),
    )
    parser.add_argument(
        "--zvec-jsonl",
        default=None,
        help="Optional zvec document JSONL output path (empty disables).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("SEQ_AGENT_QA_POLL_SECONDS", "1.0")),
    )
    parser.add_argument(
        "--rescan-seconds",
        type=float,
        default=float(os.environ.get("SEQ_AGENT_QA_RESCAN_SECONDS", "15.0")),
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=int(os.environ.get("SEQ_AGENT_QA_FLUSH_EVERY", "10")),
    )
    parser.add_argument(
        "--max-text-chars",
        type=int,
        default=int(os.environ.get("SEQ_AGENT_QA_MAX_TEXT_CHARS", "8000")),
    )
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore saved offsets and rescan from scratch.",
    )
    parser.add_argument(
        "--include-text",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SEQ_AGENT_QA_INCLUDE_TEXT", True),
        help="Capture question/answer text payloads (default true).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Q/A ingest daemon for seq")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run in foreground.")
    add_common_args(p_run)

    p_once = sub.add_parser("once", help="Run one scan pass and exit.")
    add_common_args(p_once)

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

    if args.command == "start":
        return cmd_start(cfg, args)
    if args.command == "stop":
        return cmd_stop(cfg, args)
    if args.command == "status":
        return cmd_status(cfg, args)

    ingestor = Ingestor(cfg)
    if args.command == "once":
        ingestor.load_state()
        if cfg.reset_state:
            ingestor.offsets = {}
        emitted = ingestor.run_once()
        print(f"emitted_rows={emitted} files_seen={ingestor.files_seen}")
        return 0
    if args.command == "run":
        return ingestor.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
