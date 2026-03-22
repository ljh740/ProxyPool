"""Flask-based admin application."""

import gzip
from pathlib import Path

from flask import Blueprint, Flask, redirect, request, send_from_directory
from flask_wtf.csrf import CSRFProtect

from .app_runtime import AdminRouteRuntime
from .routes.auth import register_auth_routes
from .routes.compat import register_compat_routes
from .routes.config import register_config_routes
from .routes.dashboard import register_dashboard_routes
from .routes.logs import register_log_routes
from .routes.proxies import _prepend_hop_value, register_proxy_routes

_JINJA_TEMPLATE_ROOT = Path(__file__).resolve().parent / "jinja_templates"
_PUBLIC_STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
_GZIP_MIN_SIZE = 512
_GZIP_TYPES = {"text/html", "application/json"}
__all__ = ["build_admin_app", "build_hybrid_admin_app", "_prepend_hop_value"]


def build_admin_app(
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
    """Create the admin Flask WSGI app."""

    flask_app = Flask(
        __name__,
        template_folder=str(_JINJA_TEMPLATE_ROOT),
        static_folder=None,
    )
    flask_app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_NAME="admin_web_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    flask_app.secret_key = secret_key
    CSRFProtect(flask_app)
    runtime = AdminRouteRuntime(
        get_storage=get_storage,
        secret_key=secret_key,
        cookie_name=cookie_name,
        cookie_value=cookie_value,
        cookie_max_age=cookie_max_age,
        theme_cookie_name=theme_cookie_name,
        theme_cookie_max_age=theme_cookie_max_age,
        trigger_reload_callback=trigger_reload_callback,
        proxies_per_page=proxies_per_page,
    )
    blueprint = Blueprint("admin_stage2", __name__)

    @flask_app.after_request
    def _maybe_gzip_response(resp):
        accept = request.headers.get("Accept-Encoding", "").lower()
        if "gzip" not in accept:
            return resp
        if resp.direct_passthrough or resp.headers.get("Content-Encoding"):
            return resp
        if resp.status_code < 200 or resp.status_code >= 300:
            return resp
        if resp.mimetype not in _GZIP_TYPES:
            return resp

        body = resp.get_data()
        if len(body) < _GZIP_MIN_SIZE:
            return resp

        resp.set_data(gzip.compress(body, compresslevel=6))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        resp.headers["Vary"] = "Accept-Encoding"
        return resp

    @flask_app.get("/")
    def index():
        return redirect("/dashboard", code=302)

    @flask_app.get("/static/<path:filepath>")
    def static_files(filepath):
        return send_from_directory(_PUBLIC_STATIC_ROOT, filepath)

    register_auth_routes(blueprint, runtime)
    register_dashboard_routes(blueprint, runtime)
    register_config_routes(blueprint, runtime)
    register_log_routes(blueprint, runtime)
    register_compat_routes(blueprint, runtime)
    register_proxy_routes(blueprint, runtime)

    flask_app.register_blueprint(blueprint)
    return flask_app


def build_hybrid_admin_app(legacy_app=None, **kwargs):
    """Compatibility wrapper retained during module migration."""
    return build_admin_app(**kwargs)
