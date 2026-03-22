"""Log viewer helpers extracted from the admin web module."""

import json

from i18n import get_translations, t

from .i18n_utils import TranslationNamespace
from .resources import load_template_source
from .server import DEFAULT_LOG_BUFFER_SIZE
from .templating import render_template, render_template_string

VALID_LOG_LEVELS = ("ALL", "INFO", "WARNING", "ERROR")


def normalize_level(level):
    candidate = (level or "ALL").upper()
    return candidate if candidate in VALID_LOG_LEVELS else "ALL"


def build_log_page_state(locale, level, log_handler):
    normalized_level = normalize_level(level)
    if log_handler is None:
        entries = []
        entry_count = 0
        max_buffer = DEFAULT_LOG_BUFFER_SIZE
    else:
        entries = log_handler.get_entries(normalized_level)
        entry_count = len(log_handler.buffer)
        max_buffer = log_handler.buffer.maxlen

    namespace_data = dict(get_translations(locale))
    namespace_data["logs_buffer_stats"] = t(
        "logs_buffer_stats", locale, count=entry_count, max=max_buffer
    )
    return {
        "entries": entries,
        "entry_count": entry_count,
        "max_buffer": max_buffer,
        "level": normalized_level,
        "i": TranslationNamespace(namespace_data),
    }


def render_log_view(state):
    return render_template("logs/view.html", **state)


def render_log_scripts(level, locale):
    script = render_template_string(
        load_template_source("logs/scripts.js"),
        level=normalize_level(level),
        logs_empty_text_json=json.dumps(
            t("logs_empty", locale), ensure_ascii=False
        ),
        logs_buffer_stats_template_json=json.dumps(
            t("logs_buffer_stats", locale, count="{count}", max="{max}"),
            ensure_ascii=False,
        ),
        logs_last_updated_template_json=json.dumps(
            t("logs_last_updated", locale, time="{time}"),
            ensure_ascii=False,
        ),
        logs_refresh_failed_template_json=json.dumps(
            t("logs_refresh_failed", locale, time="{time}"),
            ensure_ascii=False,
        ),
    )
    return "<script>\n%s\n</script>" % script


def build_log_api_payload(level, log_handler):
    normalized_level = normalize_level(level)
    if log_handler is None:
        entries = []
        total = 0
        max_buffer = DEFAULT_LOG_BUFFER_SIZE
    else:
        entries = log_handler.get_entries(normalized_level)
        total = len(log_handler.buffer)
        max_buffer = log_handler.buffer.maxlen
    return {
        "entries": entries,
        "count": total,
        "max_buffer": max_buffer,
        "level": normalized_level,
    }
