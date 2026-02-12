#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/cli/cpp/out/bin/seq"
SOCK="/tmp/seqd.perf.sock"
LOG="/tmp/seq_perf_smoke.log"

rm -f "$SOCK" "$LOG"

if [ ! -x "$BIN" ]; then
  echo "error: missing binary: $BIN" >&2
  exit 1
fi

export SEQ_CH_MEM_PATH="/tmp/seq_mem_perf.jsonl"
export RISE_LOG_DIR="/tmp/seq_perf_logs"
mkdir -p "$RISE_LOG_DIR"
# Keep the daemon as idle as possible for the CPU regression check.
export SEQ_APP_POLL_MS="0"

"$BIN" --socket "$SOCK" daemon >/dev/null 2>&1 &
DAEMON_PID="$!"
cleanup() {
  kill "$DAEMON_PID" >/dev/null 2>&1 || true
  wait "$DAEMON_PID" >/dev/null 2>&1 || true
  rm -f "$SOCK"
}
trap cleanup EXIT

for _ in $(seq 1 100); do
  if [ -S "$SOCK" ]; then
    break
  fi
  sleep 0.02
done
if [ ! -S "$SOCK" ]; then
  echo "error: daemon socket not created: $SOCK" >&2
  exit 1
fi

perf() {
  "$BIN" --socket "$SOCK" perf 2>>"$LOG"
}

cpu_us() {
  python3 -c 'import json,sys,re; s=sys.stdin.read(); m=re.search(r"{.*}", s, re.S); \
             (m or sys.exit(2)); o=json.loads(m.group(0)); ru=o.get("rusage",{}); \
             print(int(ru.get("utime_us",0)+ru.get("stime_us",0)))'
}

P0="$(perf | tee -a "$LOG" | cpu_us)"
sleep 0.5
P1="$(perf | tee -a "$LOG" | cpu_us)"
IDLE_DELTA="$((P1 - P0))"

# This is a regression tripwire for busy-loops. It should be tiny on an idle daemon.
# Keep this generous for laptops under load; it should still catch "oops, no sleep".
if [ "$IDLE_DELTA" -gt 80000 ]; then
  echo "error: seqd idle cpu too high: ${IDLE_DELTA}us over 0.5s (see $LOG)" >&2
  exit 1
fi

for _ in $(seq 1 200); do
  "$BIN" --socket "$SOCK" ping >/dev/null
done

P2="$(perf | tee -a "$LOG" | cpu_us)"
LOAD_DELTA="$((P2 - P1))"
if [ "$LOAD_DELTA" -le 0 ]; then
  echo "error: unexpected cpu delta after load: ${LOAD_DELTA}us (see $LOG)" >&2
  exit 1
fi

echo "OK idle_cpu_us=$IDLE_DELTA load_cpu_us=$LOAD_DELTA"
