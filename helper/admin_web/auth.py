"""Authentication helpers shared by admin web surfaces."""

import base64
import hashlib
import hmac
from http.cookies import SimpleCookie
import pickle


def _decode_signed_cookie(cookie_data, secret_key):
    if not cookie_data:
        return None

    payload = cookie_data.encode("utf-8")
    if not payload.startswith(b"!") or b"?" not in payload:
        return None

    sig, encoded = payload.split(b"?", 1)
    expected = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            encoded,
            digestmod=hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(sig[1:], expected):
        return None

    try:
        return pickle.loads(base64.b64decode(encoded))
    except Exception:
        return None


def is_authenticated_cookie(cookie_data, *, secret_key, cookie_name, cookie_value):
    """Return True when the raw cookie payload matches the admin session."""
    if not cookie_data:
        return False
    if cookie_data == cookie_value:
        return True

    decoded = _decode_signed_cookie(cookie_data, secret_key)
    if not decoded or len(decoded) != 2:
        return False

    name, value = decoded
    return name == cookie_name and value == cookie_value


def build_signed_cookie_headers(
    name,
    value,
    *,
    secret_key,
    path="/",
    max_age=None,
    httponly=True,
    samesite="Strict",
):
    """Build Set-Cookie headers using the legacy signed cookie format."""
    encoded = base64.b64encode(
        pickle.dumps((name, value), protocol=pickle.HIGHEST_PROTOCOL)
    )
    signature = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            encoded,
            digestmod=hashlib.sha256,
        ).digest()
    )
    signed_value = b"!%s?%s" % (signature, encoded)

    cookie = SimpleCookie()
    cookie[name] = signed_value.decode("utf-8")
    morsel = cookie[name]
    morsel["path"] = path
    if max_age is not None:
        morsel["max-age"] = str(max_age)
    if httponly:
        morsel["httponly"] = True
    if samesite:
        morsel["samesite"] = samesite
    return [morsel.OutputString()]
