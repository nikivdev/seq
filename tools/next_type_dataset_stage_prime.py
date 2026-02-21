#!/usr/bin/env python3
"""Stage next-type combined dataset into prime-rl environment data path."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

DEFAULT_SRC = str(Path("~/.local/state/seq/next_type_dataset/next_type_phrases.jsonl").expanduser())
DEFAULT_DST = str(
    Path(
        "~/repos/PrimeIntellect-ai/prime-rl/examples/flow_rise/environment_next_type/data/next_type_phrases.jsonl"
    ).expanduser()
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage next-type dataset for prime-rl environment")
    parser.add_argument(
        "--src",
        default=os.environ.get("SEQ_NEXT_TYPE_STAGE_SRC", ""),
        help="Source combined dataset JSONL (defaults to dataset dir + next_type_phrases.jsonl)",
    )
    parser.add_argument(
        "--dst",
        default=os.environ.get("SEQ_NEXT_TYPE_PRIME_RL_ENV_DATA", DEFAULT_DST),
        help="Destination path inside prime-rl environment data folder",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    src = args.src.strip()
    if not src:
        dataset_dir = os.environ.get("SEQ_NEXT_TYPE_DATASET_DIR", "~/.local/state/seq/next_type_dataset")
        src = str(Path(dataset_dir).expanduser() / "next_type_phrases.jsonl")

    src_path = Path(src).expanduser().resolve()
    dst_path = Path(args.dst).expanduser().resolve()

    if not src_path.exists() or not src_path.is_file():
        print(f"missing dataset: {src_path}", file=sys.stderr)
        return 1

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dst_path)
    line_count = 0
    with dst_path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in fh:
            line_count += 1

    print(f"staged rows: {line_count} -> {dst_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
