"""Admin i18n helper utilities."""

from urllib.parse import urlencode

from i18n import get_translations


class TranslationNamespace:
    """Lightweight dict wrapper for template dot access."""

    def __init__(self, mapping):
        self._data = mapping

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError:
            return name


def i18n_ns(locale):
    return TranslationNamespace(get_translations(locale))


def build_url(path, params=None):
    if not params:
        return path
    query = urlencode(list(params), doseq=True)
    return "%s?%s" % (path, query) if query else path


def toggle_locale_url(path, params, locale):
    next_locale = "zh" if locale == "en" else "en"
    items = params.items() if hasattr(params, "items") else (params or ())
    query = [(key, value) for key, value in items if key != "lang"]
    query.append(("lang", next_locale))
    return build_url(path, query)
