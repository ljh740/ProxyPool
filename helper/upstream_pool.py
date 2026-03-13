#!/usr/bin/env python3

import csv
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List
from urllib.parse import unquote, urlsplit

SUPPORTED_UPSTREAM_SCHEMES = {"http", "socks5", "socks5h"}


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class UpstreamEntry:
    key: str
    label: str
    scheme: str
    host: str
    port: int
    username: str
    password: str


@dataclass(frozen=True)
class UpstreamPool:
    source: str
    entries: List[UpstreamEntry]
    entry_map: Dict[str, UpstreamEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not self.entries:
            raise ValueError("No upstream entries configured")
        keys = [entry.key for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("Upstream entry keys must be unique")
        object.__setattr__(self, "entry_map", {entry.key: entry for entry in self.entries})

    @property
    def count(self):
        return len(self.entries)

    def get(self, key):
        return self.entry_map.get(key)


def ensure_supported_scheme(scheme):
    normalized = (scheme or "http").lower()
    if normalized not in SUPPORTED_UPSTREAM_SCHEMES:
        raise ValueError(
            f"Unsupported upstream scheme '{scheme}'. Expected one of {', '.join(sorted(SUPPORTED_UPSTREAM_SCHEMES))}."
        )
    return normalized


def build_range_pool():
    scheme = ensure_supported_scheme(os.getenv("UPSTREAM_SCHEME", "http"))
    host = os.getenv("UPSTREAM_HOST", "")
    username = os.getenv("UP_USER", "")
    password = os.getenv("UP_PASS", "")
    port_first = env_int("PORT_FIRST", 10001)
    port_last = env_int("PORT_LAST", 10100)

    if not host:
        raise ValueError("UPSTREAM_HOST must be configured for range mode")
    if port_last < port_first:
        raise ValueError("PORT_LAST must be >= PORT_FIRST")

    entries = []
    for port in range(port_first, port_last + 1):
        key = str(port)
        entries.append(
            UpstreamEntry(
                key=key,
                label=key,
                scheme=scheme,
                host=host,
                port=port,
                username=username,
                password=password,
            )
        )
    return UpstreamPool(source="range", entries=entries)


def parse_csv_line(line, default_scheme, default_username, default_password):
    parts = next(csv.reader([line], skipinitialspace=True))
    if len(parts) == 2:
        host, port = parts
        return default_scheme, host, port, default_username, default_password
    if len(parts) == 4:
        host, port, username, password = parts
        return default_scheme, host, port, username, password
    if len(parts) == 5:
        scheme, host, port, username, password = parts
        return scheme, host, port, username, password
    raise ValueError("Expected 2, 4, or 5 comma-separated fields")


def parse_colon_line(line, default_scheme, default_username, default_password):
    parts = line.split(":")
    if len(parts) == 2:
        host, port = parts
        return default_scheme, host, port, default_username, default_password
    if len(parts) >= 4:
        host = parts[0]
        port = parts[1]
        username = parts[2]
        password = ":".join(parts[3:])
        return default_scheme, host, port, username, password
    raise ValueError("Expected host:port or host:port:user:pass")


def parse_upstream_line(line, index, default_scheme, default_username, default_password):
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    if "://" in raw:
        parsed = urlsplit(raw)
        scheme = ensure_supported_scheme(parsed.scheme)
        if not parsed.hostname:
            raise ValueError("Missing host in upstream URL")
        if parsed.port is None:
            raise ValueError("Missing port in upstream URL")
        username = unquote(parsed.username or default_username)
        password = unquote(parsed.password or default_password)
        host = parsed.hostname
        port = parsed.port
    else:
        if "," in raw:
            scheme, host, port, username, password = parse_csv_line(
                raw,
                default_scheme,
                default_username,
                default_password,
            )
        else:
            scheme, host, port, username, password = parse_colon_line(
                raw,
                default_scheme,
                default_username,
                default_password,
            )
        scheme = ensure_supported_scheme(scheme)
        try:
            port = int(port)
        except ValueError as exc:
            raise ValueError(f"Invalid port '{port}'") from exc

    if not host:
        raise ValueError("Missing host")
    if port <= 0 or port > 65535:
        raise ValueError(f"Invalid port '{port}'")

    entry_number = index + 1
    return UpstreamEntry(
        key=f"upstream_{entry_number}",
        label=f"{host}:{port}#{entry_number}",
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def build_list_pool(lines: Iterable[str], source, default_scheme, default_username, default_password):
    entries = []
    for line in lines:
        entry = parse_upstream_line(line, len(entries), default_scheme, default_username, default_password)
        if entry is not None:
            entries.append(entry)
    return UpstreamPool(source=source, entries=entries)


def load_upstream_pool_from_env():
    default_scheme = ensure_supported_scheme(os.getenv("UPSTREAM_SCHEME", "http"))
    default_username = os.getenv("UP_USER", "")
    default_password = os.getenv("UP_PASS", "")
    list_file = os.getenv("UPSTREAM_LIST_FILE", "").strip()
    inline_list = os.getenv("UPSTREAM_LIST", "")

    if list_file:
        with open(list_file, "r", encoding="utf-8") as handle:
            return build_list_pool(
                handle.readlines(),
                "file",
                default_scheme,
                default_username,
                default_password,
            )

    if inline_list.strip():
        return build_list_pool(
            inline_list.splitlines(),
            "inline",
            default_scheme,
            default_username,
            default_password,
        )

    return build_range_pool()


def print_usage():
    print("Usage: upstream_pool.py [count|source|list]", file=sys.stderr)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "count"
    pool = load_upstream_pool_from_env()

    if command == "count":
        print(pool.count)
        return
    if command == "source":
        print(pool.source)
        return
    if command == "list":
        for entry in pool.entries:
            print(
                f"{entry.key}\t{entry.label}\t{entry.scheme}://{entry.host}:{entry.port}"
            )
        return

    print_usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
