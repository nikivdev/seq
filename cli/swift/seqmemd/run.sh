#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/out"
BIN="$OUT/bin"
LOG="$OUT/logs"

mkdir -p "$BIN" "$LOG"

# Keep build artifacts in a predictable location for tooling, while still using SwiftPM.
swift build -c release --package-path "$ROOT" --scratch-path "$OUT/.build" 2>&1 | tee "$LOG/build.log"

TARGET="$OUT/.build/release/seqmemd"
if [ ! -x "$TARGET" ]; then
  echo "error: expected swift build output at $TARGET" >&2
  exit 1
fi

cp -f "$TARGET" "$BIN/seqmemd"

if [ "${1:-}" = "--build-only" ]; then
  exit 0
fi

"$BIN/seqmemd" "$@"
