#!/usr/bin/env bash
# Post-deploy check: health, model list, one real completion.
# Usage: scripts/doctor.sh [base_url]   (default http://127.0.0.1:$HOST_PORT from .env, else 8127)
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
BASE="${1:-http://127.0.0.1:${HOST_PORT:-8127}}"
AUTH=(); [ -n "${AMAZEE_PROXY_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer $AMAZEE_PROXY_API_KEY")
MODEL="${DOCTOR_MODEL:-claude-sonnet-5-5}"
fail=0
ok(){ echo "OK   $*"; }; bad(){ echo "FAIL $*"; fail=$((fail+1)); }
h=$(curl -fsS -m 10 "$BASE/health") && ok "health $h" || bad "health unreachable at $BASE (container up? port?)"
m=$(curl -fsS -m 30 "${AUTH[@]}" "$BASE/v1/models" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"]))' 2>/dev/null) \
  && ok "models: $m listed" || bad "/v1/models (auth key mismatch? upstream down?)"
r=$(curl -sS -m 120 "${AUTH[@]}" -H 'Content-Type: application/json' "$BASE/v1/chat/completions" \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":300,\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: DOCTOR-OK\"}]}")
echo "$r" | grep -q 'DOCTOR-OK' && ok "chat $MODEL replied DOCTOR-OK" || bad "chat $MODEL: $(echo "$r" | head -c 300)"
echo "---- $fail failure(s)"; exit $fail
