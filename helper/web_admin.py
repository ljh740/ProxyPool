#!/usr/bin/env python3
"""Web admin application for ProxyPool."""

import os
import sys
import types

from flask import has_request_context, request

from i18n import get_translations
from admin_web import context as admin_context
from admin_web import resources as admin_resources
from admin_web.flask_hybrid import _prepend_hop_value, build_admin_app
from admin_web.i18n_utils import (
    TranslationNamespace as _TranslationNamespace,
    i18n_ns as _i18n_ns,
    toggle_locale_url as _toggle_locale_url,
)
from admin_web.rendering.layout import render_tabler_page
from admin_web.server import (
    ReloadRejectedError,
    RingBufferHandler,
    get_storage as _get_storage,
    start_admin_server as _start_admin_server,
    trigger_reload,
)
from admin_web.templating import render_template_string

__all__ = [
    "RingBufferHandler",
    "ReloadRejectedError",
    "SECRET_KEY",
    "_COOKIE_NAME",
    "_COOKIE_VALUE",
    "_COOKIE_MAX_AGE",
    "_THEME_COOKIE_NAME",
    "_THEME_COOKIE_MAX_AGE",
    "_PROXIES_PER_PAGE",
    "_TranslationNamespace",
    "_i18n_ns",
    "_toggle_locale_url",
    "_prepend_hop_value",
    "_PROXY_LIST_PAGE",
    "_PROXY_LIST_SCRIPTS",
    "_PROXY_FORM_PAGE",
    "_BATCH_FORM_PAGE",
    "app",
    "get_translations",
    "template",
    "_tabler_page",
    "start_admin_server",
    "trigger_reload",
]


SECRET_KEY = os.urandom(32).hex()

_COOKIE_NAME = "admin_session"
_COOKIE_VALUE = "authenticated"
_COOKIE_MAX_AGE = None

_THEME_COOKIE_NAME = "theme"
_THEME_COOKIE_MAX_AGE = 365 * 24 * 3600

_PROXIES_PER_PAGE = 100


class _WebAdminModule(types.ModuleType):
    _STATE_EXPORTS = {
        "_server_ref": (admin_context.get_server_ref, admin_context.set_server_ref),
        "_admin_storage": (
            admin_context.get_admin_storage,
            admin_context.set_admin_storage,
        ),
        "_log_handler": (admin_context.get_log_handler, admin_context.set_log_handler),
    }

    def __getattribute__(self, name):
        state_exports = super().__getattribute__("_STATE_EXPORTS")
        if name in state_exports:
            getter, _setter = state_exports[name]
            return getter()
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        state_exports = object.__getattribute__(self, "_STATE_EXPORTS")
        if name in state_exports:
            _getter, setter = state_exports[name]
            setter(value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _WebAdminModule


def template(source, **context):
    """Compatibility renderer for tests and legacy helpers."""
    if not has_request_context():
        with app.test_request_context("/"):
            return render_template_string(source, **context)
    return render_template_string(source, **context)


def _tabler_page(
    title,
    content,
    active_nav="",
    extra_head="",
    extra_scripts="",
    locale=None,
    theme=None,
):
    """Wrap content in the full themed HTML document."""
    request_path = ""
    request_query = ()
    if has_request_context():
        request_path = request.path
        request_query = list(request.args.items(multi=True))
    return render_tabler_page(
        title=title,
        content=content,
        active_nav=active_nav,
        extra_head=extra_head,
        extra_scripts=extra_scripts,
        locale=locale,
        theme=theme,
        request_path=request_path,
        request_query=request_query,
    )


_PROXY_LIST_PAGE = admin_resources.load_jinja_template_source("proxies/list.html")
_PROXY_LIST_SCRIPTS = admin_resources.load_template_source("proxies/scripts.js")
_PROXY_FORM_PAGE = admin_resources.load_jinja_template_source("proxies/form.html")
_BATCH_FORM_PAGE = admin_resources.load_jinja_template_source("proxies/batch_form.html")


app = build_admin_app(
    get_storage=_get_storage,
    secret_key=SECRET_KEY,
    cookie_name=_COOKIE_NAME,
    cookie_value=_COOKIE_VALUE,
    cookie_max_age=_COOKIE_MAX_AGE,
    theme_cookie_name=_THEME_COOKIE_NAME,
    theme_cookie_max_age=_THEME_COOKIE_MAX_AGE,
    trigger_reload_callback=lambda **kwargs: trigger_reload(**kwargs),
    proxies_per_page=_PROXIES_PER_PAGE,
)


def start_admin_server(host="0.0.0.0", port=None, server_ref=None, log_handler=None):
    """Start the admin web server on a daemon thread."""
    return _start_admin_server(
        app,
        host=host,
        port=port,
        server_ref=server_ref,
        log_handler=log_handler,
    )
