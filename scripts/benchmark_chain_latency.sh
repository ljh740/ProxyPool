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

env_file_value() {
  local key="$1"
  if [ ! -f .env ]; then
    return 0
  fi
  sed -n "s/^${key}=//p" .env | head -n 1
}

trim_spaces() {
  printf "%s" "$1" | tr -d ' '
}

BENCHMARK_UPSTREAM_HOST=${BENCHMARK_UPSTREAM_HOST:-$(env_file_value BENCHMARK_UPSTREAM_HOST)}
BENCHMARK_UPSTREAM_SCHEME=${BENCHMARK_UPSTREAM_SCHEME:-$(env_file_value BENCHMARK_UPSTREAM_SCHEME)}
BENCHMARK_UPSTREAM_USER=${BENCHMARK_UPSTREAM_USER:-$(env_file_value BENCHMARK_UPSTREAM_USER)}
BENCHMARK_UPSTREAM_PASS=${BENCHMARK_UPSTREAM_PASS:-$(env_file_value BENCHMARK_UPSTREAM_PASS)}
AUTH_PASSWORD=${AUTH_PASSWORD:-$(env_file_value AUTH_PASSWORD)}

if [ -z "$BENCHMARK_UPSTREAM_SCHEME" ]; then
  BENCHMARK_UPSTREAM_SCHEME="http"
fi

VERIFY_URL=${VERIFY_URL:-https://ipinfo.io/json}
PROXY_HOST=${PROXY_HOST:-localhost}
PROXY_PORT=${PROXY_PORT:-3128}
PROXY_PASS=${PROXY_PASS:-$AUTH_PASSWORD}
VERIFY_PREFIX=${VERIFY_PREFIX:-bench_user}
SAMPLE_USERS=${SAMPLE_USERS:-5}
ROUNDS=${ROUNDS:-3}
WARMUP_ROUNDS=${WARMUP_ROUNDS:-1}
BENCHMARK_PORTS=${BENCHMARK_PORTS:-10072,10064,10015,10020,10018}
BENCHMARK_HOST_PORTS=${BENCHMARK_HOST_PORTS:-30001,30002,30003,30004,30005}
DIRECT_UPSTREAM_FILE=${DIRECT_UPSTREAM_FILE:-}
CHAIN_UPSTREAM_FILE=${CHAIN_UPSTREAM_FILE:-}
RESTORE_ORIGINAL=${RESTORE_ORIGINAL:-1}
RESULT_JSON=${RESULT_JSON:-}
RESULT_DIR=${RESULT_DIR:-}

if [ -z "$PROXY_PASS" ]; then
  echo "PROXY_PASS not set. Set PROXY_PASS or AUTH_PASSWORD in .env"
  exit 1
fi

if [ -z "$BENCHMARK_UPSTREAM_HOST" ] && { [ -z "$DIRECT_UPSTREAM_FILE" ] || [ -z "$CHAIN_UPSTREAM_FILE" ]; }; then
  echo "BENCHMARK_UPSTREAM_HOST is required when auto-generating benchmark upstream files"
  exit 1
fi

tmp_dir=$(mktemp -d)
original_proxy_list_file="$tmp_dir/original_proxy_list.json"
generated_direct_file="$ROOT/config/.benchmark_direct_$$.txt"
generated_chain_file="$ROOT/config/.benchmark_chain_$$.txt"
prepared_direct_file="$ROOT/config/.benchmark_direct_input_$$.txt"
prepared_chain_file="$ROOT/config/.benchmark_chain_input_$$.txt"
raw_direct="$tmp_dir/direct.tsv"
raw_chain="$tmp_dir/chain.tsv"

backup_current_proxy_list() {
  if [ -z "$(docker compose ps -q squid 2>/dev/null)" ]; then
    printf "[]" > "$original_proxy_list_file"
    return
  fi
  docker compose exec -T squid python3 -c 'import sys; sys.path.insert(0, "/opt/helper"); from persistence import STATE_KEY_PROXY_LIST, open_storage; storage = open_storage(); raw = storage.get(STATE_KEY_PROXY_LIST); sys.stdout.write(raw if raw is not None else "[]")' > "$original_proxy_list_file"
}

restore_original() {
  if [ "$RESTORE_ORIGINAL" != "1" ]; then
    return
  fi
  if [ ! -f "$original_proxy_list_file" ]; then
    return
  fi
  if [ -z "$(docker compose ps -q squid 2>/dev/null)" ]; then
    docker compose up -d >/dev/null 2>&1 || true
  fi
  docker compose exec -T squid python3 -c 'import sys; sys.path.insert(0, "/opt/helper"); from persistence import STATE_KEY_PROXY_LIST, open_storage; storage = open_storage(); raw = sys.stdin.read().strip(); storage.set(STATE_KEY_PROXY_LIST, raw if raw else "[]")' < "$original_proxy_list_file" >/dev/null 2>&1 || true
  docker compose up -d --force-recreate squid >/dev/null 2>&1 || true
}

cleanup() {
  rm -f "$generated_direct_file" "$generated_chain_file"
  rm -f "$prepared_direct_file" "$prepared_chain_file"
  restore_original
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

generate_default_direct_file() {
  local -a ports=()
  IFS=',' read -r -a ports <<< "$BENCHMARK_PORTS"

  if [ "${#ports[@]}" -eq 0 ]; then
    echo "BENCHMARK_PORTS is empty"
    exit 1
  fi

  : > "$generated_direct_file"
  local idx=0
  while [ "$idx" -lt "${#ports[@]}" ]; do
    local upstream_port
    upstream_port=$(trim_spaces "${ports[$idx]}")
    printf "%s:%s\n" "$BENCHMARK_UPSTREAM_HOST" "$upstream_port" >> "$generated_direct_file"
    idx=$((idx + 1))
  done
  DIRECT_UPSTREAM_FILE="$generated_direct_file"
}

generate_default_chain_file() {
  local -a ports=()
  local -a host_ports=()
  IFS=',' read -r -a ports <<< "$BENCHMARK_PORTS"
  IFS=',' read -r -a host_ports <<< "$BENCHMARK_HOST_PORTS"

  if [ "${#ports[@]}" -eq 0 ]; then
    echo "BENCHMARK_PORTS is empty"
    exit 1
  fi
  if [ "${#host_ports[@]}" -eq 0 ]; then
    echo "BENCHMARK_HOST_PORTS is empty"
    exit 1
  fi
  if [ "${#ports[@]}" -ne "${#host_ports[@]}" ]; then
    echo "BENCHMARK_PORTS and BENCHMARK_HOST_PORTS must contain the same number of values"
    exit 1
  fi

  : > "$generated_chain_file"
  local idx=0
  while [ "$idx" -lt "${#ports[@]}" ]; do
    local upstream_port
    local host_port
    upstream_port=$(trim_spaces "${ports[$idx]}")
    host_port=$(trim_spaces "${host_ports[$idx]}")
    printf "http://127.0.0.1:%s | %s:%s\n" "$host_port" "$BENCHMARK_UPSTREAM_HOST" "$upstream_port" >> "$generated_chain_file"
    idx=$((idx + 1))
  done
  CHAIN_UPSTREAM_FILE="$generated_chain_file"
}

prepare_benchmark_input() {
  local kind="$1"
  local input_path="$2"
  local absolute_path
  local prepared_path
  if [ -z "$input_path" ]; then
    return 1
  fi

  if [[ "$input_path" = /* ]]; then
    absolute_path="$input_path"
  else
    absolute_path="$ROOT/${input_path#./}"
  fi

  case "$absolute_path" in
    "$ROOT/config/"*)
      printf "%s\n" "$absolute_path"
      return 0
      ;;
  esac

  if [ "$kind" = "direct" ]; then
    prepared_path="$prepared_direct_file"
  else
    prepared_path="$prepared_chain_file"
  fi
  cp "$absolute_path" "$prepared_path"
  printf "%s\n" "$prepared_path"
  return 0
}

container_path_for() {
  local host_path="$1"
  local base
  base=$(basename "$host_path")
  printf "/opt/config/%s" "$base"
}

save_profile_to_storage() {
  local host_file="$1"
  local container_file
  local saved_count
  container_file=$(container_path_for "$host_file")
  saved_count=$(docker compose exec -T squid python3 - "$container_file" "$BENCHMARK_UPSTREAM_SCHEME" "$BENCHMARK_UPSTREAM_USER" "$BENCHMARK_UPSTREAM_PASS" <<'PY'
import sys
sys.path.insert(0, "/opt/helper")
from persistence import open_storage, save_proxy_list
from upstream_pool import build_list_entries

path, default_scheme, default_username, default_password = sys.argv[1:5]
with open(path, "r", encoding="utf-8") as handle:
    entries = build_list_entries(
        handle.read().splitlines(),
        default_scheme,
        default_username,
        default_password,
    )
save_proxy_list(open_storage(), entries)
print(len(entries))
PY
)
  if ! [[ "$saved_count" =~ ^[0-9]+$ ]] || [ "$saved_count" -le 0 ]; then
    echo "failed to load benchmark profile: $host_file"
    exit 1
  fi
}

wait_for_proxy() {
  local attempt=0
  while [ "$attempt" -lt 20 ]; do
    if curl -sS -o /dev/null --max-time 3 --connect-timeout 2 "http://${PROXY_HOST}:${PROXY_PORT}" >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  return 1
}

apply_profile() {
  local host_file="$1"
  save_profile_to_storage "$host_file"
  docker compose up -d --force-recreate >/dev/null
  if ! wait_for_proxy; then
    echo "proxy did not become ready after applying profile: $host_file"
    exit 1
  fi
}

parse_ip() {
  local json_file="$1"
  python3 - "$json_file" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
print(data.get("ip", ""))
PY
}

run_requests() {
  local label="$1"
  local output_file="$2"
  local count_rounds="$3"
  local include_output="$4"
  : > "$output_file"

  local round=1
  while [ "$round" -le "$count_rounds" ]; do
    local user_index=1
    while [ "$user_index" -le "$SAMPLE_USERS" ]; do
      local user="${VERIFY_PREFIX}${user_index}"
      local proxy="http://${user}:${PROXY_PASS}@${PROXY_HOST}:${PROXY_PORT}"
      local body_file="$tmp_dir/${label}_${round}_${user_index}.json"
      local metrics
      if ! metrics=$(curl -sS --max-time 30 --connect-timeout 10 -x "$proxy" "$VERIFY_URL" \
        -o "$body_file" \
        -w '%{http_code}\t%{time_connect}\t%{time_appconnect}\t%{time_starttransfer}\t%{time_total}'); then
        echo "benchmark request failed for ${label} round=${round} user=${user}"
        exit 1
      fi
      local http_code
      http_code=$(printf "%s" "$metrics" | cut -f1)
      if [ "$http_code" != "200" ]; then
        echo "benchmark request failed for ${label} round=${round} user=${user} http_code=${http_code}"
        exit 1
      fi
      local ip
      ip=$(parse_ip "$body_file")
      if [ -z "$ip" ]; then
        echo "benchmark request returned no ip for ${label} round=${round} user=${user}"
        exit 1
      fi
      if [ "$include_output" = "1" ]; then
        printf "%s\t%s\t%s\t%s\t%s\n" "$label" "$round" "$user" "$ip" "$metrics" >> "$output_file"
      fi
      user_index=$((user_index + 1))
    done
    round=$((round + 1))
  done
}

summarize_file() {
  local label="$1"
  local input_file="$2"
  python3 - "$label" "$input_file" <<'PY'
import json
import statistics
import sys

label = sys.argv[1]
path = sys.argv[2]
rows = []
with open(path, "r", encoding="utf-8") as handle:
    for raw in handle:
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("\t")
        rows.append(
            {
                "label": parts[0],
                "round": int(parts[1]),
                "user": parts[2],
                "ip": parts[3],
                "http_code": int(parts[4]),
                "time_connect": float(parts[5]),
                "time_appconnect": float(parts[6]),
                "time_starttransfer": float(parts[7]),
                "time_total": float(parts[8]),
            }
        )

def pct(values, p):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight

time_total = [row["time_total"] for row in rows]
time_connect = [row["time_connect"] for row in rows]
summary = {
    "label": label,
    "count": len(rows),
    "unique_ips": len({row["ip"] for row in rows}),
    "avg_ms": round(statistics.mean(time_total) * 1000, 2),
    "p50_ms": round(pct(time_total, 0.50) * 1000, 2),
    "p95_ms": round(pct(time_total, 0.95) * 1000, 2),
    "min_ms": round(min(time_total) * 1000, 2),
    "max_ms": round(max(time_total) * 1000, 2),
    "connect_avg_ms": round(statistics.mean(time_connect) * 1000, 2),
}
print(json.dumps(summary, ensure_ascii=False))
PY
}

build_summary_report() {
  local direct_json="$1"
  local chain_json="$2"
  python3 - "$direct_json" "$chain_json" <<'PY'
import json
import sys

direct = json.loads(sys.argv[1])
chain = json.loads(sys.argv[2])
delta = {
    "avg_ms": round(chain["avg_ms"] - direct["avg_ms"], 2),
    "p50_ms": round(chain["p50_ms"] - direct["p50_ms"], 2),
    "p95_ms": round(chain["p95_ms"] - direct["p95_ms"], 2),
    "connect_avg_ms": round(chain["connect_avg_ms"] - direct["connect_avg_ms"], 2),
}
report = {"direct": direct, "chain": chain, "delta": delta}
print(json.dumps(report, ensure_ascii=False, indent=2))
PY
}

docker compose build squid >/dev/null
docker compose up -d >/dev/null
backup_current_proxy_list

if [ -z "$DIRECT_UPSTREAM_FILE" ]; then
  generate_default_direct_file
fi
if [ -z "$CHAIN_UPSTREAM_FILE" ]; then
  generate_default_chain_file
fi

DIRECT_UPSTREAM_FILE=$(prepare_benchmark_input direct "$DIRECT_UPSTREAM_FILE")
CHAIN_UPSTREAM_FILE=$(prepare_benchmark_input chain "$CHAIN_UPSTREAM_FILE")

if [ -z "$DIRECT_UPSTREAM_FILE" ] || [ -z "$CHAIN_UPSTREAM_FILE" ]; then
  echo "benchmark input files are required"
  exit 1
fi

apply_profile "$DIRECT_UPSTREAM_FILE"
run_requests "direct" "$tmp_dir/direct_warmup.tsv" "$WARMUP_ROUNDS" "0"
run_requests "direct" "$raw_direct" "$ROUNDS" "1"
direct_summary=$(summarize_file "direct" "$raw_direct")

apply_profile "$CHAIN_UPSTREAM_FILE"
run_requests "chain" "$tmp_dir/chain_warmup.tsv" "$WARMUP_ROUNDS" "0"
run_requests "chain" "$raw_chain" "$ROUNDS" "1"
chain_summary=$(summarize_file "chain" "$raw_chain")

report=$(build_summary_report "$direct_summary" "$chain_summary")
printf "%s\n" "$report"

if [ -n "$RESULT_DIR" ]; then
  mkdir -p "$RESULT_DIR"
  cp "$raw_direct" "$RESULT_DIR/direct.tsv"
  cp "$raw_chain" "$RESULT_DIR/chain.tsv"
  if [ -z "$RESULT_JSON" ]; then
    RESULT_JSON="$RESULT_DIR/report.json"
  fi
fi

if [ -n "$RESULT_JSON" ]; then
  printf "%s\n" "$report" > "$RESULT_JSON"
  echo "saved report -> $RESULT_JSON"
fi

if [ -n "$RESULT_DIR" ]; then
  echo "saved raw -> $RESULT_DIR/direct.tsv"
  echo "saved raw -> $RESULT_DIR/chain.tsv"
fi
