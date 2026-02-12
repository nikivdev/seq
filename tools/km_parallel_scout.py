#!/usr/bin/env python3
"""
Scan ~/config/i/kar/config.ts for km("...") macro usages, then inspect the local
Keyboard Maestro macro database to find candidates that could benefit from seq
parallel execution.

Heuristic:
- "parallel now": macros that (after expanding ExecuteMacro) contain >=2 OpenURL actions.
- "parallel later": macros with >=2 ExecuteShellScript actions (likely useful once seq
  has shell-output capture + templating/variables).
"""

from __future__ import annotations

import argparse
import pathlib
import plistlib
import re
from dataclasses import dataclass
from typing import Any


CONFIG_TS = pathlib.Path("/Users/nikiv/config/i/kar/config.ts")
KM_PLIST = pathlib.Path.home() / "Library/Application Support/Keyboard Maestro/Keyboard Maestro Macros.plist"


def extract_km_calls(text: str) -> list[str]:
    # km("...") or km('...') with basic escape handling.
    pat = re.compile(r"\bkm\(\s*([\"'])(.*?)(?<!\\)\1\s*\)", re.DOTALL)
    out: list[str] = []
    for m in pat.finditer(text):
        s = m.group(2)
        # Minimal unescape: enough for typical macro names.
        s = s.replace(r"\\", "\\").replace(r"\'", "'").replace(r"\"", "\"")
        out.append(s)
    return out


@dataclass
class MacroRec:
    name: str
    uid: str
    actions: list[dict[str, Any]]


def load_km_db() -> tuple[dict[str, MacroRec], dict[str, MacroRec]]:
    data = plistlib.load(KM_PLIST.open("rb"))
    name_map: dict[str, MacroRec] = {}
    uid_map: dict[str, MacroRec] = {}
    for grp in data.get("MacroGroups", []) or []:
        for m in grp.get("Macros", []) or []:
            name = str(m.get("Name") or "")
            uid = str(m.get("UID") or "")
            actions = list(m.get("Actions", []) or [])
            if not name or not uid:
                continue
            rec = MacroRec(name=name, uid=uid, actions=actions)
            # Names should be unique; if not, keep the first but still index by UID.
            name_map.setdefault(name, rec)
            uid_map[uid] = rec
    return name_map, uid_map


def expand_actions(actions: list[dict[str, Any]], uid_map: dict[str, MacroRec], max_depth: int = 6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(act_list: list[dict[str, Any]], depth: int, stack: set[str]) -> None:
        for a in act_list:
            t = a.get("MacroActionType")
            if t == "ExecuteMacro" and depth < max_depth:
                uid = a.get("MacroUID") or ""
                if isinstance(uid, str) and uid and uid not in stack:
                    rec = uid_map.get(uid)
                    if rec is not None:
                        stack.add(uid)
                        walk(rec.actions, depth + 1, stack)
                        stack.remove(uid)
                        continue
            # Many KM action types contain nested action lists under keys like:
            # - IfThenElse: ThenActions / ElseActions
            # - Group: Actions
            # - Switch/Repeat/While/etc: various *Actions keys
            for k, v in a.items():
                if not isinstance(k, str) or not k.endswith("Actions"):
                    continue
                if isinstance(v, list):
                    walk([x for x in v if isinstance(x, dict)], depth, stack)
            out.append(a)

    walk(actions, 0, set())
    return out


def summarize(m: MacroRec, uid_map: dict[str, MacroRec]) -> dict[str, Any]:
    flat = expand_actions(m.actions, uid_map)

    open_urls: list[str] = []
    shell_vars: list[str] = []
    shell_cmds: list[str] = []
    for a in flat:
        t = a.get("MacroActionType")
        if t == "OpenURL":
            url = a.get("URL")
            if isinstance(url, str) and url:
                open_urls.append(url)
        elif t == "ExecuteShellScript":
            v = a.get("Variable")
            if isinstance(v, str) and v:
                shell_vars.append(v)
            txt = a.get("Text")
            if isinstance(txt, str) and txt:
                shell_cmds.append(txt.strip())

    return {
        "name": m.name,
        "uid": m.uid,
        "open_url_count": len(open_urls),
        "open_urls": open_urls[:10],
        "shell_count": sum(1 for a in flat if a.get("MacroActionType") == "ExecuteShellScript"),
        "shell_vars": shell_vars[:10],
        "shell_cmds": shell_cmds[:10],
        "flat_action_types": [str(a.get("MacroActionType") or "") for a in flat[:40]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_TS))
    ap.add_argument("--limit", type=int, default=50, help="max macros to print per section")
    ap.add_argument("--grep", default="", help="only include km macros whose name matches this regex/substring")
    args = ap.parse_args()

    text = pathlib.Path(args.config).read_text()
    km_names = sorted(set(extract_km_calls(text)))
    if args.grep:
        try:
            rx = re.compile(args.grep)
            km_names = [n for n in km_names if rx.search(n)]
        except re.error:
            km_names = [n for n in km_names if args.grep in n]

    name_map, uid_map = load_km_db()

    found: list[MacroRec] = []
    missing: list[str] = []
    for n in km_names:
        rec = name_map.get(n)
        if rec is None:
            missing.append(n)
        else:
            found.append(rec)

    summaries = [summarize(m, uid_map) for m in found]

    parallel_now = [s for s in summaries if s["open_url_count"] >= 2]
    parallel_later = [s for s in summaries if s["shell_count"] >= 2 and s["open_url_count"] < 2]
    biggest = sorted(
        summaries,
        key=lambda s: (-(s["open_url_count"] + s["shell_count"]), -len(s["flat_action_types"]), s["name"]),
    )

    def print_section(title: str, items: list[dict[str, Any]]) -> None:
        print(title)
        print("-" * len(title))
        for s in items[: args.limit]:
            print(f"{s['name']}")
            if s["open_url_count"]:
                print(f"  open_url: {s['open_url_count']}  sample={s['open_urls'][:3]}")
            if s["shell_count"]:
                print(f"  shell: {s['shell_count']}  vars={s['shell_vars'][:3]}  cmds={s['shell_cmds'][:2]}")
            print(f"  first_actions={s['flat_action_types'][:8]}")
        print()

    print(f"km_macros_in_config: {len(km_names)}  found_in_km: {len(found)}  missing: {len(missing)}")
    print()
    print_section("Parallel Now (>=2 OpenURL)", sorted(parallel_now, key=lambda s: (-s["open_url_count"], s["name"])))
    print_section("Parallel Later (>=2 ExecuteShellScript)", sorted(parallel_later, key=lambda s: (-s["shell_count"], s["name"])))
    print_section("Most Interesting (by OpenURL+Shell count)", biggest)

    if missing:
        print("Missing In KM (first 30)")
        print("----------------------")
        for n in missing[:30]:
            print(n)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
