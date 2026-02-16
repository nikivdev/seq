#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description=(
      "A/B full-flow benchmark for legacy shell path vs send_user_command path. "
      "Measures command->frontmost latency and optional command->seq trace latency."
    )
  )
  p.add_argument("--iterations", type=int, default=20)
  p.add_argument("--warmup", type=int, default=3)
  p.add_argument("--timeout-s", type=float, default=3.5)
  p.add_argument("--poll-ms", type=float, default=20.0)

  p.add_argument("--app", default="Safari")
  p.add_argument("--app-mode", choices=["toggle", "open"], default="toggle")
  p.add_argument("--app-frontmost", default="Safari")
  p.add_argument("--baseline-app", default="Ghostty")

  p.add_argument("--include-zed", action="store_true")
  p.add_argument("--zed-app", default="/System/Volumes/Data/Applications/Zed Preview.app")
  p.add_argument("--zed-path", default="~/config/fish/fn.fish")
  p.add_argument("--zed-frontmost", default="zed")

  p.add_argument("--seq-bin", default="/Users/nikiv/code/seq/cli/cpp/out/bin/seq")
  p.add_argument(
    "--receiver-socket",
    default="/Library/Application Support/org.pqrs/tmp/user/501/user_command_receiver.sock",
  )
  p.add_argument("--trace-log", default="/Users/nikiv/code/seq/cli/cpp/out/logs/trace.log")
  p.add_argument("--no-trace", action="store_true")

  p.add_argument("--json-out", default="")
  p.add_argument("--verbose", action="store_true")
  return p.parse_args()


def percentile(values: list[float], p: float) -> float:
  if not values:
    return 0.0
  if len(values) == 1:
    return values[0]
  idx = int(round((p / 100.0) * (len(values) - 1)))
  idx = max(0, min(idx, len(values) - 1))
  return values[idx]


def summarize(name: str, values_us: list[float], attempted: int) -> dict[str, float | int | str]:
  xs = sorted(values_us)
  failed = max(0, attempted - len(xs))
  return {
    "name": name,
    "count": len(xs),
    "attempted": attempted,
    "failed": failed,
    "min_us": xs[0] if xs else 0.0,
    "p50_us": percentile(xs, 50),
    "p90_us": percentile(xs, 90),
    "p95_us": percentile(xs, 95),
    "p99_us": percentile(xs, 99),
    "max_us": xs[-1] if xs else 0.0,
    "mean_us": statistics.fmean(xs) if xs else 0.0,
  }


def print_row(row: dict[str, float | int | str]) -> None:
  print(
    f"{row['name']:<34} "
    f"n={row['count']:<3}/{row['attempted']:<3} "
    f"failed={row['failed']:<3} "
    f"p50={row['p50_us']:.1f}us "
    f"p95={row['p95_us']:.1f}us "
    f"p99={row['p99_us']:.1f}us "
    f"mean={row['mean_us']:.1f}us"
  )


class TraceTail:
  def __init__(self, path: Path):
    self.path = path
    self.offset = path.stat().st_size if path.exists() else 0

  def scan_new_for_event_us(self, needle: str) -> Optional[int]:
    if not self.path.exists():
      return None
    with self.path.open("rb") as f:
      f.seek(self.offset)
      data = f.read()
      self.offset = f.tell()
    if not data:
      return None

    text = data.decode("utf-8", errors="replace")
    for line in text.splitlines():
      if needle in line:
        head = line.split(" ", 1)[0]
        try:
          return int(head)
        except ValueError:
          return None
    return None


def run_shell(cmd: str) -> int:
  p = subprocess.run(["/bin/sh", "-lc", cmd], capture_output=True, text=True)
  return p.returncode


def activate_app(name: str) -> None:
  run_shell(f'open -a "{name}" >/dev/null 2>&1')


def frontmost_name() -> Optional[str]:
  p = subprocess.run(
    [
      "osascript",
      "-e",
      'tell application "System Events" to get name of first process whose frontmost is true',
    ],
    capture_output=True,
    text=True,
  )
  if p.returncode != 0:
    return None
  out = p.stdout.strip()
  return out if out else None


def is_frontmost_match(actual: Optional[str], expected: str) -> bool:
  if actual is None:
    return False
  return actual.strip().casefold() == expected.strip().casefold()


def send_user_command(receiver_socket: str, payload: dict) -> None:
  data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
  s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  try:
    s.sendto(data, receiver_socket)
  finally:
    s.close()


def bench_case(
  label: str,
  sender: Callable[[], None],
  target_frontmost: str,
  tail: Optional[TraceTail],
  trace_needle: Optional[str],
  iterations: int,
  warmup: int,
  timeout_s: float,
  poll_s: float,
  prepare: Optional[Callable[[], None]] = None,
  verbose: bool = False,
) -> tuple[list[float], list[float], int]:
  to_trace: list[float] = []
  to_front: list[float] = []
  attempted = iterations + warmup

  for i in range(attempted):
    if prepare is not None:
      prepare()

    t0_wall_us = time.time_ns() // 1000
    t0_mono_ns = time.perf_counter_ns()
    sender()

    event_us: Optional[int] = None
    front_ns: Optional[int] = None
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
      if front_ns is None:
        name = frontmost_name()
        if is_frontmost_match(name, target_frontmost):
          front_ns = time.perf_counter_ns()

      if tail is not None and trace_needle is not None and event_us is None:
        event_us = tail.scan_new_for_event_us(trace_needle)

      if front_ns is not None and (trace_needle is None or event_us is not None):
        break
      time.sleep(poll_s)

    if i >= warmup:
      if event_us is not None:
        to_trace.append(float(max(0, event_us - t0_wall_us)))
      if front_ns is not None:
        to_front.append((front_ns - t0_mono_ns) / 1000.0)

    if verbose:
      print(
        f"[{label}] i={i} trace_us={None if event_us is None else max(0, event_us - t0_wall_us)} "
        f"front_us={None if front_ns is None else (front_ns - t0_mono_ns)/1000.0:.1f}"
      )

  return to_trace, to_front, attempted


def main() -> int:
  args = parse_args()

  seq_bin = Path(args.seq_bin).expanduser()
  if not seq_bin.exists():
    raise RuntimeError(f"seq binary not found: {seq_bin}")

  receiver_socket = os.path.expanduser(args.receiver_socket)
  if not Path(receiver_socket).exists():
    raise RuntimeError(f"receiver socket not found: {receiver_socket}")

  poll_s = max(0.005, args.poll_ms / 1000.0)
  tail = None if args.no_trace else TraceTail(Path(args.trace_log).expanduser())

  rows: list[dict[str, float | int | str]] = []

  # openApp A/B
  if args.app_mode == "open":
    legacy_shell_cmd = f'{seq_bin} open-app "{args.app}" >/dev/null 2>&1'
    user_payload = {"line": f"OPEN_APP {args.app}"}
    trace_needle = None if args.no_trace else f"seqd.open_app {args.app}"
  else:
    legacy_shell_cmd = f'{seq_bin} open-app-toggle "{args.app}" >/dev/null 2>&1'
    user_payload = {"v": 1, "type": "open_app_toggle", "app": args.app}
    trace_needle = None if args.no_trace else f"seqd.open_app_toggle {args.app}"

  legacy_trace, legacy_front, legacy_attempted = bench_case(
    label="legacy_shell_open_app",
    sender=lambda: run_shell(legacy_shell_cmd),
    target_frontmost=args.app_frontmost,
    tail=tail,
    trace_needle=trace_needle,
    iterations=args.iterations,
    warmup=args.warmup,
    timeout_s=args.timeout_s,
    poll_s=poll_s,
    prepare=(lambda: activate_app(args.baseline_app)) if args.baseline_app else None,
    verbose=args.verbose,
  )

  user_trace, user_front, user_attempted = bench_case(
    label="user_command_open_app",
    sender=lambda: send_user_command(receiver_socket, user_payload),
    target_frontmost=args.app_frontmost,
    tail=tail,
    trace_needle=trace_needle,
    iterations=args.iterations,
    warmup=args.warmup,
    timeout_s=args.timeout_s,
    poll_s=poll_s,
    prepare=(lambda: activate_app(args.baseline_app)) if args.baseline_app else None,
    verbose=args.verbose,
  )

  rows.append(summarize("legacy_shell->frontmost_app", legacy_front, legacy_attempted))
  rows.append(summarize("user_command->frontmost_app", user_front, user_attempted))
  rows.append(summarize("legacy_shell->seqd_open_app", legacy_trace, legacy_attempted))
  rows.append(summarize("user_command->seqd_open_app", user_trace, user_attempted))

  if args.include_zed:
    zed_path = os.path.expanduser(args.zed_path)
    zed_line = f"OPEN_WITH_APP {args.zed_app}:{zed_path}"

    zed_legacy_trace, zed_legacy_front, zed_legacy_attempted = bench_case(
      label="legacy_shell_open_with_app",
      sender=lambda: run_shell(f'open -a "{args.zed_app}" "{zed_path}" >/dev/null 2>&1'),
      target_frontmost=args.zed_frontmost,
      tail=tail,
      trace_needle=None if args.no_trace else f"seqd.open_with_app {args.zed_app}:{zed_path}",
      iterations=args.iterations,
      warmup=args.warmup,
      timeout_s=args.timeout_s,
      poll_s=poll_s,
      prepare=None,
      verbose=args.verbose,
    )

    zed_user_trace, zed_user_front, zed_user_attempted = bench_case(
      label="user_command_open_with_app",
      sender=lambda: send_user_command(receiver_socket, {"line": zed_line}),
      target_frontmost=args.zed_frontmost,
      tail=tail,
      trace_needle=None if args.no_trace else f"seqd.open_with_app {args.zed_app}:{zed_path}",
      iterations=args.iterations,
      warmup=args.warmup,
      timeout_s=args.timeout_s,
      poll_s=poll_s,
      prepare=None,
      verbose=args.verbose,
    )

    rows.append(summarize("legacy_shell->frontmost_zed", zed_legacy_front, zed_legacy_attempted))
    rows.append(summarize("user_command->frontmost_zed", zed_user_front, zed_user_attempted))
    rows.append(summarize("legacy_shell->seqd_open_with_app", zed_legacy_trace, zed_legacy_attempted))
    rows.append(summarize("user_command->seqd_open_with_app", zed_user_trace, zed_user_attempted))

  for row in rows:
    print_row(row)

  by_name = {str(r["name"]): r for r in rows}
  a = by_name.get("legacy_shell->frontmost_app")
  b = by_name.get("user_command->frontmost_app")
  if a and b and float(a["p95_us"]) > 0:
    print(f"p95 ratio frontmost_app (user/legacy): {float(b['p95_us']) / float(a['p95_us']):.2f}x")

  if args.json_out:
    out = Path(args.json_out).expanduser()
    out.write_text(json.dumps({"rows": rows, "args": vars(args)}, indent=2), encoding="utf-8")
    print(f"json: {out}")

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
