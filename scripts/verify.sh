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

tmp_dir=$(mktemp -d)
state_snapshot_file="$tmp_dir/proxy_list_snapshot.json"
cleanup() {
  local exit_code=$?
  trap - EXIT
  if [ -f "$state_snapshot_file" ]; then
    if ! docker compose exec -T squid python3 -c '
import json
import sys
sys.path.insert(0, "/opt/helper")
from persistence import STATE_KEY_PROXY_LIST, open_storage

snapshot = json.load(sys.stdin)
storage = open_storage()
if snapshot.get("present"):
    storage.set(STATE_KEY_PROXY_LIST, snapshot["raw"])
else:
    storage.delete(STATE_KEY_PROXY_LIST)
' < "$state_snapshot_file"; then
      echo "failed to restore proxy list snapshot" >&2
      exit_code=1
    fi
  fi
  rm -rf "$tmp_dir"
  exit "$exit_code"
}
trap cleanup EXIT

backup_current_proxy_list() {
  docker compose exec -T squid python3 - <<'PY' > "$state_snapshot_file"
import json
import sys
sys.path.insert(0, "/opt/helper")
from persistence import STATE_KEY_PROXY_LIST, open_storage

storage = open_storage()
raw = storage.get(STATE_KEY_PROXY_LIST)
print(json.dumps({
    "present": raw is not None,
    "raw": raw or "",
}))
PY
}

seed_sample_proxy_list() {
  docker compose exec -T squid python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/helper")
from persistence import open_storage, save_proxy_list
from upstream_pool import UpstreamEntry, UpstreamHop, compute_entry_key

entries = []
for offset in range(5):
    port = 10001 + offset
    hop = UpstreamHop(
        scheme="http",
        host=f"verify-{offset + 1}.example.com",
        port=port,
        username="",
        password="",
    )
    hops = (hop,)
    entries.append(
        UpstreamEntry(
            key=compute_entry_key(hops),
            label=f"{hop.host}:{hop.port}",
            hops=hops,
            source_tag="verify",
            in_random_pool=True,
        )
    )

save_proxy_list(open_storage(), entries)
print(len(entries))
PY
}

read_runtime_json() {
  docker compose exec -T squid python3 - <<'PY'
import json
import sys
sys.path.insert(0, "/opt/helper")
from persistence import load_proxy_list, open_storage

storage = open_storage()
entries = load_proxy_list(storage)
print(json.dumps({
    "routing": "shared",
    "upstream_source": "admin",
    "upstream_count": len(entries),
}))
PY
}

RUNTIME_JSON=$(read_runtime_json)
ROUTING=$(printf "%s" "$RUNTIME_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["routing"])')
UPSTREAM_SOURCE=$(printf "%s" "$RUNTIME_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upstream_source"])')
UPSTREAM_COUNT=$(printf "%s" "$RUNTIME_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upstream_count"])')

if ! [[ "$UPSTREAM_COUNT" =~ ^[0-9]+$ ]] || [ "$UPSTREAM_COUNT" -le 0 ]; then
  backup_current_proxy_list
  seeded_count=$(seed_sample_proxy_list)
  echo "seeded temporary upstreams: $seeded_count"
  RUNTIME_JSON=$(read_runtime_json)
  ROUTING=$(printf "%s" "$RUNTIME_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["routing"])')
  UPSTREAM_SOURCE=$(printf "%s" "$RUNTIME_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upstream_source"])')
  UPSTREAM_COUNT=$(printf "%s" "$RUNTIME_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upstream_count"])')
  if ! [[ "$UPSTREAM_COUNT" =~ ^[0-9]+$ ]] || [ "$UPSTREAM_COUNT" -le 0 ]; then
    echo "invalid upstream count after seeding: $UPSTREAM_COUNT"
    exit 1
  fi
fi

SAMPLE_USERS=${SAMPLE_USERS:-20}
if [ "$SAMPLE_USERS" -lt 2 ]; then
  SAMPLE_USERS=2
fi

VERIFY_PREFIX=${VERIFY_PREFIX:-verify_user}

users_file="$tmp_dir/users.txt"
ports1="$tmp_dir/ports1.txt"
ports2="$tmp_dir/ports2.txt"

i=1
while [ "$i" -le "$SAMPLE_USERS" ]; do
  printf "%s\n" "${VERIFY_PREFIX}${i}" >> "$users_file"
  i=$((i + 1))
done

exec_router() {
  docker compose exec -T squid python3 /opt/helper/router.py
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

unique=$(sort -u "$ports1" | wc -l | tr -d ' ')
echo "routing=$ROUTING sample=$SAMPLE_USERS unique_targets=$unique source=$UPSTREAM_SOURCE"

echo "ok"
