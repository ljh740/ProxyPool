"""Shared HTTP helpers for admin routes."""

import json

from flask import Response, request


def is_ajax_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def json_response(payload, status=200):
    return Response(
        json.dumps(payload),
        status=status,
        mimetype="application/json",
    )
