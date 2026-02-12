#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


SEQ_LOCAL = Path("/Users/nikiv/code/seq/seq.macros.local.yaml")
KAR_CONFIG = Path("/Users/nikiv/config/i/kar/config.ts")

BEGIN = "# BEGIN km-arc-import"
END = "# END km-arc-import"


def sh(cmd: list[str]) -> str:
    # Keep stderr quiet: most arc:* entries in kar config are seq-native already (no KM macro),
    # and `km inspect` is noisy on "not found".
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, output=p.stdout, stderr=p.stderr)
    return p.stdout


def km_inspect(name: str) -> list[dict] | None:
    try:
        out = sh(["km", "inspect", name])
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def km_macro_name_by_id(uid: str) -> str | None:
    script = (
        'tell application "Keyboard Maestro" to get name of first macro whose id is "{}"'.format(uid)
    )
    try:
        return sh(["osascript", "-e", script]).strip() or None
    except subprocess.CalledProcessError:
        return None


def find_arc_names_in_config(text: str) -> list[str]:
    names: list[str] = []
    seen = set()
    # km("arc: ...") or seqSocket("arc: ...") or seq("arc: ...", ...)
    # NOTE: keep regex unescaped (raw string) so we match literal "(" in source text.
    for m in re.finditer(r'\b(?:km|seqSocket|seq)\(\s*"(?P<name>arc:[^"]+)"', text):
        n = m.group("name")
        if n not in seen:
            seen.add(n)
            names.append(n)
    return names


def keycode_to_token(keycode: int) -> str | None:
    # Digits: these match kVK_ANSI_0..9 codes on macOS (0=29, 1=18, ..., 9=25).
    digits = {18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7", 28: "8", 25: "9", 29: "0"}
    if keycode in digits:
        return digits[keycode]
    # Common keys we already parse in seq.
    if keycode == 48:
        return "tab"
    if keycode == 49:
        return "space"
    if keycode == 36:
        return "return"
    if keycode == 53:
        return "escape"
    return None


def mods_to_tokens(mods: int) -> list[str]:
    # KM modifier mask (empirically: cmd=256, ctrl=4096; these align with common KM docs)
    out: list[str] = []
    if mods & 4096:
        out.append("ctrl")
    if mods & 2048:
        out.append("opt")
    if mods & 512:
        out.append("shift")
    if mods & 256:
        out.append("cmd")
    return out


def km_keystroke_to_spec(action: dict) -> str | None:
    if action.get("MacroActionType") != "SimulateKeystroke":
        return None
    keycode = action.get("KeyCode")
    mods = action.get("Modifiers", 0)
    if not isinstance(keycode, int) or not isinstance(mods, int):
        return None
    key = keycode_to_token(keycode)
    if not key:
        return None
    toks = mods_to_tokens(mods)
    toks.append(key)
    return "+".join(toks)


@dataclass
class ArcSeq:
    name: str
    steps: list[tuple[str, str, str | None]]  # (action, arg, app)


def extract_arc_sequence(macro_name: str) -> ArcSeq | None:
    actions = km_inspect(macro_name)
    if not actions:
        return None

    # We only handle Arc-focused macros.
    app_name = None
    for a in actions:
        if a.get("MacroActionType") == "ActivateApplication":
            app = (a.get("Application") or {}).get("Name")
            if isinstance(app, str):
                app_name = app
            break
    if app_name != "Arc":
        return None

    steps: list[tuple[str, str, str | None]] = [("open_app", "Arc", None)]

    for a in actions:
        t = a.get("MacroActionType")
        if t == "ExecuteMacro":
            uid = a.get("MacroUID")
            if not isinstance(uid, str) or not uid:
                continue
            subname = km_macro_name_by_id(uid)
            if not subname:
                continue
            try:
                sub_actions = km_inspect(subname)
            except Exception:
                continue
            # Recognize: SelectMenuItem ["Spaces","21"] in Arc.
            if (
                len(sub_actions) == 1
                and sub_actions[0].get("MacroActionType") == "SelectMenuItem"
                and isinstance(sub_actions[0].get("Menu"), list)
            ):
                menu = sub_actions[0]["Menu"]
                if len(menu) >= 2 and str(menu[0]) == "Spaces":
                    steps.append(("select_menu_item", f"{menu[0]}/{menu[1]}", "Arc"))
            continue

        if t == "SelectMenuItem" and isinstance(a.get("Menu"), list):
            menu = a["Menu"]
            if len(menu) >= 2 and str(menu[0]) == "Spaces":
                steps.append(("select_menu_item", f"{menu[0]}/{menu[1]}", "Arc"))
            continue

        if t == "SimulateKeystroke":
            spec = km_keystroke_to_spec(a)
            if spec:
                steps.append(("keystroke", spec, None))
            continue

    if len(steps) <= 1:
        return None
    return ArcSeq(name=macro_name, steps=steps)


def render_yaml(seqs: list[ArcSeq]) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    lines: list[str] = []
    lines.append(BEGIN)
    lines.append("# Generated from Keyboard Maestro arc:* macros (do not edit by hand).")
    for s in seqs:
        lines.append(f'- name: "{esc(s.name)}"')
        lines.append("  action: sequence")
        lines.append('  arg: ""')
        lines.append("  steps:")
        for action, arg, app in s.steps:
            lines.append(f"    - action: {action}")
            if app:
                lines.append(f'      app: "{app}"')
            lines.append(f'      arg: "{esc(arg)}"')
    lines.append(END)
    return "\n".join(lines) + "\n"


def main() -> int:
    cfg = KAR_CONFIG.read_text()
    names = find_arc_names_in_config(cfg)
    seqs: list[ArcSeq] = []
    for n in names:
        s = extract_arc_sequence(n)
        if s:
            seqs.append(s)

    if not SEQ_LOCAL.exists():
        SEQ_LOCAL.write_text(render_yaml(seqs))
        print(f"wrote {SEQ_LOCAL} ({len(seqs)} macros)")
        return 0

    text = SEQ_LOCAL.read_text()
    block = render_yaml(seqs)
    if BEGIN in text and END in text:
        pre = text.split(BEGIN, 1)[0]
        post = text.split(END, 1)[1]
        out = pre + block + post.lstrip("\n")
    else:
        out = text.rstrip() + "\n\n" + block
    SEQ_LOCAL.write_text(out)
    print(f"updated {SEQ_LOCAL} ({len(seqs)} macros)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
