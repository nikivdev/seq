#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/out"
LOG="$OUT/logs"

COPY_TO=""
MODE="build"

usage() {
  cat <<EOF
seqmem (Swift) build helper

Usage:
  $0 build [--copy-to <dir>]

Env:
  WAX_PATH=/path/to/Wax   (defaults to /Users/nikiv/repos/christopherkarani/Wax)
EOF
}

if [ $# -eq 0 ]; then
  usage
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    build)
      MODE="build"
      shift
      ;;
    --copy-to)
      COPY_TO="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$OUT" "$LOG"

# -undefined dynamic_lookup: allows dlsym-resolved C bridge functions (seq_ch_*)
# to be referenced at compile time without a link-time definition. The symbols
# are provided by the host binary (seq) which links libseqch.dylib.
swift build -c release --package-path "$ROOT" --scratch-path "$OUT/.build" \
  -Xlinker -undefined -Xlinker dynamic_lookup \
  2>&1 | tee "$LOG/build.log"

LIB="$OUT/.build/release/libseqmem.dylib"
if [ ! -f "$LIB" ]; then
  echo "error: expected SwiftPM dylib at $LIB" >&2
  exit 1
fi

mkdir -p "$OUT/lib"
cp -f "$LIB" "$OUT/lib/libseqmem.dylib"

if [ -n "$COPY_TO" ]; then
  mkdir -p "$COPY_TO"
  cp -f "$OUT/lib/libseqmem.dylib" "$COPY_TO/libseqmem.dylib"
fi

