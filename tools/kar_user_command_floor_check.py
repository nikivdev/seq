#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description=(
      "Clean-room floor check for Karabiner send_user_command latency. "
      "Runs system readiness, bridge restart, repeated E2E A/B, and transport benchmarks."
    )
  )
  p.add_argument(
    "--receiver-repo",
    default="~/repos/pqrs-org/Karabiner-Elements-user-command-receiver",
    help="Path to Karabiner-Elements-user-command-receiver repo.",
  )
  p.add_argument("--rounds", type=int, default=3, help="Number of repeated E2E runs.")
  p.add_argument("--iterations", type=int, default=20, help="E2E iterations per run.")
  p.add_argument("--warmup", type=int, default=5, help="E2E warmup per run.")
  p.add_argument("--timeout-s", type=float, default=1.6, help="E2E per-attempt timeout.")
  p.add_argument("--poll-ms", type=float, default=5.0, help="E2E frontmost poll interval.")
  p.add_argument("--app", default="Safari", help="Target app name.")
  p.add_argument("--app-frontmost", default="Safari", help="Expected frontmost app name.")
  p.add_argument("--baseline-app", default="Safari", help="Baseline app activated before each attempt.")
  p.add_argument("--include-zed", action="store_true", help="Include zed/open-with-app checks.")
  p.add_argument("--zed-app", default="/System/Volumes/Data/Applications/Zed Preview.app")
  p.add_argument("--zed-path", default="~/config/fish/fn.fish")
  p.add_argument("--zed-frontmost", default="zed")
  p.add_argument("--transport-iterations", type=int, default=120)
  p.add_argument("--transport-warmup", type=int, default=20)
  p.add_argument("--transport-timeout-s", type=float, default=0.05)
  p.add_argument(
    "--force",
    action="store_true",
    help="Run even when system-check says NOT_READY_FOR_CLEAN_TEST.",
  )
  p.add_argument(
    "--require-floor",
    action="store_true",
    help="Exit non-zero when practical_floor_candidate is false.",
  )
  p.add_argument(
    "--json-out",
    default="/tmp/kar_uc_floor_check.json",
    help="Path to write final JSON report.",
  )
  return p.parse_args()


def run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    cmd,
    cwd=str(cwd) if cwd else None,
    check=False,
    capture_output=True,
    text=True,
  )


def extract_json_block(text: str) -> Any:
  start = text.find("{")
  end = text.rfind("}")
  if start == -1 or end == -1 or end <= start:
    raise RuntimeError("JSON block not found in command output")
  return json.loads(text[start : end + 1])


def median_or_zero(values: list[float]) -> float:
  if not values:
    return 0.0
  return float(statistics.median(values))


def spread_pct(values: list[float]) -> float:
  if not values:
    return 0.0
  m = median_or_zero(values)
  if m <= 0:
    return 0.0
  return ((max(values) - min(values)) / m) * 100.0


def row_value(rows: dict[str, dict[str, Any]], name: str, key: str) -> float:
  row = rows.get(name)
  if not row:
    return 0.0
  v = row.get(key, 0.0)
  try:
    return float(v)
  except Exception:
    return 0.0


def main() -> int:
  args = parse_args()
  receiver_repo = Path(args.receiver_repo).expanduser().resolve()
  seq_repo = Path(__file__).resolve().parents[1]

  if not receiver_repo.exists():
    raise RuntimeError(f"receiver repo not found: {receiver_repo}")

  # 1) System readiness gate.
  check = run_cmd(["swift", "run", "kar-uc-system-check", "--json"], cwd=receiver_repo)
  if check.returncode not in (0, 2):
    raise RuntimeError(
      "kar-uc-system-check failed\n"
      f"stdout:\n{check.stdout}\n\nstderr:\n{check.stderr}"
    )
  sys_report = extract_json_block(check.stdout + "\n" + check.stderr)
  system_ready = bool(sys_report.get("ready", False))
  if not system_ready and not args.force:
    print("System is NOT_READY_FOR_CLEAN_TEST. Re-run with --force to continue anyway.")
    print(f"json: {args.json_out}")
    out = {
      "system_check": sys_report,
      "status": "blocked_by_system_pressure",
      "hint": "Use --force to run benchmarks despite pressure.",
    }
    Path(args.json_out).expanduser().write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 2

  # 2) Ensure release bridge is active.
  restart = run_cmd(["python3", "tools/kar_uc_launchd.py", "restart", "--build-if-missing"], cwd=seq_repo)
  if restart.returncode != 0:
    raise RuntimeError(
      "bridge restart failed\n"
      f"stdout:\n{restart.stdout}\n\nstderr:\n{restart.stderr}"
    )

  run_summaries: list[dict[str, Any]] = []
  user_app_p95: list[float] = []
  legacy_app_p95: list[float] = []
  ratio_app: list[float] = []
  user_zed_p95: list[float] = []
  legacy_zed_p95: list[float] = []
  ratio_zed: list[float] = []

  # 3) Repeated E2E A/B runs.
  for i in range(args.rounds):
    with tempfile.NamedTemporaryFile(prefix=f"kar_uc_floor_e2e_{i}_", suffix=".json", delete=False) as tf:
      e2e_json = Path(tf.name)

    cmd = [
      "python3",
      "tools/kar_user_command_e2e_ab.py",
      "--iterations",
      str(args.iterations),
      "--warmup",
      str(args.warmup),
      "--timeout-s",
      str(args.timeout_s),
      "--poll-ms",
      str(args.poll_ms),
      "--app-mode",
      "open",
      "--app",
      args.app,
      "--app-frontmost",
      args.app_frontmost,
      "--baseline-app",
      args.baseline_app,
      "--json-out",
      str(e2e_json),
    ]
    if args.include_zed:
      cmd += [
        "--include-zed",
        "--zed-app",
        args.zed_app,
        "--zed-path",
        args.zed_path,
        "--zed-frontmost",
        args.zed_frontmost,
      ]

    e2e = run_cmd(cmd, cwd=seq_repo)
    if e2e.returncode != 0:
      raise RuntimeError(
        f"E2E run {i + 1} failed\nstdout:\n{e2e.stdout}\n\nstderr:\n{e2e.stderr}"
      )
    e2e_report = json.loads(e2e_json.read_text(encoding="utf-8"))
    rows = {str(r["name"]): r for r in e2e_report.get("rows", [])}

    la = row_value(rows, "legacy_shell->frontmost_app", "p95_us")
    ua = row_value(rows, "user_command->frontmost_app", "p95_us")
    legacy_app_p95.append(la)
    user_app_p95.append(ua)
    ratio_app.append((ua / la) if la > 0 else 0.0)

    run_summary: dict[str, Any] = {
      "run": i + 1,
      "legacy_frontmost_app_p95_us": la,
      "user_frontmost_app_p95_us": ua,
      "ratio_user_vs_legacy_app": (ua / la) if la > 0 else 0.0,
      "user_frontmost_app_failed": int(row_value(rows, "user_command->frontmost_app", "failed")),
      "legacy_frontmost_app_failed": int(row_value(rows, "legacy_shell->frontmost_app", "failed")),
    }

    if args.include_zed:
      lz = row_value(rows, "legacy_shell->frontmost_zed", "p95_us")
      uz = row_value(rows, "user_command->frontmost_zed", "p95_us")
      legacy_zed_p95.append(lz)
      user_zed_p95.append(uz)
      ratio_zed.append((uz / lz) if lz > 0 else 0.0)
      run_summary.update(
        {
          "legacy_frontmost_zed_p95_us": lz,
          "user_frontmost_zed_p95_us": uz,
          "ratio_user_vs_legacy_zed": (uz / lz) if lz > 0 else 0.0,
          "user_frontmost_zed_failed": int(row_value(rows, "user_command->frontmost_zed", "failed")),
          "legacy_frontmost_zed_failed": int(row_value(rows, "legacy_shell->frontmost_zed", "failed")),
        }
      )

    run_summaries.append(run_summary)

  # 4) Transport benchmark.
  with tempfile.NamedTemporaryFile(prefix="kar_uc_floor_transport_", suffix=".json", delete=False) as tf:
    transport_json = Path(tf.name)
  bench_cmd = [
    "python3",
    "tools/kar_user_command_latency_bench.py",
    "--iterations",
    str(args.transport_iterations),
    "--warmup",
    str(args.transport_warmup),
    "--timeout-s",
    str(args.transport_timeout_s),
    "--include-process-baseline",
    "--json-out",
    str(transport_json),
  ]
  bench = run_cmd(bench_cmd, cwd=seq_repo)
  if bench.returncode != 0:
    raise RuntimeError(
      f"transport benchmark failed\nstdout:\n{bench.stdout}\n\nstderr:\n{bench.stderr}"
    )
  bench_report = json.loads(transport_json.read_text(encoding="utf-8"))
  bench_rows = {str(r["name"]): r for r in bench_report.get("results", [])}

  # 5) Aggregate and verdict.
  med_user_app = median_or_zero(user_app_p95)
  med_legacy_app = median_or_zero(legacy_app_p95)
  med_ratio_app = median_or_zero(ratio_app)
  app_spread = spread_pct(user_app_p95)
  med_user_zed = median_or_zero(user_zed_p95)
  med_legacy_zed = median_or_zero(legacy_zed_p95)
  med_ratio_zed = median_or_zero(ratio_zed)
  zed_spread = spread_pct(user_zed_p95)

  practical_floor_candidate = (
    bool(system_ready)
    and med_user_app > 0
    and med_ratio_app > 0
    and med_ratio_app <= 0.50
    and app_spread <= 20.0
  )
  if args.include_zed:
    practical_floor_candidate = (
      practical_floor_candidate
      and med_user_zed > 0
      and med_ratio_zed > 0
      and med_ratio_zed <= 0.50
      and zed_spread <= 20.0
    )

  summary = {
    "system_check": sys_report,
    "rounds": args.rounds,
    "runs": run_summaries,
    "aggregates": {
      "median_user_frontmost_app_p95_us": med_user_app,
      "median_legacy_frontmost_app_p95_us": med_legacy_app,
      "median_ratio_user_vs_legacy_app": med_ratio_app,
      "user_frontmost_app_spread_pct": app_spread,
      "median_user_frontmost_zed_p95_us": med_user_zed,
      "median_legacy_frontmost_zed_p95_us": med_legacy_zed,
      "median_ratio_user_vs_legacy_zed": med_ratio_zed,
      "user_frontmost_zed_spread_pct": zed_spread,
    },
    "transport": {
      "bridge_run_macro_p95_us": row_value(bench_rows, "bridge_via_user_command:run_macro", "p95_us"),
      "process_seq_ping_p95_us": row_value(bench_rows, "process_seq_ping", "p95_us"),
      "shell_seq_ping_p95_us": row_value(bench_rows, "shell_seq_ping", "p95_us"),
      "bridge_run_macro_failed": int(row_value(bench_rows, "bridge_via_user_command:run_macro", "failed")),
    },
    "verdict": {
      "practical_floor_candidate": practical_floor_candidate,
      "criteria": {
        "system_ready_required": True,
        "median_ratio_user_vs_legacy_max": 0.50,
        "user_app_spread_pct_max": 20.0,
        "user_zed_spread_pct_max": 20.0 if args.include_zed else None,
      },
    },
  }

  out_path = Path(args.json_out).expanduser()
  out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

  print("kar-uc-floor-check")
  print(f"system_ready: {system_ready}")
  print(
    f"frontmost_app p95 median: user={med_user_app:.1f}us legacy={med_legacy_app:.1f}us "
    f"ratio={med_ratio_app:.3f} spread={app_spread:.1f}%"
  )
  if args.include_zed:
    print(
      f"frontmost_zed p95 median: user={med_user_zed:.1f}us legacy={med_legacy_zed:.1f}us "
      f"ratio={med_ratio_zed:.3f} spread={zed_spread:.1f}%"
    )
  print(
    f"transport p95: bridge_run_macro={summary['transport']['bridge_run_macro_p95_us']:.1f}us "
    f"process_seq_ping={summary['transport']['process_seq_ping_p95_us']:.1f}us "
    f"shell_seq_ping={summary['transport']['shell_seq_ping_p95_us']:.1f}us"
  )
  print(f"practical_floor_candidate: {practical_floor_candidate}")
  print(f"json: {out_path}")
  if args.require_floor and not practical_floor_candidate:
    return 3
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
