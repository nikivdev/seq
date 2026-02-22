# Handoff: Claude Code → Codex
Updated: 2026-02-22T12:28:31Z

## Original Prompt
[Request interrupted by user for tool use]

## Plan
Implement the following plan:

# Codex ↔ Claude Code Context Bridge

## Context
You use Codex for coding and Claude Code for planning/learning. Two problems:
1. When switching Codex→Claude Code, Claude doesn't know what Codex was doing
2. When switching Claude Code→Codex, the plan/prompt needs to be handed off reliably (Claude Code loses context during execution/compaction)

## Architecture

```
Codex session JSONL ──→ codex_bridge.py ──→ Claude Code (via hooks)
                                              │
Claude Code plan ──→ .ai/handoff.md ──→ Codex (via agents.md reference)
```

Central handoff file: **`.ai/handoff.md`** in each project root — the durable artifact both agents can read/write.

---

## Part 1: Codex → Claude Code (hooks inject Codex context)

### New file: `tools/codex_bridge.py`

Single Python script with two modes, reusing parsing patterns from `tools/agent_qa_ingest.py`:

**`--mode session-start`**: Runs on SessionStart hook
- Scans `~/.codex/sessions/**/*.jsonl` for sessions matching `$CLAUDE_PROJECT_DIR` (via `cwd` in session_meta)
- Extracts last ~10 user prompts + assistant response summaries from most recent session
- Outputs JSON with `additionalContext` field (same pattern as superpowers plugin `hooks/session-start.sh`)
- Also reads `.ai/handoff.md` if it exists and includes it

**`--mode prompt-submit`**: Runs on UserPromptSubmit hook
- Same session discovery, but only outputs NEW exchanges since last check
- State tracked in `~/.local/state/seq/codex_bridge_state.json` (per-project byte offsets)
- Outputs nothing if no new Codex activity (zero overhead)
- Target: <500ms (only reads tail of most recent file)

### Modify: `~/.claude/settings.json`

Add hooks section:
```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python3 ~/code/seq/tools/codex_bridge.py --mode session-start",
        "timeout": 5
      }]
    }],
    "UserPromptSubmit": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python3 ~/code/seq/tools/codex_bridge.py --mode prompt-submit",
        "timeout": 3
      }]
    }]
  }
}
```

---

## Part 2: Claude Code → Codex (handoff file)

### New file: `tools/codex_handoff.py`

Script that packages Claude Code context into `.ai/handoff.md`:

**`--mode save`** (called by PreCompact hook or manually):
- Reads the Claude Code transcript via `transcript_path` from stdin JSON
- Extracts: original user prompt, current plan (from `~/.claude/plans/`), key decisions
- Writes structured markdown to `$CLAUDE_PROJECT_DIR/.ai/handoff.md`:

```markdown
# Handoff: Claude Code → Codex
Updated: 2026-02-22T14:30:00Z

## Original Prompt
<the user's original request verbatim>

## Plan
<the plan that was created>

## Key Decisions
- <extracted from conversation>

## Changes Made So Far
- <files modified, git diff summary>

## What's Left
- <remaining work items>
```

**`--mode clear`**: Clears the handoff file after Codex has consumed it.

### Hook: PreCompact saves context before compression

Add to `~/.claude/settings.json`:
```json
{
  "PreCompact": [{
    "matcher": "*",
    "hooks": [{
      "type": "command",
      "command": "python3 ~/code/seq/tools/codex_handoff.py --mode save",
      "timeout": 10
    }]
  }]
}
```

### Codex reads handoff via agents.md

Add to each project's `.codex/agents.md` (or create if missing):
```markdown
## Context from Claude Code
If `.ai/handoff.md` exists, read it first — it contains planning context from a Claude Code session.
```

For seq specifically, create `/Users/nikiv/code/seq/.codex/agents.md` with this instruction.

---

## Part 3: Context preservation within Claude Code

The handoff file (`.ai/handoff.md`) also solves context loss:
- **PreCompact hook** writes current state to `.ai/handoff.md` before compression
- **SessionStart hook** reads `.ai/handoff.md` back and injects it as `additionalContext`
- So even if Claude Code compresses context, the plan/prompt survives in the file and gets re-injected

---

## Files to Create

| File | Purpose |
|------|---------|
| `tools/codex_bridge.py` | Codex→Claude: reads Codex sessions, injects via hooks |
| `tools/codex_handoff.py` | Claude→Codex: saves plan/prompt to `.ai/handoff.md` |
| `.codex/agents.md` | Tells Codex to read `.ai/handoff.md` |

## Files to Modify

| File | Change |
|------|--------|
| `~/.claude/settings.json` | Add SessionStart, UserPromptSubmit, PreCompact hooks |

## Verification

1. **Codex→Claude**: Run Codex in seq, send prompts. Start new Claude Code session → see Codex history injected. Type prompt in existing session → see only new Codex exchanges.
2. **Claude→Codex**: Plan something in Claude Code. Check `.ai/handoff.md` exists with plan. Start Codex → verify it sees the handoff context.
3. **Context preservation**: Start long Claude Code session, let it compact. Verify plan/prompt survives via `.ai/handoff.md` re-injection.
4. **No-op case**: No Codex activity + no handoff file → hooks output nothing, zero overhead.


If you need specific details from before exiting plan mode (like exact code snippets, error messages, or content you generated), read the full transcript at: /Users/nikiv/.claude/projects/-Users-nikiv-code-seq/a6b2a4f2-4bc5-4e9d-9a9c-ff7327b4a3fb.jsonl

## Changes Made So Far
```
cli/cpp/src/actions.mm        | 80 ++++++++++++++++++++++++++++++++++++++-----
 cli/cpp/src/macros.cpp        |  5 +++
 cli/cpp/src/macros.h          |  1 +
 docs/next-type-suggestions.md |  5 +++
 seq.macros.yaml               |  3 ++
 5 files changed, 85 insertions(+), 9 deletions(-)
New files:
  .ai/handoff.md
  tools/codex_bridge.py
  tools/codex_handoff.py
  tools/save_link_from_clipboard.py
```
