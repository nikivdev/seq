#!/usr/bin/env python3
"""Capture Zed editor context (window title, file, language, git) for RL training pairs.

Runs on a configurable poll interval, emitting `next_type.context.v1` events to
seq_mem. Never blocks keystroke capture — runs in its own process/thread.

Usage:
    python3 next_type_context_probe.py run      # foreground
    python3 next_type_context_probe.py once      # single probe and exit
    python3 next_type_context_probe.py start     # background daemon
    python3 next_type_context_probe.py stop
    python3 next_type_context_probe.py status
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

DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_STATE = str(Path("~/.local/state/seq/next_type_context_probe_state.json").expanduser())
DEFAULT_PIDFILE = str(Path("~/.local/state/seq/next_type_context_probe.pid").expanduser())
DEFAULT_LOG = str(Path("~/code/seq/cli/cpp/out/logs/next_type_context_probe.log").expanduser())

# File extension → language mapping (common coding languages)
EXT_TO_LANGUAGE: dict[str, str] = {
    "rs": "rust", "py": "python", "js": "javascript", "ts": "typescript",
    "tsx": "typescript", "jsx": "javascript", "go": "go", "rb": "ruby",
    "java": "java", "kt": "kotlin", "swift": "swift", "c": "c", "cpp": "cpp",
    "h": "c", "hpp": "cpp", "cs": "csharp", "lua": "lua", "zig": "zig",
    "toml": "toml", "yaml": "yaml", "yml": "yaml", "json": "json",
    "md": "markdown", "sh": "shell", "bash": "shell", "zsh": "shell",
    "html": "html", "css": "css", "scss": "scss", "sql": "sql",
    "mbt": "moonbit", "ml": "ocaml", "mli": "ocaml", "ex": "elixir",
    "exs": "elixir", "hs": "haskell", "nix": "nix", "vue": "vue",
    "svelte": "svelte",
}

# Zed window title pattern: "filename — project_name"
_TITLE_PATTERN = re.compile(r"^(.+?)\s*[—–-]\s*(.+?)(?:\s*[—–-]\s*Zed)?$")


@dataclass
class Config:
    seq_mem: Path
    state_path: Path
    pidfile: Path
    log_path: Path
    poll_seconds: float
    session_id: str | None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_frontmost_app() -> str | None:
    """Get bundle identifier of the frontmost application."""
    script = (
        'tell application "System Events" to get bundle identifier '
        'of first application process whose frontmost is true'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _get_window_title() -> str | None:
    """Get the title of the frontmost window."""
    script = (
        'tell application "System Events" to get name of first window '
        'of (first application process whose frontmost is true)'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _parse_zed_title(title: str) -> tuple[str, str]:
    """Parse Zed window title into (file_path, project_name)."""
    m = _TITLE_PATTERN.match(title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title.strip(), ""


def _infer_language(file_path: str) -> str:
    """Infer programming language from file extension."""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return EXT_TO_LANGUAGE.get(ext, "")


def _get_file_ext(file_path: str) -> str:
    if "." in file_path:
        return file_path.rsplit(".", 1)[-1].lower()
    return ""


def _get_git_info(project_name: str) -> tuple[str, int]:
    """Get git branch and count of changed files for a project.

    Searches common project root locations. Returns (branch, changed_files_count).
    """
    candidates = [
        Path.home() / "code" / project_name,
        Path.home() / "repos" / project_name,
        Path.home() / project_name,
    ]
    project_dir: Path | None = None
    for c in candidates:
        if (c / ".git").exists():
            project_dir = c
            break

    if project_dir is None:
        return "", 0

    branch = ""
    changed = 0
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "diff", "--stat", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
            # Last line is summary like " 3 files changed, ..." — count others
            changed = max(0, len(lines) - 1) if lines else 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return branch, changed


def probe_context(session_id: str) -> dict[str, Any] | None:
    """Run one context probe. Returns a context event dict or None if not in Zed."""
    app_id = _get_frontmost_app()
    if app_id != "dev.zed.Zed":
        return None

    title = _get_window_title()
    if not title:
        return None

    file_path, project_name = _parse_zed_title(title)
    file_ext = _get_file_ext(file_path)
    language = _infer_language(file_path)
    git_branch, git_changed_files = _get_git_info(project_name) if project_name else ("", 0)

    return {
        "schema_version": "next_type_context_v1",
        "ts_ms": _now_ms(),
        "app_id": app_id,
        "window_title": title,
        "file_path": file_path,
        "file_ext": file_ext,
        "language": language,
        "project_name": project_name,
        "git_branch": git_branch,
        "git_changed_files": git_changed_files,
        "session_id": session_id,
    }


def _emit_context_event(seq_mem: Path, context: dict[str, Any]) -> None:
    """Write a context event to seq_mem."""
    row = {
        "ts_ms": context["ts_ms"],
        "dur_us": 0,
        "ok": True,
        "session_id": context.get("session_id", "next-type-context"),
        "name": "next_type.context.v1",
        "subject": json.dumps(context, ensure_ascii=True),
    }
    seq_mem.parent.mkdir(parents=True, exist_ok=True)
    with seq_mem.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


class ContextProbe:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_requested = False
        self.probes_emitted = 0
        self.probes_skipped = 0
        self.last_context_hash = ""

    def log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] {message}", flush=True)

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def _context_hash(self, ctx: dict[str, Any]) -> str:
        """Dedup identical consecutive contexts."""
        return f"{ctx.get('window_title', '')}|{ctx.get('git_branch', '')}|{ctx.get('git_changed_files', 0)}"

    def run_once(self) -> int:
        session_id = self.cfg.session_id or "next-type-context"
        ctx = probe_context(session_id)
        if ctx is None:
            print("not in Zed (skipped)")
            return 0
        _emit_context_event(self.cfg.seq_mem, ctx)
        print(f"emitted: file={ctx['file_path']} lang={ctx['language']} project={ctx['project_name']}")
        return 0

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        session_id = self.cfg.session_id or "next-type-context"
        self.log(
            f"context probe started (seq_mem={self.cfg.seq_mem}, "
            f"poll_seconds={self.cfg.poll_seconds})"
        )

        while not self.stop_requested:
            ctx = probe_context(session_id)
            if ctx is None:
                self.probes_skipped += 1
            else:
                h = self._context_hash(ctx)
                if h != self.last_context_hash:
                    _emit_context_event(self.cfg.seq_mem, ctx)
                    self.last_context_hash = h
                    self.probes_emitted += 1
                else:
                    self.probes_skipped += 1

            time.sleep(self.cfg.poll_seconds)

        self.log(f"context probe stopped emitted={self.probes_emitted} skipped={self.probes_skipped}")
        return 0


# --- Daemon management (same pattern as key capture daemon) ---

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
    return "next_type_context_probe.py" in cmd and (" run " in cmd or cmd.endswith(" run"))


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
        "--seq-mem", str(cfg.seq_mem),
        "--state-path", str(cfg.state_path),
        "--pidfile", str(cfg.pidfile),
        "--log-path", str(cfg.log_path),
        "--poll-seconds", str(cfg.poll_seconds),
    ]
    if cfg.session_id:
        run_cmd.extend(["--session-id", cfg.session_id])

    with cfg.log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            run_cmd,
            stdout=log_fh, stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    _write_pid(cfg.pidfile, proc.pid)
    print(f"started next-type context probe: pid={proc.pid}")
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
    print(f"stopped next-type context probe: pid={pid}")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    pid = _read_pid(cfg.pidfile)
    alive = _is_pid_alive(pid)
    print(f"pidfile: {cfg.pidfile}")
    print(f"log: {cfg.log_path}")
    print(f"seq_mem: {cfg.seq_mem}")
    print(f"status: {'running' if alive else 'stopped'}")
    if pid > 0:
        print(f"pid: {pid}")
    return 0 if alive else 1


def cmd_preflight(cfg: Config, _args: argparse.Namespace) -> int:
    print("next-type context probe preflight")
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.seq_mem.parent.mkdir(parents=True, exist_ok=True)
    print("- OK: writable state/log/output directories")

    # Quick osascript test
    try:
        result = subprocess.run(
            ["osascript", "-e", "return 1"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            print("- OK: osascript accessible")
        else:
            print("- WARN: osascript returned non-zero")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("- WARN: osascript not available")

    print("- NOTE: grant Accessibility permission if macOS prompts for System Events")
    return 0


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seq-mem", default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM))
    parser.add_argument("--state-path", default=os.environ.get("SEQ_NEXT_TYPE_CONTEXT_STATE", DEFAULT_STATE))
    parser.add_argument("--pidfile", default=os.environ.get("SEQ_NEXT_TYPE_CONTEXT_PIDFILE", DEFAULT_PIDFILE))
    parser.add_argument("--log-path", default=os.environ.get("SEQ_NEXT_TYPE_CONTEXT_LOG", DEFAULT_LOG))
    parser.add_argument(
        "--poll-seconds", type=float,
        default=float(os.environ.get("SEQ_NEXT_TYPE_CONTEXT_POLL_SECONDS", "3.0")),
    )
    parser.add_argument("--session-id", default=os.environ.get("SEQ_NEXT_TYPE_SESSION_ID"))


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        seq_mem=Path(args.seq_mem).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        pidfile=Path(args.pidfile).expanduser().resolve(),
        log_path=Path(args.log_path).expanduser().resolve(),
        poll_seconds=max(1.0, float(args.poll_seconds)),
        session_id=str(args.session_id) if args.session_id else None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zed editor context probe for next-type RL")
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

    probe = ContextProbe(cfg)
    if args.command == "once":
        return probe.run_once()
    if args.command == "run":
        return probe.run_forever()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
