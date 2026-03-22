"""Shared Flask admin runtime helpers."""

from dataclasses import dataclass
from urllib.parse import urlencode

from flask import Response, redirect, request

from config_center import AppConfig
from i18n import (
    COOKIE_MAX_AGE as LANG_COOKIE_MAX_AGE,
    COOKIE_NAME as LANG_COOKIE_NAME,
    detect_locale,
)

from . import auth as admin_auth
from .rendering.layout import render_tabler_page
from persistence import (
    load_compat_port_mappings,
    load_proxy_list,
    save_compat_port_mappings,
    save_proxy_list,
)

@dataclass(frozen=True)
class RequestUIState:
    """Resolved per-request UI state."""

    locale: str
    persist_locale: bool
    theme: str
    persist_theme: bool


def build_redirect_location(path, **params):
    """Append truthy query params to a path."""
    query = urlencode([(key, value) for key, value in params.items() if value])
    return "%s?%s" % (path, query) if query else path


class AdminRouteRuntime:
    """Shared state and helpers used by route registrars."""

    def __init__(
        self,
        *,
        get_storage,
        secret_key,
        cookie_name,
        cookie_value,
        cookie_max_age,
        theme_cookie_name,
        theme_cookie_max_age,
        trigger_reload_callback,
        proxies_per_page,
    ):
        self._get_storage = get_storage
        self.secret_key = secret_key
        self.cookie_name = cookie_name
        self.cookie_value = cookie_value
        self.cookie_max_age = cookie_max_age
        self.theme_cookie_name = theme_cookie_name
        self.theme_cookie_max_age = theme_cookie_max_age
        self._trigger_reload_callback = trigger_reload_callback
        self.proxies_per_page = proxies_per_page

    def trigger_reload(self, **kwargs):
        self._trigger_reload_callback(**kwargs)

    def get_storage(self):
        return self._get_storage()

    def load_app_config(self):
        return AppConfig.load(self.get_storage())

    def load_entries(self, storage=None):
        client = self.get_storage() if storage is None else storage
        return load_proxy_list(client)

    def save_entries(self, entries, storage=None):
        client = self.get_storage() if storage is None else storage
        save_proxy_list(client, entries)

    def load_compat_mappings(self, storage=None):
        client = self.get_storage() if storage is None else storage
        return load_compat_port_mappings(client)

    def save_compat_mappings(self, mappings, storage=None):
        client = self.get_storage() if storage is None else storage
        save_compat_port_mappings(client, mappings)

    def resolve_locale(self):
        query_lang = request.args.get("lang")
        cookie_lang = request.cookies.get(LANG_COOKIE_NAME)
        accept_header = request.headers.get("Accept-Language", "")
        locale = detect_locale(query_lang, cookie_lang, accept_header)
        persist = bool(query_lang and query_lang == locale)
        return locale, persist

    def resolve_theme(self):
        query_theme = request.args.get("theme")
        cookie_theme = request.cookies.get(self.theme_cookie_name)
        theme = "dark" if query_theme == "dark" or (
            query_theme is None and cookie_theme == "dark"
        ) else "light"
        persist = bool(query_theme and query_theme in ("light", "dark"))
        return theme, persist

    def resolve_ui_state(self):
        locale, persist_locale = self.resolve_locale()
        theme, persist_theme = self.resolve_theme()
        return RequestUIState(
            locale=locale,
            persist_locale=persist_locale,
            theme=theme,
            persist_theme=persist_theme,
        )

    def apply_ui_cookies(self, resp, *, ui):
        if ui.persist_locale:
            resp.set_cookie(
                LANG_COOKIE_NAME,
                ui.locale,
                path="/",
                max_age=LANG_COOKIE_MAX_AGE,
            )
        if ui.persist_theme:
            resp.set_cookie(
                self.theme_cookie_name,
                ui.theme,
                path="/",
                max_age=self.theme_cookie_max_age,
            )
        return resp

    def redirect(self, location, *, ui):
        resp = redirect(location, code=302)
        return self.apply_ui_cookies(resp, ui=ui)

    def build_page_response(
        self,
        *,
        title,
        content,
        active_nav,
        ui,
        extra_scripts="",
    ):
        body = render_tabler_page(
            title,
            content,
            active_nav=active_nav,
            extra_scripts=extra_scripts,
            locale=ui.locale,
            theme=ui.theme,
            request_path=request.path,
            request_query=list(request.args.items(multi=True)),
        )
        return self.apply_ui_cookies(
            Response(body, mimetype="text/html"),
            ui=ui,
        )

    def is_authenticated_request(self):
        return admin_auth.is_authenticated_cookie(
            request.cookies.get(self.cookie_name),
            secret_key=self.secret_key,
            cookie_name=self.cookie_name,
            cookie_value=self.cookie_value,
        )

    def require_admin(self):
        ui = self.resolve_ui_state()
        app_config = self.load_app_config()
        if not app_config.admin_password:
            return self.redirect("/setup", ui=ui)
        if self.is_authenticated_request():
            return None

        return self.redirect("/login", ui=ui)
