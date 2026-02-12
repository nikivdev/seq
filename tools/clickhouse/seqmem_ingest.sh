#!/usr/bin/env bash
set -euo pipefail

client="${CLICKHOUSE_CLIENT:-clickhouse-client}"
db="${SEQ_CH_DB:-seq}"
table="${SEQ_CH_TABLE:-mem_events}"

default_file="${SEQ_CH_MEM_PATH:-${HOME}/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl}"
file="${1:-$default_file}"

if [[ ! -f "$file" ]]; then
  echo "error: file not found: $file" >&2
  exit 1
fi

"$client" --query "INSERT INTO ${db}.${table} FORMAT JSONEachRow" < "$file"

