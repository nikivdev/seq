#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description=(
      "Smoke test Karabiner user-command -> seq bridge forwarding using a mock seqd dgram socket."
    )
  )
  p.add_argument(
    "--receiver-repo",
    default="~/repos/pqrs-org/Karabiner-Elements-user-command-receiver",
    help="Path to KarabinerElementsUserCommandReceiver repo.",
  )
  p.add_argument(
    "--bridge-bin",
    default="",
    help=(
      "Bridge binary path. Default: <receiver-repo>/.build/debug/seq-user-command-bridge "
      "(builds it if missing)."
    ),
  )
  p.add_argument(
    "--macro",
    default="open Safari new tab",
    help="Macro name to send in payload.",
  )
  p.add_argument("--timeout-s", type=float, default=3.0, help="Timeout in seconds.")
  p.add_argument("--verbose", action="store_true")
  return p.parse_args()


def ensure_bridge_binary(receiver_repo: Path, bridge_bin: str) -> Path:
  if bridge_bin:
    path = Path(os.path.expanduser(bridge_bin))
  else:
    path = receiver_repo / ".build/debug/seq-user-command-bridge"
  if path.exists():
    return path

  cmd = ["make", "build-bridge"]
  res = subprocess.run(cmd, cwd=str(receiver_repo), check=False, capture_output=True, text=True)
  if res.returncode != 0:
    raise RuntimeError(
      "failed to build bridge:\n"
      + res.stdout
      + "\n"
      + res.stderr
    )
  if not path.exists():
    raise RuntimeError(f"bridge binary not found after build: {path}")
  return path


def main() -> int:
  args = parse_args()
  receiver_repo = Path(os.path.expanduser(args.receiver_repo)).resolve()
  if not receiver_repo.exists():
    print(f"error: receiver repo not found: {receiver_repo}", file=sys.stderr)
    return 2

  try:
    bridge_bin = ensure_bridge_binary(receiver_repo, args.bridge_bin)
  except Exception as e:
    print(f"error: {e}", file=sys.stderr)
    return 3

  with tempfile.TemporaryDirectory(prefix="seq-uc-smoke-") as td:
    tmp = Path(td)
    recv_sock = tmp / "kar.sock"
    seq_dgram = tmp / "seqd.sock.dgram"
    seq_stream = tmp / "seqd.sock"
    expected = f"RUN {args.macro}\n".encode("utf-8")
    got: dict[str, bytes] = {"data": b""}
    done = threading.Event()

    def dgram_listener() -> None:
      s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
      try:
        s.bind(str(seq_dgram))
        s.settimeout(args.timeout_s)
        data, _ = s.recvfrom(4096)
        got["data"] = data
        done.set()
      except Exception:
        pass
      finally:
        s.close()

    t = threading.Thread(target=dgram_listener, daemon=True)
    t.start()

    bridge_cmd = [
      str(bridge_bin),
      "--receiver-socket",
      str(recv_sock),
      "--seq-dgram-socket",
      str(seq_dgram),
      "--seq-stream-socket",
      str(seq_stream),
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
      deadline = time.time() + args.timeout_s
      while time.time() < deadline:
        if recv_sock.exists():
          break
        if bridge.poll() is not None:
          break
        time.sleep(0.02)

      if not recv_sock.exists():
        stderr = bridge.stderr.read() if bridge.stderr else ""
        print("error: bridge did not create receiver socket", file=sys.stderr)
        if stderr:
          print(stderr, file=sys.stderr)
        return 4

      payload = {"v": 1, "type": "run", "name": args.macro}
      sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
      try:
        sender.sendto(json.dumps(payload).encode("utf-8"), str(recv_sock))
      finally:
        sender.close()

      if not done.wait(args.timeout_s):
        stderr = bridge.stderr.read() if bridge.stderr else ""
        print("error: no forwarded datagram received from bridge", file=sys.stderr)
        if stderr:
          print(stderr, file=sys.stderr)
        return 5

      if got["data"] != expected:
        print("error: forwarded payload mismatch", file=sys.stderr)
        print(f"expected: {expected!r}", file=sys.stderr)
        print(f"actual:   {got['data']!r}", file=sys.stderr)
        return 6

      print("ok: bridge forwarded expected command")
      print(f"bridge: {bridge_bin}")
      print(f"receiver_socket: {recv_sock}")
      print(f"forwarded: {got['data'].decode('utf-8', errors='replace').rstrip()}")
      return 0
    finally:
      bridge.terminate()
      try:
        bridge.wait(timeout=1.0)
      except subprocess.TimeoutExpired:
        bridge.kill()
      t.join(timeout=0.2)


if __name__ == "__main__":
  raise SystemExit(main())
