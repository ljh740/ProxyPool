# ProxyPool

Sticky upstream proxy router with switchable routing modes.

## Overview
- Entry proxy: Python HTTP proxy (Docker)
- Client auth + routing decision: Python helper
- State (exclusive/shared_capped): Redis
- Upstream proxies: same host, port range (default 10001-10100), shared credentials
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

## Configuration
Edit `.env` for your setup:
- `UPSTREAM_SCHEME` = http | socks5 | socks5h
- `UPSTREAM_HOST`, `UP_USER`, `UP_PASS`
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
