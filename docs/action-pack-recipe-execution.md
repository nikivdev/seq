# Seq Executable Markdown Recipes Via Action-Pack

This is the canonical reference for executable markdown recipes in `seq`.

Scope:
- markdown recipes that contain a ` ```action-pack ` fenced block
- how those blocks are compiled/signed/sent/executed
- strict and permissive templates for real workflows (Zed + Claude/Codex resume)

## What Is Executable

Only the extracted `action-pack` block is executable.

`seq` does not parse markdown directly. A caller extracts the block to a plain script file, then runs one of:
- `seq action-pack run --to <receiver|ip:port> <script.ap> ...`
- `seq action-pack pack <script.ap> --out <pack.sap> ...` then `seq action-pack send ...`

## Canonical Markdown Shape

Recommended recipe shape is one explicit block:

````markdown
# Restore workspace

Some human notes here.

```action-pack
timeout 0
cd /Users/nikiv/repos/alibaba/zvec
exec /Users/nikiv/.flow/bin/f ai codex continue
```
````

Caller-side extraction example (first action-pack block):

```bash
awk '
  /^```action-pack[[:space:]]*$/ { in_block=1; next }
  /^```[[:space:]]*$/ && in_block { exit }
  in_block { print }
' /path/to/recipe.md > /tmp/recipe.ap
```

## Script Grammar (From `compile_script`)

Supported instructions:
- `cd <path>`
- `timeout <ms>`
- `env KEY=VALUE`
- `put <dest_abs_path> @<src_path>`
- `exec <argv...>`

Parsing behavior:
- blank lines and `# comment` lines are ignored
- quotes are token delimiters (`'` and `"`)
- backslash escapes the next character
- no shell evaluation in script lines

Important constraints:
- script must produce at least one step (`script has no steps` otherwise)
- `put` destination must be absolute
- total embedded `put` bytes max is 8 MiB
- `timeout` is parsed as integer ms (`uint32`)

## End-to-End Path

1. Sender compiles script to `Pack` (`action_pack::compile_script`).
2. Sender encodes payload (`APK1`), signs with P-256 key, wraps into `SAP1` envelope.
3. Sender sends envelope bytes over TCP to receiver.
4. Receiver verifies signature and TTL/replay constraints.
5. Receiver executes steps with root/policy/env guards.
6. Sender prints textual response (`OK ...`, `STEP ...`, stdout/stderr).

## Sender Commands

One-shot path:

```bash
seq action-pack run \
  --to testmac \
  /tmp/recipe.ap \
  --id default \
  --ttl-ms 600000
```

Two-step reusable path:

```bash
seq action-pack pack /tmp/recipe.ap --out /tmp/recipe.sap --id default --ttl-ms 600000
seq action-pack send --to testmac /tmp/recipe.sap
```

Pair/register helpers:

```bash
seq action-pack pair testmac testmac:5011 --id default
seq action-pack receivers
```

## Receiver Setup

Fast path:

```bash
cd ~/code/seq
tools/action_pack_receiver_enable.sh \
  --listen 0.0.0.0:5011 \
  --trust default "$(seq action-pack export-pub --id default)" \
  --root /Users/nikiv
```

This writes:
- `~/Library/Application Support/seq/action_pack_receiver.conf`
- `~/Library/Application Support/seq/action_pack_pubkeys`
- `~/Library/Application Support/seq/action_pack.policy`

Receiver-side daemon must run with action-pack enabled (or load `action_pack_receiver.conf`).
Receiver refuses to run action-pack server without `--action-pack-root`.

## Policy Model

If a policy file is configured, receiver enters strict per-key mode:
- missing `key_id` in policy => `ERR policy missing for key_id`
- only listed `cmd=` absolute commands are executable (unless `allow_root_scripts=1`)
- only listed `env=` keys survive filtering
- executable writes are blocked unless `allow_exec_writes=1`

Policy line format:

```text
<key_id> cmd=/usr/bin/git cmd=/bin/bash env=HOME allow_root_scripts=0 allow_exec_writes=0
```

Without policy file, built-in allowlist is used (small default set in `action_pack_server.cpp`).

## Command Resolution Details

Receiver resolves short command names through a fixed map before policy checks:
- `git`, `make`, `pwd`, `echo`, `ls`, `rm`, `mkdir`
- `bash`, `zsh`, `python3`, `xcodebuild`, `clang`, `clang++`

If `argv[0]`:
- is absolute (`/usr/bin/git`): used directly
- has `/` but is relative (`./tools/x`): resolved under `cwd` and must stay under root
- is short and not in map (`f`): rejected (`cmd_not_allowed`)

So `exec f ai codex continue` is usually rejected on strict receivers.

## Variables and Paths

Receiver performs minimal expansion on `cwd`, command args, and write paths:
- leading `~/`
- `$HOME`
- `${HOME}`

CWD and relative command resolution must stay under `--action-pack-root`.

## Zed + Session Restore Recipe Patterns

Strict pattern (prefer for production, explicit command allowlist):

```action-pack
timeout 0
exec /usr/bin/open -a Zed /Users/nikiv/repos/alibaba/zvec
cd /Users/nikiv/repos/alibaba/zvec
exec /Users/nikiv/.flow/bin/f ai codex continue
```

Permissive pattern (fewer steps, easier to allow, broader shell power):

```action-pack
timeout 0
exec /bin/bash -lc "/usr/bin/open -a Zed /Users/nikiv/repos/alibaba/zvec && cd /Users/nikiv/repos/alibaba/zvec && /Users/nikiv/.flow/bin/f ai codex continue"
```

Tradeoff:
- strict: safer, more policy maintenance
- bash wrapper: simpler packaging, less strict security boundary

## Build-Now Checklist

1. Build/deploy seq CLI on sender and receiver.
2. Generate/export sender pubkey (`seq action-pack keygen --id ...`).
3. Configure receiver trust + root + policy.
4. Extract `action-pack` block from markdown into `.ap`.
5. Run with `seq action-pack run --to ...`.
6. Verify response has:
   - `OK pack_id=... steps=...`
   - `STEP ... exec exit=0 ...` for each step

## Troubleshooting Quick Map

- `ERR unknown instruction: X`
  - typo in script op
- `ERR script has no steps`
  - no `exec`/`put` after extraction
- `STEP ... ERR cmd_not_allowed`
  - command not in policy/allowlist, or short name not resolvable
- `STEP ... ERR cwd_outside_root` / `cmd_outside_root`
  - command/cwd escapes `--action-pack-root`
- `ERR policy missing for key_id`
  - add key line to policy file
- `ERR pack expired`
  - increase `--ttl-ms` or fix clock skew
- `ERR replay`
  - pack reused before expiry; repack to get new `pack_id`
- `ERR signature invalid`
  - sender key mismatch with receiver pubkey entry

## Performance Notes

For high-throughput loops:
- use `pack` once + repeated `send` when script is stable
- reduce step count
- avoid large embedded `put` payloads
- tune receiver `--action-pack-max-conns`, `--action-pack-io-timeout-ms`, output/request caps
