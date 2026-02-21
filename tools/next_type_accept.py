#!/usr/bin/env python3
"""Accept the latest next-type suggestion with low latency via seq RPC.

Intended for hotkey binding (e.g., Tab-like completion).
Reads latest suggestion from predictor state and types only the suggested suffix.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

DEFAULT_STATE = str(Path("~/.local/state/seq/next_type_predictor_state.json").expanduser())
DEFAULT_SEQ_MEM = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def append_event(seq_mem: Path, name: str, subject_obj: dict[str, Any], ok: bool = True) -> None:
    row = {
        "ts_ms": int(time.time() * 1000),
        "dur_us": 0,
        "ok": bool(ok),
        "session_id": "next-type-predictor",
        "name": name,
        "subject": json.dumps(subject_obj, ensure_ascii=True),
    }
    seq_mem.parent.mkdir(parents=True, exist_ok=True)
    with seq_mem.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def send_seq_rpc(socket_path: str, request: dict[str, Any], timeout_s: float = 0.5) -> dict[str, Any]:
    payload = (json.dumps(request, ensure_ascii=True) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        sock.connect(socket_path)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in chunk or len(data) > 1_000_000:
                break
    if not data:
        raise RuntimeError("empty_rpc_response")
    line = data.split(b"\n", 1)[0]
    try:
        decoded = json.loads(line.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise RuntimeError(f"invalid_rpc_json: {exc}")
    if not isinstance(decoded, dict):
        raise RuntimeError("invalid_rpc_shape")
    return decoded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Accept latest next-type suggestion")
    parser.add_argument(
        "--state",
        default=os.environ.get("SEQ_NEXT_TYPE_PREDICTOR_STATE", DEFAULT_STATE),
        help="Predictor state JSON path.",
    )
    parser.add_argument(
        "--seq-mem",
        default=os.environ.get("SEQ_CH_MEM_PATH", DEFAULT_SEQ_MEM),
        help="seq_mem JSONL path for emit audit rows.",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("SEQ_SOCKET_PATH", "/tmp/seqd.sock"),
        help="seqd unix socket path.",
    )
    parser.add_argument(
        "--max-age-ms",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_ACCEPT_MAX_AGE_MS", "15000")),
        help="Reject stale suggestions older than this age.",
    )
    parser.add_argument(
        "--require-score",
        type=int,
        default=int(os.environ.get("SEQ_NEXT_TYPE_ACCEPT_MIN_SCORE", "1")),
        help="Minimum suggestion score required.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print suggestion without sending seq RPC.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state).expanduser().resolve()
    seq_mem = Path(args.seq_mem).expanduser().resolve()

    if not state_path.exists():
        print(f"no_state: {state_path}")
        return 1

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"invalid_state_json: {exc}")
        return 1

    if not isinstance(payload, dict):
        print("invalid_state_shape")
        return 1

    latest = payload.get("latest_suggestion")
    if not isinstance(latest, dict) or not latest:
        print("no_active_suggestion")
        return 1

    suggestion_id = str(latest.get("id") or "")
    suggestion_text = str(latest.get("suggestion_text") or "")
    created_at_ms = int(latest.get("created_at_ms") or 0)
    expires_at_ms = int(latest.get("expires_at_ms") or 0)
    score = int(latest.get("score") or 0)

    now_ms = int(time.time() * 1000)
    if not suggestion_text:
        print("empty_suggestion")
        return 1
    if score < int(args.require_score):
        print(f"score_too_low: {score} < {args.require_score}")
        return 1
    if created_at_ms and (now_ms - created_at_ms) > int(args.max_age_ms):
        print(f"stale_suggestion: age_ms={now_ms - created_at_ms}")
        return 1
    if expires_at_ms and now_ms > expires_at_ms:
        print("expired_suggestion")
        return 1

    if args.dry_run:
        print(f"ready: id={suggestion_id} text={suggestion_text!r} score={score}")
        return 0

    request = {
        "op": "type_text",
        "request_id": f"next-type-accept-{now_ms}",
        "args": {"text": suggestion_text},
    }

    try:
        response = send_seq_rpc(args.socket, request)
    except Exception as exc:
        append_event(
            seq_mem,
            "next_type.suggestion_accept.v1",
            {
                "schema_version": "next_type_suggestion_accept_v1",
                "suggestion_id": suggestion_id,
                "accepted": False,
                "reason": f"rpc_error:{exc}",
            },
            ok=False,
        )
        print(f"rpc_error: {exc}")
        return 1

    ok = bool(response.get("ok"))
    if not ok:
        err = str(response.get("error") or "rpc_not_ok")
        append_event(
            seq_mem,
            "next_type.suggestion_accept.v1",
            {
                "schema_version": "next_type_suggestion_accept_v1",
                "suggestion_id": suggestion_id,
                "accepted": False,
                "reason": err,
            },
            ok=False,
        )
        print(f"accept_failed: {err}")
        return 1

    latest["accepted_at_ms"] = now_ms
    latest["accepted"] = True
    payload["latest_suggestion"] = latest
    state_path.write_text(safe_json(payload) + "\n", encoding="utf-8")

    append_event(
        seq_mem,
        "next_type.suggestion_accept.v1",
        {
            "schema_version": "next_type_suggestion_accept_v1",
            "suggestion_id": suggestion_id,
            "accepted": True,
            "suggestion_text": suggestion_text,
            "score": score,
        },
        ok=True,
    )
    print(f"accepted: id={suggestion_id} text={suggestion_text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
