#!/usr/bin/env bash
set -euo pipefail

HOURS="${1:-6}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/tools/seq_snapshot.py" --hours "$HOURS"

