#!/usr/bin/env bash
# Smoke test in fake mode: connect, poll nothing, add a mail, poll it, poll nothing.
set -euo pipefail
cd "$(dirname "$0")"
export OUTLOOK_FAKE_GRAPH=1 OUTLOOK_DB_PATH="$(mktemp -d)/smoke.sqlite" OUTLOOK_PORT=8779
.venv/bin/python server.py & PID=$!; trap 'kill $PID' EXIT; sleep 2
U=http://127.0.0.1:8779/mcp
call() { local h=(); [ -n "${3:-}" ] && h=(-H "X-Outlook-Account: $3")
  curl -s $U -H 'Content-Type: application/json' ${h[@]+"${h[@]}"} \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
  | .venv/bin/python -c 'import json,sys; print(json.loads(json.load(sys.stdin)["result"]["content"][0]["text"]))'; }
CID=$(call connect_start '{}' | .venv/bin/python -c "import ast,sys; print(ast.literal_eval(sys.stdin.read())['connect_id'])")
sleep 6
KEY=$(call connect_status "{\"connect_id\":\"$CID\"}" | .venv/bin/python -c "import ast,sys; print(ast.literal_eval(sys.stdin.read())['connection_key'])")
call poll_new_mail '{"folder":"Inbox"}' "$KEY"
curl -s -X POST http://127.0.0.1:8779/fake/messages -H 'Content-Type: application/json' \
  -d '{"subject":"Smoke","sender":"billing@acme.com","attachments":[{"name":"a.txt","text":"x"}]}' > /dev/null
call poll_new_mail '{"folder":"Inbox"}' "$KEY"
call poll_new_mail '{"folder":"Inbox"}' "$KEY"
curl -s http://127.0.0.1:8779/fake/requests | grep -c '\$value' && echo "FAIL: attachment fetched" || echo "OK: no attachment content fetched"
