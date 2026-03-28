#!/usr/bin/env python3

import csv
import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple
from urllib.parse import quote, unquote, urlsplit

SUPPORTED_UPSTREAM_SCHEMES = {"http", "socks5", "socks5h"}
CHAIN_SEPARATOR_PATTERN = re.compile(r"\s+\|\s+")


def _normalize_entry_tags(tags):
    if not tags:
        return {}
    normalized = {}
    for key, value in dict(tags).items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(value).strip()
        if not normalized_key or not normalized_value:
            continue
        normalized[normalized_key] = normalized_value
    return normalized


@dataclass(frozen=True)
class UpstreamHop:
    scheme: str
    host: str
    port: int
    username: str
    password: str

    @property
    def display(self):
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class UpstreamEntry:
    key: str
    label: str
    hops: Tuple[UpstreamHop, ...]
    source_tag: str = "manual"
    in_random_pool: bool = True
    tags: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.hops:
            raise ValueError("Upstream entry must contain at least one hop")
        object.__setattr__(self, "tags", _normalize_entry_tags(self.tags))

    @property
    def chain_length(self):
        return len(self.hops)

    @property
    def first_hop(self):
        return self.hops[0]

    @property
    def last_hop(self):
        return self.hops[-1]

    @property
    def display(self):
        return " -> ".join(hop.display for hop in self.hops)


@dataclass(frozen=True)
class UpstreamPool:
    source: str
    entries: List[UpstreamEntry]
    entry_map: Dict[str, UpstreamEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        keys = [entry.key for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("Upstream entry keys must be unique")
        object.__setattr__(
            self, "entry_map", {entry.key: entry for entry in self.entries}
        )

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


def compute_entry_key(hops):
    """Compute a stable md5-based key from the full hop chain."""
    canonical_parts = []
    for hop in hops:
        auth = ""
        if hop.username or hop.password:
            auth = "%s:%s@" % (
                quote(hop.username, safe=""),
                quote(hop.password, safe=""),
            )
        canonical_parts.append(
            "%s://%s%s:%s" % (hop.scheme, auth, hop.host, hop.port)
        )
    canonical = "|".join(canonical_parts)
    return hashlib.md5(canonical.encode()).hexdigest()[:12]


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


def parse_upstream_hop(raw, default_scheme, default_username, default_password):
    if "://" in raw:
        parsed = urlsplit(raw)
        scheme = ensure_supported_scheme(parsed.scheme)
        if not parsed.hostname:
            raise ValueError("Missing host in upstream URL")
        if parsed.port is None:
            raise ValueError("Missing port in upstream URL")
        username = (
            default_username
            if parsed.username in (None, "")
            else unquote(parsed.username)
        )
        password = (
            default_password
            if parsed.password in (None, "")
            else unquote(parsed.password)
        )
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

    return UpstreamHop(
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def parse_upstream_line(
    line, index, default_scheme, default_username, default_password
):
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    hop_parts = [part.strip() for part in CHAIN_SEPARATOR_PATTERN.split(raw)]
    hop_parts = [part for part in hop_parts if part]
    if not hop_parts:
        return None

    hops = []
    last_hop_index = len(hop_parts) - 1
    for hop_index, hop_raw in enumerate(hop_parts):
        hop_default_username = default_username if hop_index == last_hop_index else ""
        hop_default_password = default_password if hop_index == last_hop_index else ""
        hops.append(
            parse_upstream_hop(
                hop_raw,
                default_scheme,
                hop_default_username,
                hop_default_password,
            )
        )

    hops = tuple(hops)
    label = " -> ".join(f"{hop.host}:{hop.port}" for hop in hops)
    return UpstreamEntry(
        key=compute_entry_key(hops),
        label=label,
        hops=hops,
    )


def build_list_entries(
    lines: Iterable[str], default_scheme, default_username, default_password
):
    entries = []
    for line in lines:
        entry = parse_upstream_line(
            line, len(entries), default_scheme, default_username, default_password
        )
        if entry is not None:
            entries.append(entry)
    return entries
