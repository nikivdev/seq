# Next-Type RL Pipeline Runbook

Last updated: 2026-02-21

This runbook is the production path for building next-phrase datasets from local Zed coding sessions and staging them into `prime-rl`.

## What Is Implemented

- Keystroke ingest with modifier-aware char decoding (`decoded_char`).
- Text burst segmentation events (`next_type.text_burst.v1`) with triggers:
  - `pause`
  - `delimiter`
  - `enter`
  - `app_switch`
  - `flush`
- Zed context probe (`next_type.context.v1`) with file/language/project/git metadata.
- Phrase builder joining bursts + nearest context window.
- Dataset exporter producing:
  - `train.jsonl`
  - `val.jsonl`
  - `test.jsonl`
  - `next_type_phrases.jsonl` (combined for env loaders)
  - `manifest.json`
- Flow tasks for one-shot pipeline and staging into `prime-rl` env data path.

## Core Commands

```bash
cd ~/code/seq

# 1) Start continuous capture for training data
f next-type-data-collect-on

# 2) Let it collect while you code in Zed (24-48h recommended)

# 3) Build + export + stage dataset into prime-rl env
f next-type-dataset-build-and-stage

# 4) Inspect resulting dataset stats
f next-type-dataset-stats
```

## Fine-Grained Commands

```bash
# Context probe lifecycle
f next-type-context-probe-preflight
f next-type-context-probe-on
f next-type-context-probe-status
f next-type-context-probe-logs
f next-type-context-probe-off

# Dataset pipeline
f next-type-phrase-build
f next-type-dataset-export
f next-type-dataset-stage-prime
```

## Data Paths

- Phrase pairs output:
  - `${SEQ_NEXT_TYPE_PHRASES_OUT:-~/.local/state/seq/next_type_phrases.jsonl}`
- Dataset dir:
  - `${SEQ_NEXT_TYPE_DATASET_DIR:-~/.local/state/seq/next_type_dataset}`
- `prime-rl` staged file:
  - `${SEQ_NEXT_TYPE_PRIME_RL_ENV_DATA:-~/repos/PrimeIntellect-ai/prime-rl/examples/flow_rise/environment_next_type/data/next_type_phrases.jsonl}`

## Quality Gates Before Training

- Only use runs where:
  - enough contexts are present (few missing-context drops)
  - language/project stats in `manifest.json` look realistic
  - answer lengths are not dominated by tiny fragments
- If quality is poor, keep collecting; do not train on sparse/noisy snapshots.

## Notes

- Pipeline is local-first and does not add blocking work to typing path.
- Context probe is asynchronous and can be stopped independently.
- App filtering is Zed-first by default in phrase builder.
