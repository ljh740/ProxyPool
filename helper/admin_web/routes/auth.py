"""Authentication routes for the admin Flask app."""

import hmac

from flask import current_app, render_template, request, session

from config_center import AppConfig
from i18n import get_translations, t
from persistence import save_config

from .. import auth as admin_auth
from ..app_runtime import build_redirect_location


def _issue_admin_session(runtime, ui, destination):
    resp = runtime.redirect(destination, ui=ui)
    for header_value in admin_auth.build_signed_cookie_headers(
        runtime.cookie_name,
        runtime.cookie_value,
        secret_key=runtime.secret_key,
        path="/",
        max_age=runtime.cookie_max_age,
        samesite="Strict",
    ):
        resp.headers.add("Set-Cookie", header_value)
    return resp


def register_auth_routes(blueprint, runtime):
    """Register login/logout routes."""

    @blueprint.get("/setup")
    def setup_page():
        ui = runtime.resolve_ui_state()
        app_config = runtime.load_app_config()
        if app_config.admin_password:
            destination = "/dashboard" if runtime.is_authenticated_request() else "/login"
            return runtime.redirect(destination, ui=ui)

        content = render_template(
            "auth/setup.html",
            error=request.args.get("error", ""),
            message=request.args.get("msg", ""),
            i=get_translations(ui.locale),
        )
        return runtime.build_page_response(
            title=t("setup_title", ui.locale),
            content=content,
            active_nav=None,
            ui=ui,
        )

    @blueprint.post("/setup")
    def setup_submit():
        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        app_config = AppConfig.load(storage)
        if app_config.admin_password:
            destination = "/dashboard" if runtime.is_authenticated_request() else "/login"
            return runtime.redirect(destination, ui=ui)

        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not password:
            return runtime.redirect(
                build_redirect_location(
                    "/setup",
                    error=t("setup_error_required", ui.locale),
                ),
                ui=ui,
            )
        if password != confirm_password:
            return runtime.redirect(
                build_redirect_location(
                    "/setup",
                    error=t("setup_error_mismatch", ui.locale),
                ),
                ui=ui,
            )

        updated_values = app_config.runtime_values()
        updated_values["ADMIN_PASSWORD"] = password
        save_config(storage, updated_values)
        return _issue_admin_session(runtime, ui, "/dashboard")

    @blueprint.get("/login")
    def login_page():
        ui = runtime.resolve_ui_state()
        app_config = AppConfig.load(runtime.get_storage())
        if not app_config.admin_password:
            return runtime.redirect(
                build_redirect_location(
                    "/setup",
                    msg=t("login_setup_required", ui.locale),
                ),
                ui=ui,
            )
        if runtime.is_authenticated_request():
            return runtime.redirect("/dashboard", ui=ui)

        content = render_template(
            "auth/login.html",
            error=request.args.get("error", ""),
            i=get_translations(ui.locale),
        )
        return runtime.build_page_response(
            title=t("login_title", ui.locale),
            content=content,
            active_nav=None,
            ui=ui,
        )

    @blueprint.get("/logout")
    def logout():
        ui = runtime.resolve_ui_state()
        session.clear()
        destination = (
            "/setup" if not AppConfig.load(runtime.get_storage()).admin_password else "/login"
        )
        resp = runtime.redirect(destination, ui=ui)
        resp.delete_cookie(runtime.cookie_name, path="/")
        resp.delete_cookie(current_app.config["SESSION_COOKIE_NAME"], path="/")
        return resp

    @blueprint.post("/login")
    def login_submit():
        ui = runtime.resolve_ui_state()
        app_config = AppConfig.load(runtime.get_storage())
        if not app_config.admin_password:
            return runtime.redirect(
                build_redirect_location(
                    "/setup",
                    msg=t("login_setup_required", ui.locale),
                ),
                ui=ui,
            )

        provided = request.form.get("password", "")
        if not hmac.compare_digest(provided, app_config.admin_password):
            return runtime.redirect(
                build_redirect_location(
                    "/login",
                    error=t("login_invalid", ui.locale),
                ),
                ui=ui,
            )

        return _issue_admin_session(runtime, ui, "/dashboard")
