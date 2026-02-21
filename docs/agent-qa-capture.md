# Agent Q/A Capture (Claude + Codex -> seq -> ClickHouse)

This keeps a continuous high-signal dataset of real user question/assistant answer pairs for RL tuning.

The capture daemon tails:

- `~/.claude/projects/**/*.jsonl`
- `~/.codex/sessions/**/*.jsonl`

It writes normalized rows to `seq_mem.jsonl` as:

- `name=agent.qa.pair`
- `subject={agent, session_id, project_path, question, answer, ...}`

## One-time setup

From `~/code/seq`:

```bash
f rl-capture-on
f agent-qa-capture-on
```

`rl-capture-on` keeps low-latency local file spool mode. `agent-qa-capture-on` starts the background ingest daemon.

## Daily commands

```bash
f agent-qa-capture-status
f agent-qa-capture-logs
f rl-signal-summary --last 10000
```

For a single foreground pass:

```bash
f agent-qa-capture-once
```

To stop capture:

```bash
f agent-qa-capture-off
```

## Backfill historical sessions

```bash
f agent-qa-capture-on-backfill
```

This resets saved offsets and ingests old JSONL history from scratch once. Keep normal `agent-qa-capture-on` for steady-state.

## Env keys

These are configurable through Flow env store:

- `SEQ_AGENT_QA_CLAUDE_DIR`
- `SEQ_AGENT_QA_CODEX_DIR`
- `SEQ_AGENT_QA_STATE`
- `SEQ_AGENT_QA_PIDFILE`
- `SEQ_AGENT_QA_LOG`
- `SEQ_AGENT_QA_ZVEC_JSONL`
- `SEQ_AGENT_QA_POLL_SECONDS`
- `SEQ_AGENT_QA_RESCAN_SECONDS`
- `SEQ_AGENT_QA_FLUSH_EVERY`
- `SEQ_AGENT_QA_MAX_TEXT_CHARS`
- `SEQ_AGENT_QA_INCLUDE_TEXT`

## zvec handoff

If `SEQ_AGENT_QA_ZVEC_JSONL` is set, the daemon also writes retrieval documents for vector indexing.

Default output path:

- `~/repos/alibaba/zvec/data/agent_qa.jsonl`

Each row includes `id`, merged `text` (`Question:` + `Answer:`), and metadata.

## Important scope note

`agent.qa.pair` events are useful for retrieval/SFT-style data, but **not sufficient alone** for router RL.

For routing RL, you also need linked ground-truth events:

- `flow.router.decision.v1`
- `flow.router.outcome.v1`
- `flow.router.override.v1`

See `docs/router-rl-signal-contract.md`.
