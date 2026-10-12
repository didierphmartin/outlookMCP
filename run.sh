#!/usr/bin/env bash
# Start the Outlook MCP server. Reads .env next to this file (OUTLOOK_CLIENT_ID, OUTLOOK_SECRET_KEY, ...).
# OUTLOOK_PYTHON picks the interpreter used to create .venv (default: python3 on PATH); it must be >= 3.10.
# e.g. OUTLOOK_PYTHON=/usr/local/bin/python3 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
# .env is read line by line, not sourced: values such as the Event Hub connection string contain ';'.
# KEY=value, optionally quoted; blank lines and # comments skipped; variables already set win.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    case "$val" in \"*\") val="${val:1:${#val}-2}" ;; \'*\') val="${val:1:${#val}-2}" ;; esac
    [ -n "$key" ] && [ -z "${!key+x}" ] && export "$key=$val"
  done < .env
fi
if [ ! -d .venv ]; then
  PY="${OUTLOOK_PYTHON:-python3}"
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "run.sh: '$PY' is missing or older than Python 3.10; set OUTLOOK_PYTHON to a 3.10+ interpreter" >&2
    exit 1
  fi
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python server.py
