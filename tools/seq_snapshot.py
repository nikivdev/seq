#!/usr/bin/env python3
"""
Generate a Markdown snapshot of recent seq/seqd collected context.
Sources (best-effort):
- /tmp/seqd.sock live queries (PING, PERF, AX_STATUS, AFK_STATUS, CTX_TAIL, MEM_*)
- ClickHouse JSONEachRow logs:
  - ~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl (or $SEQ_CH_MEM_PATH)
  - ~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl (or $SEQ_CH_LOG_PATH)
- OCR FTS sqlite db:
  - ~/Library/Application Support/seq/seqmem_fts.db
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _fmt_dt(dt: _dt.datetime) -> str:
    # RFC3339-ish, readable, includes tz offset.
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _read_lines(path: pathlib.Path) -> Iterable[str]:
    # Tail-ish read without loading huge files: read last ~8MB.
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    except Exception:
        return []

    max_tail = 8 * 1024 * 1024
    start = max(0, st.st_size - max_tail)
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
    except Exception:
        return []

    # Ensure we start on a newline boundary for JSONEachRow.
    try:
        if start != 0:
            nl = data.find(b"\n")
            if nl >= 0:
                data = data[nl + 1 :]
    except Exception:
        pass
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return []
    return text.splitlines()


def _json_each_row(path: pathlib.Path) -> Iterable[Dict[str, Any]]:
    for line in _read_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _maybe_sw_vers() -> str:
    try:
        out = subprocess.check_output(["sw_vers"], stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except Exception:
        return ""


def _query_sock(path: str, payload: bytes, timeout_s: float = 1.5) -> Optional[bytes]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout_s)
        s.connect(path)
        s.sendall(payload)
        try:
            s.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        chunks: List[bytes] = []
        while True:
            try:
                b = s.recv(1 << 20)
            except socket.timeout:
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def _query_seqd(sock_path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    # Keep this small and reliable.
    queries: List[Tuple[str, bytes]] = [
        ("PING", b"PING\n"),
        ("PERF", b"PERF\n"),
        ("AX_STATUS", b"AX_STATUS\n"),
        ("AFK_STATUS", b"AFK_STATUS\n"),
        ("MEM_METRICS", b"MEM_METRICS\n"),
        ("MEM_TAIL_50", b"MEM_TAIL 50\n"),
        ("CTX_TAIL_50", b"CTX_TAIL 50\n"),
    ]
    for k, q in queries:
        resp = _query_sock(sock_path, q)
        if resp is None:
            out[k] = "(unavailable)"
        else:
            # Keep only first ~8KB in snapshot.
            s = resp.decode("utf-8", "replace").strip()
            if len(s) > 8192:
                s = s[:8192] + "\n...(truncated)"
            out[k] = s
    return out


def _default_mem_log() -> pathlib.Path:
    env = os.environ.get("SEQ_CH_MEM_PATH")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path.home() / "repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl"


def _default_trace_log() -> pathlib.Path:
    env = os.environ.get("SEQ_CH_LOG_PATH")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path.home() / "repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl"


def _default_fts_db() -> pathlib.Path:
    return pathlib.Path.home() / "Library/Application Support/seq/seqmem_fts.db"


def _summarize_mem_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    windows: List[Dict[str, Any]] = []
    frames: List[Dict[str, Any]] = []
    afk: List[Dict[str, Any]] = []

    for r in rows:
        name = str(r.get("name") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1

        if name == "ctx.window":
            subject = str(r.get("subject") or "")
            title, bundle_id, url = "", "", ""
            parts = subject.split("\t")
            if len(parts) >= 1:
                title = parts[0]
            if len(parts) >= 2:
                bundle_id = parts[1]
            if len(parts) >= 3:
                url = parts[2]
            windows.append(
                {
                    "ts_ms": _safe_int(r.get("ts_ms")),
                    "dur_us": _safe_int(r.get("dur_us")),
                    "ok": bool(r.get("ok", True)),
                    "title": title,
                    "bundle_id": bundle_id,
                    "url": url,
                }
            )
        elif name == "ctx.frame":
            subject = str(r.get("subject") or "")
            app, title, path = "", "", ""
            parts = subject.split("\t")
            if len(parts) >= 1:
                app = parts[0]
            if len(parts) >= 2:
                title = parts[1]
            if len(parts) >= 3:
                path = parts[2]
            frames.append(
                {
                    "ts_ms": _safe_int(r.get("ts_ms")),
                    "ok": bool(r.get("ok", True)),
                    "app": app,
                    "title": title,
                    "path": path,
                }
            )
        elif name.startswith("afk."):
            afk.append(
                {
                    "name": name,
                    "ts_ms": _safe_int(r.get("ts_ms")),
                    "dur_us": _safe_int(r.get("dur_us")),
                    "ok": bool(r.get("ok", True)),
                    "subject": str(r.get("subject") or ""),
                }
            )

    windows.sort(key=lambda x: x["ts_ms"])
    frames.sort(key=lambda x: x["ts_ms"])
    afk.sort(key=lambda x: x["ts_ms"])

    return {
        "counts": counts,
        "windows": windows,
        "frames": frames,
        "afk": afk,
    }


def _ts_ms_to_dt(ts_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.datetime.now().astimezone().tzinfo)


def _ts_us_to_dt(ts_us: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts_us / 1_000_000.0, tz=_dt.datetime.now().astimezone().tzinfo)


def _fts_summary(db_path: pathlib.Path, cutoff_ms: int) -> Dict[str, Any]:
    if not db_path.exists():
        return {"present": False}
    try:
        con = sqlite3.connect(str(db_path))
    except Exception as e:
        return {"present": True, "error": str(e)}
    try:
        cur = con.cursor()
        cur.execute("select count(*) from frame_text where ts_ms >= ?", (cutoff_ms,))
        n = int(cur.fetchone()[0] or 0)
        cur.execute(
            "select app_name, count(*) as c from frame_text where ts_ms >= ? group by app_name order by c desc limit 8",
            (cutoff_ms,),
        )
        top_apps = [(str(a or ""), int(c or 0)) for (a, c) in cur.fetchall()]
        cur.execute(
            "select ts_ms, app_name, window_title, ocr_text from frame_text where ts_ms >= ? order by ts_ms desc limit 10",
            (cutoff_ms,),
        )
        last = []
        for ts_ms, app, title, ocr in cur.fetchall():
            o = (ocr or "").replace("\r", " ").replace("\n", " ").strip()
            if len(o) > 180:
                o = o[:180] + "..."
            last.append(
                {
                    "ts_ms": int(ts_ms or 0),
                    "app": str(app or ""),
                    "title": str(title or ""),
                    "ocr": o,
                }
            )
        return {"present": True, "rows": n, "top_apps": top_apps, "last": last}
    except Exception as e:
        return {"present": True, "error": str(e)}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _summarize_trace_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    last: List[Dict[str, Any]] = []
    for r in rows:
        name = str(r.get("name") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
        last.append(
            {
                "ts_us": _safe_int(r.get("ts_us")),
                "level": str(r.get("level") or ""),
                "kind": str(r.get("kind") or ""),
                "name": name,
                "message": str(r.get("message") or ""),
            }
        )
    last.sort(key=lambda x: x["ts_us"])
    return {"counts": counts, "last": last}


def _md_escape(s: str) -> str:
    # Keep it simple; code blocks are used for raw anyway.
    return s.replace("\r", "").strip()


def _write_md(
    out_path: pathlib.Path,
    *,
    now: _dt.datetime,
    cutoff: _dt.datetime,
    hours: float,
    sw_vers: str,
    seqd_live: Optional[Dict[str, str]],
    mem_log: pathlib.Path,
    mem_rows: List[Dict[str, Any]],
    mem_summary: Dict[str, Any],
    trace_log: pathlib.Path,
    trace_rows: List[Dict[str, Any]],
    trace_summary: Dict[str, Any],
    fts_db: pathlib.Path,
    fts: Dict[str, Any],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def w(line: str = "") -> None:
        f.write(line + "\n")

    with out_path.open("w", encoding="utf-8") as f:
        w(f"# seq snapshot (last {hours:g}h)")
        w("")
        w(f"- generated: `{_fmt_dt(now)}`")
        w(f"- window: `{_fmt_dt(cutoff)}` .. `{_fmt_dt(now)}`")
        w("")

        if sw_vers:
            w("## system")
            w("```")
            w(sw_vers)
            w("```")
            w("")

        w("## sources")
        w(f"- seqd socket: `/tmp/seqd.sock`")
        w(f"- mem log: `{mem_log}`")
        w(f"- trace log: `{trace_log}`")
        w(f"- ocr fts db: `{fts_db}`")
        w("")

        w("## seqd (live)")
        if seqd_live is None:
            w("_unavailable (socket connect failed)_")
        else:
            # Show a few key responses.
            for k in ["PING", "PERF", "AX_STATUS", "AFK_STATUS"]:
                w(f"### {k}")
                w("```")
                w(seqd_live.get(k, "(missing)"))
                w("```")
                w("")
        w("")

        w("## mem events (seq_mem.jsonl)")
        w(f"- rows in window: **{len(mem_rows)}**")
        counts = mem_summary.get("counts", {})
        if counts:
            w("")
            w("### counts by name")
            for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:40]:
                w(f"- `{name}`: {n}")
        w("")

        windows = mem_summary.get("windows", [])
        if windows:
            w("### window context (ctx.window)")
            w(f"- events: **{len(windows)}**")
            w("")
            w("| time | dur | bundle_id | title | url |")
            w("| --- | --- | --- | --- | --- |")
            for ev in windows[-80:]:
                ts = _fmt_dt(_ts_ms_to_dt(ev["ts_ms"]))
                dur_s = f'{ev["dur_us"] / 1_000_000.0:.1f}s'
                bid = _md_escape(ev.get("bundle_id", ""))
                title = _md_escape(ev.get("title", ""))
                url = _md_escape(ev.get("url", ""))
                w(f"| `{ts}` | `{dur_s}` | `{bid}` | {title} | {url} |")
            w("")

        afk = mem_summary.get("afk", [])
        if afk:
            w("### afk (afk.*)")
            w(f"- events: **{len(afk)}**")
            for ev in afk[-40:]:
                ts = _fmt_dt(_ts_ms_to_dt(ev["ts_ms"]))
                dur_s = f"{ev.get('dur_us', 0) / 1_000_000.0:.1f}s"
                w(f"- `{ts}` `{ev['name']}` dur={dur_s} subject={_md_escape(ev.get('subject',''))}")
            w("")

        frames = mem_summary.get("frames", [])
        if frames:
            w("### frames (ctx.frame)")
            w(f"- events: **{len(frames)}**")
            total_bytes = 0
            missing = 0
            for ev in frames:
                p = ev.get("path") or ""
                if not p:
                    continue
                try:
                    st = pathlib.Path(p).stat()
                    total_bytes += int(st.st_size)
                except Exception:
                    missing += 1
            if total_bytes:
                w(f"- total size (existing files): **{total_bytes/1024/1024:.1f} MiB**")
            if missing:
                w(f"- missing paths: **{missing}**")
            w("")
            w("| time | app | title | path |")
            w("| --- | --- | --- | --- |")
            for ev in frames[-40:]:
                ts = _fmt_dt(_ts_ms_to_dt(ev["ts_ms"]))
                app = _md_escape(ev.get("app", ""))
                title = _md_escape(ev.get("title", ""))
                p = _md_escape(ev.get("path", ""))
                w(f"| `{ts}` | {app} | {title} | `{p}` |")
            w("")

        w("## ocr / fts (seqmem_fts.db)")
        if not fts.get("present"):
            w("_missing_")
            w("")
        elif "error" in fts:
            w(f"_error: `{fts['error']}`_")
            w("")
        else:
            w(f"- rows in window: **{fts.get('rows', 0)}**")
            top_apps = fts.get("top_apps") or []
            if top_apps:
                w("")
                w("### top apps")
                for app, n in top_apps:
                    w(f"- {app}: {n}")
            last = fts.get("last") or []
            if last:
                w("")
                w("### last ocr rows (truncated)")
                for ev in last:
                    ts = _fmt_dt(_ts_ms_to_dt(ev["ts_ms"]))
                    w(f"- `{ts}` {ev.get('app','')} | {ev.get('title','')}")
                    if ev.get("ocr"):
                        w(f"  - `{_md_escape(ev['ocr'])}`")
            w("")

        w("## trace events (seq_trace.jsonl)")
        w(f"- rows in window: **{len(trace_rows)}**")
        tcounts = trace_summary.get("counts", {})
        if tcounts:
            w("")
            w("### counts by name")
            for name, n in sorted(tcounts.items(), key=lambda kv: (-kv[1], kv[0]))[:30]:
                w(f"- `{name}`: {n}")
        w("")
        last = trace_summary.get("last", [])
        if last:
            w("### last events")
            w("```")
            for ev in last[-60:]:
                ts = _fmt_dt(_ts_us_to_dt(ev["ts_us"]))
                level = ev.get("level", "")
                name = ev.get("name", "")
                msg = (ev.get("message", "") or "").replace("\n", " ").strip()
                if len(msg) > 240:
                    msg = msg[:240] + "..."
                w(f"{ts} {level} {name} {msg}")
            w("```")
            w("")

        # Include raw tails for debugging.
        w("## raw tails")
        w("### seq_mem.jsonl (last 20 lines in window tail buffer)")
        w("```")
        for r in mem_rows[-20:]:
            try:
                w(json.dumps(r, ensure_ascii=False))
            except Exception:
                pass
        w("```")
        w("")
        w("### seq_trace.jsonl (last 20 lines in window tail buffer)")
        w("```")
        for r in trace_rows[-20:]:
            try:
                w(json.dumps(r, ensure_ascii=False))
            except Exception:
                pass
        w("```")
        w("")


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Generate a Markdown snapshot of seq collected context.")
    ap.add_argument("--hours", type=float, default=6.0, help="Lookback window in hours (default: 6).")
    ap.add_argument("--out", type=str, default="", help="Output .md path (default: cli/cpp/out/snapshots/...).")
    ap.add_argument("--no-socket", action="store_true", help="Do not query /tmp/seqd.sock.")
    args = ap.parse_args(argv)

    now = _now()
    cutoff = now - _dt.timedelta(seconds=float(args.hours) * 3600.0)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_us = int(cutoff.timestamp() * 1_000_000)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    if args.out:
        out_path = pathlib.Path(args.out).expanduser()
    else:
        snap_dir = repo_root / "cli/cpp/out/snapshots"
        out_name = f"seq_snapshot_{now.strftime('%Y%m%d_%H%M%S')}_last{args.hours:g}h.md"
        out_path = snap_dir / out_name

    sw_vers = _maybe_sw_vers()

    seqd_live: Optional[Dict[str, str]] = None
    if not args.no_socket:
        sock_path = "/tmp/seqd.sock"
        if os.path.exists(sock_path):
            seqd_live = _query_seqd(sock_path)

    mem_log = _default_mem_log()
    mem_rows: List[Dict[str, Any]] = []
    for r in _json_each_row(mem_log):
        ts = _safe_int(r.get("ts_ms"))
        if ts >= cutoff_ms:
            mem_rows.append(r)
    mem_rows.sort(key=lambda x: _safe_int(x.get("ts_ms")))
    mem_summary = _summarize_mem_rows(mem_rows)

    trace_log = _default_trace_log()
    trace_rows: List[Dict[str, Any]] = []
    for r in _json_each_row(trace_log):
        ts = _safe_int(r.get("ts_us"))
        if ts >= cutoff_us:
            trace_rows.append(r)
    trace_rows.sort(key=lambda x: _safe_int(x.get("ts_us")))
    trace_summary = _summarize_trace_rows(trace_rows)

    fts_db = _default_fts_db()
    fts = _fts_summary(fts_db, cutoff_ms)

    _write_md(
        out_path,
        now=now,
        cutoff=cutoff,
        hours=float(args.hours),
        sw_vers=sw_vers,
        seqd_live=seqd_live,
        mem_log=mem_log,
        mem_rows=mem_rows,
        mem_summary=mem_summary,
        trace_log=trace_log,
        trace_rows=trace_rows,
        trace_summary=trace_summary,
        fts_db=fts_db,
        fts=fts,
    )

    # Print the output path for easy scripting.
    sys.stdout.write(str(out_path) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
