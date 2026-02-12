#!/usr/bin/env bash
set -euo pipefail

cmd="${1:-}"

DATA_DIR="${SEQ_CH_LOCAL_DATA_DIR:-/tmp/seq_clickhouse_data}"
PIDFILE="${SEQ_CH_LOCAL_PIDFILE:-/tmp/seq_clickhouse_server.pid}"
OUTLOG="${SEQ_CH_LOCAL_OUTLOG:-/tmp/seq_clickhouse_server.out}"
CFG="${SEQ_CH_LOCAL_CONFIG:-/tmp/seq_clickhouse_config.xml}"
USERS="${SEQ_CH_LOCAL_USERS:-/tmp/seq_clickhouse_users.xml}"

HOST="${SEQ_CH_LOCAL_HOST:-127.0.0.1}"
HTTP_PORT="${SEQ_CH_LOCAL_HTTP_PORT:-8123}"
TCP_PORT="${SEQ_CH_LOCAL_TCP_PORT:-9000}"

USER_FILES_PATH="${SEQ_CH_USER_FILES_PATH:-$HOME/repos/ClickHouse/ClickHouse/user_files/}"

die() {
  echo "error: $*" >&2
  exit 1
}

is_listening() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1
}

ensure_not_listening() {
  local port="$1"
  if is_listening "$port"; then
    local pid
    pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1 || true)"
    die "port $port is already in use (pid ${pid:-?})"
  fi
}

write_config() {
  mkdir -p "$DATA_DIR/tmp" "$DATA_DIR/format_schemas"

  cat >"$USERS" <<'XML'
<clickhouse>
  <profiles>
    <default>
      <max_memory_usage>4000000000</max_memory_usage>
      <max_threads>8</max_threads>
    </default>
  </profiles>

  <users>
    <default>
      <password></password>
      <networks>
        <ip>::1</ip>
        <ip>127.0.0.1</ip>
      </networks>
      <profile>default</profile>
      <quota>default</quota>
      <access_management>1</access_management>
    </default>
  </users>

  <quotas>
    <default>
      <interval>
        <duration>3600</duration>
        <queries>0</queries>
        <errors>0</errors>
        <result_rows>0</result_rows>
        <read_rows>0</read_rows>
        <execution_time>0</execution_time>
      </interval>
    </default>
  </quotas>
</clickhouse>
XML

  cat >"$CFG" <<XML
<clickhouse>
  <logger>
    <level>information</level>
    <console>1</console>
  </logger>

  <listen_host>${HOST}</listen_host>
  <http_port>${HTTP_PORT}</http_port>
  <tcp_port>${TCP_PORT}</tcp_port>

  <path>${DATA_DIR}/</path>
  <tmp_path>${DATA_DIR}/tmp/</tmp_path>
  <user_files_path>${USER_FILES_PATH}</user_files_path>
  <format_schema_path>${DATA_DIR}/format_schemas/</format_schema_path>

  <users_config>${USERS}</users_config>
</clickhouse>
XML
}

start_server() {
  ensure_not_listening "$HTTP_PORT"
  ensure_not_listening "$TCP_PORT"

  if [[ -s "$PIDFILE" ]]; then
    die "pidfile exists ($PIDFILE). Run: $0 stop"
  fi

  write_config

  rm -f "$OUTLOG"
  (clickhouse server --config-file="$CFG" --pidfile="$PIDFILE" >"$OUTLOG" 2>&1 &)

  for _ in $(seq 1 80); do
    if clickhouse-client --host "$HOST" --port "$TCP_PORT" -q "SELECT 1" >/dev/null 2>&1; then
      echo "ok: clickhouse server ready on ${HOST}:${TCP_PORT} (http ${HOST}:${HTTP_PORT})"
      echo "logs: $OUTLOG"
      return 0
    fi
    sleep 0.1
  done

  echo "server did not become ready; last log lines:" >&2
  tail -50 "$OUTLOG" >&2 || true
  exit 1
}

stop_server() {
  if [[ ! -s "$PIDFILE" ]]; then
    echo "not running (no pidfile: $PIDFILE)"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -z "${pid}" ]]; then
    rm -f "$PIDFILE"
    echo "not running (empty pidfile)"
    return 0
  fi
  kill "$pid" >/dev/null 2>&1 || true
  rm -f "$PIDFILE"
  echo "stopped (pid $pid)"
}

status_server() {
  if [[ -s "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && ps -p "$pid" >/dev/null 2>&1; then
      echo "running (pid $pid) tcp=${HOST}:${TCP_PORT} http=${HOST}:${HTTP_PORT}"
      exit 0
    fi
  fi
  echo "not running"
  exit 1
}

case "$cmd" in
  start) start_server ;;
  stop) stop_server ;;
  status) status_server ;;
  *)
    cat >&2 <<EOF
Usage: $0 {start|stop|status}

Env overrides:
  SEQ_CH_LOCAL_DATA_DIR, SEQ_CH_LOCAL_PIDFILE, SEQ_CH_LOCAL_OUTLOG
  SEQ_CH_LOCAL_HOST, SEQ_CH_LOCAL_HTTP_PORT, SEQ_CH_LOCAL_TCP_PORT
  SEQ_CH_USER_FILES_PATH (default: \$HOME/repos/ClickHouse/ClickHouse/user_files/)
EOF
    exit 2
    ;;
esac

