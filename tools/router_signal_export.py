#!/usr/bin/env python3
"""Export high-signal Flow router training rows from seq_mem JSONEachRow."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM_PATH = str(
    Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser()
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        obj = json.loads(stripped)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{i} not object")
        out.append(obj)
    return out


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True))
            f.write("\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _bucket(row_id: str, seed: int) -> int:
    digest = hashlib.sha256(f"{seed}:{row_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _subject(row: dict[str, Any]) -> dict[str, Any]:
    subj = row.get("subject")
    if isinstance(subj, dict):
        return subj
    if isinstance(subj, str):
        try:
            parsed = json.loads(subj)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _reward(outcome: str) -> float:
    return {
        "success": 1.0,
        "partial": 0.55,
        "failure": 0.0,
        "wasted": -0.2,
    }.get(outcome, 0.0)


def _dedupe_id(row: dict[str, Any]) -> str:
    payload = {
        "schema_version": str(row.get("schema_version") or ""),
        "id": str(row.get("id") or ""),
        "ts_ms": int(row.get("ts_ms") or 0),
        "chosen_task": str(row.get("chosen_task") or ""),
        "task_executed": str(row.get("task_executed") or ""),
        "outcome": str(row.get("outcome") or ""),
        "manual_override_task": str(row.get("manual_override_task") or ""),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export flow router RL rows from seq_mem")
    parser.add_argument(
        "--seq-mem",
        default=DEFAULT_SEQ_MEM_PATH,
        help="path to seq_mem.jsonl",
    )
    parser.add_argument("--output-dir", default="/tmp/seq_router_signal")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-percent", type=int, default=10)
    parser.add_argument("--test-percent", type=int, default=10)
    parser.add_argument("--history-size", type=int, default=3)
    parser.add_argument("--max-age-hours", type=int, default=168)
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    max_age_ms = max(1, args.max_age_hours) * 3600 * 1000

    rows = _load_jsonl(Path(args.seq_mem).expanduser().resolve())

    decisions: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    overrides_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parse_errors = 0

    duplicate_decision_ids = 0
    duplicate_outcome_ids = 0
    for row in rows:
        try:
            name = str(row.get("name") or "")
            ts_ms = int(row.get("ts_ms") or 0)
            if ts_ms <= 0 or (now_ms - ts_ms) > max_age_ms:
                continue
            subj = _subject(row)
            decision_id = str(subj.get("decision_id") or "")
            if not decision_id:
                continue

            if name == "flow.router.decision.v1":
                if decision_id in decisions:
                    duplicate_decision_ids += 1
                decisions[decision_id] = {"row": row, "subject": subj}
            elif name == "flow.router.outcome.v1":
                if decision_id in outcomes:
                    duplicate_outcome_ids += 1
                outcomes[decision_id] = {"row": row, "subject": subj}
            elif name == "flow.router.override.v1":
                overrides_by_decision[decision_id].append({"row": row, "subject": subj})
        except Exception:
            parse_errors += 1

    # build per-session decision history for temporal context
    session_decisions: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for decision_id, payload in decisions.items():
        subj = payload["subject"]
        row = payload["row"]
        session_id = str(subj.get("session_id") or row.get("session_id") or "")
        ts_ms = int(row.get("ts_ms") or 0)
        chosen = str(subj.get("chosen_task") or "")
        if session_id and ts_ms > 0:
            session_decisions[session_id].append((ts_ms, decision_id, chosen))
    for session_id in list(session_decisions.keys()):
        session_decisions[session_id].sort(key=lambda x: x[0])

    joined_rows: list[dict[str, Any]] = []
    missing_outcome = 0
    for decision_id, payload in decisions.items():
        decision_subj = payload["subject"]
        decision_row = payload["row"]
        outcome_payload = outcomes.get(decision_id)
        if not outcome_payload:
            missing_outcome += 1
            continue
        outcome_subj = outcome_payload["subject"]

        session_id = str(decision_subj.get("session_id") or decision_row.get("session_id") or "")
        ts_ms = int(decision_row.get("ts_ms") or 0)
        chosen_task = str(decision_subj.get("chosen_task") or "")
        if not chosen_task:
            continue

        history: list[dict[str, Any]] = []
        timeline = session_decisions.get(session_id, [])
        idx = -1
        for i, item in enumerate(timeline):
            if item[1] == decision_id:
                idx = i
                break
        if idx >= 0:
            start = max(0, idx - max(0, args.history_size))
            for past_ts, past_id, past_task in timeline[start:idx]:
                history.append(
                    {
                        "t_minus": idx - timeline.index((past_ts, past_id, past_task)),
                        "decision_id": past_id,
                        "chosen_task": past_task,
                        "ts_ms": past_ts,
                    }
                )

        override_task = str(outcome_subj.get("manual_override_task") or "")
        if not override_task and overrides_by_decision.get(decision_id):
            override_task = str(overrides_by_decision[decision_id][-1]["subject"].get("override_task") or "")

        outcome = str(outcome_subj.get("outcome") or "")
        joined = {
            "schema_version": "flow_router_train_row_v1",
            "id": decision_id,
            "ts_ms": ts_ms,
            "source": str(decision_subj.get("source") or "flow"),
            "session_id": session_id,
            "project_fingerprint": decision_subj.get("project_fingerprint"),
            "project_path": decision_subj.get("project_path"),
            "git_branch": decision_subj.get("git_branch"),
            "git_commit": decision_subj.get("git_commit"),
            "user_hash": decision_subj.get("user_hash"),
            "user_intent": decision_subj.get("user_intent"),
            "context": decision_subj.get("context") if isinstance(decision_subj.get("context"), dict) else {},
            "candidates": decision_subj.get("candidates") if isinstance(decision_subj.get("candidates"), list) else [],
            "chosen_task": chosen_task,
            "confidence": float(decision_subj.get("confidence") or 0.0),
            "task_executed": str(outcome_subj.get("task_executed") or chosen_task),
            "outcome": outcome,
            "time_to_resolution_ms": int(outcome_subj.get("time_to_resolution_ms") or 0),
            "error_kind": str(outcome_subj.get("error_kind") or ""),
            "manual_override_task": override_task,
            "event_history": history,
            "reward": _reward(outcome),
        }
        joined["dedupe_id"] = _dedupe_id(joined)
        joined_rows.append(joined)

    joined_rows.sort(key=lambda r: int(r.get("ts_ms") or 0))
    deduped_joined_rows: list[dict[str, Any]] = []
    seen_dedupe: set[str] = set()
    dropped_duplicate_rows = 0
    for row in joined_rows:
        dedupe_id = str(row.get("dedupe_id") or "")
        if dedupe_id and dedupe_id in seen_dedupe:
            dropped_duplicate_rows += 1
            continue
        if dedupe_id:
            seen_dedupe.add(dedupe_id)
        deduped_joined_rows.append(row)
    joined_rows = deduped_joined_rows

    val_pct = max(0, min(int(args.val_percent), 100))
    test_pct = max(0, min(int(args.test_percent), 100 - val_pct))

    train_rows: list[dict[str, Any]] = []
    val_rows: list[dict[str, Any]] = []
    test_rows: list[dict[str, Any]] = []
    for row in joined_rows:
        b = _bucket(str(row["id"]), args.seed)
        if b < test_pct:
            test_rows.append(row)
        elif b < test_pct + val_pct:
            val_rows.append(row)
        else:
            train_rows.append(row)

    outcome_counts = Counter(str(r.get("outcome") or "unknown") for r in joined_rows)
    task_counts = Counter(str(r.get("chosen_task") or "unknown") for r in joined_rows)
    override_count = sum(1 for r in joined_rows if str(r.get("manual_override_task") or ""))
    linked_rate = (len(joined_rows) / max(1, len(decisions)))

    out_root = Path(args.output_dir).expanduser().resolve()
    _write_jsonl(out_root / "train.jsonl", train_rows)
    _write_jsonl(out_root / "val.jsonl", val_rows)
    _write_jsonl(out_root / "test.jsonl", test_rows)

    summary = {
        "schema_version": "flow_router_signal_export_v1",
        "generated_at_ms": now_ms,
        "input": {
            "seq_mem": str(Path(args.seq_mem).expanduser().resolve()),
            "max_age_hours": args.max_age_hours,
            "seed": args.seed,
            "val_percent": val_pct,
            "test_percent": test_pct,
            "history_size": args.history_size,
        },
        "counts": {
            "decisions": len(decisions),
            "outcomes": len(outcomes),
            "joined_rows": len(joined_rows),
            "missing_outcome": missing_outcome,
            "duplicate_decision_ids": duplicate_decision_ids,
            "duplicate_outcome_ids": duplicate_outcome_ids,
            "dropped_duplicate_rows": dropped_duplicate_rows,
            "overrides": override_count,
            "train_rows": len(train_rows),
            "val_rows": len(val_rows),
            "test_rows": len(test_rows),
            "parse_errors": parse_errors,
        },
        "quality": {
            "decision_outcome_link_rate": round(linked_rate, 6),
            "override_rate": round(override_count / max(1, len(joined_rows)), 6),
            "task_dominance": round((task_counts.most_common(1)[0][1] / max(1, len(joined_rows))) if task_counts else 0.0, 6),
        },
        "distributions": {
            "outcomes": dict(outcome_counts),
            "chosen_tasks": dict(task_counts),
        },
        "outputs": {
            "train": str(out_root / "train.jsonl"),
            "val": str(out_root / "val.jsonl"),
            "test": str(out_root / "test.jsonl"),
            "summary": str(out_root / "summary.json"),
        },
    }

    _write_json(out_root / "summary.json", summary)

    print(f"Exported flow router signal dataset: {out_root}")
    print(f"  decisions={len(decisions)} outcomes={len(outcomes)} joined={len(joined_rows)}")
    print(f"  train={len(train_rows)} val={len(val_rows)} test={len(test_rows)}")
    print(f"  link_rate={linked_rate:.3f} override_rate={summary['quality']['override_rate']:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
