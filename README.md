# ProxyPool

Sticky upstream proxy router with switchable routing modes.

## Overview
- Entry proxy: Python HTTP proxy (Docker)
- Client auth + routing decision: Python helper
- State (exclusive/shared_capped): Redis
- Upstream proxies: either generated from one host + port range, or imported as a line-based list
- Upstream chains: supports single-hop or multi-hop proxy chains
- Supported upstream schemes: `http`, `socks5`, `socks5h`

## Features
- shared: any username is accepted; same username always maps to the same upstream port
- exclusive: each username gets a dedicated upstream port (optionally fallback to shared)
- shared_capped: shared with a per-port user cap

## Quick Start
1. Copy environment template and edit it:
   - `cp .env.example .env`
2. Build and start:
   - `docker compose up --build`

## Usage
Set client proxy to the local entry proxy (password is required):
- `HTTP_PROXY=http://userA:YOUR_PASSWORD@localhost:3128`
- `HTTPS_PROXY=http://userA:YOUR_PASSWORD@localhost:3128`

## Validation
- Logic-only: `./scripts/verify.sh`
- Real request via ipinfo: `./scripts/verify_real.sh`
  - Uses `https://ipinfo.io/json` to check sticky behavior and IP distribution
- Latency benchmark: `./scripts/benchmark_chain_latency.sh`
  - Compares single-hop vs chained proxy latency and can save rerun artifacts with `RESULT_DIR=...`
  - Auto-generated chain benchmarks assume host relay proxies already exist on `127.0.0.1:30001..30005`, or you can pass explicit `DIRECT_UPSTREAM_FILE` / `CHAIN_UPSTREAM_FILE`

## Configuration
Edit `.env` for your setup:
- `UPSTREAM_SCHEME` = http | socks5 | socks5h
- `UPSTREAM_HOST`, `UP_USER`, `UP_PASS` for range-generated upstreams
- `UPSTREAM_LIST_FILE` for line-based upstream imports
- `UPSTREAM_LIST` for newline-separated inline imports from shell / CI
- `UPSTREAM_CONNECT_RETRIES` for transient upstream handshake retries
- `REWRITE_LOOPBACK_TO_HOST` = auto | always | off
- `HOST_LOOPBACK_ADDRESS` for first-hop loopback rewrite inside Docker
- `AUTH_PASSWORD` (fixed password for all users)
- `PORT_FIRST`, `PORT_LAST`
- `MODE` = shared | exclusive | shared_capped
- `SALT` (keep stable to preserve stickiness)
- `EXCLUSIVE_FALLBACK` = deny | shared
- `TTL_SECONDS` (0 = permanent)
- `CAP_PER_PORT`
- `REDIS_HOST`, `REDIS_PORT`

## Notes
- `.env` contains credentials and should stay local.
- In `.env`, write the actual password characters directly. If your shell command used `\~`, that usually means the real password character is `~`.

## Importing 1000+ Upstreams
- **Range mode**: keep `UPSTREAM_LIST_FILE` empty and set `UPSTREAM_HOST`, `PORT_FIRST`, `PORT_LAST`, and shared credentials.
- **File mode**: put one upstream per line into `./config/upstreams.txt`, set `UPSTREAM_LIST_FILE=/opt/config/upstreams.txt`, then restart `squid`.
- **Inline text mode**: export `UPSTREAM_LIST` as newline-separated text before `docker compose up`; useful for quick imports, but file mode is more practical for 1000+ entries.
- **Chain mode**: join hops with ` | `, for example `http://127.0.0.1:30001 | socks5://user:pass@dc.decodo.com:10001`. Keep spaces around `|` so passwords containing `|` stay valid.
- Supported line formats:
  - `socks5://user:pass@host:port`
  - `http://user:pass@host:port`
  - `host:port`
  - `host:port:user:pass`
  - `scheme,host,port,user,pass`
- For chained lines, default `UP_USER` / `UP_PASS` are applied to the last hop only.
- If a line omits credentials, the default `UP_USER` / `UP_PASS` values are used.
- To generate a large list from a host + port range, run `python3 scripts/generate_upstream_list.py --output config/upstreams.txt`.
- To import raw text directly, use `export UPSTREAM_LIST="$(cat config/upstreams.txt)"`.
- To generate chained output, use `python3 scripts/generate_upstream_list.py --cycle-first-hop http://127.0.0.1:30001 --cycle-first-hop http://127.0.0.1:30002 --output config/upstreams.txt`.
- The proxy retries transient upstream connect/handshake failures a few times before returning `502`.
- Inside Docker, only the first chain hop rewrites `127.0.0.1` / `localhost` / `::1` to `host.docker.internal` by default.
