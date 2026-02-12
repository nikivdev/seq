#!/usr/bin/env python3
import json
import sys
import time


def parse_kv_subject(subject: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in subject.split("\t"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k] = v
    return out


def main() -> int:
    pending = None  # {"mode": str, "prev": str, "target": str, "front": str, "ts_ms": int, "t0": float}
    timeout_s = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        name = obj.get("name", "")

        # Timeout handling (wall clock, not event clock)
        if pending is not None and (time.monotonic() - pending["t0"]) > timeout_s:
            sys.stdout.write("MISS ")
            sys.stdout.write(pending["mode"])
            sys.stdout.write(" -> app.activate ")
            sys.stdout.write(f"prev={pending['prev']} target={pending['target']} front={pending['front']}\n")
            sys.stdout.flush()
            pending = None

        if name == "cli.open_app_toggle.action":
            subject = obj.get("subject", "") or ""
            kv = parse_kv_subject(subject)
            decision = kv.get("decision", "")
            if decision in ("cmd_tab", "open_prev_no_ax", "open_prev"):
                pending = {
                    "mode": decision,
                    "prev": kv.get("prev", ""),
                    "target": kv.get("target", ""),
                    "front": kv.get("front", ""),
                    "ts_ms": int(obj.get("ts_ms") or 0),
                    "t0": time.monotonic(),
                }
            continue

        if name == "app.activate" and pending is not None:
            subj = obj.get("subject", "")
            if not subj:
                continue
            if pending["mode"] == "cmd_tab":
                # We don't know what Cmd-Tab will pick, but it must not be the target app.
                if subj != pending["target"]:
                    sys.stdout.write(f"OK cmd_tab -> app.activate app={subj}\n")
                    sys.stdout.flush()
                    pending = None
            else:
                if subj == pending["prev"]:
                    sys.stdout.write(f"OK {pending['mode']} -> app.activate app={subj}\n")
                    sys.stdout.flush()
                    pending = None

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
