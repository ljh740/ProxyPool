# ProxyPool

[中文说明](README.zh-CN.md) | [English](README.md)

A self-hosted proxy management and routing service that gives your apps one stable local entrypoint in front of many upstream proxies.

## What This Project Does
- manages upstream proxies through Web Admin instead of hand-editing runtime files
- exposes one main local proxy entry on `3128` for browsers, scripts, and services
- keeps outbound sessions stable by username while still allowing direct entry binding and random-pool routing
- supports `http`, `socks5`, and `socks5h` upstreams, including multi-hop proxy chains
- provides fixed compatibility ports for tools that cannot send proxy credentials

## Typical Use Cases
- browser automation profiles that need stable outbound identity
- local tools or services that should use one proxy address instead of tracking many upstream credentials
- teams that want a web-managed proxy pool with sticky sessions and explicit fallback access paths

## Core Capabilities
- username-based sticky routing: the same username stays on the same upstream entry
- main authenticated listener on `3128` accepts inbound `http`, `socks5`, and `socks5h`
- random pool override via `RANDOM_POOL_PREFIX`
- proxy-list import adds pasted entries as manual proxies
- import can optionally run a live connectivity check and save only the valid entries after confirmation
- manual add/edit supports prepend-hop chains
- batch generation supports direct hops and chained hops
- compatibility port mappings can point to an exact `entry_key` or a sticky `session_name`

## Quick Start
1. Copy environment template and edit it:
   - `cp .env.example .env`
2. Build and start:
   - `docker compose up --build`
3. Open Web Admin:
   - `http://localhost:8077`
4. Complete first-boot setup:
   - Create the admin password on `/setup`
   - `AUTH_PASSWORD` and `SALT` are generated automatically on first boot
5. Add proxies in Web Admin:
   - `Proxies` → `Import List`, `Add Proxy`, or `Batch Generate`
6. Optional: configure compatibility ports:
   - `Compat Ports` → map one of `33100-33199` to an `entry_key` or `session_name`

## Bootstrap Configuration
Only these values are read directly from the container environment at startup:
- `STATE_DB_PATH`
- `WEB_PORT`

All runtime proxy settings, the admin password, and the proxy list are persisted in the SQLite admin state file.
If an older `.env` or `docker compose` override still contains `ADMIN_PASSWORD`, it is ignored by the current bootstrap flow.

## Runtime Configuration
Configure these in Web Admin `Config Center`:
- `PROXY_HOST`, `PROXY_PORT`
- `AUTH_PASSWORD`, `AUTH_REALM`
- `UPSTREAM_CONNECT_TIMEOUT`, `UPSTREAM_CONNECT_RETRIES`
- `RELAY_TIMEOUT`
- `REWRITE_LOOPBACK_TO_HOST` = auto | always | off
- `HOST_LOOPBACK_ADDRESS`
- `LOG_LEVEL`
- `SALT`
- `RANDOM_POOL_PREFIX`
- `ROUTER_DEBUG_LOG`

Notes:
- `AUTH_PASSWORD` is the shared password used by the main authenticated proxy listener.
- `AUTH_REALM` is the HTTP Basic proxy realm returned in `407 Proxy Authentication Required`. It only affects the client auth prompt and credential cache scope. It does not affect routing and does not participate in password verification.

## Admin Setup And Reset
- Web Admin always starts, even before the admin password is configured.
- On first boot, `/setup` is the only allowed management entrypoint. Complete it once to create the admin password.
- The admin password is stored in SQLite state. It is no longer provisioned from `ADMIN_PASSWORD` environment variables.
- If you forget the admin password, clear it and return to setup mode:
  - Local run: `python3 scripts/reset_admin_password.py`
  - Custom DB path: `python3 scripts/reset_admin_password.py --state-db-path /path/to/proxypool.sqlite3`
  - Docker example: `docker compose exec squid python3 /opt/scripts/reset_admin_password.py`

## Usage
Set client proxy to the main local entry proxy on `3128` (password is required):
- `HTTP_PROXY=http://userA:YOUR_PASSWORD@localhost:3128`
- `HTTPS_PROXY=http://userA:YOUR_PASSWORD@localhost:3128`
- `ALL_PROXY=socks5://userA:YOUR_PASSWORD@localhost:3128`
- `ALL_PROXY=socks5h://userA:YOUR_PASSWORD@localhost:3128`

On the main authenticated port:
- The password must match the current `AUTH_PASSWORD`.
- The username is the routing key.
- If the username equals an `entry_key`, the request is pinned to that exact upstream entry.
- If the username starts with `RANDOM_POOL_PREFIX`, the request is routed through the random pool.
- Otherwise, the username is hashed through shared routing, so the same username stays on the same upstream entry.
- `socks5h` uses the same SOCKS5 listener and keeps destination hostname resolution on the proxy side when the client sends a domain target.

For clients that cannot send proxy credentials, use a compatibility port instead:
- Example exact entry binding: `http://127.0.0.1:33100`
- Example sticky session binding: map `33101` to `chrome-profile-a`, then use `http://127.0.0.1:33101`

Compatibility ports are no-auth HTTP listeners managed in Web Admin.
They use a fixed Docker-published range because Docker cannot expose new host ports dynamically after the container starts.

## Managing Proxies
Use Web Admin `Proxies` page as the only runtime management entrypoint.
The current Web Admin supports:
- `Import List`: paste line-based proxy definitions, optionally run a live connectivity check, then save valid entries as manual proxies
- `Add Proxy`: create or edit one manual proxy entry
- `Batch Generate`: generate auto proxies from one host plus a port range

## Compatibility Ports
Use Web Admin `Compat Ports` to manage the pre-published range `127.0.0.1:33100-33199`.

Each mapping supports:
- `entry_key`: always route that local port to one exact upstream entry
- `session_name`: treat that value as a stable session alias and hash it through shared routing

Notes:
- The main authenticated proxy on `3128` remains unchanged.
- Compatibility listeners are separate no-auth ports intended for tools such as `undetected-chromedriver`.
- Compatibility listeners remain HTTP-only; inbound SOCKS5 support is only enabled on the main authenticated listener.
- If an `entry_key` mapping points to an entry that is later removed, requests on that compatibility port will fail until you update the mapping.
- `session_name` is not a unique per-entry access key. The unique direct-access identifier is `entry_key`.

### Manual Add/Edit
You can add a single proxy entry with:
- scheme
- host
- port
- username / password
- optional prepend hop chain
- pool inclusion toggle

### Batch Generate
Batch generation creates many entries from one host + port range.
It also supports:
- optional prepend hop applied to every generated entry
- optional cycling first hop for chained relay scenarios

### Supported Line Formats
When importing a proxy list or configuring chain-style hop values for batch generation, supported hop formats are:
- `socks5://user:pass@host:port`
- `http://user:pass@host:port`
- `host:port`
- `host:port:user:pass`
- `scheme,host,port,user,pass`

For chained lines, join hops with ` | `, for example:
- `http://127.0.0.1:30001 | socks5://user:pass@dc.decodo.com:10001`

Keep spaces around `|` so passwords containing `|` stay valid.

## Validation
- Logic-only: `./scripts/verify.sh`
- Real request via ipinfo: `./scripts/verify_real.sh`
  - Uses `https://ipinfo.io/json` to check sticky behavior and IP distribution

## Utilities
- Generate a large line-based list file:
  - `python3 scripts/generate_upstream_list.py --host proxy.example.com --port-first 10001 --port-last 10100 --output config/upstreams.txt`
- Generate chained output:
  - `python3 scripts/generate_upstream_list.py --host proxy.example.com --port-first 10001 --port-last 10100 --cycle-first-hop http://127.0.0.1:30001 --cycle-first-hop http://127.0.0.1:30002 --output config/upstreams.txt`
- Latency benchmark: `./scripts/benchmark_chain_latency.sh`
  - Compares direct vs chained profiles using explicit proxy configurations stored in project state

## Notes
- `.env` contains credentials and should stay local.
- In `.env`, write the actual password characters directly. If your shell command used `\~`, that usually means the real password character is `~`.
- The proxy retries transient upstream connect/handshake failures a few times before returning `502`.
- Inside Docker, only the first chain hop rewrites `127.0.0.1` / `localhost` / `::1` to `host.docker.internal` by default.
