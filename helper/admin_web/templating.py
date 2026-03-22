"""Shared Jinja rendering utilities for admin views."""

from functools import lru_cache

from flask import has_app_context, has_request_context
from flask_wtf.csrf import generate_csrf
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .resources import JINJA_TEMPLATE_ROOT


def _safe_generate_csrf():
    if not has_app_context() or not has_request_context():
        return ""
    return generate_csrf()


@lru_cache(maxsize=1)
def get_template_environment():
    environment = Environment(
        loader=FileSystemLoader(str(JINJA_TEMPLATE_ROOT)),
        autoescape=select_autoescape(
            enabled_extensions=("html", "xml"),
            default_for_string=True,
        ),
    )
    environment.globals["csrf_token"] = _safe_generate_csrf
    return environment


def render_template(relative_path, **context):
    return get_template_environment().get_template(relative_path).render(**context)


def render_template_string(source, **context):
    return get_template_environment().from_string(source).render(**context)
