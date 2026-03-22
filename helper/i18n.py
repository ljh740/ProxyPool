#!/usr/bin/env python3
"""Internationalisation utilities for the ProxyPool admin panel.

Loads locale dictionaries from JSON files in ``helper/locales/`` and
provides a lightweight translation lookup used by the admin templates
and route handlers.

Supported locales are auto-discovered from the ``locales/`` directory.
The active locale for each request is determined by:

1. ``?lang=xx`` query parameter  (highest priority, also sets cookie)
2. ``lang`` cookie               (persists user preference)
3. ``Accept-Language`` header     (browser default)
4. Fallback to ``DEFAULT_LOCALE`` (``en``)

Usage in route handlers::

    from i18n import get_locale, t

    @app.route("/example")
    def example():
        locale = get_locale()
        greeting = t("nav_dashboard", locale)
"""

import json
import logging
import os
import re

LOGGER = logging.getLogger("i18n")

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "zh")
COOKIE_NAME = "lang"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 year

# ---------------------------------------------------------------------------
# Locale loading
# ---------------------------------------------------------------------------

_locales: dict[str, dict[str, str]] = {}


def _locale_dir() -> str:
    """Return absolute path to the locales directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


def _load_locales() -> None:
    """Load all JSON locale files from the ``locales/`` directory.

    Called once at import time.  Each file is named ``<code>.json``
    (e.g. ``en.json``, ``zh.json``) and contains a flat dict of
    translation-key -> translated-string.
    """
    locale_path = _locale_dir()
    if not os.path.isdir(locale_path):
        LOGGER.warning("Locale directory not found: %s", locale_path)
        return

    for filename in os.listdir(locale_path):
        if not filename.endswith(".json"):
            continue
        code = filename[:-5]  # strip .json
        filepath = os.path.join(locale_path, filename)
        try:
            with open(filepath, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                _locales[code] = data
                LOGGER.debug("Loaded locale %s (%d keys)", code, len(data))
        except (json.JSONDecodeError, OSError) as exc:
            LOGGER.warning("Failed to load locale %s: %s", code, exc)


_load_locales()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def available_locales() -> tuple[str, ...]:
    """Return locale codes that have been successfully loaded."""
    return tuple(sorted(_locales.keys()))


def t(key: str, locale: str | None = None, **kwargs: object) -> str:
    """Translate *key* in *locale*, falling back to the default locale.

    Positional interpolation placeholders use ``{name}`` syntax::

        t("proxies_deleted_count", "en", count=3)
        # -> "Deleted 3 proxies"

    Returns the raw key if no translation is found (fail-open).
    """
    if locale is None:
        locale = DEFAULT_LOCALE
    table = _locales.get(locale) or _locales.get(DEFAULT_LOCALE) or {}
    text = table.get(key)
    if text is None:
        # Fallback chain: requested -> default -> raw key
        fallback = _locales.get(DEFAULT_LOCALE, {})
        text = fallback.get(key, key)
    if kwargs:
        try:
            text = text.format(**{k: str(v) for k, v in kwargs.items()})
        except (KeyError, IndexError):
            pass  # return partially formatted
    return text


def get_translations(locale: str | None = None) -> dict[str, str]:
    """Return the full translation dict for *locale*.

    Merges the default locale as a base, then overlays the requested
    locale so that missing keys fall back gracefully.
    """
    if locale is None:
        locale = DEFAULT_LOCALE
    base = dict(_locales.get(DEFAULT_LOCALE, {}))
    if locale != DEFAULT_LOCALE:
        overlay = _locales.get(locale, {})
        base.update(overlay)
    return base


def detect_locale(query_lang: str | None = None,
                  cookie_lang: str | None = None,
                  accept_header: str | None = None) -> str:
    """Determine the best locale from request signals.

    Priority:

    1. Explicit query parameter (``?lang=zh``)
    2. Persisted cookie value
    3. ``Accept-Language`` header parse (first supported match)
    4. ``DEFAULT_LOCALE``
    """
    # 1. Query parameter
    if query_lang and query_lang in _locales:
        return query_lang

    # 2. Cookie
    if cookie_lang and cookie_lang in _locales:
        return cookie_lang

    # 3. Accept-Language header
    if accept_header:
        locale = _parse_accept_language(accept_header)
        if locale:
            return locale

    return DEFAULT_LOCALE


_ACCEPT_LANG_RE = re.compile(r"([a-zA-Z]{2})(?:-[a-zA-Z]+)?(?:\s*;\s*q=([0-9.]+))?")


def _parse_accept_language(header: str) -> str | None:
    """Extract the first supported locale from an Accept-Language header.

    Parses quality values and returns the highest-priority match.
    """
    candidates: list[tuple[float, str]] = []
    for match in _ACCEPT_LANG_RE.finditer(header):
        code = match.group(1).lower()
        try:
            quality = float(match.group(2)) if match.group(2) else 1.0
        except ValueError:
            quality = 0.0
        if code in _locales:
            candidates.append((quality, code))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]
