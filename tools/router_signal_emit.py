#!/usr/bin/env python3
"""Emit high-signal Flow router decision/outcome events into seq_mem JSONEachRow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM_PATH = str(
    Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser()
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _load_json_arg(raw: str | None) -> Any:
    if not raw:
        return None
    parsed = json.loads(raw)
    return parsed


def _append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(_safe_json(row))
        f.write("\n")


def _mk_row(*, ts_ms: int, session_id: str, name: str, subject_obj: dict[str, Any], ok: bool = True) -> dict[str, Any]:
    event_id = _sha(f"{name}|{session_id}|{subject_obj.get('decision_id','')}|{subject_obj.get('outcome','')}|{ts_ms}")
    return {
        "ts_ms": ts_ms,
        "dur_us": 0,
        "ok": bool(ok),
        "session_id": session_id,
        "event_id": event_id,
        "content_hash": _sha(_safe_json(subject_obj)),
        "name": name,
        "subject": _safe_json(subject_obj),
    }


def cmd_decision(args: argparse.Namespace) -> int:
    ts_ms = args.ts_ms if args.ts_ms is not None else _now_ms()
    session_id = args.session_id or os.environ.get("FLOW_ROUTER_SESSION_ID") or "flow-router"

    context = _load_json_arg(args.context_json)
    candidates = _load_json_arg(args.candidates_json)
    if candidates is None:
        candidates = []
    if context is None:
        context = {}

    subject = {
        "schema_version": "flow_router_decision_v1",
        "decision_id": args.decision_id,
        "source": args.source,
        "session_id": session_id,
        "user_hash": args.user_hash,
        "project_fingerprint": args.project_fingerprint,
        "project_path": args.project_path,
        "git_branch": args.git_branch,
        "git_commit": args.git_commit,
        "chosen_task": args.chosen_task,
        "confidence": args.confidence,
        "user_intent": args.user_intent,
        "candidates": candidates,
        "context": context,
    }

    row = _mk_row(
        ts_ms=ts_ms,
        session_id=session_id,
        name="flow.router.decision.v1",
        subject_obj=subject,
        ok=True,
    )
    _append_row(Path(args.seq_mem).expanduser().resolve(), row)
    print(f"emitted flow.router.decision.v1 decision_id={args.decision_id}")
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    ts_ms = args.ts_ms if args.ts_ms is not None else _now_ms()
    session_id = args.session_id or os.environ.get("FLOW_ROUTER_SESSION_ID") or "flow-router"
    extra = _load_json_arg(args.extra_json)
    if extra is None:
        extra = {}

    subject = {
        "schema_version": "flow_router_outcome_v1",
        "decision_id": args.decision_id,
        "source": args.source,
        "session_id": session_id,
        "outcome": args.outcome,
        "task_executed": args.task_executed,
        "time_to_resolution_ms": args.time_to_resolution_ms,
        "manual_override_task": args.manual_override_task,
        "error_kind": args.error_kind,
        "extra": extra,
    }

    ok_flag = args.outcome in {"success", "partial"}
    row = _mk_row(
        ts_ms=ts_ms,
        session_id=session_id,
        name="flow.router.outcome.v1",
        subject_obj=subject,
        ok=ok_flag,
    )
    _append_row(Path(args.seq_mem).expanduser().resolve(), row)
    print(f"emitted flow.router.outcome.v1 decision_id={args.decision_id} outcome={args.outcome}")
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    ts_ms = args.ts_ms if args.ts_ms is not None else _now_ms()
    session_id = args.session_id or os.environ.get("FLOW_ROUTER_SESSION_ID") or "flow-router"

    subject = {
        "schema_version": "flow_router_override_v1",
        "decision_id": args.decision_id,
        "source": args.source,
        "session_id": session_id,
        "original_task": args.original_task,
        "override_task": args.override_task,
        "reason": args.reason,
    }

    row = _mk_row(
        ts_ms=ts_ms,
        session_id=session_id,
        name="flow.router.override.v1",
        subject_obj=subject,
        ok=True,
    )
    _append_row(Path(args.seq_mem).expanduser().resolve(), row)
    print(f"emitted flow.router.override.v1 decision_id={args.decision_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit Flow router high-signal events into seq_mem")
    parser.add_argument(
        "--seq-mem",
        default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM_PATH),
        help="seq_mem JSONEachRow path",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    decision = sub.add_parser("decision", help="emit flow.router.decision.v1")
    decision.add_argument("--decision-id", required=True)
    decision.add_argument("--chosen-task", required=True)
    decision.add_argument("--confidence", type=float, required=True)
    decision.add_argument("--user-intent", default="")
    decision.add_argument("--candidates-json", help="JSON list of candidate tasks/scores")
    decision.add_argument("--context-json", help="JSON object with runtime context")
    decision.add_argument("--project-fingerprint", default="")
    decision.add_argument("--project-path", default="")
    decision.add_argument("--git-branch", default="")
    decision.add_argument("--git-commit", default="")
    decision.add_argument("--user-hash", default="")
    decision.add_argument("--session-id", default="")
    decision.add_argument("--source", default="flow")
    decision.add_argument("--ts-ms", type=int)
    decision.set_defaults(func=cmd_decision)

    outcome = sub.add_parser("outcome", help="emit flow.router.outcome.v1")
    outcome.add_argument("--decision-id", required=True)
    outcome.add_argument(
        "--outcome",
        required=True,
        choices=["success", "partial", "failure", "wasted"],
    )
    outcome.add_argument("--task-executed", required=True)
    outcome.add_argument("--time-to-resolution-ms", type=int, default=0)
    outcome.add_argument("--manual-override-task", default="")
    outcome.add_argument("--error-kind", default="")
    outcome.add_argument("--extra-json", help="JSON object with extra metrics")
    outcome.add_argument("--session-id", default="")
    outcome.add_argument("--source", default="flow")
    outcome.add_argument("--ts-ms", type=int)
    outcome.set_defaults(func=cmd_outcome)

    override = sub.add_parser("override", help="emit flow.router.override.v1")
    override.add_argument("--decision-id", required=True)
    override.add_argument("--original-task", required=True)
    override.add_argument("--override-task", required=True)
    override.add_argument("--reason", default="")
    override.add_argument("--session-id", default="")
    override.add_argument("--source", default="flow")
    override.add_argument("--ts-ms", type=int)
    override.set_defaults(func=cmd_override)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
