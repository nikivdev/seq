# Seq Action-Pack Execution For Zed Restore Recipes

This document explains exactly how `seq` executes a markdown recipe like:

- `/Users/nikiv/code/org/la/la/.ai/recipes/manual/restore-zvec-claude-zed.md`

It also includes performance and hardening guidance for production usage.

## The recipe input

The markdown recipe contains an `action-pack` fenced block:

```action-pack
timeout 0
exec /usr/bin/open -a Zed /Users/nikiv/repos/alibaba/zvec
cd /Users/nikiv/repos/alibaba/zvec
exec f ai claude resume 32e0aaad-203c-4980-bdb9-5a9a1a7ce878
```

`seq` itself does not parse markdown. A caller (for example `la-py recipe build`) extracts that block and writes a plain script file, then calls `seq action-pack pack ...`.

## End-to-end execution path

1. Compile script into signed pack (`.sap`):
   - `seq action-pack pack <script> --out <pack.sap> --id <key_id> [--ttl-ms <n>]`
2. Send pack to receiver:
   - `seq action-pack send --to <receiver|ip:port> <pack.sap>`
3. Receiver `seqd` validates and executes steps.
4. Client prints receiver response (`OK ...`, per-step status, stdout/stderr).

## What `pack` does internally

In `cli/cpp/src/action_pack_cli.cpp`, `pack` does:

1. Read script file.
2. Compile script text into `Pack` using `action_pack::compile_script(...)`.
3. Encode payload (`APK1` envelope payload version 2).
4. Sign payload with P-256 private key (`action_pack_crypto::sign_p256`).
5. Encode envelope (`payload + signature`) and write `.sap`.

`compile_script` supports these ops:

- `cd <path>`: updates cwd for subsequent exec steps.
- `timeout <ms>`: updates timeout for subsequent exec steps.
- `env KEY=VALUE`: stores env var in pack env map.
- `put <abs-dest> @<local-src>`: embeds file bytes into pack.
- `exec <argv...>`: creates one executable step.

Important defaults:

- `seq action-pack` default TTL is `5m` if not provided by caller.
- If upstream caller passes TTL (like la recipe flow), that value is used.

## What `send` does internally

In `cli/cpp/src/action_pack_cli.cpp`, `send` does:

1. Resolve receiver name via:
   - `~/Library/Application Support/seq/action_pack_receivers`
2. Resolve `host:port` and open TCP connection.
3. Write raw envelope bytes.
4. Read textual response from receiver and print it.

## What receiver `seqd` does

In `cli/cpp/src/action_pack_server.cpp`, for each request:

1. Peer filter:
   - local and/or tailscale ranges depending on flags.
2. Decode envelope and payload.
3. Lookup `key_id` pubkey, verify signature.
4. Enforce time window:
   - reject future `created_ms` and expired packs (with skew allowance).
5. Replay protection:
   - track `pack_id` in seen store; reject repeats before expiry.
6. Execute steps with sandbox and policy checks:
   - root restriction (`--action-pack-root`) required.
   - command allowlist/policy enforced.
   - env filtering (`DYLD_*`, `LD_*` denied).
   - writes use safe atomic write path.
   - per-step output captured with max output cap.

## Why the current sample recipe can fail on strict receivers

With default policy behavior:

- `exec /usr/bin/open ...` can be rejected unless `/usr/bin/open` is allowed by policy.
- `exec f ai claude ...` can be rejected because `f` is a short command not in resolver allowlist.

So the recipe is readable, but strict receiver policy may block it unless adjusted.

## Optimized recipe shape (recommended)

For this Zed restore scenario, prefer a single exec step via `/bin/bash`:

```action-pack
timeout 0
exec /bin/bash -lc "/usr/bin/open -a Zed /Users/nikiv/repos/alibaba/zvec && cd /Users/nikiv/repos/alibaba/zvec && f ai claude resume 32e0aaad-203c-4980-bdb9-5a9a1a7ce878"
```

Why this is better:

- One exec step instead of two exec steps.
- Uses `/bin/bash` (commonly allowlisted in current receiver defaults).
- Fewer server-side step transitions and less response payload.

Tradeoff:

- Shell startup adds small overhead.
- Policy is less strict if `/bin/bash` remains allowlisted.

## Performance guidance (zvec-style mindset)

zvec wins from batching and data-local execution. Apply the same principles here:

1. Batch, do not recompile every run:
   - Build `.sap` once.
   - Reuse `send` many times.
2. Reduce per-pack step count:
   - Fuse setup into one exec for latency-sensitive restores.
3. Keep payloads lean:
   - avoid `put` for large files unless needed.
4. Use fixed receiver aliases:
   - avoid avoidable DNS/lookup overhead in hot loops.
5. Tune receiver limits for throughput tests:
   - `--action-pack-max-conns`
   - `--action-pack-io-timeout-ms`
   - max request/output limits

## Hardening for production

For high-trust production receivers:

1. Remove `/bin/bash` from policy allowlist.
2. Add only exact absolute commands needed.
3. Use key-specific policy files per sender.
4. Keep `--action-pack-root` narrow (repo subtree, not home root).
5. Keep TTL short for one-shot automation.

## Practical test loop

From sender side:

```bash
seq action-pack pack /tmp/restore-zvec.action-pack.txt --out /tmp/restore-zvec.sap --id default --ttl-ms 600000
seq action-pack send --to testmac /tmp/restore-zvec.sap
```

Receiver should return:

- one `OK pack_id=... steps=...`
- per-step status lines (`STEP ... exec exit=... dur_ms=...`)

