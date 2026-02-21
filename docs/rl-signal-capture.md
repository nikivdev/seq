# RL Signal Capture (Seq)

Use this when collecting low-latency local traces for RL datasets.

## Enable local capture

From `~/code/seq`:

```bash
f rl-capture-on
f agent-qa-capture-on
```

This sets:

- `SEQ_CH_MODE=file`
- `SEQ_CH_MEM_PATH=~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl`
- `SEQ_CH_LOG_PATH=~/repos/ClickHouse/ClickHouse/user_files/seq_trace.jsonl`

## Inspect high-signal events

```bash
f rl-signal-tail
f rl-signal-summary
```

`rl-signal-tail` filters only high-signal event families (tool/runtime/action outcomes) from `seq_mem.jsonl`.
`agent-qa-capture-on` adds continuous `agent.qa.pair` events from Claude/Codex sessions.

See `docs/agent-qa-capture.md` for always-on daemon controls (`status`, `logs`, `off`, backfill).

Then build a Harbor snapshot from `~/code/flow`:

```bash
cd ~/code/flow
f rl-dataset-build
f rl-dataset-validate
```
