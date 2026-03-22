"""Log routes for the admin Flask app."""

import json

from flask import Response, request

from i18n import t

from .. import context
from .. import logs as admin_logs
from ..app_runtime import build_redirect_location


def register_log_routes(blueprint, runtime):
    """Register log viewer routes."""

    @blueprint.get("/dashboard/logs")
    def log_viewer():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        state = admin_logs.build_log_page_state(
            ui.locale,
            request.args.get("level", "ALL"),
            context.get_log_handler(),
        )
        content = admin_logs.render_log_view(state)
        return runtime.build_page_response(
            title=t("logs_title", ui.locale),
            content=content,
            active_nav="nav_logs",
            ui=ui,
            extra_scripts=admin_logs.render_log_scripts(state["level"], ui.locale),
        )

    @blueprint.get("/dashboard/logs/api")
    def log_api():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        payload = admin_logs.build_log_api_payload(
            request.args.get("level", "ALL"),
            context.get_log_handler(),
        )
        return Response(
            json.dumps(payload),
            mimetype="application/json",
        )

    @blueprint.post("/dashboard/logs/clear")
    def log_clear():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        handler = context.get_log_handler()
        if handler is not None:
            handler.clear()
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/logs",
                msg=t("logs_cleared", ui.locale),
            ),
            ui=ui,
        )
