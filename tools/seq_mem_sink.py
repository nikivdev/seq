#!/usr/bin/env python3
"""Shared seq mem sink with remote ClickHouse support.

Modes:
- file: append JSONL rows to local `SEQ_CH_MEM_PATH` (current behavior)
- remote: POST rows to remote ClickHouse HTTP API; on failure append to local fallback queue
- dual: send remote and also append local JSONL
- auto: use remote when `SEQ_MEM_REMOTE_URL` is set, otherwise file
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_LOCAL_MEM = Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser()
DEFAULT_FALLBACK = Path("~/.local/state/seq/remote_fallback/seq_mem_fallback.jsonl").expanduser()


def _env_bool(key: str, default: bool) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _jsonl_blob(rows: list[dict[str, Any]]) -> bytes:
    lines = [json.dumps(row, ensure_ascii=True) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass
class SinkConfig:
    mode: str
    local_path: Path
    remote_url: str
    remote_db: str
    remote_table: str
    remote_user: str
    remote_password: str
    remote_timeout_s: float
    remote_verify_tls: bool
    fallback_path: Path
    local_tail_enabled: bool
    local_tail_max_bytes: int

    @classmethod
    def from_env(cls, local_path: Path) -> "SinkConfig":
        return cls(
            mode=(os.environ.get("SEQ_MEM_SINK_MODE", "auto").strip().lower() or "auto"),
            local_path=local_path.expanduser(),
            remote_url=(os.environ.get("SEQ_MEM_REMOTE_URL", "").strip()),
            remote_db=(os.environ.get("SEQ_MEM_REMOTE_DB", "seq").strip() or "seq"),
            remote_table=(os.environ.get("SEQ_MEM_REMOTE_TABLE", "mem_events").strip() or "mem_events"),
            remote_user=(os.environ.get("SEQ_MEM_REMOTE_USER", "").strip()),
            remote_password=(os.environ.get("SEQ_MEM_REMOTE_PASSWORD", "").strip()),
            remote_timeout_s=max(0.2, float(os.environ.get("SEQ_MEM_REMOTE_TIMEOUT_S", "2.5"))),
            remote_verify_tls=_env_bool("SEQ_MEM_REMOTE_VERIFY_TLS", True),
            fallback_path=Path(
                os.environ.get("SEQ_MEM_REMOTE_FALLBACK_PATH", str(DEFAULT_FALLBACK))
            ).expanduser(),
            local_tail_enabled=_env_bool("SEQ_MEM_LOCAL_TAIL_ENABLED", True),
            local_tail_max_bytes=max(0, int(os.environ.get("SEQ_MEM_LOCAL_TAIL_MAX_BYTES", str(50 * 1024 * 1024)))),
        )

    def effective_mode(self) -> str:
        if self.mode == "auto":
            return "remote" if self.remote_url else "file"
        return self.mode


class SeqMemSink:
    def __init__(self, cfg: SinkConfig) -> None:
        self.cfg = cfg

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        mode = self.cfg.effective_mode()
        if mode == "file":
            self._append_local(rows)
            return
        if mode == "off":
            return
        if mode == "dual":
            remote_ok = self._append_remote(rows)
            self._append_local(rows)
            if not remote_ok:
                self._append_fallback(rows)
            return
        if mode == "remote":
            remote_ok = self._append_remote(rows)
            if remote_ok and self.cfg.local_tail_enabled and self.cfg.local_tail_max_bytes > 0:
                self._append_local(rows)
                self._cap_file(self.cfg.local_path, self.cfg.local_tail_max_bytes)
            if not remote_ok:
                self._append_fallback(rows)
            return
        # Unknown mode: fail-safe to local append.
        self._append_local(rows)

    def _append_local(self, rows: list[dict[str, Any]]) -> None:
        self.cfg.local_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg.local_path.open("ab") as fh:
            fh.write(_jsonl_blob(rows))

    def _append_fallback(self, rows: list[dict[str, Any]]) -> None:
        self.cfg.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg.fallback_path.open("ab") as fh:
            fh.write(_jsonl_blob(rows))

    def _append_remote(self, rows: list[dict[str, Any]]) -> bool:
        if not self.cfg.remote_url:
            return False
        query = f"INSERT INTO {self.cfg.remote_db}.{self.cfg.remote_table} FORMAT JSONEachRow"
        url = self.cfg.remote_url.rstrip("/") + "/?" + urllib.parse.urlencode({"query": query})
        req = urllib.request.Request(url, data=_jsonl_blob(rows), method="POST")
        req.add_header("Content-Type", "application/x-ndjson")
        if self.cfg.remote_user:
            raw = f"{self.cfg.remote_user}:{self.cfg.remote_password}".encode("utf-8")
            req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.remote_timeout_s) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                if status >= 300:
                    return False
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return False

    def _cap_file(self, path: Path, max_bytes: int) -> None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size <= max_bytes:
            return
        keep = int(max_bytes * 0.9)
        if keep <= 0:
            path.write_text("", encoding="utf-8")
            return
        with path.open("rb") as fh:
            fh.seek(max(0, size - keep))
            data = fh.read()
        # Realign to next newline so we keep full JSON lines.
        nl = data.find(b"\n")
        if nl >= 0 and nl + 1 < len(data):
            data = data[nl + 1 :]
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as out:
            out.write(data)
        tmp.replace(path)


def append_seq_mem_rows(rows: list[dict[str, Any]], local_path: Path) -> None:
    sink = SeqMemSink(SinkConfig.from_env(local_path=local_path))
    sink.append_rows(rows)


def drain_fallback(
    *,
    local_path: Path,
    batch_size: int,
    max_batches: int,
) -> tuple[int, int]:
    cfg = SinkConfig.from_env(local_path=local_path)
    sink = SeqMemSink(cfg)
    mode = cfg.effective_mode()
    if mode not in {"remote", "dual"}:
        return 0, 0
    path = cfg.fallback_path
    if not path.exists():
        return 0, 0

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0, 0
    if not lines:
        return 0, 0

    sent = 0
    idx = 0
    batches = 0
    while idx < len(lines) and batches < max_batches:
        chunk = lines[idx : idx + batch_size]
        rows: list[dict[str, Any]] = []
        for line in chunk:
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        if not rows:
            idx += len(chunk)
            batches += 1
            continue
        if not sink._append_remote(rows):
            break
        sent += len(rows)
        idx += len(chunk)
        batches += 1

    remaining = lines[idx:]
    if remaining:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        tmp.replace(path)
    else:
        path.unlink(missing_ok=True)

    return sent, len(remaining)


def _status(local_path: Path) -> int:
    cfg = SinkConfig.from_env(local_path=local_path)
    print("seq mem sink")
    print(f"  mode={cfg.mode} (effective={cfg.effective_mode()})")
    print(f"  local_path={cfg.local_path}")
    print(f"  remote_url={cfg.remote_url or '<unset>'}")
    print(f"  remote_db={cfg.remote_db}")
    print(f"  remote_table={cfg.remote_table}")
    print(f"  remote_user={'<set>' if cfg.remote_user else '<unset>'}")
    print(f"  remote_timeout_s={cfg.remote_timeout_s}")
    print(f"  remote_verify_tls={cfg.remote_verify_tls}")
    print(f"  fallback_path={cfg.fallback_path}")
    print(f"  local_tail_enabled={cfg.local_tail_enabled}")
    print(f"  local_tail_max_bytes={cfg.local_tail_max_bytes}")
    if cfg.local_path.exists():
        print(f"  local_size_bytes={cfg.local_path.stat().st_size}")
    if cfg.fallback_path.exists():
        print(f"  fallback_size_bytes={cfg.fallback_path.stat().st_size}")
    return 0


def _drain(local_path: Path, batch_size: int, max_batches: int) -> int:
    sent, remaining = drain_fallback(local_path=local_path, batch_size=batch_size, max_batches=max_batches)
    print(f"drain sent_rows={sent} remaining_lines={remaining}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seq mem sink utility.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_status = sub.add_parser("status", help="Show sink config/status.")
    p_status.add_argument("--local-path", default=os.environ.get("SEQ_CH_MEM_PATH", str(DEFAULT_LOCAL_MEM)))
    p_drain = sub.add_parser("drain", help="Drain fallback queue to remote.")
    p_drain.add_argument("--local-path", default=os.environ.get("SEQ_CH_MEM_PATH", str(DEFAULT_LOCAL_MEM)))
    p_drain.add_argument("--batch-size", type=int, default=int(os.environ.get("SEQ_MEM_REMOTE_DRAIN_BATCH_SIZE", "1000")))
    p_drain.add_argument("--max-batches", type=int, default=int(os.environ.get("SEQ_MEM_REMOTE_DRAIN_MAX_BATCHES", "50")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    local_path = Path(args.local_path).expanduser()
    if args.command == "status":
        return _status(local_path)
    if args.command == "drain":
        return _drain(local_path, batch_size=max(1, int(args.batch_size)), max_batches=max(1, int(args.max_batches)))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
