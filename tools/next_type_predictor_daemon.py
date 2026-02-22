#!/usr/bin/env python3
"""Always-on local next-type predictor for OS-level completion.

Consumes `next_type.key_down` rows from seq mem spool and learns a lightweight
personal language model online:
- token completion from unigram counts
- next-token prediction from bigram counts

Predictions are emitted as Lin widgets (action=paste) and stored as latest
active suggestion for hotkey acceptance via `next_type_accept.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seq_mem_sink import append_seq_mem_rows

DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_INBOX = str((Path.home() / "Library" / "Application Support" / "Lin" / "intent-inbox.jsonl"))
DEFAULT_STATE = str(Path("~/.local/state/seq/next_type_predictor_state.json").expanduser())
DEFAULT_MODEL = str(Path("~/.local/state/seq/next_type_predictor_model.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/next_type_predictor.pid").expanduser())
DEFAULT_LOG = str(Path("~/code/seq/cli/cpp/out/logs/next_type_predictor.log").expanduser())

# macOS ANSI keycodes (US layout assumptions) for low-latency online learning.
KEYCODE_TO_CHAR = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v",
    11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t", 18: "1", 19: "2", 20: "3",
    21: "4", 22: "6", 23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0", 30: "]",
    31: "o", 32: "u", 33: "[", 34: "i", 35: "p", 37: "l", 38: "j", 39: "'", 40: "k", 41: ";",
    42: "\\", 43: ",", 44: "/", 45: "n", 46: "m", 47: ".", 50: "`",
}
SPACE_CODES = {49}
ENTER_CODES = {36, 76}
TAB_CODES = {48}
DELETE_CODES = {51, 117}
DELIMITER_CHARS = {" ", "\n", "\t", ",", ";", "!", "?", "(", ")", "{", "}", '"'}
TOKEN_EXTRA_CHARS = {"-", "_", "/", ".", ":"}


@dataclass
class Config:
    seq_mem: Path
    inbox: Path
    state_path: Path
    model_path: Path
    pidfile: Path
    log_path: Path
    poll_seconds: float
    save_interval_seconds: float
    cooldown_ms: int
    ttl_ms: int
    min_prefix: int
    min_token_count: int
    min_bigram_count: int
    max_vocab: int
    emit_seq_events: bool
    reset_state: bool


class Predictor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False

        self.offset = 0
        self.inode = 0
        self.current_token = ""
        self.prev_token = ""

        self.rows_seen = 0
        self.rows_keydown = 0
        self.rows_skipped = 0
        self.suggestions_emitted = 0

        self.last_emit_ms = 0
        self.last_emit_signature = ""
        self.last_state_save = 0.0
        self.last_model_save = 0.0

        self.latest_suggestion: dict[str, Any] = {}

        self.unigrams: dict[str, int] = {}
        self.bigrams: dict[str, dict[str, int]] = {}

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] {message}", flush=True)

    def _safe_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def load_state(self) -> None:
        if self.cfg.reset_state:
            return
        payload = self._read_json(self.cfg.state_path)
        self.offset = int(payload.get("offset") or 0)
        self.inode = int(payload.get("inode") or 0)
        self.current_token = str(payload.get("current_token") or "")
        self.prev_token = str(payload.get("prev_token") or "")
        self.last_emit_ms = int(payload.get("last_emit_ms") or 0)
        self.last_emit_signature = str(payload.get("last_emit_signature") or "")
        latest = payload.get("latest_suggestion")
        if isinstance(latest, dict):
            self.latest_suggestion = latest

    def load_model(self) -> None:
        if self.cfg.reset_state:
            return
        payload = self._read_json(self.cfg.model_path)
        unigrams = payload.get("unigrams")
        bigrams = payload.get("bigrams")
        if isinstance(unigrams, dict):
            for k, v in unigrams.items():
                if not isinstance(k, str):
                    continue
                try:
                    count = int(v)
                except Exception:
                    continue
                if count > 0:
                    self.unigrams[k] = count
        if isinstance(bigrams, dict):
            for prev, nxt in bigrams.items():
                if not isinstance(prev, str) or not isinstance(nxt, dict):
                    continue
                clean: dict[str, int] = {}
                for token, count_raw in nxt.items():
                    if not isinstance(token, str):
                        continue
                    try:
                        count = int(count_raw)
                    except Exception:
                        continue
                    if count > 0:
                        clean[token] = count
                if clean:
                    self.bigrams[prev] = clean

    def save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_state_save) < 1.0:
            return
        payload = {
            "schema_version": "next_type_predictor_state_v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "offset": int(self.offset),
            "inode": int(self.inode),
            "current_token": self.current_token,
            "prev_token": self.prev_token,
            "last_emit_ms": int(self.last_emit_ms),
            "last_emit_signature": self.last_emit_signature,
            "latest_suggestion": self.latest_suggestion,
            "rows_seen": int(self.rows_seen),
            "rows_keydown": int(self.rows_keydown),
            "rows_skipped": int(self.rows_skipped),
            "suggestions_emitted": int(self.suggestions_emitted),
        }
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(self._safe_json(payload) + "\n", encoding="utf-8")
        self.last_state_save = now

    def save_model(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_model_save) < self.cfg.save_interval_seconds:
            return
        payload = {
            "schema_version": "next_type_predictor_model_v1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "unigrams": self.unigrams,
            "bigrams": self.bigrams,
        }
        self.cfg.model_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.model_path.write_text(self._safe_json(payload) + "\n", encoding="utf-8")
        self.last_model_save = now

    def _append_seq_event(self, name: str, subject_obj: dict[str, Any]) -> None:
        if not self.cfg.emit_seq_events:
            return
        row = {
            "ts_ms": int(time.time() * 1000),
            "dur_us": 0,
            "ok": True,
            "session_id": "next-type-predictor",
            "name": name,
            "subject": json.dumps(subject_obj, ensure_ascii=True),
        }
        append_seq_mem_rows([row], local_path=self.cfg.seq_mem)

    def _emit_widget(self, *, suggestion_id: str, suggestion_text: str, message: str, ttl_ms: int) -> None:
        now_ms = int(time.time() * 1000)
        entry = {
            "id": suggestion_id,
            "kind": "widget",
            "title": "Next to Type",
            "message": message,
            "createdAt": now_ms,
            "expiresAt": now_ms + max(1, ttl_ms),
            "action": "paste",
            "actionTitle": "Tab Complete",
            "value": suggestion_text,
        }
        self.cfg.inbox.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg.inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _is_token_char(self, ch: str) -> bool:
        if not ch:
            return False
        if ch.isalnum():
            return True
        return ch in TOKEN_EXTRA_CHARS

    def _commit_token(self) -> None:
        token = self.current_token.strip().lower()
        self.current_token = ""
        if len(token) < 2:
            return

        self.unigrams[token] = self.unigrams.get(token, 0) + 1
        if self.prev_token:
            nxt = self.bigrams.setdefault(self.prev_token, {})
            nxt[token] = nxt.get(token, 0) + 1
        self.prev_token = token

        if len(self.unigrams) > int(self.cfg.max_vocab * 1.2):
            self._prune_model()

    def _prune_model(self) -> None:
        top_tokens = sorted(self.unigrams.items(), key=lambda kv: kv[1], reverse=True)[: self.cfg.max_vocab]
        keep = {k for k, _ in top_tokens}
        self.unigrams = {k: v for k, v in top_tokens}

        pruned_bigrams: dict[str, dict[str, int]] = {}
        for prev, nxt in self.bigrams.items():
            if prev not in keep:
                continue
            filtered = [(k, v) for k, v in nxt.items() if k in keep]
            if not filtered:
                continue
            filtered.sort(key=lambda kv: kv[1], reverse=True)
            pruned_bigrams[prev] = {k: v for k, v in filtered[:32]}
        self.bigrams = pruned_bigrams

    def _best_completion(self, prefix: str) -> tuple[str, int]:
        best_token = ""
        best_count = 0
        for token, count in self.unigrams.items():
            if token == prefix:
                continue
            if not token.startswith(prefix):
                continue
            if count > best_count:
                best_token = token
                best_count = count
        return best_token, best_count

    def _best_next_token(self, prev_token: str) -> tuple[str, int]:
        nxt = self.bigrams.get(prev_token)
        if not nxt:
            return "", 0
        best_token = ""
        best_count = 0
        for token, count in nxt.items():
            if count > best_count:
                best_token = token
                best_count = count
        return best_token, best_count

    def _emit_suggestion(self, *, mode: str, prefix: str, candidate: str, score: int) -> None:
        now_ms = int(time.time() * 1000)
        if now_ms - self.last_emit_ms < self.cfg.cooldown_ms:
            return

        if mode == "completion":
            suggestion_text = candidate[len(prefix) :]
            if not suggestion_text:
                return
            message = f"Complete '{prefix}' -> {candidate}"
        else:
            suggestion_text = candidate + " "
            message = f"Next token after '{self.prev_token}' -> {candidate}"

        signature = f"{mode}|{prefix}|{candidate}"
        if signature == self.last_emit_signature:
            return

        suggestion_id = f"seq-next-type-{now_ms}"
        self._emit_widget(
            suggestion_id=suggestion_id,
            suggestion_text=suggestion_text,
            message=message,
            ttl_ms=self.cfg.ttl_ms,
        )

        self.latest_suggestion = {
            "id": suggestion_id,
            "created_at_ms": now_ms,
            "expires_at_ms": now_ms + self.cfg.ttl_ms,
            "mode": mode,
            "prefix": prefix,
            "candidate": candidate,
            "suggestion_text": suggestion_text,
            "score": int(score),
        }
        self.last_emit_ms = now_ms
        self.last_emit_signature = signature
        self.suggestions_emitted += 1

        self._append_seq_event(
            "next_type.suggestion_emit.v1",
            {
                "schema_version": "next_type_suggestion_emit_v1",
                "mode": mode,
                "prefix": prefix,
                "candidate": candidate,
                "suggestion_text": suggestion_text,
                "score": int(score),
                "suggestion_id": suggestion_id,
            },
        )

    def _handle_char(self, ch: str) -> None:
        if ch in DELIMITER_CHARS:
            self._commit_token()
            if ch in {" ", "\n", "\t"} and self.prev_token:
                candidate, score = self._best_next_token(self.prev_token)
                if candidate and score >= self.cfg.min_bigram_count:
                    self._emit_suggestion(mode="next_token", prefix="", candidate=candidate, score=score)
            return

        if not self._is_token_char(ch):
            self._commit_token()
            return

        self.current_token = (self.current_token + ch.lower())[-64:]
        if len(self.current_token) < self.cfg.min_prefix:
            return

        candidate, score = self._best_completion(self.current_token)
        if not candidate:
            return
        if score < self.cfg.min_token_count:
            return
        self._emit_suggestion(mode="completion", prefix=self.current_token, candidate=candidate, score=score)

    def _parse_key_down(self, line: str) -> int | None:
        try:
            row = json.loads(line)
        except Exception:
            self.rows_skipped += 1
            return None
        if not isinstance(row, dict):
            self.rows_skipped += 1
            return None
        if str(row.get("name") or "") != "next_type.key_down":
            self.rows_skipped += 1
            return None

        subject_raw = row.get("subject")
        subject: dict[str, Any] = {}
        if isinstance(subject_raw, dict):
            subject = subject_raw
        elif isinstance(subject_raw, str):
            try:
                maybe = json.loads(subject_raw)
                if isinstance(maybe, dict):
                    subject = maybe
            except Exception:
                pass

        payload = subject.get("payload") if isinstance(subject, dict) else None
        if not isinstance(payload, dict):
            self.rows_skipped += 1
            return None

        key_code_raw = payload.get("key_code")
        try:
            return int(key_code_raw)
        except Exception:
            self.rows_skipped += 1
            return None

    def _handle_key_code(self, key_code: int) -> None:
        if key_code in DELETE_CODES:
            if self.current_token:
                self.current_token = self.current_token[:-1]
            return

        if key_code in SPACE_CODES:
            self._handle_char(" ")
            return
        if key_code in ENTER_CODES:
            self._handle_char("\n")
            return
        if key_code in TAB_CODES:
            self._handle_char("\t")
            return

        ch = KEYCODE_TO_CHAR.get(key_code)
        if not ch:
            return
        self._handle_char(ch)

    def _process_line(self, line: str) -> None:
        self.rows_seen += 1
        key_code = self._parse_key_down(line)
        if key_code is None:
            return
        self.rows_keydown += 1
        self._handle_key_code(key_code)

    def _process_available(self) -> int:
        if not self.cfg.seq_mem.exists():
            return 0

        try:
            stat = self.cfg.seq_mem.stat()
        except FileNotFoundError:
            return 0

        inode = int(getattr(stat, "st_ino", 0))
        if self.inode and inode != self.inode:
            self.offset = 0
        self.inode = inode

        processed = 0
        with self.cfg.seq_mem.open("r", encoding="utf-8", errors="replace") as fh:
            try:
                fh.seek(self.offset)
            except Exception:
                self.offset = 0
                fh.seek(0)
            while True:
                line = fh.readline()
                if not line:
                    break
                self.offset = fh.tell()
                processed += 1
                self._process_line(line)
        return processed

    def run_once(self) -> int:
        self.load_state()
        self.load_model()
        processed = self._process_available()
        self.save_state(force=True)
        self.save_model(force=True)
        print(
            f"processed={processed} rows_seen={self.rows_seen} key_down={self.rows_keydown} "
            f"suggestions_emitted={self.suggestions_emitted}"
        )
        return 0

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        self.load_state()
        self.load_model()
        self.log(
            "predictor started "
            f"(seq_mem={self.cfg.seq_mem}, inbox={self.cfg.inbox}, cooldown_ms={self.cfg.cooldown_ms}, ttl_ms={self.cfg.ttl_ms})"
        )

        while not self.stop_requested:
            processed = self._process_available()
            self.save_state(force=False)
            self.save_model(force=False)
            if processed == 0:
                time.sleep(self.cfg.poll_seconds)

        self.save_state(force=True)
        self.save_model(force=True)
        self.log(
            f"predictor stopped rows_seen={self.rows_seen} key_down={self.rows_keydown} "
            f"suggestions_emitted={self.suggestions_emitted}"
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

    proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True)
    if proc.returncode != 0:
        return False
    cmd = proc.stdout.strip()
    return "next_type_predictor_daemon.py" in cmd and (" run " in cmd or cmd.endswith(" run"))


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
    print("next-type predictor preflight")

    if cfg.seq_mem.exists():
        print(f"- OK: seq mem exists: {cfg.seq_mem}")
    else:
        print(f"- WARN: seq mem missing (will wait): {cfg.seq_mem}")

    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.model_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    print("- OK: writable state/model/log directories")

    cfg.inbox.parent.mkdir(parents=True, exist_ok=True)
    print(f"- OK: Lin inbox path: {cfg.inbox}")
    return 0


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
        "--seq-mem",
        str(cfg.seq_mem),
        "--inbox",
        str(cfg.inbox),
        "--state-path",
        str(cfg.state_path),
        "--model-path",
        str(cfg.model_path),
        "--pidfile",
        str(cfg.pidfile),
        "--log-path",
        str(cfg.log_path),
        "--poll-seconds",
        str(cfg.poll_seconds),
        "--save-interval-seconds",
        str(cfg.save_interval_seconds),
        "--cooldown-ms",
        str(cfg.cooldown_ms),
        "--ttl-ms",
        str(cfg.ttl_ms),
        "--min-prefix",
        str(cfg.min_prefix),
        "--min-token-count",
        str(cfg.min_token_count),
        "--min-bigram-count",
        str(cfg.min_bigram_count),
        "--max-vocab",
        str(cfg.max_vocab),
    ]
    if cfg.emit_seq_events:
        run_cmd.append("--emit-seq-events")
    else:
        run_cmd.append("--no-emit-seq-events")

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    _write_pid(cfg.pidfile, proc.pid)
    print(f"started next-type predictor: pid={proc.pid}")
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
    print(f"stopped next-type predictor: pid={pid}")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)

    state = {}
    if cfg.state_path.exists():
        try:
            state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    latest = state.get("latest_suggestion") if isinstance(state, dict) else None

    print(f"pidfile: {cfg.pidfile}")
    print(f"log: {cfg.log_path}")
    print(f"state: {cfg.state_path}")
    print(f"model: {cfg.model_path}")
    print(f"seq_mem: {cfg.seq_mem}")
    print(f"inbox: {cfg.inbox}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    if isinstance(latest, dict) and latest:
        print(
            f"latest_suggestion: id={latest.get('id','')} mode={latest.get('mode','')} "
            f"text={latest.get('suggestion_text','')!r} score={latest.get('score',0)}"
        )
    return 0 if alive else 1


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        seq_mem=Path(args.seq_mem).expanduser().resolve(),
        inbox=Path(args.inbox).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        model_path=Path(args.model_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        save_interval_seconds=max(5.0, float(args.save_interval_seconds)),
        cooldown_ms=max(100, int(args.cooldown_ms)),
        ttl_ms=max(1000, int(args.ttl_ms)),
        min_prefix=max(1, int(args.min_prefix)),
        min_token_count=max(1, int(args.min_token_count)),
        min_bigram_count=max(1, int(args.min_bigram_count)),
        max_vocab=max(200, int(args.max_vocab)),
        emit_seq_events=bool(args.emit_seq_events),
        reset_state=bool(args.reset_state),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seq-mem", default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM))
    parser.add_argument("--inbox", default=os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_INBOX", DEFAULT_INBOX))
    parser.add_argument(
        "--state-path",
        default=os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_STATE", DEFAULT_STATE),
    )
    parser.add_argument(
        "--model-path",
        default=os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument(
        "--pidfile",
        default=os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_PIDFILE", DEFAULT_PIDFILE),
    )
    parser.add_argument(
        "--log-path",
        default=os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_LOG", DEFAULT_LOG),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_POLL_SECONDS", "0.20")),
    )
    parser.add_argument(
        "--save-interval-seconds",
        type=float,
        default=float(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_SAVE_INTERVAL_SECONDS", "20")),
    )
    parser.add_argument(
        "--cooldown-ms",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_COOLDOWN_MS", "1200")),
    )
    parser.add_argument(
        "--ttl-ms",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_TTL_MS", "12000")),
    )
    parser.add_argument(
        "--min-prefix",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_MIN_PREFIX", "2")),
    )
    parser.add_argument(
        "--min-token-count",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_MIN_TOKEN_COUNT", "2")),
    )
    parser.add_argument(
        "--min-bigram-count",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_MIN_BIGRAM_COUNT", "2")),
    )
    parser.add_argument(
        "--max-vocab",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_MAX_VOCAB", "12000")),
    )
    parser.add_argument(
        "--emit-seq-events",
        action=argparse.BooleanOptionalAction,
        default=env_bool("SEQ_NEXT_TYPE_PREDICTOR_EMIT_SEQ_EVENTS", True),
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore saved state/model and rebuild online from current stream.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Online next-type predictor daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "once", "start", "stop", "status", "preflight"):
        p = sub.add_parser(name)
        add_common_args(p)

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

    predictor = Predictor(cfg)
    if args.command == "once":
        return predictor.run_once()
    if args.command == "run":
        return predictor.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
