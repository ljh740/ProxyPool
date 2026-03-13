#!/usr/bin/env python3

import hashlib
import os
import sys

import redis

from upstream_pool import env_int, load_upstream_pool_from_env


def normalize_username(raw):
    if not raw:
        return ""
    if raw in ("-", "unknown"):
        return ""
    return raw


class Router:
    def __init__(self):
        self.mode = os.getenv("MODE", "shared").lower()
        self.salt = os.getenv("SALT", "change-me")
        self.exclusive_fallback = os.getenv("EXCLUSIVE_FALLBACK", "deny").lower()
        self.ttl_seconds = env_int("TTL_SECONDS", 0)
        self.cap_per_port = env_int("CAP_PER_PORT", 2)
        self.redis_host = os.getenv("REDIS_HOST", "redis")
        self.redis_port = env_int("REDIS_PORT", 6379)
        self.debug_log_path = os.getenv("ROUTER_DEBUG_LOG", "")

        self.upstream_pool = load_upstream_pool_from_env()
        self.upstream_count = self.upstream_pool.count

        self.redis = None
        if self.mode in ("exclusive", "shared_capped"):
            self.redis = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                decode_responses=True,
            )

        self._shared_capped_lua = (
            "local users_key = KEYS[1]\n"
            "local bind_key = KEYS[2]\n"
            "local username = ARGV[1]\n"
            "local cap = tonumber(ARGV[2])\n"
            "local ttl = tonumber(ARGV[3])\n"
            "local entry_key = ARGV[4]\n"
            "if redis.call('SISMEMBER', users_key, username) == 1 then\n"
            "  redis.call('SET', bind_key, entry_key)\n"
            "  if ttl > 0 then\n"
            "    redis.call('EXPIRE', users_key, ttl)\n"
            "    redis.call('EXPIRE', bind_key, ttl)\n"
            "  end\n"
            "  return 2\n"
            "end\n"
            "local count = redis.call('SCARD', users_key)\n"
            "if count >= cap then\n"
            "  return 0\n"
            "end\n"
            "redis.call('SADD', users_key, username)\n"
            "redis.call('SET', bind_key, entry_key)\n"
            "if ttl > 0 then\n"
            "  redis.call('EXPIRE', users_key, ttl)\n"
            "  redis.call('EXPIRE', bind_key, ttl)\n"
            "end\n"
            "return 1\n"
        )

    def _log(self, message):
        if not self.debug_log_path:
            return
        try:
            with open(self.debug_log_path, "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except Exception:
            pass

    def _hash_idx(self, username):
        digest = hashlib.sha256((self.salt + username).encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self.upstream_count

    def _shared_key(self, username):
        return self.upstream_pool.entries[self._hash_idx(username)].key

    def _iter_keys(self, username):
        start = self._hash_idx(username)
        for i in range(self.upstream_count):
            yield self.upstream_pool.entries[(start + i) % self.upstream_count].key

    def get_entry(self, entry_key):
        if entry_key is None:
            return None
        return self.upstream_pool.get(str(entry_key))

    def route_entry(self, username):
        return self.get_entry(self.route(username))

    def _exclusive_get_bound(self, username):
        bind_key = f"bind:user:{username}"
        entry_key = self.redis.get(bind_key)
        if not entry_key:
            return None
        owner = self.redis.get(f"entry:{entry_key}")
        if owner == username:
            return entry_key
        if owner is None:
            self.redis.delete(bind_key)
        return None

    def _exclusive_key(self, username):
        bound = self._exclusive_get_bound(username)
        if bound is not None:
            return bound

        ttl = self.ttl_seconds if self.ttl_seconds > 0 else None
        bind_key = f"bind:user:{username}"
        for entry_key in self._iter_keys(username):
            owner_key = f"entry:{entry_key}"
            if self.redis.set(owner_key, username, nx=True, ex=ttl):
                if ttl is None:
                    self.redis.set(bind_key, entry_key)
                else:
                    self.redis.set(bind_key, entry_key, ex=ttl)
                return entry_key
        return None

    def _shared_capped_key(self, username):
        if self.cap_per_port <= 0:
            return None

        bind_key = f"bind:user:{username}"
        bound = self.redis.get(bind_key)
        if bound:
            users_key = f"entry:{bound}:users"
            if self.redis.sismember(users_key, username):
                return bound
            self.redis.delete(bind_key)

        ttl = self.ttl_seconds if self.ttl_seconds > 0 else 0
        for entry_key in self._iter_keys(username):
            users_key = f"entry:{entry_key}:users"
            result = self.redis.eval(
                self._shared_capped_lua,
                2,
                users_key,
                bind_key,
                username,
                str(self.cap_per_port),
                str(ttl),
                entry_key,
            )
            if result in (1, 2):
                return entry_key
        return None

    def route(self, username):
        if self.mode == "shared":
            return self._shared_key(username)
        if self.mode == "exclusive":
            entry_key = self._exclusive_key(username)
            if entry_key is None and self.exclusive_fallback == "shared":
                return self._shared_key(username)
            return entry_key
        if self.mode == "shared_capped":
            return self._shared_capped_key(username)
        return None


def main():
    try:
        router = Router()
    except Exception as exc:
        print(f"ERR message=init_failed:{exc}")
        return

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        router._log(f"in: {line}")

        parts = line.split()
        username = normalize_username(parts[0]) if parts else ""
        expected_key = parts[1] if len(parts) > 1 else None
        if expected_key in ("", "-", "unknown"):
            expected_key = None

        if not username:
            response = "ERR message=missing_user"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        try:
            entry_key = router.route(username)
        except Exception as exc:
            response = f"ERR message=route_failed:{exc}"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        if entry_key is None:
            response = "ERR message=no_port"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        if expected_key is not None and entry_key != expected_key:
            response = "ERR message=not_match"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        entry = router.get_entry(entry_key)
        if entry is None:
            response = "ERR message=missing_entry"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        response = f"OK tag={entry.key} label={entry.label} message={entry.key}"
        router._log(f"out: {response}")
        print(response, flush=True)


if __name__ == "__main__":
    main()
