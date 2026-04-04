"""Compatibility-port routes for the admin Flask app."""

from compat_ports import (
    COMPAT_PORT_MAX,
    COMPAT_PORT_MIN,
    TARGET_TYPE_ENTRY_KEY,
    TARGET_TYPE_SESSION_NAME,
    TARGET_TYPES,
    CompatPortMapping,
)
from flask import render_template, request
from i18n import get_translations, t

from .. import resources as admin_resources
from ..app_runtime import build_redirect_location
from ..http import is_ajax_request as _is_ajax_request
from ..http import json_response as _json_response


def _compat_form_state(mapping=None):
    if mapping is None:
        return {
            "listen_port": str(COMPAT_PORT_MIN),
            "original_listen_port": "",
            "target_type": TARGET_TYPE_SESSION_NAME,
            "target_value": "",
            "enabled": True,
            "note": "",
            "edit_mode": False,
        }
    return {
        "listen_port": str(mapping.listen_port),
        "original_listen_port": str(mapping.listen_port),
        "target_type": mapping.target_type,
        "target_value": mapping.target_value,
        "enabled": mapping.enabled,
        "note": mapping.note,
        "edit_mode": True,
    }


def _build_compat_form_context(locale, entries, *, form_state=None, error=""):
    state = _compat_form_state()
    if form_state is not None:
        state.update(form_state)

    translations = get_translations(locale)
    target_type = state["target_type"]
    target_hint = (
        t("compat_form_target_hint_entry_key", locale)
        if target_type == TARGET_TYPE_ENTRY_KEY
        else t("compat_form_target_hint_session_name", locale)
    )
    return {
        "entry_options": entries,
        "target_types": [
            {
                "value": TARGET_TYPE_ENTRY_KEY,
                "label": t("compat_target_type_entry_key", locale),
            },
            {
                "value": TARGET_TYPE_SESSION_NAME,
                "label": t("compat_target_type_session_name", locale),
            },
        ],
        "port_min": COMPAT_PORT_MIN,
        "port_max": COMPAT_PORT_MAX,
        "listen_port": state["listen_port"],
        "original_listen_port": state["original_listen_port"],
        "target_type": target_type,
        "target_value": state["target_value"],
        "enabled": state["enabled"],
        "note": state["note"],
        "edit_mode": state["edit_mode"],
        "submit_label": t(
            "compat_form_update" if state["edit_mode"] else "compat_form_save",
            locale,
        ),
        "target_hint": target_hint,
        "error": error,
        "i": translations,
    }


def _build_compat_page_context(locale, mappings, entries, *, form_state=None, error=""):
    context = _build_compat_form_context(
        locale,
        entries,
        form_state=form_state,
        error=error,
    )
    context["mappings"] = mappings
    return context


def _render_compat_form_html(locale, entries, *, form_state=None, error=""):
    return render_template(
        "compat/_form.html",
        **_build_compat_form_context(
            locale,
            entries,
            form_state=form_state,
            error=error,
        ),
    )


def _render_compat_mappings_html(locale, mappings):
    return render_template(
        "compat/_mappings.html",
        mappings=mappings,
        i=get_translations(locale),
    )


def _compat_ajax_payload(locale, entries, mappings, *, form_state=None, error="", message=None):
    payload = {
        "form_html": _render_compat_form_html(
            locale,
            entries,
            form_state=form_state,
            error=error,
        ),
        "mappings_html": _render_compat_mappings_html(locale, mappings),
    }
    if message:
        payload["message"] = message
    if error:
        payload["error"] = error
    return payload


def _compat_ajax_response(locale, entries, mappings, *, form_state=None, error="", message=None, status=200):
    return _json_response(
        {
            "ok": not bool(error),
            **_compat_ajax_payload(
                locale,
                entries,
                mappings,
                form_state=form_state,
                error=error,
                message=message,
            ),
        },
        status=status,
    )


def _compat_form_error_response(runtime, ui, *, entries, mappings, form_state, error_message, status=400):
    if _is_ajax_request():
        return _compat_ajax_response(
            ui.locale,
            entries,
            mappings,
            form_state=form_state,
            error=error_message,
            status=status,
        )
    return _render_compat_page(
        runtime,
        ui,
        mappings=mappings,
        entries=entries,
        form_state=form_state,
        error=error_message,
    )


def _render_compat_page(runtime, ui, *, mappings, entries, form_state=None, error=""):
    content = render_template(
        "compat/page.html",
        **_build_compat_page_context(
            ui.locale,
            mappings,
            entries,
            form_state=form_state,
            error=error,
        ),
    )
    return runtime.build_page_response(
        title=t("compat_title", ui.locale),
        content=content,
        active_nav="nav_compat",
        ui=ui,
        extra_scripts=admin_resources.load_template_source("compat/scripts.js"),
    )


def register_compat_routes(blueprint, runtime):
    """Register compatibility-port routes."""

    @blueprint.get("/dashboard/compat")
    def compat_page():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        entries = list(runtime.load_entries(storage))
        edit_value = request.args.get("edit", "").strip()
        edit_mapping = None
        if edit_value:
            try:
                edit_mapping = next(
                    (mapping for mapping in mappings if mapping.listen_port == int(edit_value)),
                    None,
                )
            except ValueError:
                edit_mapping = None

        return _render_compat_page(
            runtime,
            ui,
            mappings=mappings,
            entries=entries,
            form_state=_compat_form_state(edit_mapping),
        )

    @blueprint.post("/dashboard/compat/save")
    def compat_save():
        guard = runtime.require_admin()
        if guard is not None:
            if _is_ajax_request():
                return _json_response({"ok": False, "error": "unauthorized"}, status=401)
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        entries = list(runtime.load_entries(storage))
        listen_port = request.form.get("listen_port", "").strip()
        original_listen_port = request.form.get("original_listen_port", "").strip()
        target_type = request.form.get("target_type", TARGET_TYPE_SESSION_NAME).strip()
        target_value = request.form.get("target_value", "").strip()
        enabled = request.form.get("enabled", "0") == "1"
        note = request.form.get("note", "").strip()
        form_state = {
            "listen_port": listen_port,
            "original_listen_port": original_listen_port,
            "target_type": target_type,
            "target_value": target_value,
            "enabled": enabled,
            "note": note,
            "edit_mode": bool(original_listen_port),
        }

        original_port = None
        if original_listen_port:
            try:
                original_port = int(original_listen_port)
            except ValueError:
                return _compat_form_error_response(
                    runtime,
                    ui,
                    entries=entries,
                    mappings=mappings,
                    form_state=form_state,
                    error_message=t("compat_not_found", ui.locale),
                    status=404,
                )

            if not any(mapping.listen_port == original_port for mapping in mappings):
                return _compat_form_error_response(
                    runtime,
                    ui,
                    entries=entries,
                    mappings=mappings,
                    form_state=form_state,
                    error_message=t("compat_not_found", ui.locale),
                    status=404,
                )

        if target_type not in TARGET_TYPES:
            return _compat_form_error_response(
                runtime,
                ui,
                entries=entries,
                mappings=mappings,
                form_state=form_state,
                error_message=t("compat_invalid_target_type", ui.locale),
            )

        if target_type == TARGET_TYPE_ENTRY_KEY and not any(entry.key == target_value for entry in entries):
            return _compat_form_error_response(
                runtime,
                ui,
                entries=entries,
                mappings=mappings,
                form_state=form_state,
                error_message=t("compat_entry_missing", ui.locale),
            )

        if (
            original_port is not None
            and listen_port.isdigit()
            and int(listen_port) != original_port
            and any(mapping.listen_port == int(listen_port) for mapping in mappings)
        ):
            return _compat_form_error_response(
                runtime,
                ui,
                entries=entries,
                mappings=mappings,
                form_state=form_state,
                error_message=t("compat_port_conflict", ui.locale),
                status=409,
            )

        try:
            mapping = CompatPortMapping(
                listen_port=int(listen_port),
                target_type=target_type,
                target_value=target_value,
                enabled=enabled,
                note=note,
            )
        except (TypeError, ValueError):
            return _compat_form_error_response(
                runtime,
                ui,
                entries=entries,
                mappings=mappings,
                form_state=form_state,
                error_message=t(
                    "compat_invalid_mapping",
                    ui.locale,
                    min=COMPAT_PORT_MIN,
                    max=COMPAT_PORT_MAX,
                ),
            )

        updated = [
            item for item in mappings if item.listen_port != mapping.listen_port and item.listen_port != original_port
        ]
        updated.append(mapping)
        updated.sort(key=lambda item: item.listen_port)
        runtime.save_compat_mappings(updated, storage)
        runtime.trigger_reload()
        success_message = t("compat_saved", ui.locale)
        if _is_ajax_request():
            return _compat_ajax_response(
                ui.locale,
                entries,
                updated,
                message=success_message,
            )
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/compat",
                msg=success_message,
                type="success",
            ),
            ui=ui,
        )

    @blueprint.post("/dashboard/compat/<listen_port>/delete")
    def compat_delete(listen_port):
        guard = runtime.require_admin()
        if guard is not None:
            if _is_ajax_request():
                return _json_response({"ok": False, "error": "unauthorized"}, status=401)
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        mappings = list(runtime.load_compat_mappings(storage))
        entries = list(runtime.load_entries(storage))
        try:
            port = int(listen_port)
        except ValueError:
            if _is_ajax_request():
                return _compat_ajax_response(
                    ui.locale,
                    entries,
                    mappings,
                    error=t("compat_not_found", ui.locale),
                    status=404,
                )
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/compat",
                    msg=t("compat_not_found", ui.locale),
                    type="error",
                ),
                ui=ui,
            )

        updated = [mapping for mapping in mappings if mapping.listen_port != port]
        if len(updated) == len(mappings):
            if _is_ajax_request():
                return _compat_ajax_response(
                    ui.locale,
                    entries,
                    mappings,
                    error=t("compat_not_found", ui.locale),
                    status=404,
                )
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/compat",
                    msg=t("compat_not_found", ui.locale),
                    type="error",
                ),
                ui=ui,
            )

        runtime.save_compat_mappings(updated, storage)
        runtime.trigger_reload()
        success_message = t("compat_deleted", ui.locale)
        if _is_ajax_request():
            return _compat_ajax_response(
                ui.locale,
                entries,
                updated,
                message=success_message,
            )
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/compat",
                msg=success_message,
                type="success",
            ),
            ui=ui,
        )
