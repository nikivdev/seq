#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF' >&2
USAGE:
  tools/action_pack_receiver_enable.sh --listen <ip:port|:port> --trust <key_id> <pubkey_b64> [--root <path>]

Writes:
  ~/Library/Application Support/seq/action_pack_receiver.conf
  ~/Library/Application Support/seq/action_pack_pubkeys
  ~/Library/Application Support/seq/action_pack.policy
EOF
  exit 2
}

LISTEN=""
ROOT="/tmp"
KEY_ID=""
PUBKEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --listen)
      [[ $# -ge 2 ]] || usage
      LISTEN="$2"
      shift 2
      ;;
    --root)
      [[ $# -ge 2 ]] || usage
      ROOT="$2"
      shift 2
      ;;
    --trust)
      [[ $# -ge 2 ]] || usage
      KEY_ID="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      if [[ -z "${PUBKEY}" ]]; then
        PUBKEY="$1"
        shift 1
      else
        usage
      fi
      ;;
  esac
done

[[ -n "${LISTEN}" && -n "${KEY_ID}" && -n "${PUBKEY}" ]] || usage

SUPPORT_DIR="${HOME}/Library/Application Support/seq"
PUBKEYS="${SUPPORT_DIR}/action_pack_pubkeys"
POLICY="${SUPPORT_DIR}/action_pack.policy"
CONF="${SUPPORT_DIR}/action_pack_receiver.conf"

mkdir -p "${SUPPORT_DIR}"

rewrite_kv_file() {
  local path="$1"
  local key="$2"
  local value="$3"
  local tmp="${path}.tmp.$$"
  if [[ -f "${path}" ]]; then
    # Keep other keys, replace this key.
    awk -v k="${key}" '($1!=k){print $0}' "${path}" > "${tmp}" || true
  else
    : > "${tmp}"
  fi
  printf '%s %s\n' "${key}" "${value}" >> "${tmp}"
  mv "${tmp}" "${path}"
}

rewrite_policy_line() {
  local path="$1"
  local key="$2"
  local tmp="${path}.tmp.$$"
  if [[ -f "${path}" ]]; then
    awk -v k="${key}" '($1!=k){print $0}' "${path}" > "${tmp}" || true
  else
    : > "${tmp}"
  fi
  # Keep this intentionally strict. You can relax on the receiver if needed.
  printf '%s cmd=/usr/bin/git cmd=/usr/bin/make cmd=/bin/rm cmd=/bin/mkdir cmd=/bin/bash cmd=/usr/bin/python3 cmd=/usr/bin/xcodebuild cmd=/usr/bin/xcrun cmd=/usr/bin/clang cmd=/usr/bin/clang++ allow_root_scripts=0 allow_exec_writes=0\n' "${key}" >> "${tmp}"
  mv "${tmp}" "${path}"
}

rewrite_kv_file "${PUBKEYS}" "${KEY_ID}" "${PUBKEY}"
rewrite_policy_line "${POLICY}" "${KEY_ID}"

cat > "${CONF}" <<EOF
# seq action-pack receiver config
listen=${LISTEN}
root=${ROOT}
pubkeys=${PUBKEYS}
policy=${POLICY}
allow_local=1
allow_tailscale=1
max_conns=4
io_timeout_ms=5000
max_request=4194304
max_output=1048576
EOF

echo "OK wrote:"
echo "  ${CONF}"
echo "  ${PUBKEYS}"
echo "  ${POLICY}"
