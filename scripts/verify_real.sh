#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found"
  exit 1
fi

squid_id=$(docker compose ps -q squid)
if [ -z "$squid_id" ]; then
  echo "squid is not running. run: docker compose up -d"
  exit 1
fi

MODE=$(docker compose exec -T squid sh -c 'printf "%s" "${MODE:-shared}"')
UPSTREAM_SOURCE=$(docker compose exec -T squid python3 /opt/helper/upstream_pool.py source)
UPSTREAM_COUNT=$(docker compose exec -T squid python3 /opt/helper/upstream_pool.py count)
if ! [[ "$UPSTREAM_COUNT" =~ ^[0-9]+$ ]] || [ "$UPSTREAM_COUNT" -le 0 ]; then
  echo "invalid upstream count: $UPSTREAM_COUNT"
  exit 1
fi

VERIFY_URL=${VERIFY_URL:-https://ipinfo.io/json}
PROXY_HOST=${PROXY_HOST:-localhost}
PROXY_PORT=${PROXY_PORT:-3128}
PROXY_PASS=${PROXY_PASS:-}
DEBUG=${DEBUG:-0}

if [ -z "$PROXY_PASS" ] && [ -f .env ]; then
  PROXY_PASS=$(grep -E '^AUTH_PASSWORD=' .env | head -n 1 | sed 's/^AUTH_PASSWORD=//')
fi
if [ -z "$PROXY_PASS" ]; then
  echo "PROXY_PASS not set. Set PROXY_PASS or AUTH_PASSWORD in .env"
  exit 1
fi

SAMPLE_USERS=${SAMPLE_USERS:-5}
if [ "$SAMPLE_USERS" -lt 1 ]; then
  SAMPLE_USERS=1
fi
if [ "$MODE" = "exclusive" ] && [ "$SAMPLE_USERS" -gt "$UPSTREAM_COUNT" ]; then
  SAMPLE_USERS="$UPSTREAM_COUNT"
fi

VERIFY_PREFIX=${VERIFY_PREFIX:-verify_user}
VERIFY_USERS=${VERIFY_USERS:-}

tmp_dir=$(mktemp -d)
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

users_file="$tmp_dir/users.txt"
ips1="$tmp_dir/ips1.txt"
ips2="$tmp_dir/ips2.txt"

if [ -n "$VERIFY_USERS" ]; then
  printf "%s" "$VERIFY_USERS" | tr ',' '\n' > "$users_file"
else
  i=1
  while [ "$i" -le "$SAMPLE_USERS" ]; do
    printf "%s\n" "${VERIFY_PREFIX}${i}" >> "$users_file"
    i=$((i + 1))
  done
fi

fetch_ip() {
  user="$1"
  proxy="http://${user}:${PROXY_PASS}@${PROXY_HOST}:${PROXY_PORT}"
  curl_log="${CURL_LOG:-/dev/null}"
  if ! json=$(curl -sS --max-time 20 --connect-timeout 10 -x "$proxy" "$VERIFY_URL" ${DEBUG:+-v} --stderr "$curl_log"); then
    return 1
  fi
  if [ -z "$json" ]; then
    return 1
  fi
  ip=$(printf "%s" "$json" | python3 -c 'import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get("ip", ""))
except Exception:
    print("")
')
  if [ -z "$ip" ]; then
    return 1
  fi
  printf "%s" "$ip"
}

show_diagnostics() {
  if [ "$DEBUG" != "1" ]; then
    return
  fi
  echo "---- curl debug (last user) ----"
  if [ -n "${CURL_LOG:-}" ] && [ -f "$CURL_LOG" ]; then
    tail -n 80 "$CURL_LOG" || true
  fi
  echo "---- proxy service logs ----"
  docker compose logs --tail 80 squid || true
}

while IFS= read -r user; do
  CURL_LOG="$tmp_dir/curl_${user}.log"
  if ! ip=$(fetch_ip "$user"); then
    echo "request failed for user: $user"
    show_diagnostics
    exit 1
  fi
  printf "%s %s\n" "$user" "$ip" >> "$ips1"
done < "$users_file"

while IFS= read -r user; do
  CURL_LOG="$tmp_dir/curl_${user}.log"
  if ! ip=$(fetch_ip "$user"); then
    echo "request failed for user: $user"
    show_diagnostics
    exit 1
  fi
  printf "%s %s\n" "$user" "$ip" >> "$ips2"
done < "$users_file"

if ! awk 'NR==FNR{a[$1]=$2;next} {if(a[$1]!=$2){exit 1}}' "$ips1" "$ips2"; then
  echo "sticky check failed"
  exit 1
fi

unique=$(cut -d' ' -f2 "$ips1" | sort -u | wc -l | tr -d ' ')
count=$(wc -l < "$ips1" | tr -d ' ')

printf "mode=%s users=%s unique_ips=%s source=%s\n" "$MODE" "$count" "$unique" "$UPSTREAM_SOURCE"
cat "$ips1"

echo "ok"
