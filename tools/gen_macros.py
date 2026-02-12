#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Tuple, Optional


CONFIG_PATH = Path("/Users/nikiv/config/i/kar/config.ts")
OUT_PATH = Path("/Users/nikiv/code/seq/seq.macros.yaml")
ARC_ALIASES_PATH = Path("/Users/nikiv/code/seq/tools/arc_aliases.tsv")
TELEGRAM_ALIASES_PATH = Path("/Users/nikiv/code/seq/tools/telegram_aliases.tsv")
WEB_ALIASES_PATH = Path("/Users/nikiv/code/seq/tools/web_aliases.tsv")

SWITCH_WINDOW_OR_APP = (
    "switch between windows of same app (or switch to another app if no more than 1 window)"
)

def find_call_strings(text: str, func_name: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(text)
    needle = f"{func_name}("
    while True:
        pos = text.find(needle, i)
        if pos == -1:
            break
        line_start = text.rfind("\n", 0, pos) + 1
        line_prefix = text[line_start:pos]
        if "//" in line_prefix:
            i = pos + 3
            continue
        j = pos + len(needle)
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n:
            break
        quote = text[j]
        if quote not in ("\"", "'"):
            i = j + 1
            continue
        j += 1
        buf = []
        while j < n:
            c = text[j]
            if c == "\\":
                if j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                break
            if c == quote:
                out.append("".join(buf))
                j += 1
                break
            buf.append(c)
            j += 1
        i = j
    return out


def strip_paren_suffix(value: str) -> str:
    return re.sub(r"\s+\([^)]*\)$", "", value).strip()


def guess_url(value: str) -> Optional[str]:
    raw = strip_paren_suffix(value)
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("localhost:") or raw.startswith("localhost/"):
        return "http://" + raw
    if " " in raw:
        return None
    if re.search(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}", raw):
        return "https://" + raw
    return None


def escape_yaml(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\"", "\\\"")

def load_tsv_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            continue
        k, v = line.split("\t", 1)
        k = k.strip()
        v = v.strip()
        if k and v:
            out[k] = v
    return out

def find_seq_step_macros(text: str) -> dict[str, list[tuple[str, str]]]:
    """
    Extremely narrow parser for:

      seq("Name", [openApp("Arc"), keystroke("ctrl+1"), keystroke("cmd+6")])

    Returns: { "Name": [("open_app","Arc"), ("keystroke","ctrl+1"), ...] }
    """
    out: dict[str, list[tuple[str, str]]] = {}

    # Scan for seq("<name>", [ ... ]) without backtracking across unrelated seq(...) calls.
    i = 0
    needle = "seq("
    n = len(text)
    while True:
        pos = text.find(needle, i)
        if pos == -1:
            break
        j = pos + len(needle)
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n:
            break
        quote = text[j]
        if quote not in ("\"", "'"):
            i = j + 1
            continue
        j += 1
        name_buf = []
        while j < n:
            c = text[j]
            if c == "\\" and j + 1 < n:
                name_buf.append(text[j + 1])
                j += 2
                continue
            if c == quote:
                j += 1
                break
            name_buf.append(c)
            j += 1
        name = "".join(name_buf)

        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n or text[j] != ",":
            i = j
            continue
        j += 1
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n or text[j] != "[":
            i = j
            continue

        # Extract body up to matching ']'
        body_start = j + 1
        depth = 1
        j += 1
        while j < n and depth > 0:
            c = text[j]
            if c in ("\"", "'"):
                q = c
                j += 1
                while j < n:
                    cc = text[j]
                    if cc == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if cc == q:
                        j += 1
                        break
                    j += 1
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            i = pos + len(needle)
            continue
        body = text[body_start:j]

        ordered: list[tuple[str, str]] = []
        # Keep this intentionally narrow: only the primitives we generate in config.ts.
        # Note: do not over-escape parens in raw regex strings.
        item_pat = re.compile(
            r'openApp\(\s*(?P<q1>["\'])(?P<app>.*?)(?P=q1)\s*\)'
            r'|keystroke\(\s*(?P<q2>["\'])(?P<k>.*?)(?P=q2)\s*\)',
            re.DOTALL,
        )
        for im in item_pat.finditer(body):
            if im.group("app") is not None:
                ordered.append(("open_app", im.group("app")))
            else:
                ordered.append(("keystroke", im.group("k")))
        if ordered:
            out[name] = ordered

        i = j + 1

    return out


def classify(
    name: str,
    arc_aliases: dict[str, str],
    telegram_aliases: dict[str, str],
    web_aliases: dict[str, str],
) -> Tuple[str, str, Optional[str]]:
    if name == SWITCH_WINDOW_OR_APP:
        return "switch_window_or_app", "", None
    if name.startswith("open:"):
        tail = name.split(":", 1)[1].strip()
        # Deep links for System Settings.
        if tail.lower().startswith("system settings:"):
            section = tail.split(":", 1)[1].strip().lower()
            if section == "network":
                return "open_url", "x-apple.systempreferences:com.apple.preference.network", None
            return "todo", "", None
        app = strip_paren_suffix(tail)
        return "open_app_toggle", app, None
    # Common pattern in config: "open Safari new tab"
    m = re.match(r"^open\s+(.+?)\s+new\s+tab$", name.strip(), re.IGNORECASE)
    if m:
        app = m.group(1).strip()
        # `open -a <App> about:blank` typically opens a new tab in browsers.
        return "open_url", "about:blank", app
    if name.startswith("arc:"):
        tail = name.split(":", 1)[1].strip()
        url = guess_url(tail)
        if url:
            return "open_url", url, "Arc"
        if tail in arc_aliases:
            return "open_url", arc_aliases[tail], "Arc"
        return "todo", "", None
    if name.startswith("telegram:"):
        tail = name.split(":", 1)[1].strip()
        if tail in telegram_aliases:
            return "open_url", telegram_aliases[tail], None
        return "todo", "", None
    if name.startswith("linear:"):
        tail = name.split(":", 1)[1].strip().lower()
        # These are the 1-key team switchers; keep them fast and deterministic.
        if tail in {"nikiv", "linsa", "gen"}:
            return "sequence", f"linear_team:{tail}", None
        if tail == "focus initiative":
            return "sequence", "linear_focus_initiative", None
        return "todo", "", None
    if name == "New Linear task":
        return "sequence", "linear_new_task", None
    if name.startswith("enter:"):
        tail = name.split(":", 1)[1].strip()
        return "sequence", f"enter:{tail}", None
    if name in web_aliases:
        return "open_url", web_aliases[name], "Safari"
    if name == "Selection -> Claude":
        return "sequence", "selection_to_claude", None
    if name == "Move selection to LM Studio":
        return "sequence", "selection_to_lm_studio", None
    if name.startswith("paste:"):
        return "paste_text", name.split(":", 1)[1].strip(), None
    return "todo", "", None


def unique(items: Iterable[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def main() -> int:
    text = CONFIG_PATH.read_text()
    seq_step_macros = find_seq_step_macros(text)
    arc_aliases = load_tsv_map(ARC_ALIASES_PATH)
    telegram_aliases = load_tsv_map(TELEGRAM_ALIASES_PATH)
    web_aliases = load_tsv_map(WEB_ALIASES_PATH)
    items = unique(
        find_call_strings(text, "km")
        + find_call_strings(text, "seq")
        + find_call_strings(text, "seqSocket")
    )
    lines = [
        "# Generated from /Users/nikiv/config/i/kar/config.ts",
        "# Fields: name, action, arg, app (optional)",
        "# Actions: open_app, open_app_toggle, open_url, session_save, paste_text, switch_window_or_app, keystroke, select_menu_item, click, double_click, right_click, scroll, drag, mouse_move, screenshot, sequence, todo",
        "",
    ]
    for name in items:
        if name in seq_step_macros:
            lines.append(f'- name: "{escape_yaml(name)}"')
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            for action, arg in seq_step_macros[name]:
                lines.append(f"    - action: {action}")
                lines.append(f'      arg: "{escape_yaml(arg)}"')
            continue

        action, arg, app = classify(name, arc_aliases, telegram_aliases, web_aliases)
        lines.append(f'- name: "{escape_yaml(name)}"')
        if action == "sequence" and arg.startswith("linear_team:"):
            team = arg.split(":", 1)[1]
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            lines.append("    - action: open_app")
            lines.append('      arg: "Linear"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+1"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+k"')
            lines.append("    - action: paste_text")
            lines.append(f'      arg: "team {escape_yaml(team)}"')
        elif action == "sequence" and arg == "linear_new_task":
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            lines.append("    - action: open_app")
            lines.append('      arg: "Linear"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+1"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+n"')
        elif action == "sequence" and arg == "linear_focus_initiative":
            # Best-effort port of the current KM macro (cmd+1 then cmd+n).
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            lines.append("    - action: open_app")
            lines.append('      arg: "Linear"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+1"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+n"')
        elif action == "sequence" and arg.startswith("enter:"):
            text_to_enter = arg.split(":", 1)[1]
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            lines.append("    - action: paste_text")
            lines.append(f'      arg: "{escape_yaml(text_to_enter)}"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "return"')
        elif action == "sequence" and arg == "selection_to_claude":
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+c"')
            lines.append("    - action: open_app")
            lines.append('      arg: "Claude"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+v"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "return"')
        elif action == "sequence" and arg == "selection_to_lm_studio":
            lines.append("  action: sequence")
            lines.append('  arg: ""')
            lines.append("  steps:")
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+c"')
            lines.append("    - action: open_app")
            lines.append('      arg: "LM Studio"')
            lines.append("    - action: keystroke")
            lines.append('      arg: "cmd+v"')
        else:
            lines.append(f"  action: {action}")
            lines.append(f'  arg: "{escape_yaml(arg)}"')
        if app:
            lines.append(f'  app: "{escape_yaml(app)}"')
    OUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {OUT_PATH} with {len(items)} macros")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
