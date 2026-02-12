#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import queue
import re
import signal
import sys
import time
from dataclasses import dataclass


def default_trace_path() -> str:
    p = os.environ.get("SEQ_CH_LOG_PATH")
    if p:
        return p
    return os.path.expanduser("~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl")


def default_mem_path() -> str:
    p = os.environ.get("SEQ_CH_MEM_PATH")
    if p:
        return p
    return os.path.expanduser("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl")


def _tail_lines(path: str, max_lines: int, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return []

    size = st.st_size
    take = min(size, max_bytes)
    with open(path, "rb") as f:
        f.seek(size - take)
        data = f.read(take)
    lines = data.splitlines()
    # If we cut mid-line, drop the first partial line.
    if take < size and lines:
        lines = lines[1:]
    out = [b.decode("utf-8", errors="replace") for b in lines[-max_lines:]]
    return out


@dataclass(frozen=True)
class Event:
    ts_ms: int
    src: str  # "trace" | "mem"
    name: str
    obj: dict


def parse_trace(line: str) -> Event | None:
    try:
        o = json.loads(line)
    except Exception:
        return None
    ts_us = o.get("ts_us")
    if not isinstance(ts_us, int):
        return None
    name = o.get("name") or ""
    return Event(ts_ms=ts_us // 1000, src="trace", name=str(name), obj=o)


def parse_mem(line: str) -> Event | None:
    try:
        o = json.loads(line)
    except Exception:
        return None
    ts_ms = o.get("ts_ms")
    if not isinstance(ts_ms, int):
        return None
    name = o.get("name") or ""
    return Event(ts_ms=ts_ms, src="mem", name=str(name), obj=o)


def fmt_ts(ts_ms: int) -> str:
    t = dt.datetime.fromtimestamp(ts_ms / 1000.0)
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def human_line(e: Event) -> str:
    if e.src == "trace":
        o = e.obj
        level = o.get("level", "")
        kind = o.get("kind", "")
        pid = o.get("pid", "")
        tid = o.get("tid", "")
        msg = o.get("message", "")
        dur_us = o.get("dur_us", "")
        return f"{fmt_ts(e.ts_ms)} trace {level}/{kind} pid={pid} tid={tid} {e.name} dur_us={dur_us} {msg}"
    else:
        o = e.obj
        ok = o.get("ok", "")
        dur_us = o.get("dur_us", "")
        subj = o.get("subject", "")
        sid = o.get("session_id", "")
        return f"{fmt_ts(e.ts_ms)} mem ok={ok} dur_us={dur_us} sid={sid} {e.name} {subj}"


def follow_file(path: str, src: str, outq: "queue.Queue[Event]") -> None:
    parse = parse_trace if src == "trace" else parse_mem
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    # Follow: start at end.
    f.seek(0, os.SEEK_END)
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.05)
            continue
        line = line.strip()
        if not line:
            continue
        ev = parse(line)
        if ev is None:
            continue
        outq.put(ev)


def main() -> int:
    # Don't crash when the consumer (eg `head`) closes the pipe.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Tail seq trace + mem logs (human or json)")
    ap.add_argument("--trace", default=default_trace_path(), help="seq_trace.jsonl path")
    ap.add_argument("--mem", default=default_mem_path(), help="seq_mem.jsonl path")
    ap.add_argument("--last", type=int, default=200, help="print last N from each file (default 200)")
    ap.add_argument("--json", action="store_true", help="output JSON objects (one per line)")
    ap.add_argument("--follow", action="store_true", help="follow new entries")
    ap.add_argument("--grep", default="", help="substring/regex filter (applies to formatted line)")
    args = ap.parse_args()

    rx = None
    if args.grep:
        try:
            rx = re.compile(args.grep)
        except re.error:
            rx = re.compile(re.escape(args.grep))

    events: list[Event] = []
    for line in _tail_lines(args.trace, args.last):
        ev = parse_trace(line)
        if ev:
            events.append(ev)
    for line in _tail_lines(args.mem, args.last):
        ev = parse_mem(line)
        if ev:
            events.append(ev)
    events.sort(key=lambda e: (e.ts_ms, e.src))

    for e in events:
        if args.json:
            o = {"ts_ms": e.ts_ms, "src": e.src, "name": e.name, "raw": e.obj}
            s = json.dumps(o, separators=(",", ":"), ensure_ascii=True)
        else:
            s = human_line(e)
        if rx and not rx.search(s):
            continue
        try:
            sys.stdout.write(s + "\n")
        except BrokenPipeError:
            return 0
    sys.stdout.flush()

    if not args.follow:
        return 0

    q: "queue.Queue[Event]" = queue.Queue()
    import threading

    t1 = threading.Thread(target=follow_file, args=(args.trace, "trace", q), daemon=True)
    t2 = threading.Thread(target=follow_file, args=(args.mem, "mem", q), daemon=True)
    t1.start()
    t2.start()

    while True:
        e = q.get()
        if args.json:
            o = {"ts_ms": e.ts_ms, "src": e.src, "name": e.name, "raw": e.obj}
            s = json.dumps(o, separators=(",", ":"), ensure_ascii=True)
        else:
            s = human_line(e)
        if rx and not rx.search(s):
            continue
        try:
            sys.stdout.write(s + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
