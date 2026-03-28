"""Helpers for enriching upstream entries with API-facing tags."""

import http.client
import json
import logging
import ssl
from dataclasses import replace

from proxy_server import ProxyConfig, UpstreamError, open_upstream_tunnel

COUNTRY_TAG = "country"
IPINFO_HOST = "ipinfo.io"
IPINFO_PATH = "/json"
IPINFO_PORT = 443
IPINFO_USER_AGENT = "ProxyPool/1.0"

LOGGER = logging.getLogger("proxy_tags")


def normalize_country_tag(value):
    return str(value or "").strip().upper()


def entry_country_tag(entry):
    return normalize_country_tag(entry.tags.get(COUNTRY_TAG))


def with_entry_tags(entry, tags):
    merged_tags = dict(entry.tags)
    merged_tags.update(tags)
    return replace(entry, tags=merged_tags)


def without_entry_tags(entry, *tag_keys):
    remaining_tags = dict(entry.tags)
    changed = False
    for tag_key in tag_keys:
        normalized_key = str(tag_key).strip().lower()
        if normalized_key and normalized_key in remaining_tags:
            remaining_tags.pop(normalized_key, None)
            changed = True
    if not changed:
        return entry
    return replace(entry, tags=remaining_tags)


def merge_country_tag_updates(entries, country_updates):
    updated_entries = []
    changed = False
    for entry in entries:
        country = normalize_country_tag(country_updates.get(entry.key))
        if not country:
            updated_entries.append(entry)
            continue
        updated_entry = with_entry_tags(entry, {COUNTRY_TAG: country})
        if updated_entry.tags != entry.tags:
            changed = True
        updated_entries.append(updated_entry)
    return updated_entries, changed


def resolve_entry_country_tag(app_config, entry):
    config = ProxyConfig.from_app_config(app_config, strict=False)
    sock = open_upstream_tunnel(config, entry, IPINFO_HOST, IPINFO_PORT)
    try:
        tls_context = ssl.create_default_context()
        with tls_context.wrap_socket(sock, server_hostname=IPINFO_HOST) as tls_sock:
            tls_sock.settimeout(config.connect_timeout)
            request_bytes = (
                "GET %s HTTP/1.1\r\n"
                "Host: %s\r\n"
                "Accept: application/json\r\n"
                "Connection: close\r\n"
                "User-Agent: %s\r\n"
                "\r\n"
            ) % (IPINFO_PATH, IPINFO_HOST, IPINFO_USER_AGENT)
            tls_sock.sendall(request_bytes.encode("ascii"))

            response = http.client.HTTPResponse(tls_sock)
            response.begin()
            response_body = response.read()
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if response.status != 200:
        raise UpstreamError(
            "ipinfo returned unexpected status %s for entry %s"
            % (response.status, entry.key)
        )

    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError(
            "ipinfo returned invalid JSON for entry %s" % entry.key
        ) from exc

    country = normalize_country_tag(payload.get(COUNTRY_TAG))
    if not country:
        raise UpstreamError("ipinfo response missing country for entry %s" % entry.key)
    return country


def populate_country_tags(app_config, entries, *, only_missing=True):
    country_updates = {}
    for entry in entries:
        if only_missing and entry_country_tag(entry):
            continue
        try:
            country = resolve_entry_country_tag(app_config, entry)
        except Exception as exc:
            LOGGER.warning(
                "Failed to resolve %s tag for entry %s: %s",
                COUNTRY_TAG,
                entry.key,
                exc,
            )
            continue
        country_updates[entry.key] = country
    return merge_country_tag_updates(entries, country_updates)


def populate_missing_country_tags(app_config, entries):
    return populate_country_tags(app_config, entries, only_missing=True)
