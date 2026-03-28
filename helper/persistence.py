"""SQLite-backed persistence layer for admin state."""

import json
import logging
import os
import sqlite3
import threading

from compat_ports import CompatPortMapping
from config_center import AppConfig

LOGGER = logging.getLogger("persistence")

STATE_KEY_PROXY_LIST = "proxy_list"
STATE_KEY_CONFIG = "config"
STATE_KEY_BATCH_PARAMS = "batch_params"
STATE_KEY_COMPAT_PORT_MAPPINGS = "compat_port_mappings"
DEFAULT_STATE_DB_PATH = "./data/proxypool.sqlite3"


class SQLiteStorage:
    """Thread-safe key/value wrapper backed by a single SQLite database."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA synchronous=NORMAL")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_state (
                state_key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, state_key):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM admin_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
            return row[0] if row is not None else None

    def set(self, state_key, value):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO admin_state (state_key, value)
                VALUES (?, ?)
                ON CONFLICT(state_key) DO UPDATE SET value = excluded.value
                """,
                (state_key, value),
            )
            self._conn.commit()

    def delete(self, state_key):
        with self._lock:
            self._conn.execute(
                "DELETE FROM admin_state WHERE state_key = ?",
                (state_key,),
            )
            self._conn.commit()

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM admin_state")
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()


def resolve_state_db_path(path=None):
    if path not in (None, ""):
        return path
    return os.environ.get("STATE_DB_PATH", DEFAULT_STATE_DB_PATH)


def open_storage(path=None):
    return SQLiteStorage(resolve_state_db_path(path))


def clear_storage(storage):
    storage.clear()


def _serialize_hop(hop):
    return {
        "scheme": hop.scheme,
        "host": hop.host,
        "port": hop.port,
        "username": hop.username,
        "password": hop.password,
    }


def _serialize_entry(entry):
    return {
        "key": entry.key,
        "label": entry.label,
        "hops": [_serialize_hop(hop) for hop in entry.hops],
        "source_tag": entry.source_tag,
        "in_random_pool": entry.in_random_pool,
        "tags": dict(entry.tags),
    }


def _deserialize_entry(data):
    from upstream_pool import UpstreamEntry, UpstreamHop

    hops = tuple(
        UpstreamHop(
            scheme=hop["scheme"],
            host=hop["host"],
            port=hop["port"],
            username=hop.get("username", ""),
            password=hop.get("password", ""),
        )
        for hop in data["hops"]
    )
    return UpstreamEntry(
        key=data["key"],
        label=data["label"],
        hops=hops,
        source_tag=data.get("source_tag", "manual"),
        in_random_pool=data.get("in_random_pool", True),
        tags=data.get("tags", {}),
    )


def save_proxy_list(storage, entries):
    try:
        payload = json.dumps([_serialize_entry(entry) for entry in entries])
        storage.set(STATE_KEY_PROXY_LIST, payload)
    except Exception:
        LOGGER.exception("Failed to save proxy list to SQLite storage")


def load_proxy_list(storage):
    try:
        raw = storage.get(STATE_KEY_PROXY_LIST)
        if raw is None:
            return []
        data = json.loads(raw)
        return [_deserialize_entry(item) for item in data]
    except Exception:
        LOGGER.exception("Failed to load proxy list from SQLite storage")
        return []


def save_config(storage, config_dict):
    try:
        payload = json.dumps(AppConfig.from_mapping(config_dict).persisted_values())
        storage.set(STATE_KEY_CONFIG, payload)
    except Exception:
        LOGGER.exception("Failed to save config to SQLite storage")


def load_config(storage):
    try:
        raw = storage.get(STATE_KEY_CONFIG)
        if raw is None:
            return {}
        return AppConfig.from_mapping(json.loads(raw)).persisted_values()
    except Exception:
        LOGGER.exception("Failed to load config from SQLite storage")
        return {}


def clear_admin_password(storage):
    """Return admin auth to first-boot setup mode."""
    raw = storage.get(STATE_KEY_CONFIG)
    config = json.loads(raw) if raw else {}
    config["ADMIN_PASSWORD"] = ""
    storage.set(STATE_KEY_CONFIG, json.dumps(config))


def save_batch_params(storage, params):
    try:
        storage.set(STATE_KEY_BATCH_PARAMS, json.dumps(params))
    except Exception:
        LOGGER.exception("Failed to save batch params to SQLite storage")


def load_batch_params(storage):
    try:
        raw = storage.get(STATE_KEY_BATCH_PARAMS)
        if raw is None:
            return {}
        return json.loads(raw)
    except Exception:
        LOGGER.exception("Failed to load batch params from SQLite storage")
        return {}


def _normalize_compat_mapping(mapping):
    if isinstance(mapping, CompatPortMapping):
        return mapping
    return CompatPortMapping.from_dict(mapping)


def save_compat_port_mappings(storage, mappings):
    try:
        normalized = sorted(
            [_normalize_compat_mapping(mapping) for mapping in mappings],
            key=lambda item: item.listen_port,
        )
        storage.set(
            STATE_KEY_COMPAT_PORT_MAPPINGS,
            json.dumps([mapping.to_dict() for mapping in normalized]),
        )
    except Exception:
        LOGGER.exception("Failed to save compat port mappings to SQLite storage")


def load_compat_port_mappings(storage):
    try:
        raw = storage.get(STATE_KEY_COMPAT_PORT_MAPPINGS)
        if raw is None:
            return []
        data = json.loads(raw)
        mappings = [CompatPortMapping.from_dict(item) for item in data]
        return sorted(mappings, key=lambda item: item.listen_port)
    except Exception:
        LOGGER.exception("Failed to load compat port mappings from SQLite storage")
        return []
