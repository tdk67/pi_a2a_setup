#!/bin/bash
# a2a-send.sh — Send a message to an A2A agent and poll for the result
# Usage: ./a2a-send.sh <agent-url> <token> "Your message here"

set -e

AGENT_URL="${1:?Usage: $0 <agent-url> <token> <message>}"
A2A_TOKEN="${2:?Missing token}"
MESSAGE="${3:?Missing message}"
MSG_ID="cli-$(date +%s)-$$"

echo "→ Sending to $AGENT_URL..."

# Step 1: Send task
RESP=$(curl -s -X POST "$AGENT_URL/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $A2A_TOKEN" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 1,
    \"method\": \"message/send\",
    \"params\": {
      \"message\": {
        \"role\": \"user\",
        \"parts\": [{\"kind\": \"text\", \"text\": $(echo "$MESSAGE" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")}],
        \"kind\": \"message\",
        \"messageId\": \"$MSG_ID\"
      }
    }
  }")

TASK_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['id'])" 2>/dev/null) || {
  echo "✗ Failed to get task ID. Response:"
  echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
  exit 1
}

echo "  Task ID: $TASK_ID"
echo "  Waiting for response..."

# Step 2: Poll with relaxed backoff (4,4,8,8,16,16 = max 56s)
DELAYS="4 4 8 8 16 16"
for delay in $DELAYS; do
  sleep $delay
  TASK=$(curl -s -X POST "$AGENT_URL/" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $A2A_TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tasks/get\",\"params\":{\"id\":\"$TASK_ID\"}}")

  STATE=$(echo "$TASK" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['status']['state'])" 2>/dev/null || echo "error")

  case "$STATE" in
    completed)
      echo ""
      echo "══════════════ RESPONSE ══════════════"
      echo "$TASK" | python3 -c "
import sys, json
task = json.load(sys.stdin)
for a in task['result'].get('artifacts',[]):
    for p in a.get('parts',[]):
        if p['kind'] == 'text': print(p['text'])
"
      echo "═══════════════════════════════════════"
      exit 0
      ;;
    failed)
      echo "✗ Task failed!"
      echo "$TASK" | python3 -m json.tool 2>/dev/null || echo "$TASK"
      exit 1
      ;;
    canceled)
      echo "✗ Task was canceled."
      exit 1
      ;;
  esac
  echo -n "."
done

echo ""
echo "✗ Timeout after 56s (relaxed backoff)"
exit 1