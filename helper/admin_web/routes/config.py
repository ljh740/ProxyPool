"""Configuration routes for the admin Flask app."""

import logging

from flask import render_template, request

from config_center import (
    AppConfig,
    CONFIG_PAGE_FIELDS,
    CONFIG_PAGE_GROUPED,
    CONFIG_PAGE_GROUPS,
    validate_config_form,
)
from i18n import get_translations, t
from persistence import save_config

from ..app_runtime import build_redirect_location
from ..server import ReloadRejectedError

LOGGER = logging.getLogger("web_admin")


_CONFIG_ERROR_KEYS = {
    "required": "config_error_required",
    "integer": "config_error_integer",
    "between": "config_error_between",
    "at_least": "config_error_at_least",
    "non_negative": "config_error_non_negative",
    "number": "config_error_number",
    "positive": "config_error_positive",
    "one_of": "config_error_one_of",
}
_CONFIG_RELOAD_ERROR_KEYS = {
    "auth_password_missing": "config_reload_error_auth_password_missing",
}


def _config_field_label_key(env_key):
    return "config_field_%s_label" % env_key.lower()


def _config_field_help_key(env_key):
    return "config_field_%s_help" % env_key.lower()


def _config_option_label_key(env_key, option):
    return "config_option_%s_%s" % (env_key.lower(), option.lower())


def _localized_config_field_label(field, translations):
    return translations.get(_config_field_label_key(field.env_key), field.label)


def _localized_config_field_help(field, translations):
    return translations.get(_config_field_help_key(field.env_key), field.help_text)


def _localized_config_option_label(field, option, translations):
    return translations.get(_config_option_label_key(field.env_key, option), option)


def _build_config_field_labels(translations):
    return {
        field.env_key: _localized_config_field_label(field, translations)
        for field in CONFIG_PAGE_FIELDS
    }


def _build_config_option_labels(translations):
    return {
        field.env_key: {
            option: _localized_config_option_label(field, option, translations)
            for option in field.options
        }
        for field in CONFIG_PAGE_FIELDS
        if field.options
    }


def _format_config_error(locale, code, **kwargs):
    return t(_CONFIG_ERROR_KEYS[code], locale, **kwargs)


def _format_reload_error(locale, exc):
    key = _CONFIG_RELOAD_ERROR_KEYS.get(exc.code)
    if key:
        return t(key, locale)
    return str(exc)


def _build_config_sections(locale, config):
    translations = get_translations(locale)
    sections = []
    for group in CONFIG_PAGE_GROUPS:
        group_fields = CONFIG_PAGE_GROUPED.get(group.key, ())
        if not group_fields:
            continue

        title_key = "config_group_%s" % group.key
        desc_key = "%s_desc" % title_key
        sections.append(
            {
                "key": group.key,
                "icon": group.icon,
                "collapsed": group.collapsed,
                "title": translations.get(title_key, group.key),
                "description": translations.get(desc_key, ""),
                "fields": [
                    {
                        "env_key": field.env_key,
                        "label": _localized_config_field_label(field, translations),
                        "input_type": field.input_type,
                        "value": config.get(field.env_key, field.default),
                        "help_text": _localized_config_field_help(field, translations),
                        "options": [
                            {
                                "value": option,
                                "label": _localized_config_option_label(
                                    field, option, translations
                                ),
                            }
                            for option in field.options
                        ],
                    }
                    for field in group_fields
                ],
            }
        )
    return sections


def register_config_routes(blueprint, runtime):
    """Register configuration routes."""

    @blueprint.get("/dashboard/config")
    def config_page():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        storage = runtime.get_storage()
        config = AppConfig.load(storage).config_page_values()
        content = render_template(
            "config/form.html",
            sections=_build_config_sections(ui.locale, config),
            i=get_translations(ui.locale),
        )
        return runtime.build_page_response(
            title=t("config_title", ui.locale),
            content=content,
            active_nav="nav_configuration",
            ui=ui,
        )

    @blueprint.post("/dashboard/config/save")
    def config_save():
        guard = runtime.require_admin()
        if guard is not None:
            return guard

        ui = runtime.resolve_ui_state()
        translations = get_translations(ui.locale)
        form_data = {
            field.env_key: request.form.get(field.env_key, "")
            for field in CONFIG_PAGE_FIELDS
        }
        clean, errors = validate_config_form(
            form_data,
            field_labels=_build_config_field_labels(translations),
            option_labels=_build_config_option_labels(translations),
            error_formatter=lambda code, **kwargs: _format_config_error(
                ui.locale, code, **kwargs
            ),
        )
        if errors:
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/config",
                    msg="; ".join(errors),
                    type="error",
                ),
                ui=ui,
            )

        storage = runtime.get_storage()
        persisted_values = AppConfig.load(storage).runtime_values()
        persisted_values.update(clean)
        save_config(storage, persisted_values)
        LOGGER.info("Configuration saved via admin panel")
        try:
            runtime.trigger_reload(raise_on_error=True)
        except ReloadRejectedError as exc:
            LOGGER.warning("Configuration saved but reload was rejected: %s", exc)
            return runtime.redirect(
                build_redirect_location(
                    "/dashboard/config",
                    msg=_format_reload_error(ui.locale, exc),
                    type="error",
                ),
                ui=ui,
            )
        return runtime.redirect(
            build_redirect_location(
                "/dashboard/config",
                msg=t("config_saved", ui.locale),
                type="success",
            ),
            ui=ui,
        )
