"""Dashboard routes for the admin Flask app."""

from flask import render_template

from config_center import AppConfig
from i18n import get_translations, t

from .. import context


def register_dashboard_routes(blueprint, runtime):
    """Register the dashboard route."""

    @blueprint.get("/dashboard")
    def dashboard():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        app_config = AppConfig.load(storage)
        config = app_config.config_page_values()
        entries = runtime.load_entries(storage)
        log_handler = context.get_log_handler()
        log_count = len(log_handler.get_entries()) if log_handler else 0
        content = render_template(
            "dashboard/index.html",
            proxy_count=len(entries),
            log_count=log_count,
            listen_host=config.get("PROXY_HOST", "0.0.0.0"),
            listen_port=config.get("PROXY_PORT", "3128"),
            web_port=str(app_config.admin_port),
            has_auth=bool(app_config.auth_password),
            i=get_translations(ui.locale),
        )
        return runtime.build_page_response(
            title=t("dashboard_title", ui.locale),
            content=content,
            active_nav="nav_dashboard",
            ui=ui,
        )
