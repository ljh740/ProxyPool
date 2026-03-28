#!/usr/bin/env python3
"""Centralized runtime configuration model for ProxyPool.

This module is the single source of truth for runtime configuration values.
It merges persisted admin configuration with environment variables and typed
field defaults, then exposes a typed ``AppConfig`` model for runtime
consumers and a shared set of UI field definitions for the admin panel.
"""

import os
import secrets
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ConfigField:
    """Definition for a runtime configuration field."""

    env_key: str
    label: str
    input_type: str
    default: str
    help_text: str
    options: tuple[str, ...] = ()
    group: str = ""


@dataclass(frozen=True)
class ConfigGroup:
    """Metadata for a UI configuration group (card)."""

    key: str
    icon: str
    collapsed: bool = False


PROXY_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "PROXY_HOST",
        "Listen Host",
        "text",
        "0.0.0.0",
        "PROXY_HOST - Address to bind the proxy server",
        group="proxy_server",
    ),
    ConfigField(
        "PROXY_PORT",
        "Listen Port",
        "number",
        "3128",
        "PROXY_PORT - Port for the proxy server",
        group="proxy_server",
    ),
    ConfigField(
        "AUTH_PASSWORD",
        "Auth Password",
        "text",
        "",
        "AUTH_PASSWORD - Required password for proxy authentication",
        group="proxy_server",
    ),
    ConfigField(
        "AUTH_REALM",
        "Auth Realm",
        "text",
        "Proxy",
        "AUTH_REALM - HTTP authentication realm name",
        group="proxy_server",
    ),
    ConfigField(
        "UPSTREAM_CONNECT_TIMEOUT",
        "Connect Timeout",
        "float",
        "20.0",
        "UPSTREAM_CONNECT_TIMEOUT - Timeout in seconds for upstream connections",
        group="advanced",
    ),
    ConfigField(
        "UPSTREAM_CONNECT_RETRIES",
        "Connect Retries",
        "number",
        "3",
        "UPSTREAM_CONNECT_RETRIES - Max connection attempts per upstream",
        group="advanced",
    ),
    ConfigField(
        "COUNTRY_DETECT_MAX_WORKERS",
        "Country Detect Workers",
        "number",
        "4",
        "COUNTRY_DETECT_MAX_WORKERS - Max concurrent country-detection requests",
        group="advanced",
    ),
    ConfigField(
        "RELAY_TIMEOUT",
        "Relay Timeout",
        "float",
        "120.0",
        "RELAY_TIMEOUT - Timeout in seconds for relay operations",
        group="advanced",
    ),
    ConfigField(
        "REWRITE_LOOPBACK_TO_HOST",
        "Loopback Host Mode",
        "select",
        "auto",
        "REWRITE_LOOPBACK_TO_HOST - Rewrite loopback addresses in Docker",
        ("auto", "always", "off"),
        group="advanced",
    ),
    ConfigField(
        "HOST_LOOPBACK_ADDRESS",
        "Host Loopback Address",
        "text",
        "host.docker.internal",
        "HOST_LOOPBACK_ADDRESS - Address to use when rewriting loopback",
        group="advanced",
    ),
    ConfigField(
        "LOG_LEVEL",
        "Log Level",
        "select",
        "INFO",
        "LOG_LEVEL - Logging verbosity level",
        ("DEBUG", "INFO", "WARNING", "ERROR"),
        group="advanced",
    ),
)

ROUTER_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "SALT",
        "Hash Salt",
        "text",
        "",
        "SALT - Salt for consistent hash routing",
        group="router",
    ),
    ConfigField(
        "RANDOM_POOL_PREFIX",
        "Random Pool Prefix",
        "text",
        "",
        "RANDOM_POOL_PREFIX - Username prefix for random pool routing (empty = disabled)",
        group="router",
    ),
    ConfigField(
        "ROUTER_DEBUG_LOG",
        "Debug Log Path",
        "text",
        "",
        "ROUTER_DEBUG_LOG - File path for router debug log (empty = disabled)",
        group="advanced",
    ),
)

BOOTSTRAP_ENV_FIELDS: tuple[str, ...] = (
    "STATE_DB_PATH",
    "WEB_PORT",
)
LEGACY_ENV_ALIASES: dict[str, str] = {
    "ADMIN_PORT": "WEB_PORT",
}

ADMIN_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("ADMIN_PASSWORD", "Admin Password", "text", "", ""),
    ConfigField("ADMIN_PORT", "Admin Port", "number", "8077", ""),
)

CONFIG_PAGE_FIELDS: tuple[ConfigField, ...] = PROXY_CONFIG_FIELDS + ROUTER_CONFIG_FIELDS
RUNTIME_CONFIG_FIELDS: tuple[ConfigField, ...] = PROXY_CONFIG_FIELDS + ROUTER_CONFIG_FIELDS
ALL_FIELDS: tuple[ConfigField, ...] = RUNTIME_CONFIG_FIELDS + ADMIN_CONFIG_FIELDS
CONFIG_DEFAULTS = {field.env_key: field.default for field in ALL_FIELDS}
CONFIG_DEFAULTS.update(
    {
        "STATE_DB_PATH": "./data/proxypool.sqlite3",
        "WEB_PORT": "8077",
    }
)
CONFIG_PAGE_DEFAULTS = {field.env_key: field.default for field in CONFIG_PAGE_FIELDS}

# Ordered group definitions for the config page UI.
CONFIG_PAGE_GROUPS: tuple[ConfigGroup, ...] = (
    ConfigGroup(key="proxy_server", icon="ti-server"),
    ConfigGroup(key="router", icon="ti-route"),
    ConfigGroup(key="advanced", icon="ti-adjustments", collapsed=True),
)

# Pre-computed mapping: group_key -> tuple of ConfigField in that group.
CONFIG_PAGE_GROUPED: dict[str, tuple[ConfigField, ...]] = {}
for _group in CONFIG_PAGE_GROUPS:
    CONFIG_PAGE_GROUPED[_group.key] = tuple(
        f for f in CONFIG_PAGE_FIELDS if f.group == _group.key
    )

SELECT_OPTIONS = {
    field.env_key: list(field.options) for field in ALL_FIELDS if field.options
}

_GENERATED_RUNTIME_FIELDS = frozenset({"AUTH_PASSWORD", "SALT"})


def _parse_int(value: object, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: object, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bootstrap_value_for_key(env_key: str, environ: Mapping[str, str]) -> str | None:
    for key in (env_key, LEGACY_ENV_ALIASES.get(env_key, "")):
        if not key:
            continue
        value = environ.get(key)
        if value not in (None, ""):
            return value
    return None


def _bootstrap_value_for_field(
    field: ConfigField,
    environ: Mapping[str, str],
) -> str | None:
    value = _bootstrap_value_for_key(field.env_key, environ)
    if value in (None, ""):
        return None
    return value


def _generated_runtime_default(env_key: str) -> str:
    if env_key == "AUTH_PASSWORD":
        return secrets.token_urlsafe(18)
    if env_key == "SALT":
        return secrets.token_hex(16)
    raise KeyError("Unsupported generated runtime field: %s" % env_key)


def _field_default(field: ConfigField, *, generate_runtime_secrets: bool = False) -> str:
    if generate_runtime_secrets and field.env_key in _GENERATED_RUNTIME_FIELDS:
        return _generated_runtime_default(field.env_key)
    return field.default


@dataclass(frozen=True)
class AppConfig:
    """Typed runtime configuration for the proxy server and admin panel."""

    proxy_host: str
    proxy_port: int
    auth_password: str
    auth_realm: str
    upstream_connect_timeout: float
    upstream_connect_retries: int
    country_detect_max_workers: int
    relay_timeout: float
    rewrite_loopback_to_host: str
    host_loopback_address: str
    log_level: str
    salt: str
    state_db_path: str
    random_pool_prefix: str
    router_debug_log: str
    admin_password: str
    admin_port: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "AppConfig":
        """Build typed config from a flat env-keyed mapping."""
        return cls(
            proxy_host=str(values.get("PROXY_HOST", CONFIG_DEFAULTS["PROXY_HOST"])),
            proxy_port=_parse_int(values.get("PROXY_PORT"), 3128),
            auth_password=str(values.get("AUTH_PASSWORD", "")),
            auth_realm=str(values.get("AUTH_REALM", CONFIG_DEFAULTS["AUTH_REALM"])),
            upstream_connect_timeout=_parse_float(
                values.get("UPSTREAM_CONNECT_TIMEOUT"), 20.0
            ),
            upstream_connect_retries=max(
                1, _parse_int(values.get("UPSTREAM_CONNECT_RETRIES"), 3)
            ),
            country_detect_max_workers=max(
                1, _parse_int(values.get("COUNTRY_DETECT_MAX_WORKERS"), 4)
            ),
            relay_timeout=_parse_float(values.get("RELAY_TIMEOUT"), 120.0),
            rewrite_loopback_to_host=str(
                values.get(
                    "REWRITE_LOOPBACK_TO_HOST",
                    CONFIG_DEFAULTS["REWRITE_LOOPBACK_TO_HOST"],
                )
            ).lower(),
            host_loopback_address=str(
                values.get(
                    "HOST_LOOPBACK_ADDRESS",
                    CONFIG_DEFAULTS["HOST_LOOPBACK_ADDRESS"],
                )
            ),
            log_level=str(values.get("LOG_LEVEL", CONFIG_DEFAULTS["LOG_LEVEL"])).upper(),
            salt=str(values.get("SALT", CONFIG_DEFAULTS["SALT"])),
            state_db_path=str(
                values.get("STATE_DB_PATH", CONFIG_DEFAULTS["STATE_DB_PATH"])
            ),
            random_pool_prefix=str(
                values.get(
                    "RANDOM_POOL_PREFIX",
                    CONFIG_DEFAULTS["RANDOM_POOL_PREFIX"],
                )
            ),
            router_debug_log=str(
                values.get("ROUTER_DEBUG_LOG", CONFIG_DEFAULTS["ROUTER_DEBUG_LOG"])
            ),
            admin_password=str(values.get("ADMIN_PASSWORD", CONFIG_DEFAULTS["ADMIN_PASSWORD"])),
            admin_port=_parse_int(values.get("ADMIN_PORT"), 8077),
        )

    @classmethod
    def from_sources(
        cls,
        saved_values: Mapping[str, object] | None = None,
        environ: Mapping[str, str] | None = None,
        *,
        generate_runtime_secrets: bool = False,
    ) -> "AppConfig":
        """Build config from persisted values, bootstrap env, and defaults."""
        environ = os.environ if environ is None else environ
        merged: dict[str, object] = {}
        saved_values = saved_values or {}

        for field in RUNTIME_CONFIG_FIELDS:
            value = saved_values.get(field.env_key)
            if value not in (None, ""):
                merged[field.env_key] = value
            else:
                merged[field.env_key] = _field_default(
                    field,
                    generate_runtime_secrets=generate_runtime_secrets,
                )

        for field in ADMIN_CONFIG_FIELDS:
            bootstrap_value = (
                _bootstrap_value_for_field(field, environ)
                if field.env_key == "ADMIN_PORT"
                else None
            )
            if bootstrap_value is not None:
                merged[field.env_key] = bootstrap_value
                continue
            value = saved_values.get(field.env_key)
            if value not in (None, ""):
                merged[field.env_key] = value
            else:
                merged[field.env_key] = field.default

        for env_key in ("STATE_DB_PATH",):
            bootstrap_value = _bootstrap_value_for_key(env_key, environ)
            if bootstrap_value is not None:
                merged[env_key] = bootstrap_value

        return cls.from_mapping(merged)

    @classmethod
    def from_bootstrap_env(cls, environ: Mapping[str, str] | None = None) -> "AppConfig":
        """Build config from bootstrap environment variables only."""
        return cls.from_sources(
            saved_values=None,
            environ=environ,
            generate_runtime_secrets=False,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AppConfig":
        """Build config from environment variables for first-boot seeding."""
        environ = os.environ if environ is None else environ
        merged: dict[str, object] = {}
        for field in ALL_FIELDS:
            value = None
            if field.env_key != "ADMIN_PASSWORD":
                value = _bootstrap_value_for_key(field.env_key, environ)
                if value is None:
                    value = environ.get(field.env_key)
            if value not in (None, ""):
                merged[field.env_key] = value
            else:
                merged[field.env_key] = _field_default(
                    field,
                    generate_runtime_secrets=True,
                )
        for env_key in ("STATE_DB_PATH",):
            value = _bootstrap_value_for_key(env_key, environ)
            if value is not None:
                merged[env_key] = value
            elif env_key not in merged:
                merged[env_key] = CONFIG_DEFAULTS[env_key]
        return cls.from_mapping(merged)

    @classmethod
    def load(
        cls,
        storage=None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        """Build config from persisted runtime config plus bootstrap env."""
        saved_values: Mapping[str, object] | None = None
        if storage is not None:
            from persistence import load_config

            saved_values = load_config(storage)
        if saved_values:
            app_config = cls.from_sources(
                saved_values=saved_values,
                environ=environ,
                generate_runtime_secrets=True,
            )
        else:
            app_config = cls.from_env(environ=environ)

        if storage is not None:
            from persistence import save_config

            persisted_values = app_config.persisted_values()
            if persisted_values != (saved_values or {}):
                save_config(storage, app_config.runtime_values())

        return app_config

    @classmethod
    def bootstrap_only_keys(cls) -> set[str]:
        return set(BOOTSTRAP_ENV_FIELDS) | {"ADMIN_PORT"}

    def runtime_values(self) -> dict[str, str]:
        """Return env-keyed string values for runtime consumers and UI rendering."""
        values = {
            "PROXY_HOST": self.proxy_host,
            "PROXY_PORT": str(self.proxy_port),
            "AUTH_PASSWORD": self.auth_password,
            "AUTH_REALM": self.auth_realm,
            "UPSTREAM_CONNECT_TIMEOUT": str(self.upstream_connect_timeout),
            "UPSTREAM_CONNECT_RETRIES": str(self.upstream_connect_retries),
            "COUNTRY_DETECT_MAX_WORKERS": str(self.country_detect_max_workers),
            "RELAY_TIMEOUT": str(self.relay_timeout),
            "REWRITE_LOOPBACK_TO_HOST": self.rewrite_loopback_to_host,
            "HOST_LOOPBACK_ADDRESS": self.host_loopback_address,
            "LOG_LEVEL": self.log_level,
            "SALT": self.salt,
            "STATE_DB_PATH": self.state_db_path,
            "RANDOM_POOL_PREFIX": self.random_pool_prefix,
            "ROUTER_DEBUG_LOG": self.router_debug_log,
            "ADMIN_PASSWORD": self.admin_password,
            "ADMIN_PORT": str(self.admin_port),
            "WEB_PORT": str(self.admin_port),
        }
        return values

    def persisted_values(self) -> dict[str, str]:
        """Return the SQLite-persisted config subset."""
        values = self.runtime_values()
        return {
            key: value
            for key, value in values.items()
            if key not in self.bootstrap_only_keys() and key != "WEB_PORT"
        }

    def config_page_values(self) -> dict[str, str]:
        """Return the persisted config subset used by the current admin pages."""
        values = self.runtime_values()
        return {field.env_key: values[field.env_key] for field in CONFIG_PAGE_FIELDS}

    def router_config_values(self) -> dict[str, str]:
        """Return flat router-facing config values."""
        values = self.runtime_values()
        router_keys = {field.env_key for field in ROUTER_CONFIG_FIELDS}
        return {key: value for key, value in values.items() if key in router_keys}


def config_field_tuples(fields: Sequence[ConfigField]) -> list[tuple[str, str, str, str, str]]:
    """Convert config field definitions to the tuple format used by templates."""
    return [
        (field.env_key, field.label, field.input_type, field.default, field.help_text)
        for field in fields
    ]


def _default_format_config_error(
    code: str,
    *,
    label: str,
    minimum: str = "",
    maximum: str = "",
    options: str = "",
) -> str:
    if code == "required":
        return "%s: required (cannot be empty)" % label
    if code == "integer":
        return "%s: must be an integer" % label
    if code == "between":
        return "%s: must be between %s and %s" % (label, minimum, maximum)
    if code == "at_least":
        return "%s: must be at least %s" % (label, minimum)
    if code == "number":
        return "%s: must be a number" % label
    if code == "positive":
        return "%s: must be a positive number" % label
    if code == "one_of":
        return "%s: must be one of %s" % (label, options)
    raise ValueError("Unknown config error code: %s" % code)


def validate_config_form(
    form_data: Mapping[str, str],
    *,
    field_labels: Mapping[str, str] | None = None,
    option_labels: Mapping[str, Mapping[str, str]] | None = None,
    error_formatter: Callable[..., str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Validate config form values for persistence and hot reload."""
    errors: list[str] = []
    clean: dict[str, str] = {}
    field_labels = field_labels or {}
    option_labels = option_labels or {}
    error_formatter = error_formatter or _default_format_config_error

    for field in CONFIG_PAGE_FIELDS:
        raw = str(form_data.get(field.env_key, "")).strip()
        label = field_labels.get(field.env_key, field.label)

        if field.env_key == "AUTH_PASSWORD" and not raw:
            errors.append(error_formatter("required", label=label))
            continue

        if field.input_type == "number":
            if not raw:
                raw = field.default
            try:
                val = int(raw)
            except (TypeError, ValueError):
                errors.append(error_formatter("integer", label=label))
                continue
            if field.env_key == "PROXY_PORT" and (val < 1 or val > 65535):
                errors.append(
                    error_formatter(
                        "between",
                        label=label,
                        minimum="1",
                        maximum="65535",
                    )
                )
                continue
            if field.env_key == "UPSTREAM_CONNECT_RETRIES" and val < 1:
                errors.append(error_formatter("at_least", label=label, minimum="1"))
                continue
            if field.env_key == "COUNTRY_DETECT_MAX_WORKERS" and val < 1:
                errors.append(error_formatter("at_least", label=label, minimum="1"))
                continue
            clean[field.env_key] = str(val)
            continue

        if field.input_type == "float":
            if not raw:
                raw = field.default
            try:
                val = float(raw)
            except (TypeError, ValueError):
                errors.append(error_formatter("number", label=label))
                continue
            if val <= 0:
                errors.append(error_formatter("positive", label=label))
                continue
            clean[field.env_key] = str(val)
            continue

        if field.input_type == "select":
            options = SELECT_OPTIONS.get(field.env_key, [])
            if options and raw not in options:
                localized_options = ", ".join(
                    option_labels.get(field.env_key, {}).get(option, option)
                    for option in options
                )
                errors.append(
                    error_formatter(
                        "one_of",
                        label=label,
                        options=localized_options,
                    )
                )
                continue
            clean[field.env_key] = raw or field.default
            continue

        clean[field.env_key] = raw or field.default

    if clean.get("REWRITE_LOOPBACK_TO_HOST") not in {"auto", "always", "off"}:
        errors.append(
            error_formatter(
                "one_of",
                label=field_labels.get("REWRITE_LOOPBACK_TO_HOST", "Loopback Host Mode"),
                options=", ".join(
                    option_labels.get("REWRITE_LOOPBACK_TO_HOST", {}).get(option, option)
                    for option in ("auto", "always", "off")
                ),
            )
        )

    return clean, errors
