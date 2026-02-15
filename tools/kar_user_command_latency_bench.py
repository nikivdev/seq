#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description=(
      "Benchmark direct seqd datagram vs send_user_command->bridge latency "
      "for run/open-app/zed-style command shapes."
    )
  )
  p.add_argument(
    "--receiver-repo",
    default="~/repos/pqrs-org/Karabiner-Elements-user-command-receiver",
    help="Path to Karabiner-Elements-user-command-receiver repository.",
  )
  p.add_argument(
    "--bridge-bin",
    default="",
    help=(
      "Bridge binary path. Default: <receiver-repo>/.build/debug/seq-user-command-bridge "
      "(builds if missing)."
    ),
  )
  p.add_argument("--iterations", type=int, default=300)
  p.add_argument("--warmup", type=int, default=40)
  p.add_argument("--timeout-s", type=float, default=2.0)
  p.add_argument("--macro", default="open Safari new tab")
  p.add_argument("--app", default="Safari")
  p.add_argument("--zed-app", default="/System/Volumes/Data/Applications/Zed Preview.app")
  p.add_argument("--zed-path", default="~/config/fish/fn.fish")
  p.add_argument("--include-process-baseline", action="store_true")
  p.add_argument("--seq-bin", default="/Users/nikiv/code/seq/cli/cpp/out/bin/seq")
  p.add_argument("--json-out", default="")
  p.add_argument("--verbose", action="store_true")
  return p.parse_args()


def ensure_bridge_binary(receiver_repo: Path, bridge_bin: str) -> Path:
  if bridge_bin:
    path = Path(bridge_bin).expanduser()
  else:
    path = receiver_repo / ".build/debug/seq-user-command-bridge"
  if path.exists():
    return path

  res = subprocess.run(
    ["make", "build-bridge"],
    cwd=str(receiver_repo),
    check=False,
    capture_output=True,
    text=True,
  )
  if res.returncode != 0:
    raise RuntimeError("failed to build bridge:\n" + res.stdout + "\n" + res.stderr)
  if not path.exists():
    raise RuntimeError(f"bridge binary not found after build: {path}")
  return path


def wait_for_socket(path: Path, timeout_s: float) -> bool:
  deadline = time.time() + timeout_s
  while time.time() < deadline:
    if path.exists():
      return True
    time.sleep(0.01)
  return False


def percentile(values: list[float], p: float) -> float:
  if not values:
    return 0.0
  if len(values) == 1:
    return values[0]
  idx = int(round((p / 100.0) * (len(values) - 1)))
  idx = max(0, min(idx, len(values) - 1))
  return values[idx]


def summarize(name: str, values_us: list[float], failed: int = 0) -> dict[str, float | int | str]:
  sorted_values = sorted(values_us)
  return {
    "name": name,
    "count": len(sorted_values),
    "failed": failed,
    "attempted": len(sorted_values) + failed,
    "min_us": sorted_values[0] if sorted_values else 0.0,
    "p50_us": percentile(sorted_values, 50),
    "p90_us": percentile(sorted_values, 90),
    "p95_us": percentile(sorted_values, 95),
    "p99_us": percentile(sorted_values, 99),
    "max_us": sorted_values[-1] if sorted_values else 0.0,
    "mean_us": statistics.fmean(sorted_values) if sorted_values else 0.0,
  }


def bench_direct_dgram(
  listener: socket.socket,
  seq_dgram_sock: Path,
  iterations: int,
  warmup: int,
  timeout_s: float,
  line_fn,
) -> list[float]:
  sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  values: list[float] = []
  try:
    total = iterations + warmup
    for i in range(total):
      line = f"{line_fn(i)}\n".encode("utf-8")
      t0 = time.perf_counter_ns()
      sender.sendto(line, str(seq_dgram_sock))
      listener.settimeout(timeout_s)
      got, _ = listener.recvfrom(4096)
      t1 = time.perf_counter_ns()
      if got != line:
        raise RuntimeError(f"direct mismatch at {i}: {got!r}")
      if i >= warmup:
        values.append((t1 - t0) / 1000.0)
  finally:
    sender.close()
  return values


def bench_bridge_dgram(
  listener: socket.socket,
  receiver_sock: Path,
  iterations: int,
  warmup: int,
  timeout_s: float,
  payload_fn,
  expected_line_fn,
) -> list[float]:
  sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  values: list[float] = []
  try:
    total = iterations + warmup
    for i in range(total):
      expected = f"{expected_line_fn(i)}\n".encode("utf-8")
      payload = payload_fn(i)
      t0 = time.perf_counter_ns()
      sender.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), str(receiver_sock))
      listener.settimeout(timeout_s)
      got, _ = listener.recvfrom(4096)
      t1 = time.perf_counter_ns()
      if got != expected:
        raise RuntimeError(f"bridge mismatch at {i}: {got!r}")
      if i >= warmup:
        values.append((t1 - t0) / 1000.0)
  finally:
    sender.close()
  return values


def bench_process_seq_ping(
  seq_bin: Path,
  iterations: int,
  warmup: int,
  via_shell: bool,
) -> tuple[list[float], int]:
  values: list[float] = []
  failed = 0
  total = iterations + warmup
  for i in range(total):
    if via_shell:
      cmd = ["/bin/sh", "-lc", f"{shlex.quote(str(seq_bin))} ping >/dev/null"]
    else:
      cmd = [str(seq_bin), "ping"]

    t0 = time.perf_counter_ns()
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    t1 = time.perf_counter_ns()

    ok = proc.returncode == 0
    if ok and not via_shell:
      ok = "PONG" in proc.stdout

    if not ok:
      failed += 1
      continue

    if i >= warmup:
      values.append((t1 - t0) / 1000.0)

  return values, failed


def print_summary(row: dict[str, float | int | str]) -> None:
  failed = int(row.get("failed", 0))
  failed_part = f" failed={failed}" if failed else ""
  print(
    f"{row['name']:<28} "
    f"n={row['count']:<4} "
    f"{failed_part}"
    f"p50={row['p50_us']:.1f}us "
    f"p95={row['p95_us']:.1f}us "
    f"p99={row['p99_us']:.1f}us "
    f"mean={row['mean_us']:.1f}us"
  )


def main() -> int:
  args = parse_args()
  receiver_repo = Path(args.receiver_repo).expanduser().resolve()
  if not receiver_repo.exists():
    raise RuntimeError(f"receiver repo not found: {receiver_repo}")
  bridge_bin = ensure_bridge_binary(receiver_repo, args.bridge_bin)

  with tempfile.TemporaryDirectory(prefix="seq-uc-bench-") as td:
    tmp = Path(td)
    receiver_sock = tmp / "kar.sock"
    seq_dgram_sock = tmp / "seqd.sock.dgram"
    seq_stream_sock = tmp / "seqd.sock"

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(str(seq_dgram_sock))

    bridge_cmd = [
      str(bridge_bin),
      "--receiver-socket",
      str(receiver_sock),
      "--seq-dgram-socket",
      str(seq_dgram_sock),
      "--seq-stream-socket",
      str(seq_stream_sock),
    ]
    if args.verbose:
      bridge_cmd.append("--verbose")

    bridge = subprocess.Popen(
      bridge_cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
    )

    try:
      if not wait_for_socket(receiver_sock, args.timeout_s):
        _out, err = bridge.communicate(timeout=1.0)
        raise RuntimeError("bridge receiver socket not created\n" + (err or ""))

      expanded_zed_path = os.path.expanduser(args.zed_path)
      scenarios = [
        (
          "run_macro",
          lambda _i: f"RUN {args.macro}",
          lambda _i: {"v": 1, "type": "run", "name": args.macro},
          lambda _i: f"RUN {args.macro}",
        ),
        (
          "open_app_toggle",
          lambda _i: f"OPEN_APP_TOGGLE {args.app}",
          lambda _i: {"v": 1, "type": "open_app_toggle", "app": args.app},
          lambda _i: f"OPEN_APP_TOGGLE {args.app}",
        ),
        (
          "open_with_app",
          lambda _i: f"OPEN_WITH_APP {args.zed_app}:{expanded_zed_path}",
          lambda _i: {"line": f"OPEN_WITH_APP {args.zed_app}:{expanded_zed_path}"},
          lambda _i: f"OPEN_WITH_APP {args.zed_app}:{expanded_zed_path}",
        ),
      ]

      transport_results: list[dict[str, object]] = []
      flat_rows: list[dict[str, float | int | str]] = []

      for name, direct_fn, payload_fn, expected_fn in scenarios:
        direct_values = bench_direct_dgram(
          listener=listener,
          seq_dgram_sock=seq_dgram_sock,
          iterations=args.iterations,
          warmup=args.warmup,
          timeout_s=args.timeout_s,
          line_fn=direct_fn,
        )
        bridge_values = bench_bridge_dgram(
          listener=listener,
          receiver_sock=receiver_sock,
          iterations=args.iterations,
          warmup=args.warmup,
          timeout_s=args.timeout_s,
          payload_fn=payload_fn,
          expected_line_fn=expected_fn,
        )
        direct_row = summarize(f"direct_dgram:{name}", direct_values)
        bridge_row = summarize(f"bridge_via_user_command:{name}", bridge_values)
        flat_rows.extend([direct_row, bridge_row])
        print_summary(direct_row)
        print_summary(bridge_row)
        ratio = (bridge_row["p95_us"] / direct_row["p95_us"]) if direct_row["p95_us"] else 0.0
        print(f"p95 overhead ratio ({name} bridge/direct): {ratio:.2f}x")
        transport_results.append(
          {
            "scenario": name,
            "direct": direct_row,
            "bridge": bridge_row,
            "p95_overhead_ratio": ratio,
          }
        )

      process_rows: list[dict[str, float | int | str]] = []
      if args.include_process_baseline:
        seq_bin = Path(args.seq_bin).expanduser()
        if not seq_bin.exists():
          print(f"skip process baseline: seq binary not found at {seq_bin}")
        else:
          direct_proc_values, direct_proc_failed = bench_process_seq_ping(
            seq_bin=seq_bin,
            iterations=args.iterations,
            warmup=args.warmup,
            via_shell=False,
          )
          shell_proc_values, shell_proc_failed = bench_process_seq_ping(
            seq_bin=seq_bin,
            iterations=args.iterations,
            warmup=args.warmup,
            via_shell=True,
          )
          row_direct_proc = summarize("process_seq_ping", direct_proc_values, direct_proc_failed)
          row_shell_proc = summarize("shell_seq_ping", shell_proc_values, shell_proc_failed)
          process_rows.extend([row_direct_proc, row_shell_proc])
          flat_rows.extend([row_direct_proc, row_shell_proc])
          print_summary(row_direct_proc)
          print_summary(row_shell_proc)

      # Backward-compatible top-level ratio keeps run_macro path semantics.
      run_macro = next((r for r in transport_results if r["scenario"] == "run_macro"), None)
      p95_ratio = run_macro["p95_overhead_ratio"] if run_macro else 0.0
      result = {
        "iterations": args.iterations,
        "warmup": args.warmup,
        "bridge_bin": str(bridge_bin),
        "macro": args.macro,
        "app": args.app,
        "zed_app": args.zed_app,
        "zed_path": expanded_zed_path,
        "transport": transport_results,
        "process_baselines": process_rows,
        "results": flat_rows,
        "p95_overhead_ratio": p95_ratio,
      }

      if args.json_out:
        out = Path(args.json_out)
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"json: {out}")

      return 0
    finally:
      listener.close()
      bridge.terminate()
      try:
        _out, _err = bridge.communicate(timeout=1.0)
      except subprocess.TimeoutExpired:
        bridge.kill()
        bridge.communicate(timeout=1.0)


if __name__ == "__main__":
  raise SystemExit(main())
