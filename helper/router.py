#!/usr/bin/env python3

import hashlib
import os
import sys

import redis


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


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
        self.port_first = env_int("PORT_FIRST", 10001)
        self.port_last = env_int("PORT_LAST", 10100)
        self.exclusive_fallback = os.getenv("EXCLUSIVE_FALLBACK", "deny").lower()
        self.ttl_seconds = env_int("TTL_SECONDS", 0)
        self.cap_per_port = env_int("CAP_PER_PORT", 2)
        self.redis_host = os.getenv("REDIS_HOST", "redis")
        self.redis_port = env_int("REDIS_PORT", 6379)
        self.debug_log_path = os.getenv("ROUTER_DEBUG_LOG", "")

        self.port_count = self.port_last - self.port_first + 1
        if self.port_count <= 0:
            raise ValueError("PORT_LAST must be >= PORT_FIRST")

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
            "local port = ARGV[4]\n"
            "if redis.call('SISMEMBER', users_key, username) == 1 then\n"
            "  redis.call('SET', bind_key, port)\n"
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
            "redis.call('SET', bind_key, port)\n"
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
        return int.from_bytes(digest[:4], "big") % self.port_count

    def _shared_port(self, username):
        return self.port_first + self._hash_idx(username)

    def _iter_ports(self, username):
        start = self._hash_idx(username)
        for i in range(self.port_count):
            yield self.port_first + ((start + i) % self.port_count)

    def _exclusive_get_bound(self, username):
        bind_key = f"bind:user:{username}"
        port = self.redis.get(bind_key)
        if not port:
            return None
        owner = self.redis.get(f"port:{port}")
        if owner == username:
            return int(port)
        if owner is None:
            self.redis.delete(bind_key)
        return None

    def _exclusive_port(self, username):
        bound = self._exclusive_get_bound(username)
        if bound is not None:
            return bound

        ttl = self.ttl_seconds if self.ttl_seconds > 0 else None
        bind_key = f"bind:user:{username}"
        for port in self._iter_ports(username):
            port_key = f"port:{port}"
            if self.redis.set(port_key, username, nx=True, ex=ttl):
                if ttl is None:
                    self.redis.set(bind_key, port)
                else:
                    self.redis.set(bind_key, port, ex=ttl)
                return port
        return None

    def _shared_capped_port(self, username):
        if self.cap_per_port <= 0:
            return None

        bind_key = f"bind:user:{username}"
        bound = self.redis.get(bind_key)
        if bound:
            users_key = f"port:{bound}:users"
            if self.redis.sismember(users_key, username):
                return int(bound)
            self.redis.delete(bind_key)

        ttl = self.ttl_seconds if self.ttl_seconds > 0 else 0
        for port in self._iter_ports(username):
            users_key = f"port:{port}:users"
            res = self.redis.eval(
                self._shared_capped_lua,
                2,
                users_key,
                bind_key,
                username,
                str(self.cap_per_port),
                str(ttl),
                str(port),
            )
            if res in (1, 2):
                return port
        return None

    def route(self, username):
        if self.mode == "shared":
            return self._shared_port(username)
        if self.mode == "exclusive":
            return self._exclusive_port(username)
        if self.mode == "shared_capped":
            return self._shared_capped_port(username)
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
        expected_port = parts[1] if len(parts) > 1 else None
        if expected_port in ("", "-", "unknown"):
            expected_port = None

        if not username:
            response = "ERR message=missing_user"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        try:
            port = router.route(username)
        except Exception as exc:
            response = f"ERR message=route_failed:{exc}"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        if port is None:
            if router.mode == "exclusive" and router.exclusive_fallback == "shared":
                port = router._shared_port(username)
            else:
                response = "ERR message=no_port"
                router._log(f"out: {response}")
                print(response, flush=True)
                continue

        if expected_port is not None and str(port) != expected_port:
            response = "ERR message=not_match"
            router._log(f"out: {response}")
            print(response, flush=True)
            continue

        response = f"OK tag=peer_{port} message={port}"
        router._log(f"out: {response}")
        print(response, flush=True)


if __name__ == "__main__":
    main()
