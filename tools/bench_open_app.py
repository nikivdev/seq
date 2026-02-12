#!/usr/bin/env python3
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from typing import List


def run_cmd(args: List[str]) -> int:
    return subprocess.call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def front_app() -> str:
    script = 'tell application "System Events" to name of first process whose frontmost is true'
    out = subprocess.check_output(["/usr/bin/osascript", "-e", script], stderr=subprocess.DEVNULL)
    name = out.decode("utf-8").strip()
    if name == "missing value":
        return ""
    return name


def wait_front(app: str, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if front_app() == app:
            return True
        time.sleep(0.01)
    return False


def bench_one(label: str, run_args: List[str], target_app: str, baseline_app: str, iters: int, timeout_s: float, measure: str) -> List[float]:
    times: List[float] = []
    for _ in range(iters):
        if measure == "focus":
            run_cmd(["/usr/bin/open", "-a", baseline_app])
            if not wait_front(baseline_app, timeout_s):
                print(f"{label}: failed to reach baseline app '{baseline_app}'", file=sys.stderr)
                continue
        start = time.monotonic()
        run_cmd(run_args)
        end = time.monotonic()
        if measure == "focus":
            ok = wait_front(target_app, timeout_s)
            if not ok:
                print(f"{label}: timeout waiting for '{target_app}'", file=sys.stderr)
                continue
        times.append((end - start) * 1000.0)
        time.sleep(0.05)
    return times


def print_stats(label: str, values: List[float]) -> None:
    if not values:
        print(f"{label}: no samples")
        return
    values_sorted = sorted(values)
    p50 = values_sorted[int(0.50 * (len(values_sorted) - 1))]
    p95 = values_sorted[int(0.95 * (len(values_sorted) - 1))]
    mean = statistics.mean(values_sorted)
    print(f"{label}: n={len(values_sorted)} mean={mean:.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark open-app latency for seq vs KM.")
    parser.add_argument("--app", default="Comet", help="Target app name")
    parser.add_argument("--baseline", default="Finder", help="Baseline app to switch to before each run")
    parser.add_argument("--iters", type=int, default=15, help="Iterations per command")
    parser.add_argument("--timeout", type=float, default=3.0, help="Timeout seconds to wait for front app")
    parser.add_argument("--seq-bin", default="/Users/nikiv/code/seq/cli/cpp/out/bin/seq", help="Path to seq binary")
    parser.add_argument("--seq-mode", choices=["run", "open-app", "open-app-toggle"], default="run", help="Seq command mode")
    parser.add_argument("--measure", choices=["focus", "dispatch"], default="focus", help="Measure focus-change or dispatch time")
    parser.add_argument("--km-macro", default="open: Comet", help="Keyboard Maestro macro name")
    parser.add_argument("--only", choices=["seq", "km", "both"], default="both", help="Which benchmark to run")
    args = parser.parse_args()

    target_app = args.app
    baseline_app = args.baseline

    results: List[tuple[str, List[float]]] = []

    if args.only in ("seq", "both"):
        if args.seq_mode == "open-app":
            seq_args = [args.seq_bin, "open-app", target_app]
        elif args.seq_mode == "open-app-toggle":
            seq_args = [args.seq_bin, "open-app-toggle", target_app]
        else:
            seq_args = [args.seq_bin, "run", f"open: {target_app}"]
        seq_times = bench_one("seq", seq_args, target_app, baseline_app, args.iters, args.timeout, args.measure)
        results.append(("seq", seq_times))

    if args.only in ("km", "both"):
        km_script = f'tell application "Keyboard Maestro Engine" to do script "{args.km_macro}"'
        km_args = ["/usr/bin/osascript", "-e", km_script]
        km_times = bench_one("km", km_args, target_app, baseline_app, args.iters, args.timeout, args.measure)
        results.append(("km", km_times))

    for label, vals in results:
        print_stats(label, vals)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
