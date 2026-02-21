#!/usr/bin/env python3
"""Show what data was collected between seq watchdog checkpoints.

This makes checkpoint growth visible with concrete deltas:
- file growth (`seq_mem` / `seq_trace`)
- signal deltas from checkpoint manifests
- actual event counts in seq_mem between checkpoint timestamps
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SNAPSHOT_DIR = str(Path("~/.local/state/seq/checkpoints").expanduser())

# High-signal events we care about for RL training quality.
HIGH_SIGNAL_EVENTS = (
    "next_type.key_down",
    "next_type.key_up",
    "next_type.text_burst.v1",
    "next_type.context.v1",
    "next_type.suggestion_emit.v1",
    "next_type.suggestion_accept.v1",
    "kar.intent.v1",
    "kar.outcome.v1",
    "flow.router.decision.v1",
    "flow.router.outcome.v1",
    "agent.qa.pair",
)


@dataclass
class Checkpoint:
    path: Path
    manifest: dict[str, Any]

    @property
    def created_at_ms(self) -> int:
        return int(self.manifest.get("created_at_ms") or 0)

    @property
    def seq_mem_path(self) -> Path:
        payload = self.manifest.get("seq_mem") or {}
        return Path(str(payload.get("path") or "")).expanduser()

    @property
    def seq_trace_path(self) -> Path:
        payload = self.manifest.get("seq_trace") or {}
        return Path(str(payload.get("path") or "")).expanduser()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def list_checkpoints(snapshot_dir: Path) -> list[Checkpoint]:
    checkpoints: list[Checkpoint] = []
    for path in sorted(snapshot_dir.glob("checkpoint_*")):
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = _load_json(manifest_path)
        except Exception:
            continue
        checkpoints.append(Checkpoint(path=path, manifest=manifest))
    checkpoints.sort(key=lambda cp: cp.created_at_ms)
    return checkpoints


def _safe_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    try:
        return int(value)
    except Exception:
        return 0


def _signals_counts(manifest: dict[str, Any]) -> dict[str, int]:
    signals = manifest.get("signals") or {}
    counts = signals.get("counts_in_lookback") or {}
    if not isinstance(counts, dict):
        return {}
    return {str(k): _safe_int(counts, str(k)) for k in counts.keys()}


def _file_size(manifest: dict[str, Any], key: str) -> int:
    payload = manifest.get(key) or {}
    if not isinstance(payload, dict):
        return 0
    return _safe_int(payload, "size")


def _count_events_between(path: Path, start_ms: int, end_ms: int) -> tuple[int, Counter[str]]:
    total = 0
    counts: Counter[str] = Counter()
    if not path.exists():
        return total, counts
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("ts_ms")
            try:
                ts_ms = int(ts)
            except Exception:
                continue
            if ts_ms <= start_ms or ts_ms > end_ms:
                continue
            total += 1
            name = row.get("name")
            if isinstance(name, str) and name:
                counts[name] += 1
    return total, counts


def build_delta(before: Checkpoint, after: Checkpoint, top: int) -> dict[str, Any]:
    duration_ms = max(0, after.created_at_ms - before.created_at_ms)
    duration_minutes = round(duration_ms / 60000.0, 2)
    seq_mem_before = _file_size(before.manifest, "seq_mem")
    seq_mem_after = _file_size(after.manifest, "seq_mem")
    seq_trace_before = _file_size(before.manifest, "seq_trace")
    seq_trace_after = _file_size(after.manifest, "seq_trace")

    before_counts = _signals_counts(before.manifest)
    after_counts = _signals_counts(after.manifest)
    signal_delta = {
        key: after_counts.get(key, 0) - before_counts.get(key, 0)
        for key in sorted(set(before_counts) | set(after_counts))
    }

    total_between, event_counts = _count_events_between(
        path=after.seq_mem_path,
        start_ms=before.created_at_ms,
        end_ms=after.created_at_ms,
    )

    high_signal_counts = {name: event_counts.get(name, 0) for name in HIGH_SIGNAL_EVENTS}
    emit = high_signal_counts.get("next_type.suggestion_emit.v1", 0)
    accept = high_signal_counts.get("next_type.suggestion_accept.v1", 0)
    accept_rate = (accept / emit) if emit > 0 else None

    return {
        "from_checkpoint": str(before.path),
        "to_checkpoint": str(after.path),
        "from_created_at_ms": before.created_at_ms,
        "to_created_at_ms": after.created_at_ms,
        "window_ms": duration_ms,
        "window_minutes": duration_minutes,
        "file_growth": {
            "seq_mem_bytes": seq_mem_after - seq_mem_before,
            "seq_trace_bytes": seq_trace_after - seq_trace_before,
        },
        "signal_lookback_delta": signal_delta,
        "events_between_checkpoints": {
            "total_rows": total_between,
            "high_signal_counts": high_signal_counts,
            "next_type_accept_rate": accept_rate,
            "top_events": event_counts.most_common(max(1, top)),
        },
    }


def print_human(delta: dict[str, Any]) -> None:
    print(f"from: {delta['from_checkpoint']}")
    print(f"to:   {delta['to_checkpoint']}")
    print(f"window: {delta['window_minutes']} minutes")
    growth = delta["file_growth"]
    print(
        "file_growth: "
        f"seq_mem={growth['seq_mem_bytes']} bytes, "
        f"seq_trace={growth['seq_trace_bytes']} bytes"
    )
    print("signal_lookback_delta:")
    for key, value in delta["signal_lookback_delta"].items():
        print(f"  {key}: {value:+d}")

    events = delta["events_between_checkpoints"]
    print(f"events_between_checkpoints.total_rows: {events['total_rows']}")
    print("high_signal_counts:")
    for key, value in events["high_signal_counts"].items():
        print(f"  {key}: {value}")
    rate = events["next_type_accept_rate"]
    if rate is None:
        print("next_type_accept_rate: n/a (no emits)")
    else:
        print(f"next_type_accept_rate: {rate:.3f}")
    print("top_events:")
    for name, count in events["top_events"]:
        print(f"  {name}: {count}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show data deltas between seq checkpoints.")
    parser.add_argument(
        "--snapshot-dir",
        default=os.environ.get("SEQ_SIGNAL_WATCHDOG_SNAPSHOT_DIR", DEFAULT_SNAPSHOT_DIR),
        help="Checkpoint directory containing checkpoint_*/manifest.json.",
    )
    parser.add_argument("--from", dest="from_checkpoint", help="Path to older checkpoint directory.")
    parser.add_argument("--to", dest="to_checkpoint", help="Path to newer checkpoint directory.")
    parser.add_argument("--top", type=int, default=20, help="How many top events to print.")
    parser.add_argument("--json", action="store_true", help="Output JSON.")
    parser.add_argument("--watch", action="store_true", help="Watch for new checkpoints and print deltas.")
    parser.add_argument("--interval-s", type=float, default=15.0, help="Polling interval for --watch.")
    return parser.parse_args()


def _load_checkpoint(path: Path) -> Checkpoint:
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing manifest: {manifest_path}")
    return Checkpoint(path=path, manifest=_load_json(manifest_path))


def _resolve_pair(args: argparse.Namespace, snapshot_dir: Path) -> tuple[Checkpoint, Checkpoint]:
    if args.from_checkpoint and args.to_checkpoint:
        return (
            _load_checkpoint(Path(args.from_checkpoint).expanduser().resolve()),
            _load_checkpoint(Path(args.to_checkpoint).expanduser().resolve()),
        )

    checkpoints = list_checkpoints(snapshot_dir)
    if len(checkpoints) < 2:
        raise RuntimeError("need at least two checkpoints")
    return checkpoints[-2], checkpoints[-1]


def run_once(args: argparse.Namespace) -> int:
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    try:
        before, after = _resolve_pair(args, snapshot_dir)
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    delta = build_delta(before, after, args.top)
    if args.json:
        print(json.dumps(delta, ensure_ascii=True, indent=2))
    else:
        print_human(delta)
    return 0


def run_watch(args: argparse.Namespace) -> int:
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    last_pair: tuple[str, str] | None = None
    while True:
        checkpoints = list_checkpoints(snapshot_dir)
        if len(checkpoints) >= 2:
            before, after = checkpoints[-2], checkpoints[-1]
            pair = (str(before.path), str(after.path))
            if pair != last_pair:
                delta = build_delta(before, after, args.top)
                if args.json:
                    print(json.dumps(delta, ensure_ascii=True))
                else:
                    print("-" * 80)
                    print_human(delta)
                last_pair = pair
        time.sleep(max(1.0, args.interval_s))


def main() -> int:
    args = parse_args()
    if args.watch:
        return run_watch(args)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())

