# seqmem (Swift)

Swift in-process "memory engine" for `seqd`, backed by Wax.

## Build

```bash
./cli/swift/seqmem/run.sh build
```

The C++ build (`cli/cpp/run.sh`) also builds this and copies `libseqmem.dylib` next to the `seq` binary.

## Runtime env

- `SEQ_MEM_DYLIB_PATH`: override dylib path for `dlopen` (default: `$(dirname seq)/libseqmem.dylib`)
- `SEQ_MEM_WAX_PATH`: override Wax file path (default: `~/Library/Application Support/seq/seqmem.mv2s`)
- `WAX_PATH`: override SwiftPM dependency path to Wax (defaults to `/Users/nikiv/repos/christopherkarani/Wax`)
- `SEQ_CH_MEM_PATH`: if set, append JSONEachRow events to this path (default: `~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl`)
- `SEQ_CH_MODE`: ClickHouse emission mode (default: `file`)
  - `native`: push native protocol to `SEQ_CH_HOST:SEQ_CH_PORT`; if native bridge is unavailable, fall back to `SEQ_CH_MEM_PATH`
  - `mirror`: push native protocol and append local JSONEachRow spool in parallel
  - `file`: append only to local JSONEachRow spool
  - `off`: disable ClickHouse emission
- `SEQ_MEM_SESSION_ID`: override session id tag (default: random UUID per process)
- `SEQ_MEM_TTL_DAYS`: delete old `seqmem.*` frames from Wax periodically (default: `14`, set `0` to disable)
- `SEQ_MEM_DEDUP_WINDOW_MS`: persistence dedup window for identical events (default: `250`, set `0` to disable)
- `SEQ_MEM_DEDUP_CAP`: dedup ring size (default: `4096`)

## ClickHouse

The engine can emit `JSONEachRow` rows to `SEQ_CH_MEM_PATH` (`file`/`mirror` modes, or `native` fallback). To ingest into a real ClickHouse table:

```bash
tools/clickhouse/seqmem_setup.sh
tools/clickhouse/seqmem_ingest.sh   # defaults to $SEQ_CH_MEM_PATH / ~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl
```

## Interop API

Swift exports a C ABI via `@_cdecl` in `cli/swift/seqmem/Sources/seqmem/SeqMemExports.swift`.
