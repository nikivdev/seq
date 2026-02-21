#!/usr/bin/env python3
"""Export next-type phrase training pairs into prime-rl verifiers-compatible format.

Reads phrase builder JSONL, applies quality filters, produces deterministic
train/val/test splits (80/10/10 by time), and writes output datasets.

Usage:
    python3 next_type_dataset_export.py --phrases phrases.jsonl --out-dir ./dataset/
    python3 next_type_dataset_export.py --phrases phrases.jsonl --out-dir ./dataset/ --stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_PHRASES = str(Path("~/.local/state/seq/next_type_phrases.jsonl").expanduser())
DEFAULT_OUT_DIR = str(Path("~/.local/state/seq/next_type_dataset").expanduser())

# Quality filter thresholds
MIN_ANSWER_LENGTH = 5
MAX_ANSWER_LENGTH = 200

SYSTEM_PROMPT = """You are a code completion model. Given the editor context (file, language, project, recent typing) and a prefix of the current line, predict the next phrase that will be typed.

Rules:
- Output ONLY the predicted text, nothing else
- Keep predictions concise (one logical phrase or statement)
- Match the coding style and language shown in context
- If the prefix ends mid-word, complete the word first then continue"""


def load_phrases(path: Path) -> list[dict[str, Any]]:
    """Load phrase JSONL file."""
    phrases = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    phrases.append(row)
            except json.JSONDecodeError:
                continue
    return phrases


def quality_filter(
    phrases: list[dict[str, Any]],
    *,
    min_answer_length: int = MIN_ANSWER_LENGTH,
    max_answer_length: int = MAX_ANSWER_LENGTH,
) -> list[dict[str, Any]]:
    """Apply quality filters to phrases."""
    filtered = []
    for p in phrases:
        answer = p.get("answer", "")
        # Length bounds
        if len(answer) < min_answer_length:
            continue
        if len(answer) > max_answer_length:
            continue
        # Must have some context (file or language)
        if not p.get("language") and not p.get("file_ext"):
            continue
        # Skip pure whitespace/newlines
        if not answer.strip():
            continue
        filtered.append(p)
    return filtered


def time_based_split(
    phrases: list[dict[str, Any]],
    train_ratio: float = 0.80,
    val_ratio: float = 0.10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split phrases by time: oldest → train, middle → val, newest → test.

    This matches deployment: model predicts future from past.
    """
    # Sort by timestamp
    sorted_phrases = sorted(phrases, key=lambda p: p.get("start_ts_ms", 0))

    n = len(sorted_phrases)
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))
    # Keep tiny datasets usable by reserving at least 1 row for val/test when possible.
    if n >= 3:
        train_end = min(max(train_end, 1), n - 2)
        val_end = min(max(val_end, train_end + 1), n - 1)

    train = sorted_phrases[:train_end]
    val = sorted_phrases[train_end:val_end]
    test = sorted_phrases[val_end:]

    return train, val, test


def to_verifiers_format(phrases: list[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    """Convert phrases to prime-rl verifiers-compatible format."""
    rows = []
    for p in phrases:
        prompt = p.get("prompt", "")
        answer = p.get("answer", "")
        row = {
            "question": prompt,
            "answer": answer,
            "info": {
                "language": p.get("language", ""),
                "file_ext": p.get("file_ext", ""),
                "project_name": p.get("project_name", ""),
                "burst_wpm": p.get("burst_wpm", 0),
                "burst_char_count": p.get("burst_char_count", 0),
                "split": split,
            },
            "task": "next-type-predictor",
        }
        rows.append(row)
    return rows


def write_combined_dataset(
    out_path: Path,
    train: list[dict[str, Any]],
    val: list[dict[str, Any]],
    test: list[dict[str, Any]],
) -> None:
    """Write one combined JSONL with split labels for envs that load a single file."""
    combined = train + val + test
    write_jsonl(out_path, combined)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=True) for r in rows]
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def compute_stats(
    train: list[dict[str, Any]],
    val: list[dict[str, Any]],
    test: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute summary statistics."""
    all_phrases = train + val + test

    languages: dict[str, int] = {}
    projects: dict[str, int] = {}
    total_chars = 0

    for p in all_phrases:
        lang = p.get("language", "") or "unknown"
        proj = p.get("project_name", "") or "unknown"
        languages[lang] = languages.get(lang, 0) + 1
        projects[proj] = projects.get(proj, 0) + 1
        total_chars += len(p.get("answer", ""))

    avg_chars = total_chars / len(all_phrases) if all_phrases else 0

    return {
        "total": len(all_phrases),
        "train": len(train),
        "val": len(val),
        "test": len(test),
        "avg_answer_chars": round(avg_chars, 1),
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "projects": dict(sorted(projects.items(), key=lambda kv: -kv[1])),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export next-type phrases to prime-rl dataset format."
    )
    parser.add_argument(
        "--phrases",
        default=os.environ.get("SEQ_NEXT_TYPE_PHRASES_OUT", DEFAULT_PHRASES),
        help="Input phrase JSONL from phrase builder.",
    )
    parser.add_argument(
        "--out-dir",
        default=os.environ.get("SEQ_NEXT_TYPE_DATASET_DIR", DEFAULT_OUT_DIR),
        help="Output directory for train/val/test splits.",
    )
    parser.add_argument(
        "--min-answer-length", type=int, default=MIN_ANSWER_LENGTH,
        help="Minimum answer length in characters.",
    )
    parser.add_argument(
        "--max-answer-length", type=int, default=MAX_ANSWER_LENGTH,
        help="Maximum answer length in characters.",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print dataset statistics to stdout.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    phrases_path = Path(args.phrases).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    if not phrases_path.exists():
        print(f"phrases file not found: {phrases_path}", file=sys.stderr)
        return 1

    # Load and filter
    raw = load_phrases(phrases_path)
    print(f"loaded: {len(raw)} raw phrases")

    filtered = quality_filter(
        raw,
        min_answer_length=args.min_answer_length,
        max_answer_length=args.max_answer_length,
    )
    print(f"after quality filter: {len(filtered)} phrases")

    if not filtered:
        print("no phrases pass quality filter — collect more data")
        return 0

    # Split by time
    train_raw, val_raw, test_raw = time_based_split(filtered)

    # Convert to verifiers format
    train = to_verifiers_format(train_raw, "train")
    val = to_verifiers_format(val_raw, "val")
    test = to_verifiers_format(test_raw, "test")

    # Write splits
    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_dir / "train.jsonl", train)
    write_jsonl(out_dir / "val.jsonl", val)
    write_jsonl(out_dir / "test.jsonl", test)
    write_combined_dataset(out_dir / "next_type_phrases.jsonl", train, val, test)

    # Write manifest
    stats = compute_stats(train_raw, val_raw, test_raw)
    manifest = {
        "schema_version": "next_type_dataset_v1",
        "system_prompt": SYSTEM_PROMPT,
        "splits": {
            "train": {"count": len(train), "file": "train.jsonl"},
            "val": {"count": len(val), "file": "val.jsonl"},
            "test": {"count": len(test), "file": "test.jsonl"},
        },
        "combined": {"count": len(train) + len(val) + len(test), "file": "next_type_phrases.jsonl"},
        "stats": stats,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    print(f"output: {out_dir}")
    print(f"  train: {len(train)} examples")
    print(f"  val:   {len(val)} examples")
    print(f"  test:  {len(test)} examples")

    if args.stats:
        print(json.dumps(stats, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
