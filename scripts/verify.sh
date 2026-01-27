#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found"
  exit 1
fi

squid_id=$(docker compose ps -q squid)
if [ -z "$squid_id" ]; then
  echo "squid is not running. run: docker compose up -d"
  exit 1
fi

MODE=$(docker compose exec -T squid sh -c 'printf "%s" "${MODE:-shared}"')
PORT_FIRST=$(docker compose exec -T squid sh -c 'printf "%s" "${PORT_FIRST:-10001}"')
PORT_LAST=$(docker compose exec -T squid sh -c 'printf "%s" "${PORT_LAST:-10100}"')
EXCLUSIVE_FALLBACK=$(docker compose exec -T squid sh -c 'printf "%s" "${EXCLUSIVE_FALLBACK:-deny}"')
CAP_PER_PORT=$(docker compose exec -T squid sh -c 'printf "%s" "${CAP_PER_PORT:-2}"')

PORT_COUNT=$((PORT_LAST - PORT_FIRST + 1))
if [ "$PORT_COUNT" -le 0 ]; then
  echo "invalid port range: $PORT_FIRST-$PORT_LAST"
  exit 1
fi

SAMPLE_USERS=${SAMPLE_USERS:-20}
if [ "$SAMPLE_USERS" -lt 2 ]; then
  SAMPLE_USERS=2
fi
if [ "$MODE" = "exclusive" ] && [ "$SAMPLE_USERS" -gt "$PORT_COUNT" ]; then
  SAMPLE_USERS="$PORT_COUNT"
fi

VERIFY_PREFIX=${VERIFY_PREFIX:-verify_user}
VERIFY_TTL_SECONDS=${VERIFY_TTL_SECONDS:-120}

tmp_dir=$(mktemp -d)
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

users_file="$tmp_dir/users.txt"
ports1="$tmp_dir/ports1.txt"
ports2="$tmp_dir/ports2.txt"

i=1
while [ "$i" -le "$SAMPLE_USERS" ]; do
  printf "%s\n" "${VERIFY_PREFIX}${i}" >> "$users_file"
  i=$((i + 1))
done

exec_router() {
  if [ "$MODE" = "exclusive" ] || [ "$MODE" = "shared_capped" ]; then
    docker compose exec -T -e TTL_SECONDS="$VERIFY_TTL_SECONDS" squid python3 /opt/helper/router.py
  else
    docker compose exec -T squid python3 /opt/helper/router.py
  fi
}

run_router() {
  exec_router | awk '
    /^OK / { sub(/^.*message=/, ""); print; next }
    { print "ERR" }
  '
}

run_router < "$users_file" > "$ports1"
run_router < "$users_file" > "$ports2"

line_count=$(wc -l < "$ports1" | tr -d ' ')
line_count2=$(wc -l < "$ports2" | tr -d ' ')
if [ "$line_count" -ne "$SAMPLE_USERS" ] || [ "$line_count2" -ne "$SAMPLE_USERS" ]; then
  echo "unexpected output size"
  exit 1
fi

if grep -q "^ERR$" "$ports1"; then
  echo "router returned ERR"
  exit 1
fi

if ! diff -q "$ports1" "$ports2" >/dev/null; then
  echo "sticky check failed"
  exit 1
fi

case "$MODE" in
  shared)
    unique=$(sort -u "$ports1" | wc -l | tr -d ' ')
    echo "mode=shared sample=$SAMPLE_USERS unique_ports=$unique"
    ;;
  exclusive)
    dup=$(sort "$ports1" | uniq -d | head -n 1 || true)
    if [ -n "$dup" ]; then
      echo "exclusive check failed: duplicate ports"
      exit 1
    fi
    unique=$(sort -u "$ports1" | wc -l | tr -d ' ')
    echo "mode=exclusive sample=$SAMPLE_USERS unique_ports=$unique fallback=$EXCLUSIVE_FALLBACK"
    ;;
  shared_capped)
    max=$(sort "$ports1" | uniq -c | awk '{if($1>max)max=$1}END{print max+0}')
    if [ "$max" -gt "$CAP_PER_PORT" ]; then
      echo "shared_capped check failed: cap exceeded"
      exit 1
    fi
    unique=$(sort -u "$ports1" | wc -l | tr -d ' ')
    echo "mode=shared_capped sample=$SAMPLE_USERS cap=$CAP_PER_PORT unique_ports=$unique"
    ;;
  *)
    echo "unknown mode: $MODE"
    exit 1
    ;;
esac

echo "ok"
