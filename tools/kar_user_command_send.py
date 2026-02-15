#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _karabiner_15915_default_socket() -> str:
  uid = os.geteuid()
  return f"/Library/Application Support/org.pqrs/tmp/user/{uid}/user_command_receiver.sock"


def default_receiver_socket() -> str:
  env_path = os.environ.get("SEQ_USER_COMMAND_SOCKET_PATH")
  if env_path:
    return os.path.expanduser(env_path)

  # Karabiner-Elements 15.9.15+ default.
  preferred = os.path.expanduser(_karabiner_15915_default_socket())
  # Legacy pilot path used during earlier bridge experiments.
  legacy = os.path.expanduser("~/.local/share/karabiner/tmp/karabiner_user_command_receiver.sock")

  # If one socket exists, pick it so ad-hoc testing "just works" across versions.
  if os.path.exists(preferred):
    return preferred
  if os.path.exists(legacy):
    return legacy
  return preferred


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description="Send a JSON payload to Karabiner user-command receiver socket."
  )
  p.add_argument(
    "--socket",
    default=default_receiver_socket(),
    help="Receiver socket path (default: SEQ_USER_COMMAND_SOCKET_PATH or Karabiner path).",
  )

  group = p.add_mutually_exclusive_group(required=False)
  group.add_argument("--run", help="Macro name. Sends {v:1,type:run,name:<value>}.")
  group.add_argument(
    "--open-app-toggle",
    dest="open_app_toggle",
    help="App name. Sends {v:1,type:open_app_toggle,app:<value>}.",
  )
  group.add_argument("--line", help="Raw seqd line, e.g. 'PING' or 'RUN New Linear task'.")
  group.add_argument("--json", help="Full JSON payload string.")
  p.add_argument(
    "--json-file",
    help="Load full JSON payload from file (alternative to --json).",
  )
  p.add_argument(
    "--print-only",
    action="store_true",
    help="Print payload and exit without sending.",
  )
  return p.parse_args()


def build_payload(args: argparse.Namespace) -> dict | str:
  if args.json_file:
    raw = Path(args.json_file).read_text(encoding="utf-8")
    return json.loads(raw)
  if args.json:
    return json.loads(args.json)
  if args.open_app_toggle:
    return {"v": 1, "type": "open_app_toggle", "app": args.open_app_toggle}
  if args.line:
    return {"line": args.line}
  macro = args.run or "open Safari new tab"
  return {"v": 1, "type": "run", "name": macro}


def send_payload(socket_path: str, payload: dict | str) -> None:
  encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
  s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
  try:
    s.sendto(encoded, socket_path)
  finally:
    s.close()


def main() -> int:
  args = parse_args()
  payload = build_payload(args)
  socket_path = os.path.expanduser(args.socket)

  print(f"socket: {socket_path}")
  print("payload:")
  print(json.dumps(payload, ensure_ascii=False, indent=2))

  if args.print_only:
    return 0

  try:
    send_payload(socket_path, payload)
  except FileNotFoundError:
    print(f"error: socket not found: {socket_path}", file=sys.stderr)
    return 2
  except ConnectionRefusedError:
    print(
      f"error: receiver refused datagram at: {socket_path} (bridge not running?)",
      file=sys.stderr,
    )
    return 3
  except OSError as e:
    print(f"error: send failed: {e}", file=sys.stderr)
    return 4

  print("sent")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
