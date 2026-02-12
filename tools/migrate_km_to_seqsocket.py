#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

CONFIG_PATH = Path("/Users/nikiv/config/i/kar/config.ts")
MACROS_PATH = Path("/Users/nikiv/code/seq/seq.macros.yaml")


def parse_macros_actions(path: Path) -> dict[str, str]:
    """
    Parse the generated YAML list without external YAML deps:

      - name: "foo"
        action: open_url
        arg: "..."
    """
    out: dict[str, str] = {}
    cur_name: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("- name:"):
            m = re.match(r'-\s*name:\s*"((?:\\.|[^"])*)"\s*$', line)
            cur_name = None if not m else m.group(1).replace('\\"', '"').replace("\\\\", "\\")
            continue
        if cur_name and line.startswith("action:"):
            out[cur_name] = line.split(":", 1)[1].strip()
            cur_name = None
    return out


def migrate_line(line: str, allowed: set[str]) -> tuple[str, int]:
    """
    Replace km("NAME") / km('NAME') -> seqSocket("NAME") if NAME is allowed.
    Skip commented lines (when // appears before km(...)).
    """
    if "km(" not in line:
        return line, 0
    comment = line.find("//")
    first_km = line.find("km(")
    if comment != -1 and comment < first_km:
        return line, 0

    replaced = 0

    def escape_name(name: str) -> str:
        return name.replace("\\", "\\\\").replace('"', '\\"')

    def repl(m: re.Match[str]) -> str:
        nonlocal replaced
        name = m.group("name")
        if name in allowed:
            replaced += 1
            return f'seqSocket("{escape_name(name)}")'
        return m.group(0)

    out = re.sub(r'km\(\s*"(?P<name>(?:\\.|[^"\\])*)"\s*\)', repl, line)
    out = re.sub(r"km\(\s*'(?P<name>(?:\\.|[^'\\])*)'\s*\)", repl, out)
    return out, replaced


def main() -> int:
    if not MACROS_PATH.exists():
        raise SystemExit(f"error: missing {MACROS_PATH}")
    actions = parse_macros_actions(MACROS_PATH)
    allowed = {k for k, v in actions.items() if v and v != "todo"}

    src_lines = CONFIG_PATH.read_text().splitlines(True)
    out_lines: list[str] = []
    total = 0
    for line in src_lines:
        out, n = migrate_line(line, allowed)
        out_lines.append(out)
        total += n

    if total:
        CONFIG_PATH.write_text("".join(out_lines))
    print(f"migrated {total} km(...) calls to seqSocket(...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
