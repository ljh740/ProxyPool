"""Filesystem-backed admin templates and assets."""

from functools import lru_cache
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
JINJA_TEMPLATE_ROOT = PACKAGE_ROOT / "jinja_templates"
STATIC_ROOT = PACKAGE_ROOT / "static"


@lru_cache(maxsize=None)
def load_template_source(relative_path):
    return (TEMPLATE_ROOT / relative_path).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def load_jinja_template_source(relative_path):
    return (JINJA_TEMPLATE_ROOT / relative_path).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def load_static_source(relative_path):
    return (STATIC_ROOT / relative_path).read_text(encoding="utf-8")
