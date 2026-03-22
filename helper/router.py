#!/usr/bin/env python3

import hashlib
import os
import random
import sys
from typing import Mapping

from config_center import AppConfig, RUNTIME_CONFIG_FIELDS
from persistence import open_storage
from upstream_pool import UpstreamPool


def normalize_username(raw):
    if not raw:
        return ""
    if raw in ("-", "unknown"):
        return ""
    return raw


def _coerce_app_config(config: AppConfig | Mapping[str, object]) -> AppConfig:
    if isinstance(config, AppConfig):
        return config
    return AppConfig.from_mapping(config)


class Router:
    def __init__(
        self,
        config: AppConfig | Mapping[str, object] | None = None,
        upstream_pool: UpstreamPool | None = None,
    ):
        """Initialise the router from the centralized runtime config."""
        if upstream_pool is None:
            raise ValueError("upstream_pool is required")
        app_config = AppConfig.load() if config is None else _coerce_app_config(config)
        self.salt = app_config.salt
        self.debug_log_path = app_config.router_debug_log
        self.upstream_pool = upstream_pool
        self.upstream_count = self.upstream_pool.count
        self.storage = None
        self.random_pool_prefix = app_config.random_pool_prefix
        self._random_pool_keys = [
            e.key for e in self.upstream_pool.entries if e.in_random_pool
        ]

    @classmethod
    def from_config(cls, config_dict, proxy_entries):
        """Build a Router from explicit config values and entry list."""
        pool = UpstreamPool(source="admin", entries=list(proxy_entries))
        return cls(config=config_dict, upstream_pool=pool)

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

    def get_entry(self, entry_key):
        if entry_key is None:
            return None
        return self.upstream_pool.get(str(entry_key))

    def route_entry(self, username):
        return self.get_entry(self.route(username))

    def route(self, username):
        if self.upstream_count == 0:
            return None
        if self.upstream_pool.get(username) is not None:
            return username
        if self.random_pool_prefix and username.startswith(self.random_pool_prefix):
            if not self._random_pool_keys:
                return None
            return random.choice(self._random_pool_keys)
        return self._shared_key(username)


def main():
    try:
        bootstrap_config = AppConfig.from_bootstrap_env()
        storage = open_storage(bootstrap_config.state_db_path)
        runtime_config = AppConfig.load(storage=storage)
        override_values = runtime_config.runtime_values()
        for field in RUNTIME_CONFIG_FIELDS:
            override = os.environ.get(field.env_key)
            if override not in (None, ""):
                override_values[field.env_key] = override
        runtime_config = AppConfig.from_mapping(override_values)
        from persistence import load_proxy_list

        entries = load_proxy_list(storage)
        router = Router(
            config=runtime_config,
            upstream_pool=UpstreamPool(source="admin", entries=list(entries)),
        )
        router.storage = storage
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
