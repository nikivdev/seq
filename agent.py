#!/usr/bin/env python3
"""UI-TARS computer use agent that uses seq for mouse/keyboard actions.

Usage:
    python3 agent.py "open Safari and search for weather"
    python3 agent.py --max-steps 10 "close all windows"
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000")
MODEL = os.environ.get("UI_TARS_MODEL", "ByteDance-Seed/UI-TARS-1.5-7B")
SEQ_BIN = os.environ.get("SEQ_BIN", os.path.expanduser("~/code/seq/cli/cpp/out/bin/seq"))
SCREENSHOT_PATH = "/tmp/seq_agent_screenshot.png"

SYSTEM_PROMPT = """You are a GUI agent on macOS. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
```
Thought: ...
Action: ...
```

## Action Space

open_app(name='AppName')
click(point='<point>x1 y1</point>')
left_double(point='<point>x1 y1</point>')
right_single(point='<point>x1 y1</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
hotkey(key='cmd c')
key(name='return')
type(content='xxx')
scroll(point='<point>x1 y1</point>', direction='down or up or right or left')
wait()
finished(content='xxx')

## Important Rules
- This is macOS. Use 'cmd' for ⌘, 'opt' for ⌥, 'ctrl' for ⌃, 'shift' for ⇧.
- To open any application, ALWAYS use open_app(name='AppName'). Never try to find apps visually.
  Examples: open_app(name='Finder'), open_app(name='Safari'), open_app(name='Terminal')
- To press a single key (Return, Escape, Tab, Space, Delete), use key(name='return').
- To press a shortcut with modifiers, use hotkey(key='cmd n') with space-separated keys.
- To type text, use type(content='text here'). This pastes the text reliably.
- Only use click/scroll/drag for interacting with visible UI elements in the screenshot.
- Before sending key/hotkey/type, ensure the intended application is frontmost; if unsure, call open_app(name='AppName') again.
- Never use app/window-closing shortcuts like cmd q or cmd w unless the user instruction explicitly asks to quit/close something.
- If the Action History includes a line like `Result: guard: ...`, you must adapt immediately:
  - If it says the protected app is frontmost, your next action should be open_app(name='...') for the intended app.
  - If it says a hotkey was blocked as dangerous, do not try that hotkey again.
- When the task is complete, use finished(content='description of what was done').
- If you repeat the same failing action twice, try a completely different approach.

## User Instruction
{instruction}
"""


def _seq_json(cmd_args):
    """Run seq and parse JSON output (single line)."""
    try:
        p = subprocess.run([SEQ_BIN, *cmd_args], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = (p.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def get_frontmost_app():
    """Return (name, bundle_id, pid) for the current frontmost app, or (None, None, None)."""
    # Prefer System Events: seqd can be down or its cached app-state can be stale.
    try:
        p_name = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2,
        )
        p_bid = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'tell application "System Events" to get bundle identifier of (first application process whose frontmost is true)'],
            capture_output=True, text=True, timeout=2,
        )
        p_pid = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'tell application "System Events" to get unix id of (first application process whose frontmost is true)'],
            capture_output=True, text=True, timeout=2,
        )
        if p_name.returncode == 0 and p_bid.returncode == 0 and p_pid.returncode == 0:
            pid_s = (p_pid.stdout or "").strip()
            pid = None
            try:
                if pid_s:
                    pid = int(pid_s)
            except Exception:
                pid = None
            return p_name.stdout.strip() or None, p_bid.stdout.strip() or None, pid
    except Exception:
        pass

    # Fallback: query seqd cached state.
    st = _seq_json(["app-state"])
    if not st or "current" not in st:
        return None, None, None
    cur = st.get("current") or {}
    return cur.get("name"), cur.get("bundle_id"), cur.get("pid")


def instruction_allows_close(instruction):
    """Heuristic: only allow close/quit shortcuts if user intent includes it."""
    s = (instruction or "").lower()
    return any(w in s for w in ["quit", "close", "exit", "force quit", "kill"])


def instruction_mentions_app(instruction, app_name=None, bundle_id=None):
    """Heuristic: decide if the user explicitly wants to operate in a specific app."""
    s = (instruction or "").lower()
    if app_name and app_name.lower() in s:
        return True
    if bundle_id and bundle_id.lower() in s:
        return True
    # Common case: users say "in zed" but the app name may be "Zed Preview".
    if bundle_id:
        leaf = bundle_id.split(".")[-1].lower()
        if leaf and leaf in s:
            return True
    return False


def infer_target_app(instruction):
    """Best-effort: infer which app the task wants, for safe autofocus."""
    s = (instruction or "").lower()
    # Explicit "open X" patterns first.
    if re.search(r"\bopen\s+(the\s+)?finder\b", s):
        return "Finder"
    if re.search(r"\bopen\s+(the\s+)?safari\b", s):
        return "Safari"
    if re.search(r"\bopen\s+(the\s+)?terminal\b", s):
        return "Terminal"
    if re.search(r"\bopen\s+(the\s+)?ghostty\b", s):
        return "Ghostty"
    if re.search(r"\bopen\s+(the\s+)?zed\b", s):
        return "Zed"
    if re.search(r"\bopen\s+(the\s+)?(google\s+chrome|chrome)\b", s):
        return "Google Chrome"

    # Non-"open" tasks that strongly imply Finder.
    if any(w in s for w in ["create a folder", "new folder", "rename folder", "move file", "desktop folder"]):
        return "Finder"
    return None


def parse_folder_name(instruction):
    """Extract folder name from natural-language instructions like: create a new folder called test."""
    s = (instruction or "").strip()
    if not s:
        return None
    # Quoted names first.
    m = re.search(r'folder\\s+(?:called|named)\\s+"([^"]+)"', s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"folder\\s+(?:called|named)\\s+'([^']+)'", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Unquoted: take a conservative token tail (stop at punctuation).
    m = re.search(r"folder\\s+(?:called|named)\\s+([A-Za-z0-9._ -]{1,64})", s, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        name = re.split(r"[\\n\\r\\t\\.,;:!\\?]+", name)[0].strip()
        return name or None
    return None


def _osascript_keycode(code):
    subprocess.run(
        ["/usr/bin/osascript", "-e", f'tell application "System Events" to key code {int(code)}'],
        capture_output=True,
    )


def _osascript_cmd_v():
    subprocess.run(
        ["/usr/bin/osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True,
    )


def nudge_finder_rename(folder_name):
    """Best-effort: rename current Finder selection to folder_name."""
    if not folder_name:
        return False
    if not ensure_frontmost_app("Finder", max_tries=5, delay_s=0.25):
        return False
    # Finder typically selects the newly created folder; Return enters rename.
    _osascript_keycode(36)  # Return
    time.sleep(0.15)
    subprocess.run(["/usr/bin/pbcopy"], input=folder_name.encode(), capture_output=True)
    _osascript_cmd_v()
    time.sleep(0.05)
    _osascript_keycode(36)  # Return to commit
    return True


def _normalize_hotkey_tokens(keys):
    parts = [p.strip().lower() for p in keys.split() if p.strip()]
    # Common synonyms from model outputs.
    mod_map = {"alt": "opt", "option": "opt", "command": "cmd", "control": "ctrl"}
    return [mod_map.get(p, p) for p in parts]


def hotkey_is_dangerous(keys):
    """Return True if the hotkey is likely to quit/close/lock/hide/minimize."""
    toks_set = set(_normalize_hotkey_tokens(keys))
    # cmd+q (quit), cmd+w (close window/tab), cmd+h (hide), cmd+m (minimize),
    # ctrl+cmd+q (lock screen), cmd+opt+esc (force quit dialog).
    if "cmd" in toks_set and "q" in toks_set:
        return True
    if "cmd" in toks_set and "w" in toks_set:
        return True
    if "cmd" in toks_set and "h" in toks_set:
        return True
    if "cmd" in toks_set and "m" in toks_set:
        return True
    if toks_set.issuperset({"ctrl", "cmd", "q"}):
        return True
    if toks_set.issuperset({"cmd", "opt", "esc"}):
        return True
    return False


def ensure_frontmost_app(app_name, max_tries=3, delay_s=0.35):
    """Bring app to foreground and verify via seq app-state."""
    if not app_name:
        return False
    for _ in range(max_tries):
        cur_name, _, _ = get_frontmost_app()
        if cur_name == app_name:
            return True
        try:
            subprocess.run([SEQ_BIN, "open-app", app_name], capture_output=True)
        except FileNotFoundError:
            return False
        time.sleep(delay_s)
    cur_name, _, _ = get_frontmost_app()
    return cur_name == app_name


def ensure_not_protected(state, action_desc, fallback_app="Finder"):
    """If the protected app is frontmost, switch away and force a retry (new screenshot)."""
    if not state.get("protect_frontmost"):
        return True, ""
    cur_name, cur_bundle, _ = get_frontmost_app()
    protected_bundle = state.get("protected_bundle_id")
    if not protected_bundle:
        return True, ""
    if cur_bundle != protected_bundle:
        return True, ""

    # If the user explicitly asked to operate in the launching app, allow it.
    if instruction_mentions_app(state.get("instruction"), state.get("protected_name"), protected_bundle):
        return True, ""

    # Prefer the last app that the model explicitly opened; otherwise go to Finder.
    target = state.get("last_opened_app")
    if not target:
        target = infer_target_app(state.get("instruction"))
    if not target:
        return False, f"guard: protected app is frontmost ({cur_name}); call open_app(name='...') before {action_desc}"
    ok = ensure_frontmost_app(target)
    if ok:
        # Do not execute the original action: it was planned for a different screenshot.
        return False, f"guard: switched focus to {target} (away from protected app {cur_name}); retry"
    return False, f"guard: refused to {action_desc} while protected app is frontmost ({cur_name}); could not switch to {target}"


def get_screen_size():
    """Get the main display resolution and Retina scale factor.

    Returns (pixel_width, pixel_height, scale) where scale is 2 for Retina, 1 otherwise.
    CGEvent uses 'points' (pixels / scale), so callers must divide by scale.
    """
    result = subprocess.run(
        ["system_profiler", "SPDisplaysDataType"],
        capture_output=True, text=True
    )
    is_retina = "Retina" in result.stdout
    for line in result.stdout.splitlines():
        if "Resolution" in line:
            # e.g. "Resolution: 3456 x 2234 Retina"  or  "Resolution: 1920 x 1080"
            m = re.search(r"(\d+)\s*x\s*(\d+)", line)
            if m:
                scale = 2 if is_retina else 1
                return int(m.group(1)), int(m.group(2)), scale
    # Fallback
    return 1920, 1080, 1


def take_screenshot():
    """Take a screenshot, resize to fit model context, and return base64-encoded PNG."""
    result = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-C", "-t", "png", SCREENSHOT_PATH],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"[!] screencapture failed: {result.stderr.decode()}", file=sys.stderr)
        return None
    # Resize to 1280px wide max to keep image tokens manageable for the 4096 context.
    resized = SCREENSHOT_PATH + ".resized.png"
    subprocess.run([
        "/usr/bin/sips", "--resampleWidth", "1280",
        SCREENSHOT_PATH, "--out", resized,
    ], capture_output=True)
    path = resized if os.path.exists(resized) else SCREENSHOT_PATH
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def query_ui_tars(instruction, screenshot_b64, history_text=""):
    """Send screenshot + instruction to UI-TARS via vLLM OpenAI-compatible API."""

    user_content = []
    if history_text:
        user_content.append({"type": "text", "text": history_text})
    user_content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}
    })

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.replace("{instruction}", instruction)},
        {"role": "user", "content": user_content},
    ]

    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.0,
    }).encode()

    req = urllib.request.Request(
        f"{VLLM_URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"[!] UI-TARS request failed: {e} — {body[:500]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[!] UI-TARS request failed: {e}", file=sys.stderr)
        return None


RESIZED_WIDTH = 1280  # Must match the sips resize width in take_screenshot()


def get_resized_height(screen_w, screen_h):
    """Calculate the height of the resized screenshot."""
    return int(screen_h * RESIZED_WIDTH / screen_w)


def normalize_to_pixels(x_norm, y_norm, screen_w, screen_h, scale=1):
    """Convert model coordinates to macOS point coordinates.

    The model outputs coordinates in the resized image space (1280px wide).
    We scale them back to actual screen pixel dimensions, then divide by the
    Retina scale factor because CGEvent uses 'points' not pixels.
    """
    resized_h = get_resized_height(screen_w, screen_h)
    px = int(x_norm * screen_w / RESIZED_WIDTH / scale)
    py = int(y_norm * screen_h / resized_h / scale)
    return px, py


def parse_and_execute(response, screen_w, screen_h, scale=1):
    """Parse UI-TARS response and execute via seq."""
    if not response:
        return False, "empty response", "empty response"

    # Extract thought and action
    thought = ""
    action_str = ""
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("Thought:"):
            thought = stripped[len("Thought:"):].strip()
        elif stripped.startswith("Action:"):
            action_str = stripped[len("Action:"):].strip()

    if not action_str:
        # Maybe the whole response is the action
        action_str = response.strip()

    print(f"  Thought: {thought}")
    print(f"  Action:  {action_str}")

    # Parse finished
    m = re.match(r"finished\(content='(.*)'\)", action_str)
    if m:
        print(f"\n[done] {m.group(1)}")
        return True, "finished", "finished"

    # Parse wait
    if action_str.strip() == "wait()":
        wait_s = float(parse_and_execute.state.get("wait_s", 1.0))
        print(f"  -> waiting {wait_s:.2f}s...")
        time.sleep(wait_s)
        return False, "wait", "wait"

    # Parse open_app — uses seq open-app directly (reliable, no visual search)
    m = re.match(r"open_app\(name='(.+?)'\)", action_str)
    if m:
        app_name = m.group(1)
        print(f"  -> open-app({app_name})")
        parse_and_execute.state["last_opened_app"] = app_name
        subprocess.run([SEQ_BIN, "open-app", app_name], capture_output=True)
        ok = ensure_frontmost_app(app_name, max_tries=5, delay_s=0.4)
        return False, "open_app", f"opened app: {app_name} (frontmost={ok})"

    # Parse key — single key press (return, escape, tab, space, delete, etc.)
    m = re.match(r"key\(name='(.+?)'\)", action_str)
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "send key")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        key_name = m.group(1).strip().lower()
        # Map to AppleScript key codes
        key_map = {
            "return": (36, None), "enter": (36, None),
            "escape": (53, None), "esc": (53, None),
            "tab": (48, None),
            "space": (49, None),
            "delete": (51, None), "backspace": (51, None),
            "up": (126, None), "down": (125, None),
            "left": (123, None), "right": (124, None),
        }
        if key_name in key_map:
            code, _ = key_map[key_name]
            print(f"  -> key({key_name} = keycode {code})")
            subprocess.run([
                "/usr/bin/osascript", "-e",
                f'tell application "System Events" to key code {code}'
            ])
        else:
            # Try as a single character keystroke
            print(f"  -> key({key_name})")
            subprocess.run([
                "/usr/bin/osascript", "-e",
                f'tell application "System Events" to keystroke "{key_name}"'
            ])
        return False, "key", note or "key"

    # Parse coordinates from multiple formats:
    # - <point>x y</point>  (new format, space-separated)
    # - (x,y)               (start_box format, comma-separated)
    def extract_point(s):
        # Try <point>x y</point> format
        m = re.search(r"<point>(\d+)\s+(\d+)</point>", s)
        if m:
            return int(m.group(1)), int(m.group(2))
        # Try (x,y) format (from start_box/end_box)
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", s)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

    # Parse click — multiple format variants
    m = (re.match(r"click\(point='(.+?)'\)", action_str) or
         re.match(r"click\(start_box='(.+?)'\)", action_str))
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "click")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        pt = extract_point(m.group(1))
        if pt:
            px, py = normalize_to_pixels(pt[0], pt[1], screen_w, screen_h, scale)
            print(f"  -> click({px}, {py})")
            subprocess.run([SEQ_BIN, "click", str(px), str(py)])
            return False, "click", note or "click"

    # Parse double click
    m = (re.match(r"left_double\(point='(.+?)'\)", action_str) or
         re.match(r"left_double\(start_box='(.+?)'\)", action_str))
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "double-click")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        pt = extract_point(m.group(1))
        if pt:
            px, py = normalize_to_pixels(pt[0], pt[1], screen_w, screen_h, scale)
            print(f"  -> double-click({px}, {py})")
            subprocess.run([SEQ_BIN, "double-click", str(px), str(py)])
            return False, "double_click", note or "double_click"

    # Parse right click
    m = (re.match(r"right_single\(point='(.+?)'\)", action_str) or
         re.match(r"right_single\(start_box='(.+?)'\)", action_str))
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "right-click")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        pt = extract_point(m.group(1))
        if pt:
            px, py = normalize_to_pixels(pt[0], pt[1], screen_w, screen_h, scale)
            print(f"  -> right-click({px}, {py})")
            subprocess.run([SEQ_BIN, "right-click", str(px), str(py)])
            return False, "right_click", note or "right_click"

    # Parse drag — multiple format variants
    m = (re.match(r"drag\(start_point='(.+?)',\s*end_point='(.+?)'\)", action_str) or
         re.match(r"drag\(start_box='(.+?)',\s*end_box='(.+?)'\)", action_str))
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "drag")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        sp = extract_point(m.group(1))
        ep = extract_point(m.group(2))
        if sp and ep:
            sx, sy = normalize_to_pixels(sp[0], sp[1], screen_w, screen_h, scale)
            ex, ey = normalize_to_pixels(ep[0], ep[1], screen_w, screen_h, scale)
            print(f"  -> drag({sx},{sy} -> {ex},{ey})")
            subprocess.run([SEQ_BIN, "drag", str(sx), str(sy), str(ex), str(ey)])
            return False, "drag", note or "drag"

    # Parse scroll — multiple format variants
    m = (re.match(r"scroll\(point='(.+?)',\s*direction='(.+?)'\)", action_str) or
         re.match(r"scroll\(start_box='(.+?)',\s*direction='(.+?)'\)", action_str))
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "scroll")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        pt = extract_point(m.group(1))
        direction = m.group(2).strip()
        if pt:
            px, py = normalize_to_pixels(pt[0], pt[1], screen_w, screen_h, scale)
            dy = {"down": -3, "up": 3, "left": 0, "right": 0}.get(direction, -3)
            print(f"  -> scroll({px},{py}, {direction})")
            subprocess.run([SEQ_BIN, "scroll", str(px), str(py), str(dy)])
            return False, "scroll", note or "scroll"

    # Parse hotkey (modifier combos like "cmd n", "cmd shift n")
    m = re.match(r"hotkey\(key='(.+?)'\)", action_str)
    if m:
        keys = m.group(1).strip()
        # Guardrail: block close/quit/lock/hide/minimize unless instruction explicitly requests it.
        allow_close = instruction_allows_close(parse_and_execute.state.get("instruction")) or parse_and_execute.state.get("allow_dangerous")
        if hotkey_is_dangerous(keys) and not allow_close:
            note = f"guard: blocked dangerous hotkey '{keys}' (set --allow-dangerous or include close/quit intent)"
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        ok, note = ensure_not_protected(parse_and_execute.state, "send hotkey")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        parts = keys.split()
        # Map modifier names
        mod_map = {"ctrl": "cmd", "alt": "opt", "command": "cmd", "option": "opt"}
        seq_keys = [mod_map.get(p.lower(), p.lower()) for p in parts]

        # If it's a single non-modifier key, treat as key() instead
        modifiers = {"cmd", "opt", "ctrl", "shift"}
        mods = [k for k in seq_keys if k in modifiers]
        non_mods = [k for k in seq_keys if k not in modifiers]

        if not mods and len(non_mods) == 1:
            # Bare key like "enter" — delegate to key code path
            key_name = non_mods[0]
            key_map = {
                "return": 36, "enter": 36, "escape": 53, "esc": 53,
                "tab": 48, "space": 49, "delete": 51, "backspace": 51,
                "up": 126, "down": 125, "left": 123, "right": 124,
            }
            if key_name in key_map:
                print(f"  -> key({key_name})")
                subprocess.run([
                    "/usr/bin/osascript", "-e",
                    f'tell application "System Events" to key code {key_map[key_name]}'
                ])
            else:
                print(f"  -> keystroke({key_name})")
                subprocess.run([
                    "/usr/bin/osascript", "-e",
                    f'tell application "System Events" to keystroke "{key_name}"'
                ])
        else:
            # Modifier combo
            key_char = non_mods[0] if non_mods else ""
            mod_str = _build_modifiers(mods)
            print(f"  -> hotkey({'+'.join(seq_keys)})")
            subprocess.run([
                "/usr/bin/osascript", "-e",
                f'tell application "System Events" to keystroke "{key_char}" using {{{mod_str}}}'
            ])
        return False, "hotkey", note or "hotkey"

    # Parse type
    m = re.match(r"type\(content='(.+?)'\)", action_str, re.DOTALL)
    if m:
        ok, note = ensure_not_protected(parse_and_execute.state, "type")
        if not ok:
            print(f"  [blocked] {note}", file=sys.stderr)
            return False, "blocked", note
        content = m.group(1)
        # Unescape
        content = content.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")
        print(f"  -> type({repr(content[:50])}...)")
        # Use pbcopy + cmd-v for reliable text input
        proc = subprocess.run(["/usr/bin/pbcopy"], input=content.encode(), capture_output=True)
        subprocess.run([
            "/usr/bin/osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down'
        ])
        return False, "type", note or "type"

    print(f"  [?] Could not parse action: {action_str}", file=sys.stderr)
    return False, "unknown", "unknown action"


# Populated by main(); kept as an attribute to avoid threading globals through every call.
parse_and_execute.state = {}


def action_str_from_response(response):
    """Extract the Action: line from a response for loop detection."""
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("Action:"):
            return stripped[len("Action:"):].strip()
    return response.strip()[:100]


def _build_modifiers(mods):
    """Build AppleScript modifier string."""
    mapping = {
        "cmd": "command down",
        "shift": "shift down",
        "opt": "option down",
        "ctrl": "control down",
    }
    parts = [mapping.get(m, "") for m in mods if m in mapping]
    return ", ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="UI-TARS computer use agent via seq")
    parser.add_argument("instruction", help="Task instruction for the agent")
    parser.add_argument("--max-steps", type=int, default=15, help="Max agent steps")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between steps (seconds)")
    parser.add_argument("--wait-seconds", type=float, default=1.0, help="Duration for wait() actions")
    parser.add_argument("--dry-run", action="store_true", help="Only show actions, don't execute")
    parser.add_argument("--no-autofocus", action="store_true",
                        help="Disable preflight focus/open of an inferred target app (e.g. Finder)")
    parser.add_argument("--no-protect-frontmost", action="store_true",
                        help="Disable guard that prevents sending inputs while the launch app is frontmost")
    parser.add_argument("--allow-dangerous", action="store_true",
                        help="Allow potentially destructive hotkeys like cmd+w/cmd+q even without close/quit intent")
    args = parser.parse_args()

    screen_w, screen_h, screen_scale = get_screen_size()
    print(f"[agent] Screen: {screen_w}x{screen_h} (scale={screen_scale})")
    print(f"[agent] Model: {MODEL} @ {VLLM_URL}")
    print(f"[agent] Task: {args.instruction}")
    print(f"[agent] Max steps: {args.max_steps}")
    prot_name, prot_bundle, prot_pid = get_frontmost_app()
    print(f"[agent] Protected frontmost: {prot_name} ({prot_bundle}, pid={prot_pid})")
    print()

    history = []  # list[str]
    recent_actions = []  # Last N action strings for loop detection
    consecutive_unknown = 0
    consecutive_wait = 0
    MAX_REPEAT = 3  # Abort if same action repeated this many times
    MAX_UNKNOWN = 3  # Abort after this many consecutive unparseable actions
    MAX_WAIT_NUDGE = 2  # If wait repeats, try a deterministic nudge for known tasks.

    # Execution-time state shared with parse_and_execute (keeps model-visible action format unchanged).
    parse_and_execute.state = {
        "instruction": args.instruction,
        "protect_frontmost": not args.no_protect_frontmost,
        "protected_bundle_id": prot_bundle,
        "protected_name": prot_name,
        "protected_pid": prot_pid,
        "last_opened_app": None,
        "allow_dangerous": args.allow_dangerous or (os.environ.get("SEQ_AGENT_ALLOW_DANGEROUS") == "1"),
        "wait_s": args.wait_seconds,
    }

    if not args.no_autofocus:
        target = infer_target_app(args.instruction)
        if target:
            ok = ensure_frontmost_app(target, max_tries=5, delay_s=0.4)
            if ok:
                parse_and_execute.state["last_opened_app"] = target
                print(f"[agent] Autofocus: {target} (frontmost={ok})")
    folder_name = parse_folder_name(args.instruction)

    for step in range(1, args.max_steps + 1):
        print(f"--- Step {step}/{args.max_steps} ---")

        # Take screenshot
        screenshot_b64 = take_screenshot()
        if not screenshot_b64:
            print("[!] Failed to take screenshot, retrying...")
            time.sleep(2)
            screenshot_b64 = take_screenshot()
            if not screenshot_b64:
                print("[!] Screenshot failed again, aborting.")
                break

        # Build history text
        history_text = ""
        if history:
            history_text = "## Action History\n"
            for i, h in enumerate(history, 1):
                history_text += f"Step {i}: {h}\n"
            history_text += "\n## Current Screenshot\n"

        # Query UI-TARS
        print("  Querying UI-TARS...")
        response = query_ui_tars(args.instruction, screenshot_b64, history_text)
        if not response:
            print("[!] No response from model, retrying...")
            time.sleep(2)
            continue

        print(f"  Raw: {response[:200]}")

        if args.dry_run:
            print("  [dry-run] Skipping execution")
            history.append(response.strip())
            time.sleep(args.delay)
            continue

        # Parse and execute
        done, action_type, note = parse_and_execute(response, screen_w, screen_h, screen_scale)
        # Include execution notes so the model can adapt when actions are blocked.
        if note and note not in ("wait", "finished"):
            history.append(response.strip() + f"\nResult: {note}")
        else:
            history.append(response.strip())

        if done:
            print(f"\n[agent] Task completed in {step} steps.")
            return 0

        if action_type == "wait":
            consecutive_wait += 1
        else:
            consecutive_wait = 0

        # If the model is stalling on Finder folder creation/rename, try a deterministic nudge.
        if consecutive_wait >= MAX_WAIT_NUDGE:
            target = infer_target_app(args.instruction)
            if target == "Finder" and folder_name:
                ok = nudge_finder_rename(folder_name)
                history.append(f"AutoNudge: Finder rename selection -> {folder_name} (ok={ok})")
                consecutive_wait = 0

        # Guardrail: detect unknown/unparseable actions
        if action_type == "unknown":
            consecutive_unknown += 1
            if consecutive_unknown >= MAX_UNKNOWN:
                print(f"\n[agent] Aborting: {MAX_UNKNOWN} consecutive unparseable actions.")
                return 1
        else:
            consecutive_unknown = 0

        # Guardrail: detect action loops (same action string repeated)
        action_key = action_str_from_response(response)
        recent_actions.append(action_key)
        if len(recent_actions) >= MAX_REPEAT:
            tail = recent_actions[-MAX_REPEAT:]
            if len(set(tail)) == 1:
                print(f"\n[agent] Aborting: same action repeated {MAX_REPEAT} times: {tail[0][:80]}")
                return 1

        time.sleep(args.delay)

    print(f"\n[agent] Reached max steps ({args.max_steps}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
