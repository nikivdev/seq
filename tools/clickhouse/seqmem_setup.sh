#!/usr/bin/env bash
set -euo pipefail

client="${CLICKHOUSE_CLIENT:-clickhouse-client}"
sql_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$client" --multiquery < "$sql_dir/seqmem.sql"

