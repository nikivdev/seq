#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


LABEL = "dev.nikiv.seq-user-command-bridge"


def default_receiver_socket() -> str:
  uid = os.getuid()
  return f"/Library/Application Support/org.pqrs/tmp/user/{uid}/user_command_receiver.sock"


def default_plist_path() -> Path:
  return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def default_bridge_bin() -> Path:
  repo = Path.home() / "repos" / "pqrs-org" / "Karabiner-Elements-user-command-receiver"
  return repo / ".build" / "release" / "seq-user-command-bridge"


def default_log_dir() -> Path:
  return Path.home() / "code" / "seq" / "cli" / "cpp" / "out" / "logs"


def launchd_target(label: str) -> str:
  return f"gui/{os.getuid()}/{label}"


def run(
  cmd: list[str], check: bool = True, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    cmd,
    check=check,
    capture_output=True,
    text=True,
    cwd=str(cwd) if cwd is not None else None,
  )


def build_plist(
  label: str,
  bridge_bin: Path,
  receiver_socket: str,
  seq_stream_socket: str,
  seq_dgram_socket: str,
  high_priority: bool,
  stdout_log: Path,
  stderr_log: Path,
) -> dict:
  env = {
    "SEQ_USER_COMMAND_SOCKET_PATH": receiver_socket,
    "SEQ_SOCKET_PATH": seq_stream_socket,
    "SEQ_DGRAM_SOCKET_PATH": seq_dgram_socket,
    "SEQ_BRIDGE_HIGH_PRIORITY": "1" if high_priority else "0",
  }

  return {
    "Label": label,
    "ProgramArguments": [str(bridge_bin)],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Interactive",
    "EnvironmentVariables": env,
    "StandardOutPath": str(stdout_log),
    "StandardErrorPath": str(stderr_log),
    "SoftResourceLimits": {
      "NumberOfFiles": 10240,
    },
  }


def write_plist(path: Path, payload: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("wb") as f:
    plistlib.dump(payload, f, sort_keys=False)


def ensure_bridge_binary(bridge_bin: Path, build_if_missing: bool) -> None:
  if bridge_bin.exists():
    return
  if not build_if_missing:
    raise FileNotFoundError(
      f"bridge binary not found: {bridge_bin}\n"
      "build it first with: cd ~/repos/pqrs-org/Karabiner-Elements-user-command-receiver && make build-bridge-release"
    )
  receiver_repo = bridge_bin.parent.parent.parent
  if not receiver_repo.exists():
    raise FileNotFoundError(f"bridge repo not found: {receiver_repo}")
  res = run(["make", "build-bridge-release"], check=False, cwd=receiver_repo)
  if res.returncode != 0:
    raise RuntimeError(
      "failed to build release bridge:\n"
      + res.stdout
      + "\n"
      + res.stderr
    )
  if not bridge_bin.exists():
    raise FileNotFoundError(f"bridge binary still missing after build: {bridge_bin}")


def bootout(label: str, plist_path: Path) -> None:
  target = launchd_target(label)
  run(["launchctl", "bootout", target], check=False)
  run(["launchctl", "bootout", target, str(plist_path)], check=False)


def bootstrap(label: str, plist_path: Path) -> None:
  target = f"gui/{os.getuid()}"
  res = run(["launchctl", "bootstrap", target, str(plist_path)], check=False)
  if res.returncode != 0:
    raise RuntimeError(
      "launchctl bootstrap failed:\n"
      + res.stdout
      + "\n"
      + res.stderr
    )


def kickstart(label: str) -> None:
  target = launchd_target(label)
  res = run(["launchctl", "kickstart", "-k", target], check=False)
  if res.returncode != 0:
    raise RuntimeError(
      "launchctl kickstart failed:\n"
      + res.stdout
      + "\n"
      + res.stderr
    )


def status(label: str) -> int:
  target = launchd_target(label)
  res = run(["launchctl", "print", target], check=False)
  if res.returncode != 0:
    sys.stdout.write(f"not loaded: {target}\n")
    if res.stderr.strip():
      sys.stdout.write(res.stderr)
      if not res.stderr.endswith("\n"):
        sys.stdout.write("\n")
    return 1
  sys.stdout.write(res.stdout)
  return 0


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description="Manage always-on launchd agent for seq-user-command-bridge."
  )
  sub = p.add_subparsers(dest="action", required=True)

  for name in ("install", "restart", "stop", "status", "uninstall", "print-plist"):
    sp = sub.add_parser(name)
    if name in ("install", "restart", "print-plist"):
      sp.add_argument("--label", default=LABEL)
      sp.add_argument("--plist-path", type=Path, default=default_plist_path())
      sp.add_argument("--bridge-bin", type=Path, default=default_bridge_bin())
      sp.add_argument("--receiver-socket", default=default_receiver_socket())
      sp.add_argument("--seq-stream-socket", default="/tmp/seqd.sock")
      sp.add_argument("--seq-dgram-socket", default="/tmp/seqd.sock.dgram")
      sp.add_argument("--high-priority", action="store_true", default=True)
      sp.add_argument("--normal-priority", dest="high_priority", action="store_false")
      sp.add_argument("--build-if-missing", action="store_true")
      sp.add_argument("--log-dir", type=Path, default=default_log_dir())
    if name in ("stop", "status", "uninstall"):
      sp.add_argument("--label", default=LABEL)
      if name != "status":
        sp.add_argument("--plist-path", type=Path, default=default_plist_path())

  return p.parse_args()


def main() -> int:
  args = parse_args()

  if args.action in ("install", "restart", "print-plist"):
    bridge_bin: Path = args.bridge_bin.expanduser()
    plist_path: Path = args.plist_path.expanduser()
    log_dir: Path = args.log_dir.expanduser()
    stdout_log = log_dir / "kar_uc_bridge.stdout.log"
    stderr_log = log_dir / "kar_uc_bridge.stderr.log"

    payload = build_plist(
      label=args.label,
      bridge_bin=bridge_bin,
      receiver_socket=args.receiver_socket,
      seq_stream_socket=args.seq_stream_socket,
      seq_dgram_socket=args.seq_dgram_socket,
      high_priority=args.high_priority,
      stdout_log=stdout_log,
      stderr_log=stderr_log,
    )

    if args.action == "print-plist":
      plistlib.dump(payload, sys.stdout.buffer, sort_keys=False)
      return 0

    ensure_bridge_binary(bridge_bin, args.build_if_missing)
    log_dir.mkdir(parents=True, exist_ok=True)
    write_plist(plist_path, payload)
    bootout(args.label, plist_path)
    bootstrap(args.label, plist_path)
    if args.action == "restart":
      kickstart(args.label)
    print(f"loaded: {launchd_target(args.label)}")
    print(f"plist:  {plist_path}")
    print(f"stdout: {stdout_log}")
    print(f"stderr: {stderr_log}")
    return 0

  if args.action == "stop":
    bootout(args.label, args.plist_path.expanduser())
    print(f"stopped: {launchd_target(args.label)}")
    return 0

  if args.action == "uninstall":
    plist_path: Path = args.plist_path.expanduser()
    bootout(args.label, plist_path)
    if plist_path.exists():
      plist_path.unlink()
      print(f"removed: {plist_path}")
    print(f"uninstalled: {launchd_target(args.label)}")
    return 0

  if args.action == "status":
    return status(args.label)

  print(f"unknown action: {args.action}", file=sys.stderr)
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
