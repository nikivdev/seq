#!/usr/bin/env python3
"""Dispatch one payload to BlazingMQ via bmqtool.

Reads payload from stdin and posts a base64-safe envelope string:
  seqb64:<base64(payload_utf8)>
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys


def main() -> int:
    payload = sys.stdin.read()
    if payload is None:
        payload = ""
    payload = payload.rstrip("\n")
    if not payload:
        print("error: empty payload", file=sys.stderr)
        return 1

    bmqtool_bin = os.environ.get("SEQ_BMQ_BMQTOOL_BIN", "bmqtool")
    broker = os.environ.get("SEQ_BMQ_BROKER", "tcp://localhost:30114")
    queue_uri = os.environ.get("SEQ_BMQ_QUEUE_URI", "").strip()
    timeout_s = float(os.environ.get("SEQ_BMQ_BMQTOOL_TIMEOUT_S", "5.0"))

    if not queue_uri:
        print("error: SEQ_BMQ_QUEUE_URI is required", file=sys.stderr)
        return 1

    wrapped = "seqb64:" + base64.b64encode(payload.encode("utf-8")).decode("ascii")

    # Keep it one-shot for reliability; bridge stays async so this does not hit key latency.
    script = "\n".join(
        [
            "start",
            f"open uri={queue_uri}",
            f"post uri={queue_uri} payload={wrapped}",
            "stop",
            "quit",
            "",
        ]
    )

    try:
        proc = subprocess.run(
            [bmqtool_bin, "--mode", "cli", "--broker", broker],
            input=script,
            text=True,
            capture_output=True,
            timeout=max(0.5, timeout_s),
            check=False,
        )
    except Exception as exc:
        print(f"error: failed to run bmqtool: {exc}", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "bmqtool_failed").strip()
        print(err, file=sys.stderr)
        return proc.returncode

    out = (proc.stdout or "").strip()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
