#!/usr/bin/env python3
"""Export z-mode RL signal rows from seq_mem into train/val/test splits."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_SEQ_MEM_PATH = str(Path("~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl").expanduser())
DEFAULT_OUTPUT_DIR = str(Path("~/repos/laude-institute/harbor/data/zmode_signal/latest").expanduser())


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


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True))
            f.write("\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _bucket(row_id: str, seed: int) -> int:
    digest = hashlib.sha256(f"{seed}:{row_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _dedupe_id(row: dict[str, Any]) -> str:
    payload = {
        "schema_version": str(row.get("schema_version") or ""),
        "id": str(row.get("id") or ""),
        "ts_ms": int(row.get("ts_ms") or 0),
        "mapping_epoch_id": str(row.get("mapping_epoch_id") or ""),
        "candidate_id": str(row.get("candidate_id") or ""),
        "action_name": str(row.get("action_name") or ""),
        "outcome": str(row.get("outcome") or ""),
        "quick_override": bool(row.get("quick_override") or False),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def _reward(outcome: str, quick_override: bool) -> float:
    base = {
        "success": 1.0,
        "partial": 0.3,
        "failure": -0.7,
        "wasted": -1.0,
    }.get(outcome, -0.6)
    if quick_override:
        base -= 0.5
    return base


def _event_history(
    timeline: list[tuple[int, str, str, str]],
    decision_id: str,
    history_size: int,
) -> list[dict[str, Any]]:
    idx = -1
    for i, item in enumerate(timeline):
        if item[1] == decision_id:
            idx = i
            break
    if idx < 0:
        return []
    start = max(0, idx - max(0, history_size))
    rows = timeline[start:idx]
    total = len(rows)
    out: list[dict[str, Any]] = []
    for j, (past_ts, past_id, past_action, past_outcome) in enumerate(rows):
        out.append(
            {
                "t_minus": total - j,
                "decision_id": past_id,
                "action_name": past_action,
                "outcome": past_outcome,
                "ts_ms": past_ts,
            }
        )
    return out


def _infer_mapping_from_apply_timeline(
    *,
    intent_ts_ms: int,
    apply_timeline: list[tuple[int, str, str]],
    max_lag_ms: int,
) -> tuple[str, str]:
    if intent_ts_ms <= 0 or not apply_timeline:
        return "", ""
    best_mapping = ""
    best_decision = ""
    for apply_ts, mapping_epoch_id, decision_id in apply_timeline:
        if apply_ts > intent_ts_ms:
            break
        if intent_ts_ms - apply_ts > max_lag_ms:
            continue
        best_mapping = mapping_epoch_id
        best_decision = decision_id
    return best_mapping, best_decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Export z-mode RL rows from seq_mem")
    parser.add_argument("--seq-mem", default=DEFAULT_SEQ_MEM_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-percent", type=int, default=10)
    parser.add_argument("--test-percent", type=int, default=10)
    parser.add_argument("--max-age-hours", type=int, default=168)
    parser.add_argument("--history-size", type=int, default=5)
    parser.add_argument("--quick-override-ms", type=int, default=60_000)
    parser.add_argument("--infer-mapping-max-lag-ms", type=int, default=12 * 3600 * 1000)
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    max_age_ms = max(1, int(args.max_age_hours)) * 3600 * 1000
    rows = _load_jsonl(Path(args.seq_mem).expanduser().resolve())

    zmode_decisions: dict[str, dict[str, Any]] = {}
    zmode_applies_by_epoch: dict[str, dict[str, Any]] = {}
    kar_intents: dict[str, dict[str, Any]] = {}
    kar_outcomes: dict[str, dict[str, Any]] = {}
    kar_overrides: dict[str, list[dict[str, Any]]] = defaultdict(list)
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

            if name == "zmode.policy.decision.v1":
                decision_id = str(subj.get("decision_id") or "")
                if decision_id:
                    zmode_decisions[decision_id] = {"row": row, "subject": subj}
                continue

            if name == "zmode.policy.apply.v1":
                if not bool(subj.get("compile_ok")):
                    continue
                mapping_epoch_id = str(subj.get("mapping_epoch_id") or "")
                if not mapping_epoch_id:
                    continue
                existing = zmode_applies_by_epoch.get(mapping_epoch_id)
                if existing is None:
                    zmode_applies_by_epoch[mapping_epoch_id] = {"row": row, "subject": subj}
                else:
                    old_ts = int(existing["row"].get("ts_ms") or 0)
                    if ts_ms >= old_ts:
                        zmode_applies_by_epoch[mapping_epoch_id] = {"row": row, "subject": subj}
                continue

            if name == "kar.intent.v1":
                decision_id = str(subj.get("decision_id") or "")
                if not decision_id:
                    continue
                if decision_id in kar_intents:
                    duplicate_intent_ids += 1
                kar_intents[decision_id] = {"row": row, "subject": subj}
                continue

            if name == "kar.outcome.v1":
                decision_id = str(subj.get("decision_id") or "")
                if not decision_id:
                    continue
                if decision_id in kar_outcomes:
                    duplicate_outcome_ids += 1
                kar_outcomes[decision_id] = {"row": row, "subject": subj}
                continue

            if name == "kar.override.v1":
                decision_id = str(subj.get("decision_id") or "")
                if decision_id:
                    kar_overrides[decision_id].append({"row": row, "subject": subj})
                continue
        except Exception:
            parse_errors += 1

    session_timeline: dict[str, list[tuple[int, str, str, str]]] = defaultdict(list)
    for decision_id, payload in kar_intents.items():
        subj = payload["subject"]
        row = payload["row"]
        session_id = str(subj.get("session_id") or row.get("session_id") or "")
        ts_ms = int(row.get("ts_ms") or 0)
        action_name = str(subj.get("action_name") or "")
        out = kar_outcomes.get(decision_id)
        outcome = str(out["subject"].get("outcome") or "") if out else ""
        if session_id and ts_ms > 0:
            session_timeline[session_id].append((ts_ms, decision_id, action_name, outcome))
    for sid in list(session_timeline.keys()):
        session_timeline[sid].sort(key=lambda x: x[0])

    apply_timeline: list[tuple[int, str, str]] = []
    for mapping_epoch_id, payload in zmode_applies_by_epoch.items():
        subj = payload["subject"]
        row = payload["row"]
        apply_ts = int(subj.get("ts_ms") or row.get("ts_ms") or 0)
        decision_id = str(subj.get("decision_id") or "")
        if mapping_epoch_id and apply_ts > 0:
            apply_timeline.append((apply_ts, mapping_epoch_id, decision_id))
    apply_timeline.sort(key=lambda x: x[0])

    joined: list[dict[str, Any]] = []
    missing_outcome = 0
    missing_mapping = 0
    missing_policy = 0
    quick_overrides = 0
    total_override_links = 0
    inferred_mapping_count = 0

    for decision_id, payload in kar_intents.items():
        intent_row = payload["row"]
        intent = payload["subject"]
        out = kar_outcomes.get(decision_id)
        if out is None:
            missing_outcome += 1
            continue
        outcome_subj = out["subject"]

        mapping_epoch_id = str(intent.get("mapping_epoch_id") or "")
        inferred_mapping_used = False
        if not mapping_epoch_id:
            inferred_mapping, inferred_decision = _infer_mapping_from_apply_timeline(
                intent_ts_ms=ts_ms,
                apply_timeline=apply_timeline,
                max_lag_ms=max(1, int(args.infer_mapping_max_lag_ms)),
            )
            if inferred_mapping:
                mapping_epoch_id = inferred_mapping
                inferred_mapping_used = True
                inferred_mapping_count += 1
                if not intent.get("zmode_policy_decision_id"):
                    intent["zmode_policy_decision_id"] = inferred_decision
            else:
                missing_mapping += 1
                continue

        zmode_policy_decision_id = str(intent.get("zmode_policy_decision_id") or "")
        if not zmode_policy_decision_id:
            apply = zmode_applies_by_epoch.get(mapping_epoch_id)
            if apply:
                zmode_policy_decision_id = str(apply["subject"].get("decision_id") or "")

        policy = zmode_decisions.get(zmode_policy_decision_id)
        if policy is None:
            missing_policy += 1
            continue

        policy_subj = policy["subject"]
        context = policy_subj.get("context") if isinstance(policy_subj.get("context"), dict) else {}
        llm_meta = policy_subj.get("llm_meta") if isinstance(policy_subj.get("llm_meta"), dict) else {}
        apply_payload = zmode_applies_by_epoch.get(mapping_epoch_id)
        apply_subj = apply_payload["subject"] if apply_payload else {}

        session_id = str(intent.get("session_id") or intent_row.get("session_id") or "")
        ts_ms = int(intent_row.get("ts_ms") or 0)
        action_name = str(intent.get("action_name") or "")
        candidate_id = str(intent.get("candidate_id") or "")
        candidate_key = str(intent.get("candidate_key") or "")
        candidate_action = str(intent.get("candidate_action") or "")

        override_rows = kar_overrides.get(decision_id, [])
        total_override_links += len(override_rows)
        ms_since_values = []
        for item in override_rows:
            subj = item.get("subject")
            if isinstance(subj, dict):
                ms = int(subj.get("ms_since_decision") or 0)
                if ms > 0:
                    ms_since_values.append(ms)
        quick_override = bool(ms_since_values and min(ms_since_values) <= int(args.quick_override_ms))
        if quick_override:
            quick_overrides += 1

        outcome = str(outcome_subj.get("outcome") or "")
        row = {
            "schema_version": "zmode_train_row_v1",
            "id": decision_id,
            "ts_ms": ts_ms,
            "session_id": session_id,
            "mapping_epoch_id": mapping_epoch_id,
            "zmode_policy_decision_id": zmode_policy_decision_id,
            "action_type": str(intent.get("action_type") or ""),
            "action_name": action_name,
            "candidate_id": candidate_id,
            "candidate_key": candidate_key,
            "candidate_action": candidate_action,
            "outcome": outcome,
            "latency_ms": int(outcome_subj.get("latency_ms") or 0),
            "reason": str(outcome_subj.get("reason") or ""),
            "observed_app": str(outcome_subj.get("observed_app") or ""),
            "override_count": len(override_rows),
            "quick_override": quick_override,
            "mapping_inferred": inferred_mapping_used,
            "context": {
                "frontmost_app": str(context.get("frontmost_app") or ""),
                "active_project": str(context.get("active_project") or ""),
                "zed_project": str(context.get("zed_project") or ""),
                "hour": int(context.get("hour") or 0),
                "day": str(context.get("day") or ""),
                "running_apps": context.get("running_apps") if isinstance(context.get("running_apps"), list) else [],
            },
            "llm_meta": llm_meta,
            "apply": {
                "apply_id": str(apply_subj.get("apply_id") or ""),
                "apply_ts_ms": int(apply_subj.get("ts_ms") or 0),
                "binding_count": int(apply_subj.get("binding_count") or 0),
            },
            "event_history": _event_history(session_timeline.get(session_id, []), decision_id, args.history_size),
        }
        row["reward"] = _reward(outcome, quick_override=quick_override)
        row["dedupe_id"] = _dedupe_id(row)
        joined.append(row)

    joined.sort(key=lambda r: int(r.get("ts_ms") or 0))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped_duplicate_rows = 0
    for row in joined:
        dedupe_id = str(row.get("dedupe_id") or "")
        if dedupe_id and dedupe_id in seen:
            dropped_duplicate_rows += 1
            continue
        if dedupe_id:
            seen.add(dedupe_id)
        deduped.append(row)
    joined = deduped

    val_pct = max(0, min(int(args.val_percent), 100))
    test_pct = max(0, min(int(args.test_percent), 100 - val_pct))
    train_rows: list[dict[str, Any]] = []
    val_rows: list[dict[str, Any]] = []
    test_rows: list[dict[str, Any]] = []

    for row in joined:
        b = _bucket(str(row.get("id") or ""), args.seed)
        if b < test_pct:
            test_rows.append(row)
        elif b < test_pct + val_pct:
            val_rows.append(row)
        else:
            train_rows.append(row)

    outcome_counts = Counter(str(r.get("outcome") or "unknown") for r in joined)
    action_counts = Counter(str(r.get("action_name") or "unknown") for r in joined)
    candidate_counts = Counter(str(r.get("candidate_id") or "unknown") for r in joined)

    apply_count = len(zmode_applies_by_epoch)
    decision_count = len(zmode_decisions)
    intents_with_mapping = max(0, len(kar_intents) - missing_mapping)
    link_rate = len(joined) / max(1, intents_with_mapping)
    apply_rate = apply_count / max(1, decision_count)
    action_dom = (action_counts.most_common(1)[0][1] / max(1, len(joined))) if action_counts else 0.0
    candidate_dom = (candidate_counts.most_common(1)[0][1] / max(1, len(joined))) if candidate_counts else 0.0

    out_root = Path(args.output_dir).expanduser().resolve()
    _write_jsonl(out_root / "train.jsonl", train_rows)
    _write_jsonl(out_root / "val.jsonl", val_rows)
    _write_jsonl(out_root / "test.jsonl", test_rows)

    summary = {
        "schema_version": "zmode_signal_export_v1",
        "generated_at_ms": now_ms,
        "input": {
            "seq_mem": str(Path(args.seq_mem).expanduser().resolve()),
            "max_age_hours": int(args.max_age_hours),
            "seed": int(args.seed),
            "val_percent": val_pct,
            "test_percent": test_pct,
            "history_size": int(args.history_size),
            "quick_override_ms": int(args.quick_override_ms),
            "infer_mapping_max_lag_ms": int(args.infer_mapping_max_lag_ms),
        },
        "counts": {
            "zmode_decisions": decision_count,
            "zmode_applies": apply_count,
            "kar_intents": len(kar_intents),
            "kar_outcomes": len(kar_outcomes),
            "joined_rows": len(joined),
            "missing_outcome": missing_outcome,
            "missing_mapping_epoch": missing_mapping,
            "inferred_mapping_epoch": inferred_mapping_count,
            "missing_policy_decision": missing_policy,
            "override_links": total_override_links,
            "quick_overrides": quick_overrides,
            "duplicate_intent_ids": duplicate_intent_ids,
            "duplicate_outcome_ids": duplicate_outcome_ids,
            "dropped_duplicate_rows": dropped_duplicate_rows,
            "train_rows": len(train_rows),
            "val_rows": len(val_rows),
            "test_rows": len(test_rows),
            "parse_errors": parse_errors,
        },
        "quality": {
            "apply_rate": round(apply_rate, 6),
            "intent_outcome_link_rate": round(link_rate, 6),
            "override_rate": round(total_override_links / max(1, len(joined)), 6),
            "quick_override_rate": round(quick_overrides / max(1, len(joined)), 6),
            "action_dominance": round(action_dom, 6),
            "candidate_dominance": round(candidate_dom, 6),
        },
        "distributions": {
            "outcomes": dict(outcome_counts),
            "actions": dict(action_counts),
            "candidates": dict(candidate_counts),
        },
        "outputs": {
            "train": str(out_root / "train.jsonl"),
            "val": str(out_root / "val.jsonl"),
            "test": str(out_root / "test.jsonl"),
            "summary": str(out_root / "summary.json"),
        },
    }
    _write_json(out_root / "summary.json", summary)

    print(f"Exported z-mode signal dataset: {out_root}")
    print(json.dumps(summary["counts"], ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
