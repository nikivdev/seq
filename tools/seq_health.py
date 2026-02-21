#!/usr/bin/env python3
"""Comprehensive seq capture health checks for RL training readiness."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SEQ_ROOT = Path("~/code/seq").expanduser()
DEFAULT_SEQ_BIN = DEFAULT_SEQ_ROOT / "cli/cpp/out/bin/seq"
DEFAULT_SEQ_MEM = Path(os.environ.get("SEQ_CH_MEM_PATH", "~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl")).expanduser()
DEFAULT_SEQ_TRACE = Path(os.environ.get("SEQ_CH_LOG_PATH", "~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl")).expanduser()


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str
    detail: str


def _read_pid(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    if not path.exists():
        return []
    size = path.stat().st_size
    if size <= 0:
        return []
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            _ = f.readline()
        data = f.read()
    if not data:
        return []
    return [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]


def _parse_subject(row: dict[str, Any]) -> dict[str, Any]:
    subj = row.get("subject")
    if isinstance(subj, dict):
        return subj
    if isinstance(subj, str):
        try:
            parsed = json.loads(subj)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _stream_ping(seq_bin: Path, socket_path: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [str(seq_bin), "--socket", socket_path, "ping"],
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout or proc.stderr).strip()
        return ok, detail or "no output"
    except Exception as exc:
        return False, str(exc)


def _dgram_probe(socket_path: str, timeout_s: float = 0.3) -> tuple[bool, str]:
    dgram_path = Path(socket_path + ".dgram")
    if not dgram_path.exists():
        return False, f"missing {dgram_path}"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.settimeout(timeout_s)
            sock.sendto(b"PING\n", str(dgram_path))
            return True, f"send_ok {dgram_path}"
        finally:
            sock.close()
    except Exception as exc:
        return False, f"{dgram_path} err={exc}"


def _launchctl_label_status(label: str) -> tuple[bool, str]:
    target = f"gui/{os.getuid()}/{label}"
    try:
        proc = subprocess.run(
            ["launchctl", "print", target],
            text=True,
            capture_output=True,
            timeout=3,
        )
        if proc.returncode == 0:
            return True, target
        detail = (proc.stderr or proc.stdout).strip()
        return False, detail or target
    except Exception as exc:
        return False, str(exc)


def _kickstart_launchd(label: str) -> tuple[bool, str]:
    target = f"gui/{os.getuid()}/{label}"
    try:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return True, target
        detail = (proc.stderr or proc.stdout).strip()
        return False, detail or target
    except Exception as exc:
        return False, str(exc)


def run_health(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - args.lookback_hours * 3600 * 1000

    results: list[CheckResult] = []

    mode = (os.environ.get("SEQ_CH_MODE") or "file").strip().lower()
    ch_host = (os.environ.get("SEQ_CH_HOST") or "127.0.0.1").strip()
    ch_port = int((os.environ.get("SEQ_CH_PORT") or "9000").strip())

    seq_bin = Path(args.seq_bin).expanduser().resolve()
    seq_mem = Path(args.seq_mem).expanduser().resolve()
    seq_trace = Path(args.seq_trace).expanduser().resolve()
    kar_probe_source_event_id = ""

    # 1) seq binary + seqd ping
    if not seq_bin.exists():
        results.append(CheckResult("seq_bin_exists", False, "critical", f"missing: {seq_bin}"))
    else:
        results.append(CheckResult("seq_bin_exists", True, "critical", str(seq_bin)))

    ping_ok = False
    ping_detail = "seq bin missing"
    if seq_bin.exists():
        ping_ok, ping_detail = _stream_ping(seq_bin, args.socket)

    dgram_ok, dgram_detail = _dgram_probe(args.socket)

    launchd_ok, launchd_detail = _launchctl_label_status(args.launchd_label)
    results.append(CheckResult("seqd_launchd_label", launchd_ok, "warning", launchd_detail))

    repair_attempted = False
    repair_ok = False
    repair_detail = "not_attempted"
    if args.repair_seqd and not (ping_ok or dgram_ok) and launchd_ok:
        repair_attempted = True
        repair_ok, repair_detail = _kickstart_launchd(args.launchd_label)
        if repair_ok:
            time.sleep(args.repair_wait_s)
            ping_ok, ping_detail = _stream_ping(seq_bin, args.socket)
            dgram_ok, dgram_detail = _dgram_probe(args.socket)
    if repair_attempted:
        results.append(CheckResult("seqd_repair_kickstart", repair_ok, "warning", repair_detail))

    # Final control-plane status after optional repair.
    stream_detail = ping_detail
    dgram_final_detail = dgram_detail
    if repair_attempted:
        stream_detail = f"{stream_detail}; repaired={repair_ok}"
        dgram_final_detail = f"{dgram_final_detail}; repaired={repair_ok}"
    results.append(CheckResult("seqd_stream_ping", ping_ok, "warning", stream_detail))
    results.append(CheckResult("seqd_dgram_socket", dgram_ok, "warning", dgram_final_detail))
    results.append(
        CheckResult(
            "seqd_control_plane",
            ping_ok or dgram_ok,
            "critical",
            f"stream_ping={ping_ok} dgram={dgram_ok} repair_attempted={repair_attempted}",
        )
    )

    # 2) daemon liveness
    daemon_paths = {
        "next_type_daemon": Path(os.environ.get("SEQ_NEXT_TYPE_PIDFILE", "~/.local/state/seq/next_type_key_capture.pid")).expanduser(),
        "kar_signal_daemon": Path(os.environ.get("SEQ_KAR_SIGNAL_PIDFILE", "~/.local/state/seq/kar_signal.pid")).expanduser(),
        "agent_qa_daemon": Path(os.environ.get("SEQ_AGENT_QA_PIDFILE", "~/.local/state/seq/agent_qa_ingest.pid")).expanduser(),
        "signal_watchdog_daemon": Path(os.environ.get("SEQ_SIGNAL_WATCHDOG_PIDFILE", "~/.local/state/seq/signal_watchdog.pid")).expanduser(),
    }
    for name, pidfile in daemon_paths.items():
        pid = _read_pid(pidfile)
        alive = _pid_alive(pid)
        results.append(CheckResult(name, alive, "critical", f"pid={pid} pidfile={pidfile}"))

    # 3) spool file existence and parse health
    for name, path in (("seq_mem_exists", seq_mem), ("seq_trace_exists", seq_trace)):
        exists = path.exists()
        results.append(CheckResult(name, exists, "critical", str(path)))

    mem_parse_errors = 0
    trace_parse_errors = 0
    mem_rows: list[dict[str, Any]] = []

    mem_lines = _tail_lines(seq_mem, args.tail_bytes)
    for line in mem_lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                mem_rows.append(row)
            else:
                mem_parse_errors += 1
        except Exception:
            mem_parse_errors += 1

    trace_lines = _tail_lines(seq_trace, args.tail_bytes)
    for line in trace_lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                trace_parse_errors += 1
        except Exception:
            trace_parse_errors += 1

    results.append(CheckResult("seq_mem_json_parse", mem_parse_errors == 0, "critical", f"errors={mem_parse_errors} lines={len(mem_lines)}"))
    results.append(CheckResult("seq_trace_json_parse", trace_parse_errors == 0, "critical", f"errors={trace_parse_errors} lines={len(trace_lines)}"))

    # 4) probe write+read verification
    probe_ok = False
    probe_id = f"health-{uuid.uuid4().hex[:16]}"
    probe_row = {
        "ts_ms": now_ms,
        "dur_us": 0,
        "ok": True,
        "session_id": "seq-health",
        "event_id": f"seq-health-{probe_id}",
        "content_hash": probe_id,
        "name": "seq.health.probe.v1",
        "subject": json.dumps({"probe_id": probe_id, "schema_version": "seq_health_probe_v1"}, ensure_ascii=True),
    }
    probe_error = ""
    try:
        seq_mem.parent.mkdir(parents=True, exist_ok=True)
        with seq_mem.open("a", encoding="utf-8") as f:
            f.write(json.dumps(probe_row, ensure_ascii=True) + "\n")
        recent = _tail_lines(seq_mem, min(args.tail_bytes, 2 * 1024 * 1024))
        probe_ok = any(probe_id in ln for ln in recent)
    except Exception as exc:
        probe_error = str(exc)
    results.append(CheckResult("probe_write_read_seq_mem", probe_ok, "critical", probe_error or probe_id))

    # 4b) Kar pipeline active probe (optional)
    kar_probe_ok = True
    kar_probe_detail = "disabled"
    if args.probe_kar_pipeline:
        kar_probe_source_event_id = f"seq-health-src-{uuid.uuid4().hex[:16]}"
        src_row = {
            "ts_ms": now_ms + 1,
            "dur_us": 0,
            "ok": True,
            "session_id": "seq-health",
            "event_id": kar_probe_source_event_id,
            "content_hash": kar_probe_source_event_id,
            "name": "cli.run.local",
            "subject": "__seq_health_probe__",
        }
        try:
            with seq_mem.open("a", encoding="utf-8") as f:
                f.write(json.dumps(src_row, ensure_ascii=True) + "\n")
            deadline = time.time() + (args.probe_wait_ms / 1000.0)
            found_intent = False
            found_outcome = False
            while time.time() < deadline:
                tail = _tail_lines(seq_mem, min(args.tail_bytes, 3 * 1024 * 1024))
                for ln in tail[-2000:]:
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "")
                    if name not in {"kar.intent.v1", "kar.outcome.v1"}:
                        continue
                    subj = _parse_subject(row)
                    if str(subj.get("source_event_id") or "") != kar_probe_source_event_id:
                        continue
                    if name == "kar.intent.v1":
                        found_intent = True
                    elif name == "kar.outcome.v1":
                        found_outcome = True
                if found_intent and found_outcome:
                    break
                time.sleep(0.15)
            kar_probe_ok = found_intent and found_outcome
            kar_probe_detail = f"source_event_id={kar_probe_source_event_id} intent={found_intent} outcome={found_outcome}"
        except Exception as exc:
            kar_probe_ok = False
            kar_probe_detail = str(exc)
    results.append(CheckResult("kar_pipeline_probe", kar_probe_ok, "critical", kar_probe_detail))

    # 5) signal coverage/freshness needed for training
    counts = {
        "next_type": 0,
        "kar_intent": 0,
        "kar_outcome": 0,
        "kar_override": 0,
        "agent_qa": 0,
        "router_decision": 0,
        "router_outcome": 0,
    }
    latest_ts: dict[str, int] = {}

    for row in mem_rows:
        ts = int(row.get("ts_ms") or 0)
        name = str(row.get("name") or "")

        if name.startswith("next_type."):
            latest_ts["next_type"] = max(latest_ts.get("next_type", 0), ts)
            if ts >= cutoff_ms:
                counts["next_type"] += 1
        elif name == "kar.intent.v1":
            latest_ts["kar_intent"] = max(latest_ts.get("kar_intent", 0), ts)
            if ts >= cutoff_ms:
                counts["kar_intent"] += 1
        elif name == "kar.outcome.v1":
            latest_ts["kar_outcome"] = max(latest_ts.get("kar_outcome", 0), ts)
            if ts >= cutoff_ms:
                counts["kar_outcome"] += 1
        elif name == "kar.override.v1":
            latest_ts["kar_override"] = max(latest_ts.get("kar_override", 0), ts)
            if ts >= cutoff_ms:
                counts["kar_override"] += 1
        elif name == "agent.qa.pair":
            latest_ts["agent_qa"] = max(latest_ts.get("agent_qa", 0), ts)
            if ts >= cutoff_ms:
                counts["agent_qa"] += 1
        elif name == "flow.router.decision.v1":
            latest_ts["router_decision"] = max(latest_ts.get("router_decision", 0), ts)
            if ts >= cutoff_ms:
                counts["router_decision"] += 1
        elif name == "flow.router.outcome.v1":
            latest_ts["router_outcome"] = max(latest_ts.get("router_outcome", 0), ts)
            if ts >= cutoff_ms:
                counts["router_outcome"] += 1

    coverage_expectations = {
        "next_type": args.min_next_type,
        "kar_intent": args.min_kar_intent,
        "kar_outcome": args.min_kar_outcome,
        "agent_qa": args.min_agent_qa,
    }

    for key, threshold in coverage_expectations.items():
        ok = counts.get(key, 0) >= threshold
        sev = "critical" if args.strict else "warning"
        results.append(CheckResult(f"coverage_{key}", ok, sev, f"count={counts.get(key,0)} threshold={threshold} lookback_h={args.lookback_hours}"))

    kar_link_rate = (counts["kar_outcome"] / counts["kar_intent"]) if counts["kar_intent"] > 0 else None
    kar_link_ok = True if kar_link_rate is None else kar_link_rate >= args.min_kar_link_rate
    results.append(CheckResult("kar_link_rate", kar_link_ok, "critical" if args.strict else "warning", f"rate={kar_link_rate} threshold={args.min_kar_link_rate}"))

    # 6) watchdog report freshness (if exists)
    report_path = Path(os.environ.get("SEQ_SIGNAL_WATCHDOG_REPORT", "~/.local/state/seq/signal_watchdog_report.json")).expanduser()
    watchdog_ok = False
    watchdog_detail = f"missing report: {report_path}"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            generated = int(report.get("generated_at_ms") or 0)
            age_ms = now_ms - generated if generated > 0 else None
            pass_flag = bool(report.get("overall_pass"))
            max_age_ms = args.watchdog_max_age_minutes * 60 * 1000
            watchdog_ok = pass_flag and age_ms is not None and age_ms <= max_age_ms
            watchdog_detail = f"overall_pass={pass_flag} age_ms={age_ms} max_age_ms={max_age_ms}"
        except Exception as exc:
            watchdog_detail = f"invalid report: {exc}"
    results.append(CheckResult("watchdog_report_fresh", watchdog_ok, "critical", watchdog_detail))

    # 7) ClickHouse reachability (mode-aware)
    ch_reachable = False
    ch_detail = ""
    try:
        with socket.create_connection((ch_host, ch_port), timeout=0.6):
            ch_reachable = True
    except Exception as exc:
        ch_detail = str(exc)

    if ch_reachable:
        detail = f"tcp_ok {ch_host}:{ch_port}"
        client = shutil.which("clickhouse-client")
        if client:
            proc = subprocess.run(
                [client, "--host", ch_host, "--port", str(ch_port), "--query", "SELECT 1"],
                text=True,
                capture_output=True,
                timeout=3,
            )
            if proc.returncode == 0:
                detail += " query_ok"
            else:
                detail += f" query_fail={proc.stderr.strip() or proc.stdout.strip()}"
        results.append(CheckResult("clickhouse_reachability", True, "warning", detail))
    else:
        if mode in {"native", "mirror"}:
            results.append(CheckResult("clickhouse_reachability", False, "critical", f"mode={mode} host={ch_host}:{ch_port} err={ch_detail}"))
        else:
            results.append(CheckResult("clickhouse_reachability", True, "warning", f"mode={mode} local_file_spool_only err={ch_detail}"))

    # Overall
    overall_ok = True
    for r in results:
        if r.severity == "critical" and not r.ok:
            overall_ok = False

    summary = {
        "schema_version": "seq_health_v1",
        "generated_at_ms": now_ms,
        "inputs": {
            "seq_mem": str(seq_mem),
            "seq_trace": str(seq_trace),
            "seq_bin": str(seq_bin),
            "socket": args.socket,
            "mode": mode,
            "clickhouse_host": ch_host,
            "clickhouse_port": ch_port,
            "lookback_hours": args.lookback_hours,
            "strict": args.strict,
        },
        "counts": counts,
        "latest_ts_ms": latest_ts,
        "kar_link_rate": kar_link_rate,
        "results": [r.__dict__ for r in results],
        "overall_ok": overall_ok,
    }

    if args.report_out:
        out = Path(args.report_out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return overall_ok, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Comprehensive seq RL capture health check")
    parser.add_argument("--seq-bin", default=str(DEFAULT_SEQ_BIN))
    parser.add_argument("--socket", default="/tmp/seqd.sock")
    parser.add_argument("--seq-mem", default=str(DEFAULT_SEQ_MEM))
    parser.add_argument("--seq-trace", default=str(DEFAULT_SEQ_TRACE))
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--tail-bytes", type=int, default=20 * 1024 * 1024)
    parser.add_argument("--min-next-type", type=int, default=1)
    parser.add_argument("--min-kar-intent", type=int, default=0)
    parser.add_argument("--min-kar-outcome", type=int, default=0)
    parser.add_argument("--min-agent-qa", type=int, default=1)
    parser.add_argument("--min-kar-link-rate", type=float, default=0.75)
    parser.add_argument("--watchdog-max-age-minutes", type=int, default=30)
    parser.add_argument("--launchd-label", default=os.environ.get("SEQD_LAUNCHD_LABEL", "dev.nikiv.seqd"))
    parser.add_argument("--repair-seqd", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--repair-wait-s", type=float, default=1.0)
    parser.add_argument("--probe-kar-pipeline", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--probe-wait-ms", type=int, default=3000)
    parser.add_argument("--strict", action="store_true", default=True)
    parser.add_argument("--no-strict", dest="strict", action="store_false")
    parser.add_argument(
        "--report-out",
        default=os.environ.get("SEQ_HEALTH_REPORT", "~/.local/state/seq/seq_health_report.json"),
    )
    args = parser.parse_args()

    ok, summary = run_health(args)
    print(f"seq health: {'PASS' if ok else 'FAIL'}")

    critical_failures = [r for r in summary["results"] if r["severity"] == "critical" and not r["ok"]]
    warnings = [r for r in summary["results"] if r["severity"] == "warning" and not r["ok"]]

    print(f"critical_failures={len(critical_failures)} warnings={len(warnings)}")
    print(f"counts={json.dumps(summary['counts'], ensure_ascii=True)}")
    print(f"kar_link_rate={summary['kar_link_rate']}")

    if critical_failures:
        print("critical failures:")
        for r in critical_failures:
            print(f"- {r['name']}: {r['detail']}")
    if warnings:
        print("warnings:")
        for r in warnings:
            print(f"- {r['name']}: {r['detail']}")

    print(f"report={Path(args.report_out).expanduser()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
