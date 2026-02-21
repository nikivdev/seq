#!/usr/bin/env python3
"""Export Kar RL signal rows from seq mem into train/val/test splits."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{i} row is not object")
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
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


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


def _bucket(row_id: str, seed: int) -> int:
    digest = hashlib.sha256(f"{seed}:{row_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


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
        "action_name": str(row.get("action_name") or ""),
        "outcome": str(row.get("outcome") or ""),
        "override_decision_id": str(row.get("override_decision_id") or ""),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Kar RL rows from seq_mem")
    parser.add_argument("--seq-mem", default=DEFAULT_SEQ_MEM_PATH)
    parser.add_argument("--output-dir", default="/tmp/seq_kar_signal")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-percent", type=int, default=10)
    parser.add_argument("--test-percent", type=int, default=10)
    parser.add_argument("--max-age-hours", type=int, default=168)
    parser.add_argument("--history-size", type=int, default=5)
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    max_age_ms = max(1, args.max_age_hours) * 3600 * 1000

    rows = _load_jsonl(Path(args.seq_mem).expanduser().resolve())

    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    overrides_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parse_errors = 0

    duplicate_intent_ids = 0
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

            if name == "kar.intent.v1":
                if decision_id in intents:
                    duplicate_intent_ids += 1
                intents[decision_id] = {"row": row, "subject": subj}
            elif name == "kar.outcome.v1":
                if decision_id in outcomes:
                    duplicate_outcome_ids += 1
                outcomes[decision_id] = {"row": row, "subject": subj}
            elif name == "kar.override.v1":
                overrides_by_decision[decision_id].append({"row": row, "subject": subj})
        except Exception:
            parse_errors += 1

    session_intents: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for decision_id, payload in intents.items():
        row = payload["row"]
        subj = payload["subject"]
        session_id = str(subj.get("session_id") or row.get("session_id") or "")
        ts_ms = int(row.get("ts_ms") or 0)
        action_name = str(subj.get("action_name") or "")
        if session_id and ts_ms > 0:
            session_intents[session_id].append((ts_ms, decision_id, action_name))
    for sid in list(session_intents.keys()):
        session_intents[sid].sort(key=lambda x: x[0])

    joined: list[dict[str, Any]] = []
    missing_outcome = 0

    for decision_id, payload in intents.items():
        intent_row = payload["row"]
        intent = payload["subject"]
        out = outcomes.get(decision_id)
        if not out:
            missing_outcome += 1
            continue

        out_subj = out["subject"]
        session_id = str(intent.get("session_id") or intent_row.get("session_id") or "")
        ts_ms = int(intent_row.get("ts_ms") or 0)
        action_name = str(intent.get("action_name") or "")
        if action_name.startswith("__seq_health_probe__"):
            continue

        history: list[dict[str, Any]] = []
        timeline = session_intents.get(session_id, [])
        idx = -1
        for i, item in enumerate(timeline):
            if item[1] == decision_id:
                idx = i
                break
        if idx >= 0:
            start = max(0, idx - max(0, args.history_size))
            hist_rows = timeline[start:idx]
            total = len(hist_rows)
            for j, (past_ts, past_id, past_action) in enumerate(hist_rows):
                history.append(
                    {
                        "t_minus": total - j,
                        "decision_id": past_id,
                        "action_name": past_action,
                        "ts_ms": past_ts,
                    }
                )

        override_to = ""
        if overrides_by_decision.get(decision_id):
            override_to = str(overrides_by_decision[decision_id][-1]["subject"].get("override_decision_id") or "")

        outcome = str(out_subj.get("outcome") or "")
        row = {
            "schema_version": "kar_train_row_v1",
            "id": decision_id,
            "ts_ms": ts_ms,
            "source": str(intent.get("source") or "kar"),
            "session_id": session_id,
            "action_type": str(intent.get("action_type") or ""),
            "action_name": action_name,
            "macro_name": str(intent.get("macro_name") or ""),
            "target_app": str(intent.get("target_app") or ""),
            "front_app": str(intent.get("front_app") or ""),
            "prev_app": str(intent.get("prev_app") or ""),
            "decision": str(intent.get("decision") or ""),
            "outcome": outcome,
            "latency_ms": int(out_subj.get("latency_ms") or 0),
            "observed_app": str(out_subj.get("observed_app") or ""),
            "reason": str(out_subj.get("reason") or ""),
            "override_decision_id": override_to,
            "event_history": history,
            "reward": _reward(outcome),
        }
        row["dedupe_id"] = _dedupe_id(row)
        joined.append(row)

    joined.sort(key=lambda r: int(r.get("ts_ms") or 0))
    deduped_joined: list[dict[str, Any]] = []
    seen_dedupe: set[str] = set()
    dropped_duplicate_rows = 0
    for row in joined:
        dedupe_id = str(row.get("dedupe_id") or "")
        if dedupe_id and dedupe_id in seen_dedupe:
            dropped_duplicate_rows += 1
            continue
        if dedupe_id:
            seen_dedupe.add(dedupe_id)
        deduped_joined.append(row)
    joined = deduped_joined

    val_pct = max(0, min(int(args.val_percent), 100))
    test_pct = max(0, min(int(args.test_percent), 100 - val_pct))

    train_rows: list[dict[str, Any]] = []
    val_rows: list[dict[str, Any]] = []
    test_rows: list[dict[str, Any]] = []

    for row in joined:
        b = _bucket(str(row["id"]), args.seed)
        if b < test_pct:
            test_rows.append(row)
        elif b < test_pct + val_pct:
            val_rows.append(row)
        else:
            train_rows.append(row)

    outcome_counts = Counter(str(r.get("outcome") or "unknown") for r in joined)
    action_counts = Counter(str(r.get("action_name") or "unknown") for r in joined)
    overrides = sum(1 for r in joined if str(r.get("override_decision_id") or ""))
    link_rate = len(joined) / max(1, len(intents))

    out_root = Path(args.output_dir).expanduser().resolve()
    _write_jsonl(out_root / "train.jsonl", train_rows)
    _write_jsonl(out_root / "val.jsonl", val_rows)
    _write_jsonl(out_root / "test.jsonl", test_rows)

    summary = {
        "schema_version": "kar_signal_export_v1",
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
            "intents": len(intents),
            "outcomes": len(outcomes),
            "joined_rows": len(joined),
            "missing_outcome": missing_outcome,
            "duplicate_intent_ids": duplicate_intent_ids,
            "duplicate_outcome_ids": duplicate_outcome_ids,
            "dropped_duplicate_rows": dropped_duplicate_rows,
            "overrides": overrides,
            "train_rows": len(train_rows),
            "val_rows": len(val_rows),
            "test_rows": len(test_rows),
            "parse_errors": parse_errors,
        },
        "quality": {
            "intent_outcome_link_rate": round(link_rate, 6),
            "override_rate": round(overrides / max(1, len(joined)), 6),
            "action_dominance": round((action_counts.most_common(1)[0][1] / max(1, len(joined))) if action_counts else 0.0, 6),
        },
        "distributions": {
            "outcomes": dict(outcome_counts),
            "actions": dict(action_counts),
        },
        "outputs": {
            "train": str(out_root / "train.jsonl"),
            "val": str(out_root / "val.jsonl"),
            "test": str(out_root / "test.jsonl"),
            "summary": str(out_root / "summary.json"),
        },
    }
    _write_json(out_root / "summary.json", summary)
    print(f"Exported Kar signal dataset: {out_root}")
    print(json.dumps(summary["counts"], ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
