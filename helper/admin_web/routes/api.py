"""Public API routes for automation clients and AI-agent generated scripts."""

import json
from urllib.parse import quote as _url_quote

from compat_ports import (
    COMPAT_PORT_MAX,
    COMPAT_PORT_MIN,
    TARGET_TYPE_ENTRY_KEY,
    TARGET_TYPE_SESSION_NAME,
    CompatPortMapping,
)
from flask import Response, render_template, request
from router import Router, normalize_username
from upstream_pool import UpstreamPool

API_TITLE = "ProxyPool Public API"
API_VERSION = "v1"


def _json_response(payload, status=200):
    return Response(
        json.dumps(payload),
        status=status,
        mimetype="application/json",
    )


def _text_response(content, status=200):
    return Response(content, status=status, mimetype="text/plain")


def _api_success(data, *, meta=None, status=200):
    payload = {
        "ok": True,
        "data": data,
    }
    if meta:
        payload["meta"] = meta
    return _json_response(payload, status=status)


def _api_error(code, message, *, status, hint=None):
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if hint:
        payload["error"]["hint"] = hint
    return _json_response(payload, status=status)


def _request_payload():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return request.form.to_dict(flat=True)


def _coerce_bool(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional_port(raw):
    if raw in (None, ""):
        return None
    return int(str(raw).strip())


def _connect_host(bind_host):
    if bind_host in {"0.0.0.0", "::", ""}:
        forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
        candidate = forwarded_host or request.host
        if candidate.startswith("["):
            closing = candidate.find("]")
            if closing != -1:
                return candidate[1:closing]
        if candidate.count(":") == 1:
            return candidate.rsplit(":", 1)[0]
        if candidate:
            return candidate
        return "127.0.0.1"
    return bind_host


def _main_proxy_access(app_config, username):
    connect_host = _connect_host(app_config.proxy_host)
    quoted_username = _url_quote(username, safe="")
    return {
        "type": "main_proxy",
        "listen_host": app_config.proxy_host,
        "connect_host": connect_host,
        "listen_port": app_config.proxy_port,
        "username": username,
        "requires_auth": True,
        "password_placeholder": "<AUTH_PASSWORD>",
        "http_proxy": "http://%s:<AUTH_PASSWORD>@%s:%s" % (quoted_username, connect_host, app_config.proxy_port),
        "https_proxy": "http://%s:<AUTH_PASSWORD>@%s:%s" % (quoted_username, connect_host, app_config.proxy_port),
        "socks5_proxy": "socks5://%s:<AUTH_PASSWORD>@%s:%s" % (quoted_username, connect_host, app_config.proxy_port),
        "socks5h_proxy": "socks5h://%s:<AUTH_PASSWORD>@%s:%s" % (quoted_username, connect_host, app_config.proxy_port),
    }


def _compat_access(app_config, listen_port):
    connect_host = _connect_host(app_config.proxy_host)
    return {
        "type": "compat_port",
        "listen_host": app_config.proxy_host,
        "connect_host": connect_host,
        "listen_port": listen_port,
        "requires_auth": False,
        "http_proxy": "http://%s:%s" % (connect_host, listen_port),
    }


def _serialize_hop(hop):
    return {
        "scheme": hop.scheme,
        "host": hop.host,
        "port": hop.port,
        "display": hop.display,
        "has_username": bool(hop.username),
        "has_password": bool(hop.password),
    }


def _serialize_entry(entry):
    return {
        "entry_key": entry.key,
        "label": entry.label,
        "display": entry.display,
        "hop_count": entry.chain_length,
        "source_tag": entry.source_tag,
        "in_random_pool": entry.in_random_pool,
        "hops": [_serialize_hop(hop) for hop in entry.hops],
    }


def _serialize_mapping(mapping, app_config):
    payload = mapping.to_dict()
    payload["access"] = _compat_access(app_config, mapping.listen_port)
    return payload


def _find_mapping(mappings, listen_port):
    return next((mapping for mapping in mappings if mapping.listen_port == listen_port), None)


def _find_mapping_by_target(mappings, target_type, target_value):
    return next(
        (
            mapping
            for mapping in mappings
            if mapping.target_type == target_type and mapping.target_value == target_value
        ),
        None,
    )


def _find_free_compat_port(mappings):
    used_ports = {mapping.listen_port for mapping in mappings}
    for port in range(COMPAT_PORT_MIN, COMPAT_PORT_MAX + 1):
        if port not in used_ports:
            return port
    return None


def _build_router(runtime, entries):
    router = Router(
        config=runtime.load_app_config(),
        upstream_pool=UpstreamPool(source="admin", entries=list(entries)),
    )
    router.storage = runtime.get_storage()
    return router


def _resolve_mapping_entry(router, mapping):
    if mapping.target_type == TARGET_TYPE_ENTRY_KEY:
        return router.get_entry(mapping.target_value)
    return router.route_entry(mapping.target_value)


def _pick_unbound_entry(entries, mappings):
    bound_entry_keys = {
        mapping.target_value for mapping in mappings if mapping.enabled and mapping.target_type == TARGET_TYPE_ENTRY_KEY
    }
    return next((entry for entry in entries if entry.key not in bound_entry_keys), None)


def _build_resolve_response(app_config, query_type, query_value, resolved_entry, mapping=None):
    access = (
        _compat_access(app_config, mapping.listen_port)
        if mapping is not None
        else _main_proxy_access(app_config, query_value)
    )
    data = {
        "query": {
            "type": query_type,
            "value": query_value,
        },
        "access": access,
        "resolved_entry": _serialize_entry(resolved_entry),
    }
    if mapping is not None:
        data["mapping"] = _serialize_mapping(mapping, app_config)
    return data


def _list_entries_payload(entries, app_config, mappings):
    exact_bindings = {}
    for mapping in mappings:
        if mapping.target_type == TARGET_TYPE_ENTRY_KEY:
            exact_bindings.setdefault(mapping.target_value, []).append(mapping.listen_port)
    return {
        "items": [
            {
                **_serialize_entry(entry),
                "main_access": _main_proxy_access(app_config, entry.key),
                "compat_ports": exact_bindings.get(entry.key, []),
            }
            for entry in entries
        ],
        "count": len(entries),
    }


def _list_mappings_payload(app_config, router, mappings):
    items = []
    for mapping in mappings:
        resolved_entry = _resolve_mapping_entry(router, mapping)
        items.append(
            {
                "mapping": _serialize_mapping(mapping, app_config),
                "resolved_entry": (_serialize_entry(resolved_entry) if resolved_entry is not None else None),
            }
        )
    return {
        "items": items,
        "count": len(items),
    }


def _docs_base_url():
    return request.url_root.rstrip("/")


def _example_resolve_response(app_config):
    return {
        "ok": True,
        "data": {
            "query": {
                "type": "username",
                "value": "browser-a",
            },
            "access": _main_proxy_access(app_config, "browser-a"),
            "resolved_entry": {
                "entry_key": "e1",
                "label": "host1.example.com:10001",
                "display": "socks5://host1.example.com:10001",
                "hop_count": 1,
                "source_tag": "manual",
                "in_random_pool": True,
                "hops": [
                    {
                        "scheme": "socks5",
                        "host": "host1.example.com",
                        "port": 10001,
                        "display": "socks5://host1.example.com:10001",
                        "has_username": True,
                        "has_password": True,
                    }
                ],
            },
        },
    }


def _example_bind_response(app_config):
    mapping = CompatPortMapping(
        listen_port=COMPAT_PORT_MIN,
        target_type=TARGET_TYPE_SESSION_NAME,
        target_value="browser-a",
        enabled=True,
        note="generated by script",
    )
    return {
        "ok": True,
        "data": {
            "created": True,
            "mapping": _serialize_mapping(mapping, app_config),
            "resolved_entry": {
                "entry_key": "e1",
                "label": "host1.example.com:10001",
                "display": "socks5://host1.example.com:10001",
                "hop_count": 1,
                "source_tag": "manual",
                "in_random_pool": True,
                "hops": [
                    {
                        "scheme": "socks5",
                        "host": "host1.example.com",
                        "port": 10001,
                        "display": "socks5://host1.example.com:10001",
                        "has_username": True,
                        "has_password": True,
                    }
                ],
            },
        },
    }


def _example_allocate_response(app_config):
    mapping = CompatPortMapping(
        listen_port=COMPAT_PORT_MIN,
        target_type=TARGET_TYPE_ENTRY_KEY,
        target_value="e1",
        enabled=True,
        note="allocated",
    )
    return {
        "ok": True,
        "data": {
            "created": True,
            "mapping": _serialize_mapping(mapping, app_config),
            "resolved_entry": {
                "entry_key": "e1",
                "label": "host1.example.com:10001",
                "display": "socks5://host1.example.com:10001",
                "hop_count": 1,
                "source_tag": "manual",
                "in_random_pool": True,
                "hops": [
                    {
                        "scheme": "socks5",
                        "host": "host1.example.com",
                        "port": 10001,
                        "display": "socks5://host1.example.com:10001",
                        "has_username": True,
                        "has_password": True,
                    }
                ],
            },
        },
    }


def _api_spec(runtime):
    app_config = runtime.load_app_config()
    base_url = _docs_base_url()
    docs_warning = "These endpoints are unauthenticated. Expose them only on localhost or within a trusted network."
    endpoints = [
        {
            "method": "GET",
            "path": "/api/v1/health",
            "summary": "Return health and inventory counts.",
            "description": "Useful before writing scripts or running batch jobs.",
            "query_params": [],
            "json_body": None,
            "curl_example": "curl '%s/api/v1/health'" % base_url,
            "python_example": (
                "import requests\nresp = requests.get('%s/api/v1/health', timeout=10)\nprint(resp.json())"
            )
            % base_url,
            "response_example": {
                "ok": True,
                "data": {
                    "status": "ok",
                    "api_version": API_VERSION,
                    "proxy_listener": {
                        "listen_host": app_config.proxy_host,
                        "connect_host": _connect_host(app_config.proxy_host),
                        "listen_port": app_config.proxy_port,
                    },
                    "compat_port_range": {
                        "min": COMPAT_PORT_MIN,
                        "max": COMPAT_PORT_MAX,
                    },
                    "counts": {
                        "entries": 2,
                        "compat_mappings": 1,
                    },
                },
            },
            "errors": [],
        },
        {
            "method": "GET",
            "path": "/api/v1/resolve",
            "summary": "Resolve a username, an entry key, or a compatibility port.",
            "description": (
                "Provide exactly one of username, entry_key, or listen_port. "
                "For username queries, the API returns the sticky shared-route result."
            ),
            "query_params": [
                {
                    "name": "username",
                    "type": "string",
                    "required": False,
                    "description": "Shared-routing username to resolve.",
                },
                {
                    "name": "entry_key",
                    "type": "string",
                    "required": False,
                    "description": "Exact entry key to resolve.",
                },
                {
                    "name": "listen_port",
                    "type": "integer",
                    "required": False,
                    "description": "Compatibility port to inspect.",
                },
            ],
            "json_body": None,
            "curl_example": "curl '%s/api/v1/resolve?username=browser-a'" % base_url,
            "python_example": (
                "import requests\n"
                "resp = requests.get(\n"
                "    '%s/api/v1/resolve',\n"
                "    params={'username': 'browser-a'},\n"
                "    timeout=10,\n"
                ")\n"
                "print(resp.json())"
            )
            % base_url,
            "response_example": _example_resolve_response(app_config),
            "errors": [
                {
                    "code": "invalid_lookup",
                    "message": "Provide exactly one of username, entry_key, or listen_port.",
                },
                {
                    "code": "entry_unavailable",
                    "message": "No upstream entry is available for the requested target.",
                },
            ],
        },
        {
            "method": "GET",
            "path": "/api/v1/entries",
            "summary": "List all upstream entries.",
            "description": "Useful for discovering valid entry_key values before binding exact ports.",
            "query_params": [],
            "json_body": None,
            "curl_example": "curl '%s/api/v1/entries'" % base_url,
            "python_example": (
                "import requests\nentries = requests.get('%s/api/v1/entries', timeout=10).json()\nprint(entries)"
            )
            % base_url,
            "response_example": {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "entry_key": "e1",
                            "label": "host1.example.com:10001",
                            "display": "socks5://host1.example.com:10001",
                            "hop_count": 1,
                            "source_tag": "manual",
                            "in_random_pool": True,
                            "hops": [
                                {
                                    "scheme": "socks5",
                                    "host": "host1.example.com",
                                    "port": 10001,
                                    "display": "socks5://host1.example.com:10001",
                                    "has_username": True,
                                    "has_password": True,
                                }
                            ],
                            "main_access": _main_proxy_access(app_config, "e1"),
                            "compat_ports": [33100],
                        }
                    ],
                    "count": 1,
                },
            },
            "errors": [],
        },
        {
            "method": "GET",
            "path": "/api/v1/compat/mappings",
            "summary": "List all compatibility-port bindings.",
            "description": "Each item includes the local no-auth proxy URL and the resolved upstream entry.",
            "query_params": [],
            "json_body": None,
            "curl_example": "curl '%s/api/v1/compat/mappings'" % base_url,
            "python_example": (
                "import requests\n"
                "mappings = requests.get('%s/api/v1/compat/mappings', timeout=10).json()\n"
                "print(mappings)"
            )
            % base_url,
            "response_example": {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "mapping": _serialize_mapping(
                                CompatPortMapping(
                                    listen_port=33100,
                                    target_type=TARGET_TYPE_SESSION_NAME,
                                    target_value="browser-a",
                                    enabled=True,
                                    note="generated by script",
                                ),
                                app_config,
                            ),
                            "resolved_entry": {
                                "entry_key": "e1",
                                "label": "host1.example.com:10001",
                                "display": "socks5://host1.example.com:10001",
                                "hop_count": 1,
                                "source_tag": "manual",
                                "in_random_pool": True,
                                "hops": [
                                    {
                                        "scheme": "socks5",
                                        "host": "host1.example.com",
                                        "port": 10001,
                                        "display": "socks5://host1.example.com:10001",
                                        "has_username": True,
                                        "has_password": True,
                                    }
                                ],
                            },
                        }
                    ],
                    "count": 1,
                },
            },
            "errors": [],
        },
        {
            "method": "POST",
            "path": "/api/v1/compat/bind",
            "summary": "Bind a compatibility port to a username or an entry key.",
            "description": (
                "Pass exactly one of username or entry_key. If listen_port is omitted, "
                "the server allocates the next free compatibility port."
            ),
            "query_params": [],
            "json_body": {
                "type": "object",
                "properties": {
                    "username": "string, optional",
                    "entry_key": "string, optional",
                    "listen_port": "integer, optional",
                    "replace": "boolean, optional",
                    "enabled": "boolean, optional, default true",
                    "note": "string, optional",
                },
                "required_rule": "Provide exactly one of username or entry_key.",
            },
            "curl_example": (
                "curl -X POST '%s/api/v1/compat/bind' "
                "-H 'Content-Type: application/json' "
                '-d \'{"username":"browser-a","note":"generated by script"}\''
            )
            % base_url,
            "python_example": (
                "import requests\n"
                "resp = requests.post(\n"
                "    '%s/api/v1/compat/bind',\n"
                "    json={'username': 'browser-a', 'note': 'generated by script'},\n"
                "    timeout=10,\n"
                ")\n"
                "print(resp.json())"
            )
            % base_url,
            "response_example": _example_bind_response(app_config),
            "errors": [
                {
                    "code": "invalid_target",
                    "message": "Provide exactly one of username or entry_key.",
                },
                {
                    "code": "port_conflict",
                    "message": "The requested compatibility port is already bound.",
                },
            ],
        },
        {
            "method": "POST",
            "path": "/api/v1/compat/allocate",
            "summary": "Allocate one compatibility port from the proxy pool.",
            "description": (
                "The server picks the first upstream entry that is not already bound "
                "to an exact compatibility port mapping."
            ),
            "query_params": [],
            "json_body": {
                "type": "object",
                "properties": {
                    "listen_port": "integer, optional",
                    "replace": "boolean, optional",
                    "enabled": "boolean, optional, default true",
                    "note": "string, optional",
                },
                "required_rule": "No required fields.",
            },
            "curl_example": "curl -X POST '%s/api/v1/compat/allocate'" % base_url,
            "python_example": (
                "import requests\nresp = requests.post('%s/api/v1/compat/allocate', timeout=10)\nprint(resp.json())"
            )
            % base_url,
            "response_example": _example_allocate_response(app_config),
            "errors": [
                {
                    "code": "entry_pool_empty",
                    "message": "No upstream entries are configured.",
                },
                {
                    "code": "entry_pool_exhausted",
                    "message": "No unbound upstream entries are available.",
                },
            ],
        },
        {
            "method": "POST",
            "path": "/api/v1/compat/unbind",
            "summary": "Remove one compatibility-port mapping.",
            "description": "Pass the compatibility listen_port that should be removed.",
            "query_params": [],
            "json_body": {
                "type": "object",
                "properties": {
                    "listen_port": "integer, required",
                },
                "required_rule": "listen_port is required.",
            },
            "curl_example": (
                "curl -X POST '%s/api/v1/compat/unbind' "
                "-H 'Content-Type: application/json' "
                "-d '{\"listen_port\":33100}'"
            )
            % base_url,
            "python_example": (
                "import requests\n"
                "resp = requests.post(\n"
                "    '%s/api/v1/compat/unbind',\n"
                "    json={'listen_port': 33100},\n"
                "    timeout=10,\n"
                ")\n"
                "print(resp.json())"
            )
            % base_url,
            "response_example": {
                "ok": True,
                "data": {
                    "deleted": True,
                    "mapping": _serialize_mapping(
                        CompatPortMapping(
                            listen_port=33100,
                            target_type=TARGET_TYPE_SESSION_NAME,
                            target_value="browser-a",
                            enabled=True,
                            note="generated by script",
                        ),
                        app_config,
                    ),
                },
            },
            "errors": [
                {
                    "code": "missing_listen_port",
                    "message": "listen_port is required.",
                },
                {
                    "code": "mapping_not_found",
                    "message": "Compatibility port mapping not found.",
                },
            ],
        },
    ]
    tasks = [
        {
            "title": "Resolve a username",
            "description": (
                "Ask the API where a shared-routing username will land, then build "
                "your own script using the returned access template."
            ),
            "steps": [
                "Call GET /api/v1/resolve?username=<name>.",
                "Read data.access.http_proxy or data.access.socks5_proxy.",
                "Replace <AUTH_PASSWORD> with the current AUTH_PASSWORD value in your script.",
            ],
        },
        {
            "title": "Allocate a no-auth compatibility port",
            "description": (
                "Useful when the caller cannot send proxy credentials and only needs a local HTTP proxy endpoint."
            ),
            "steps": [
                "Call POST /api/v1/compat/allocate.",
                "Read data.mapping.access.http_proxy.",
                "Use the returned local proxy URL directly in your automation client.",
            ],
        },
        {
            "title": "Bind a stable no-auth port for one username",
            "description": (
                "If a profile name should always use the same local compatibility port, "
                "bind it once and reuse the returned port."
            ),
            "steps": [
                "Call POST /api/v1/compat/bind with a username.",
                "Store data.mapping.listen_port.",
                "Use data.mapping.access.http_proxy in future runs.",
            ],
        },
    ]
    return {
        "title": API_TITLE,
        "version": API_VERSION,
        "base_url": base_url,
        "docs_url": "%s/api" % base_url,
        "text_url": "%s/api.txt" % base_url,
        "json_url": "%s/api.json" % base_url,
        "openapi_url": "%s/api/openapi.json" % base_url,
        "warning": docs_warning,
        "compat_port_range": {
            "min": COMPAT_PORT_MIN,
            "max": COMPAT_PORT_MAX,
        },
        "endpoints": endpoints,
        "tasks": tasks,
    }


def _openapi_spec(runtime):
    spec = _api_spec(runtime)
    app_config = runtime.load_app_config()
    return {
        "openapi": "3.1.0",
        "info": {
            "title": spec["title"],
            "version": spec["version"],
            "description": spec["warning"],
        },
        "servers": [{"url": spec["base_url"]}],
        "paths": {
            "/api/v1/health": {
                "get": {
                    "summary": "Return health and inventory counts.",
                    "responses": {
                        "200": {
                            "description": "Health response",
                            "content": {
                                "application/json": {
                                    "example": next(
                                        endpoint["response_example"]
                                        for endpoint in spec["endpoints"]
                                        if endpoint["path"] == "/api/v1/health"
                                    )
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/resolve": {
                "get": {
                    "summary": "Resolve a username, entry key, or compatibility port.",
                    "parameters": [
                        {
                            "name": "username",
                            "in": "query",
                            "schema": {"type": "string"},
                            "required": False,
                            "description": "Shared-routing username to resolve.",
                        },
                        {
                            "name": "entry_key",
                            "in": "query",
                            "schema": {"type": "string"},
                            "required": False,
                            "description": "Exact upstream entry key to resolve.",
                        },
                        {
                            "name": "listen_port",
                            "in": "query",
                            "schema": {"type": "integer"},
                            "required": False,
                            "description": "Compatibility port to resolve.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Resolve response",
                            "content": {"application/json": {"example": _example_resolve_response(app_config)}},
                        },
                        "400": {"description": "Invalid query"},
                        "404": {"description": "Entry or mapping not found"},
                        "503": {"description": "No upstream entry available"},
                    },
                }
            },
            "/api/v1/entries": {
                "get": {
                    "summary": "List all upstream entries.",
                    "responses": {
                        "200": {
                            "description": "Entry list",
                            "content": {
                                "application/json": {
                                    "example": next(
                                        endpoint["response_example"]
                                        for endpoint in spec["endpoints"]
                                        if endpoint["path"] == "/api/v1/entries"
                                    )
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/compat/mappings": {
                "get": {
                    "summary": "List all compatibility-port mappings.",
                    "responses": {
                        "200": {
                            "description": "Mapping list",
                            "content": {
                                "application/json": {
                                    "example": next(
                                        endpoint["response_example"]
                                        for endpoint in spec["endpoints"]
                                        if endpoint["path"] == "/api/v1/compat/mappings"
                                    )
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/compat/bind": {
                "post": {
                    "summary": "Bind one compatibility port.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": {
                                    "username": "browser-a",
                                    "note": "generated by script",
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Existing mapping reused"},
                        "201": {
                            "description": "Mapping created",
                            "content": {"application/json": {"example": _example_bind_response(app_config)}},
                        },
                        "400": {"description": "Invalid payload"},
                        "404": {"description": "Entry key not found"},
                        "409": {"description": "Port conflict or pool exhausted"},
                    },
                }
            },
            "/api/v1/compat/allocate": {
                "post": {
                    "summary": "Allocate one compatibility port from the pool.",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "example": {
                                    "note": "allocated by script",
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Mapping created",
                            "content": {"application/json": {"example": _example_allocate_response(app_config)}},
                        },
                        "400": {"description": "Invalid payload"},
                        "409": {"description": "No free port or no unbound entry"},
                        "503": {"description": "No upstream entries configured"},
                    },
                }
            },
            "/api/v1/compat/unbind": {
                "post": {
                    "summary": "Remove one compatibility-port mapping.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": {
                                    "listen_port": 33100,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Mapping deleted",
                            "content": {
                                "application/json": {
                                    "example": next(
                                        endpoint["response_example"]
                                        for endpoint in spec["endpoints"]
                                        if endpoint["path"] == "/api/v1/compat/unbind"
                                    )
                                }
                            },
                        },
                        "400": {"description": "listen_port missing or invalid"},
                        "404": {"description": "Mapping not found"},
                    },
                }
            },
        },
    }


def _render_api_text(spec):
    lines = [
        "%s (%s)" % (spec["title"], spec["version"]),
        "",
        spec["warning"],
        "",
        "Docs URLs:",
        "- HTML: %s" % spec["docs_url"],
        "- Text: %s" % spec["text_url"],
        "- JSON: %s" % spec["json_url"],
        "- OpenAPI: %s" % spec["openapi_url"],
        "",
        "Compatibility port range: %s-%s" % (spec["compat_port_range"]["min"], spec["compat_port_range"]["max"]),
        "",
        "Common tasks:",
    ]
    for task in spec["tasks"]:
        lines.append("- %s: %s" % (task["title"], task["description"]))
        for step in task["steps"]:
            lines.append("  - %s" % step)
    lines.append("")
    lines.append("Endpoints:")
    for endpoint in spec["endpoints"]:
        lines.append("")
        lines.append("%s %s" % (endpoint["method"], endpoint["path"]))
        lines.append(endpoint["summary"])
        lines.append(endpoint["description"])
        if endpoint["query_params"]:
            lines.append("Query parameters:")
            for param in endpoint["query_params"]:
                requirement = "required" if param["required"] else "optional"
                lines.append(
                    "- %s (%s, %s): %s"
                    % (
                        param["name"],
                        param["type"],
                        requirement,
                        param["description"],
                    )
                )
        if endpoint["json_body"]:
            lines.append("JSON body:")
            for key, value in endpoint["json_body"]["properties"].items():
                lines.append("- %s: %s" % (key, value))
            lines.append("Rule: %s" % endpoint["json_body"]["required_rule"])
        if endpoint["errors"]:
            lines.append("Errors:")
            for error in endpoint["errors"]:
                lines.append("- %s: %s" % (error["code"], error["message"]))
        lines.append("curl:")
        lines.append(endpoint["curl_example"])
        lines.append("python:")
        lines.append(endpoint["python_example"])
        lines.append("response:")
        lines.append(json.dumps(endpoint["response_example"], indent=2))
    lines.append("")
    return "\n".join(lines)


def register_public_api_routes(blueprint, runtime, *, csrf=None):
    """Register the public /api documentation and /api/v1 endpoints."""

    def _csrf_exempt_post(rule):
        def decorator(func):
            wrapped = func
            if csrf is not None:
                wrapped = csrf.exempt(wrapped)
            return blueprint.post(rule)(wrapped)

        return decorator

    def _docs_spec():
        return _api_spec(runtime)

    @blueprint.get("/api")
    def api_index():
        spec = _docs_spec()
        format_name = request.args.get("format", "").strip().lower()
        if format_name == "json":
            return _json_response(spec)
        if format_name in {"txt", "text"}:
            return _text_response(_render_api_text(spec))

        ui = runtime.resolve_ui_state()
        content = render_template("api/page.html", spec=spec)
        return runtime.build_page_response(
            title=API_TITLE,
            content=content,
            active_nav=None,
            ui=ui,
        )

    @blueprint.get("/api.txt")
    def api_index_text():
        return _text_response(_render_api_text(_docs_spec()))

    @blueprint.get("/api.json")
    def api_index_json():
        return _json_response(_docs_spec())

    @blueprint.get("/api/openapi.json")
    def api_openapi_json():
        return _json_response(_openapi_spec(runtime))

    @blueprint.get("/api/v1/health")
    def api_health():
        storage = runtime.get_storage()
        app_config = runtime.load_app_config()
        entries = list(runtime.load_entries(storage))
        mappings = list(runtime.load_compat_mappings(storage))
        return _api_success(
            {
                "status": "ok",
                "api_version": API_VERSION,
                "proxy_listener": {
                    "listen_host": app_config.proxy_host,
                    "connect_host": _connect_host(app_config.proxy_host),
                    "listen_port": app_config.proxy_port,
                },
                "compat_port_range": {
                    "min": COMPAT_PORT_MIN,
                    "max": COMPAT_PORT_MAX,
                },
                "counts": {
                    "entries": len(entries),
                    "compat_mappings": len(mappings),
                },
            }
        )

    @blueprint.get("/api/v1/resolve")
    def api_resolve():
        username = normalize_username(request.args.get("username", "").strip())
        entry_key = request.args.get("entry_key", "").strip()
        listen_port = request.args.get("listen_port", "").strip()
        lookup_count = sum(bool(value) for value in (username, entry_key, listen_port))
        if lookup_count != 1:
            return _api_error(
                "invalid_lookup",
                "Provide exactly one of username, entry_key, or listen_port.",
                status=400,
            )

        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        router = _build_router(runtime, entries)
        app_config = runtime.load_app_config()

        if username:
            resolved_entry = router.route_entry(username)
            if resolved_entry is None:
                return _api_error(
                    "entry_unavailable",
                    "No upstream entry is available for the requested username.",
                    status=503,
                )
            return _api_success(_build_resolve_response(app_config, "username", username, resolved_entry))

        if entry_key:
            resolved_entry = router.get_entry(entry_key)
            if resolved_entry is None:
                return _api_error(
                    "entry_not_found",
                    "The requested upstream entry does not exist.",
                    status=404,
                )
            return _api_success(_build_resolve_response(app_config, "entry_key", entry_key, resolved_entry))

        try:
            port = int(listen_port)
        except ValueError:
            return _api_error(
                "invalid_listen_port",
                "listen_port must be an integer.",
                status=400,
            )

        mapping = _find_mapping(runtime.load_compat_mappings(storage), port)
        if mapping is None:
            return _api_error(
                "mapping_not_found",
                "Compatibility port mapping not found.",
                status=404,
            )
        resolved_entry = _resolve_mapping_entry(router, mapping)
        if resolved_entry is None:
            return _api_error(
                "entry_unavailable",
                "The configured compatibility target is unavailable.",
                status=503,
            )
        return _api_success(
            _build_resolve_response(
                app_config,
                "listen_port",
                str(port),
                resolved_entry,
                mapping=mapping,
            )
        )

    @blueprint.get("/api/v1/entries")
    def api_entries():
        storage = runtime.get_storage()
        entries = list(runtime.load_entries(storage))
        mappings = list(runtime.load_compat_mappings(storage))
        return _api_success(_list_entries_payload(entries, runtime.load_app_config(), mappings))

    @blueprint.get("/api/v1/compat/mappings")
    def api_compat_mappings():
        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        entries = list(runtime.load_entries(storage))
        router = _build_router(runtime, entries)
        return _api_success(_list_mappings_payload(runtime.load_app_config(), router, mappings))

    @_csrf_exempt_post("/api/v1/compat/bind")
    def api_compat_bind():
        payload = _request_payload()
        username = normalize_username(str(payload.get("username", "")).strip())
        entry_key = str(payload.get("entry_key", "")).strip()
        if bool(username) == bool(entry_key):
            return _api_error(
                "invalid_target",
                "Provide exactly one of username or entry_key.",
                status=400,
            )

        enabled = _coerce_bool(payload.get("enabled"), True)
        note = str(payload.get("note", "")).strip()
        replace = _coerce_bool(payload.get("replace"), False)
        try:
            requested_port = _parse_optional_port(payload.get("listen_port"))
        except ValueError:
            return _api_error(
                "invalid_listen_port",
                "listen_port must be an integer.",
                status=400,
            )

        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        entries = list(runtime.load_entries(storage))
        router = _build_router(runtime, entries)
        app_config = runtime.load_app_config()

        if username:
            target_type = TARGET_TYPE_SESSION_NAME
            target_value = username
            resolved_entry = router.route_entry(username)
            if resolved_entry is None:
                return _api_error(
                    "entry_unavailable",
                    "No upstream entry is available for the requested username.",
                    status=503,
                )
        else:
            target_type = TARGET_TYPE_ENTRY_KEY
            target_value = entry_key
            resolved_entry = router.get_entry(entry_key)
            if resolved_entry is None:
                return _api_error(
                    "entry_not_found",
                    "The requested upstream entry does not exist.",
                    status=404,
                )

        existing_target = _find_mapping_by_target(mappings, target_type, target_value)
        if requested_port is None and existing_target is not None:
            return _api_success(
                {
                    "created": False,
                    "mapping": _serialize_mapping(existing_target, app_config),
                    "resolved_entry": _serialize_entry(resolved_entry),
                }
            )

        if requested_port is None:
            requested_port = _find_free_compat_port(mappings)
            if requested_port is None:
                return _api_error(
                    "port_pool_exhausted",
                    "No compatibility ports are available.",
                    status=409,
                )

        existing_port = _find_mapping(mappings, requested_port)
        if (
            existing_port is not None
            and (existing_port.target_type != target_type or existing_port.target_value != target_value)
            and not replace
        ):
            return _api_error(
                "port_conflict",
                "The requested compatibility port is already bound. Set replace=true to overwrite it.",
                status=409,
            )

        try:
            mapping = CompatPortMapping(
                listen_port=requested_port,
                target_type=target_type,
                target_value=target_value,
                enabled=enabled,
                note=note,
            )
        except (TypeError, ValueError):
            return _api_error(
                "invalid_mapping",
                "Invalid compatibility mapping payload.",
                status=400,
            )

        updated = [
            item
            for item in mappings
            if item.listen_port != mapping.listen_port
            and not (item.target_type == mapping.target_type and item.target_value == mapping.target_value)
        ]
        created = existing_port is None and existing_target is None
        updated.append(mapping)
        updated.sort(key=lambda item: item.listen_port)
        runtime.save_compat_mappings(updated, storage)
        runtime.trigger_reload()
        return _api_success(
            {
                "created": created,
                "mapping": _serialize_mapping(mapping, app_config),
                "resolved_entry": _serialize_entry(resolved_entry),
            },
            status=201 if created else 200,
        )

    @_csrf_exempt_post("/api/v1/compat/allocate")
    def api_compat_allocate():
        payload = _request_payload()
        enabled = _coerce_bool(payload.get("enabled"), True)
        note = str(payload.get("note", "")).strip()
        replace = _coerce_bool(payload.get("replace"), False)
        try:
            requested_port = _parse_optional_port(payload.get("listen_port"))
        except ValueError:
            return _api_error(
                "invalid_listen_port",
                "listen_port must be an integer.",
                status=400,
            )

        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        entries = list(runtime.load_entries(storage))
        if not entries:
            return _api_error(
                "entry_pool_empty",
                "No upstream entries are configured.",
                status=503,
            )

        entry = _pick_unbound_entry(entries, mappings)
        if entry is None:
            return _api_error(
                "entry_pool_exhausted",
                "No unbound upstream entries are available for compatibility ports.",
                status=409,
            )

        if requested_port is None:
            requested_port = _find_free_compat_port(mappings)
            if requested_port is None:
                return _api_error(
                    "port_pool_exhausted",
                    "No compatibility ports are available.",
                    status=409,
                )

        existing_port = _find_mapping(mappings, requested_port)
        if existing_port is not None and not replace:
            return _api_error(
                "port_conflict",
                "The requested compatibility port is already bound. Set replace=true to overwrite it.",
                status=409,
            )

        try:
            mapping = CompatPortMapping(
                listen_port=requested_port,
                target_type=TARGET_TYPE_ENTRY_KEY,
                target_value=entry.key,
                enabled=enabled,
                note=note,
            )
        except (TypeError, ValueError):
            return _api_error(
                "invalid_mapping",
                "Invalid compatibility mapping payload.",
                status=400,
            )

        updated = [
            item
            for item in mappings
            if item.listen_port != mapping.listen_port
            and not (item.target_type == mapping.target_type and item.target_value == mapping.target_value)
        ]
        updated.append(mapping)
        updated.sort(key=lambda item: item.listen_port)
        runtime.save_compat_mappings(updated, storage)
        runtime.trigger_reload()
        return _api_success(
            {
                "created": True,
                "mapping": _serialize_mapping(mapping, runtime.load_app_config()),
                "resolved_entry": _serialize_entry(entry),
            },
            status=201,
        )

    @_csrf_exempt_post("/api/v1/compat/unbind")
    def api_compat_unbind():
        payload = _request_payload()
        try:
            requested_port = _parse_optional_port(payload.get("listen_port"))
        except ValueError:
            return _api_error(
                "invalid_listen_port",
                "listen_port must be an integer.",
                status=400,
            )
        if requested_port is None:
            return _api_error(
                "missing_listen_port",
                "listen_port is required.",
                status=400,
            )

        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        mapping = _find_mapping(mappings, requested_port)
        if mapping is None:
            return _api_error(
                "mapping_not_found",
                "Compatibility port mapping not found.",
                status=404,
            )

        entries = list(runtime.load_entries(storage))
        router = _build_router(runtime, entries)
        resolved_entry = _resolve_mapping_entry(router, mapping)
        updated = [item for item in mappings if item.listen_port != requested_port]
        runtime.save_compat_mappings(updated, storage)
        runtime.trigger_reload()
        data = {
            "deleted": True,
            "mapping": _serialize_mapping(mapping, runtime.load_app_config()),
        }
        if resolved_entry is not None:
            data["resolved_entry"] = _serialize_entry(resolved_entry)
        return _api_success(data)
