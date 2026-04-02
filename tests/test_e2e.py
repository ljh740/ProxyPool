"""End-to-end HTTP tests for the ProxyPool Web Admin.

Uses ``wsgiref.simple_server`` + ``requests.Session`` to exercise the
full admin WSGI app over real HTTP, with an in-memory storage fixture and
in-process WSGI server on a random port.
"""

import importlib
import logging
import os
import re
import sys
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

import requests

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

config_center = importlib.import_module("config_center")
upstream_pool = importlib.import_module("upstream_pool")
proxy_server = importlib.import_module("proxy_server")
persistence = importlib.import_module("persistence")
web_admin = importlib.import_module("web_admin")
proxy_routes = importlib.import_module("admin_web.routes.proxies")

AppConfig = config_center.AppConfig
UpstreamEntry = upstream_pool.UpstreamEntry
UpstreamHop = upstream_pool.UpstreamHop
compute_entry_key = upstream_pool.compute_entry_key
ProxyConfig = proxy_server.ProxyConfig
RingBufferHandler = web_admin.RingBufferHandler
ReloadRejectedError = web_admin.ReloadRejectedError

TEST_PASSWORD = "test-admin-pw"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MemoryStorage:

    def __init__(self):
        self._values = {}

    def get(self, state_key):
        return self._values.get(state_key)

    def set(self, state_key, value):
        self._values[state_key] = value

    def delete(self, state_key):
        self._values.pop(state_key, None)

    def clear(self):
        self._values.clear()

    def close(self):
        pass


def _make_hop(host="proxy.example.com", port=10001, scheme="socks5"):
    return UpstreamHop(
        scheme=scheme, host=host, port=port,
        username="user", password="pass",
    )


def _make_entry(
    key="test_1",
    host="proxy.example.com",
    port=10001,
    source_tag="manual",
    tags=None,
):
    hop = _make_hop(host=host, port=port)
    return UpstreamEntry(
        key=key, label=f"{host}:{port}", hops=(hop,),
        source_tag=source_tag, in_random_pool=True,
        tags=tags or {},
    )


class _QuietHandler(WSGIRequestHandler):
    """Suppress the per-request log output from wsgiref."""

    def log_message(self, format, *args):  # noqa: A002
        pass


def _extract_session_cookie(resp):
    """Extract the signed admin_session cookie value from a Set-Cookie header.

    The login response uses a quoted signed session cookie. We parse the
    raw header so the session can be populated manually and consistently.
    """
    raw = resp.headers.get("Set-Cookie", "")
    m = re.search(r'admin_session="([^"]+)"', raw)
    if m:
        return m.group(1)
    return None


def _extract_csrf_token(resp):
    """Extract the hidden CSRF token from an HTML response."""
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# E2E Test Suite
# ---------------------------------------------------------------------------


class TestE2E(unittest.TestCase):
    """HTTP-level end-to-end tests for the Web Admin panel."""

    _server = None
    _thread = None
    _storage = None
    _original_server_ref = None
    _original_log_handler = None

    @classmethod
    def setUpClass(cls):
        # 1) Initialize in-memory admin storage
        cls._storage = _MemoryStorage()

        # 2) Log handler with test entries
        cls._log_handler = RingBufferHandler(maxlen=100)
        cls._log_handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test_e2e_fixture")
        logger.addHandler(cls._log_handler)
        logger.setLevel(logging.DEBUG)
        logger.info("Test log message 1")
        logger.warning("Test log message 2")
        logger.error("Test log message 3")
        logger.removeHandler(cls._log_handler)

        # 3) Server reference mock
        server_ref = MagicMock()
        server_ref.config = ProxyConfig(
            listen_host="0.0.0.0", listen_port=3128,
            auth_password="test-secret", auth_realm="Proxy",
            connect_timeout=20.0, connect_retries=3,
            relay_timeout=120.0, loopback_host_mode="auto",
            host_loopback_address="host.docker.internal",
            running_in_docker=False,
        )
        server_ref.router = MagicMock()
        server_ref.router.storage = cls._storage

        # 4) Inject into web_admin module
        cls._original_server_ref = web_admin._server_ref
        cls._original_log_handler = web_admin._log_handler
        web_admin._server_ref = server_ref
        web_admin._log_handler = cls._log_handler

        # 5) Seed initial config into storage
        persistence.save_config(
            cls._storage,
            {
                "AUTH_PASSWORD": "test-secret",
                "ADMIN_PASSWORD": TEST_PASSWORD,
            },
        )

        # 6) Start WSGI server on port 0
        cls._server = make_server(
            "127.0.0.1", 0, web_admin.app,
            handler_class=_QuietHandler,
        )
        cls._port = cls._server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls._port}"
        cls._thread = threading.Thread(
            target=cls._server.serve_forever,
            daemon=True,
        )
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls._server:
            cls._server.shutdown()
        if cls._storage:
            cls._storage.close()
        # Restore module state
        web_admin._server_ref = cls._original_server_ref
        web_admin._log_handler = cls._original_log_handler

    def setUp(self):
        self._reset_storage()
        self.session = requests.Session()
        # Patch trigger_reload to a no-op — it is tested separately in
        # test_web_admin.py.  Without this patch, trigger_reload would
        # replace server_ref.router with a real Router (which lacks our
        # test storage), breaking subsequent persistence access.
        self._reload_patcher = patch.object(web_admin, "trigger_reload")
        self._reload_patcher.start()

    def tearDown(self):
        self._reload_patcher.stop()
        self.session.close()

    # --- helpers ---

    def _login(self):
        """POST /login with the correct password and populate session cookie.

        The signed session cookie is extracted from the raw
        ``Set-Cookie`` header and injected into the session jar to keep
        the fixture deterministic across client implementations.
        """
        resp = self._post_with_csrf(
            "/login",
            data={"password": TEST_PASSWORD},
        )
        cookie_val = _extract_session_cookie(resp)
        if cookie_val:
            self.session.cookies.set(
                "admin_session", cookie_val,
                domain="127.0.0.1", path="/",
            )
        return resp

    def _post_with_csrf(self, submit_path, *, data=None, form_path=None, allow_redirects=False, headers=None):
        """Fetch a CSRF token from the form page, then submit the POST request."""
        resolved_form_path = form_path or self._resolve_form_path(submit_path)
        csrf_token = self._csrf_token_for(resolved_form_path)
        payload = dict(data or {})
        payload["csrf_token"] = csrf_token
        return self.session.post(
            self.base_url + submit_path,
            data=payload,
            headers=headers,
            allow_redirects=allow_redirects,
        )

    def _csrf_token_for(self, form_path):
        form_resp = self.session.get(self.base_url + form_path)
        self.assertEqual(form_resp.status_code, 200)
        csrf_token = _extract_csrf_token(form_resp)
        self.assertIsNotNone(csrf_token, "expected CSRF token on %s" % form_path)
        return csrf_token

    def _resolve_form_path(self, submit_path):
        """Map POST-only endpoints back to the GET page that renders the form."""
        explicit_paths = {
            "/dashboard/config/save": "/dashboard/config",
            "/dashboard/compat/save": "/dashboard/compat",
            "/dashboard/logs/clear": "/dashboard/logs",
            "/dashboard/proxies/import": "/dashboard/proxies/import",
            "/dashboard/proxies/batch/generate": "/dashboard/proxies/batch",
            "/dashboard/proxies/batch/clear": "/dashboard/proxies",
            "/dashboard/proxies/batch/delete": "/dashboard/proxies",
            "/dashboard/proxies/batch/toggle-pool": "/dashboard/proxies",
        }
        if submit_path in explicit_paths:
            return explicit_paths[submit_path]
        if submit_path.endswith("/delete") or submit_path.endswith("/toggle-pool"):
            if submit_path.startswith("/dashboard/compat/"):
                return "/dashboard/compat"
            if submit_path.startswith("/dashboard/proxies/"):
                return "/dashboard/proxies"
        return submit_path

    def _reset_storage(self, *, admin_password=TEST_PASSWORD):
        """Clear the test storage and re-seed config."""
        self._storage.clear()
        persistence.save_config(
            self._storage,
            {
                "AUTH_PASSWORD": "test-secret",
                "ADMIN_PASSWORD": admin_password,
            },
        )

    # ====================================================================
    # Tier 1: Auth
    # ====================================================================

    def test_root_redirects_to_dashboard(self):
        resp = self.session.get(self.base_url + "/", allow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_unauthenticated_redirects_to_login(self):
        resp = self.session.get(
            self.base_url + "/dashboard", allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_uninitialized_dashboard_redirects_to_setup(self):
        self._reset_storage(admin_password="")

        resp = self.session.get(
            self.base_url + "/dashboard", allow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/setup", resp.headers["Location"])

    def test_setup_page_renders_when_admin_password_missing(self):
        self._reset_storage(admin_password="")

        resp = self.session.get(self.base_url + "/setup")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("confirm_password", resp.text)

    def test_login_redirects_to_setup_when_admin_password_missing(self):
        self._reset_storage(admin_password="")

        resp = self.session.get(self.base_url + "/login", allow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/setup", resp.headers["Location"])

    def test_setup_success_persists_admin_password(self):
        self._reset_storage(admin_password="")

        resp = self._post_with_csrf(
            "/setup",
            data={
                "password": TEST_PASSWORD,
                "confirm_password": TEST_PASSWORD,
            },
        )

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        self.assertEqual(
            persistence.load_config(self._storage)["ADMIN_PASSWORD"],
            TEST_PASSWORD,
        )
        self.assertIn("admin_session=", resp.headers.get("Set-Cookie", ""))

    def test_login_page_renders(self):
        resp = self.session.get(self.base_url + "/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("password", resp.text.lower())

    def test_login_missing_csrf_rejected(self):
        resp = self.session.post(
            self.base_url + "/login",
            data={"password": TEST_PASSWORD},
            allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)

    def test_login_wrong_password(self):
        resp = self._post_with_csrf(
            "/login",
            data={"password": "wrong-pw"},
        )
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/login", location)
        self.assertIn("error=", location)

    def test_login_success(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])
        # Set-Cookie header should contain the signed admin_session cookie
        self.assertIn("admin_session=", resp.headers.get("Set-Cookie", ""))

    def test_logout(self):
        self._login()
        resp = self.session.get(
            self.base_url + "/logout", allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        raw_set_cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("admin_web_session=", raw_set_cookie)
        # Clear the cookie from session to simulate browser behavior
        self.session.cookies.clear()
        # After logout, dashboard should redirect to login
        resp2 = self.session.get(
            self.base_url + "/dashboard", allow_redirects=False,
        )
        self.assertEqual(resp2.status_code, 302)
        self.assertIn("/login", resp2.headers["Location"])

    # ====================================================================
    # Tier 2: Dashboard
    # ====================================================================

    def test_dashboard_renders(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard")
        self.assertEqual(resp.status_code, 200)
        # Dashboard has stat cards with specific structure (not just CSS class)
        self.assertIn("pp-stat-value", resp.text)
        self.assertIn("/dashboard/config", resp.text)

    def test_dashboard_shows_counts(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard")
        self.assertEqual(resp.status_code, 200)
        # The dashboard shows proxy count and log count as pp-stat-value
        # Log count should reflect our 3 test log entries
        self.assertIn(">3<", resp.text)

    # ====================================================================
    # Tier 3: Config
    # ====================================================================

    def test_config_page_renders_grouped(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/config")
        self.assertEqual(resp.status_code, 200)
        # Should contain group cards for proxy_server and router
        self.assertIn("group-proxy_server", resp.text)
        self.assertIn("group-router", resp.text)

    def test_config_page_renders_zh_translations(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/config?lang=zh")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('lang="zh"', resp.text)
        self.assertIn("监听地址", resp.text)
        self.assertIn("代理服务器的监听地址。", resp.text)
        self.assertIn("随机池前缀", resp.text)
        self.assertNotIn("Listen Host", resp.text)
        self.assertNotIn("Routing Mode", resp.text)

    def test_config_save_success(self):
        self._login()
        self._reset_storage()
        form = {f.env_key: f.default for f in config_center.CONFIG_PAGE_FIELDS}
        form["AUTH_PASSWORD"] = "new-secret"
        form["COUNTRY_DETECT_MAX_WORKERS"] = "6"
        resp = self._post_with_csrf(
            "/dashboard/config/save",
            data=form,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/config", resp.headers["Location"])
        # Verify flash message is success (no error= param)
        self.assertNotIn("type=error", resp.headers["Location"])
        # Verify persisted in storage
        loaded = persistence.load_config(self._storage)
        self.assertEqual(loaded.get("AUTH_PASSWORD"), "new-secret")
        self.assertEqual(loaded.get("COUNTRY_DETECT_MAX_WORKERS"), "6")
        self.assertEqual(loaded.get("ADMIN_PASSWORD"), TEST_PASSWORD)

    def test_config_save_invalid_rejected(self):
        self._login()
        form = {f.env_key: f.default for f in config_center.CONFIG_PAGE_FIELDS}
        form["PROXY_PORT"] = "99999"  # invalid port
        resp = self._post_with_csrf(
            "/dashboard/config/save",
            data=form,
        )
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/dashboard/config", location)
        # Should have error flash (encoded as msg= with type=error)
        self.assertIn("msg=", location)

    def test_config_save_reload_rejection_shows_explicit_error(self):
        self._login()
        self._reset_storage()
        form = {f.env_key: f.default for f in config_center.CONFIG_PAGE_FIELDS}
        form["AUTH_PASSWORD"] = "new-secret"
        web_admin.trigger_reload.side_effect = ReloadRejectedError(
            "auth_password_missing",
            "AUTH_PASSWORD must be configured before enabling the proxy listener.",
        )

        resp = self._post_with_csrf(
            "/dashboard/config/save",
            data=form,
        )

        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/dashboard/config", location)
        self.assertIn("type=error", location)
        message = parse_qs(urlsplit(location).query).get("msg", [""])[0]
        self.assertIn("Auth Password is empty", message)
        loaded = persistence.load_config(self._storage)
        self.assertEqual(loaded.get("AUTH_PASSWORD"), "new-secret")

    def test_config_save_invalid_uses_zh_error_message(self):
        self._login()
        self.session.get(self.base_url + "/dashboard/config?lang=zh")
        form = {f.env_key: f.default for f in config_center.CONFIG_PAGE_FIELDS}
        form["PROXY_PORT"] = "99999"
        resp = self._post_with_csrf(
            "/dashboard/config/save",
            data=form,
            form_path="/dashboard/config?lang=zh",
        )
        self.assertEqual(resp.status_code, 302)
        query = parse_qs(urlsplit(resp.headers["Location"]).query)
        self.assertIn("msg", query)
        self.assertIn("监听端口", query["msg"][0])
        self.assertIn("1 到 65535", query["msg"][0])
        self.assertNotIn("Listen Port", query["msg"][0])

    def test_config_save_ajax_returns_json_without_redirect(self):
        self._login()
        self._reset_storage()
        form = {f.env_key: f.default for f in config_center.CONFIG_PAGE_FIELDS}
        form["AUTH_PASSWORD"] = "ajax-secret"

        resp = self._post_with_csrf(
            "/dashboard/config/save",
            data=form,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertIn("saved", payload["message"].lower())
        self.assertEqual(persistence.load_config(self._storage)["AUTH_PASSWORD"], "ajax-secret")

    def test_config_save_ajax_invalid_returns_json_error(self):
        self._login()
        form = {f.env_key: f.default for f in config_center.CONFIG_PAGE_FIELDS}
        form["PROXY_PORT"] = "99999"

        resp = self._post_with_csrf(
            "/dashboard/config/save",
            data=form,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(resp.status_code, 400)
        payload = resp.json()
        self.assertFalse(payload["ok"])
        self.assertIn("port", payload["error"].lower())

    # ====================================================================
    # Tier 3.5: Compatibility Ports
    # ====================================================================

    def test_compat_page_renders(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/compat")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("33100", resp.text)
        self.assertIn("/dashboard/compat/save", resp.text)

    def test_compat_save_persists_mapping(self):
        self._login()
        self._reset_storage()
        entry = _make_entry("compat_entry", "compat.example.com", 10001)
        persistence.save_proxy_list(self._storage, [entry])

        resp = self._post_with_csrf(
            "/dashboard/compat/save",
            data={
                "listen_port": "33100",
                "target_type": "entry_key",
                "target_value": "compat_entry",
                "enabled": "1",
                "note": "uc bridge",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/compat", resp.headers["Location"])
        mappings = persistence.load_compat_port_mappings(self._storage)
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].listen_port, 33100)
        self.assertEqual(mappings[0].target_type, "entry_key")
        self.assertEqual(mappings[0].target_value, "compat_entry")

    def test_compat_edit_replaces_original_mapping_when_port_changes(self):
        self._login()
        self._reset_storage()
        persistence.save_compat_port_mappings(
            self._storage,
            [
                {
                    "listen_port": 33100,
                    "target_type": "session_name",
                    "target_value": "browser-a",
                    "enabled": True,
                    "note": "before",
                }
            ],
        )

        resp = self._post_with_csrf(
            "/dashboard/compat/save",
            data={
                "original_listen_port": "33100",
                "listen_port": "33101",
                "target_type": "session_name",
                "target_value": "browser-b",
                "enabled": "1",
                "note": "after",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mappings = persistence.load_compat_port_mappings(self._storage)
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].listen_port, 33101)
        self.assertEqual(mappings[0].target_value, "browser-b")
        self.assertEqual(mappings[0].note, "after")

    def test_compat_edit_rejects_port_conflict(self):
        self._login()
        self._reset_storage()
        persistence.save_compat_port_mappings(
            self._storage,
            [
                {
                    "listen_port": 33100,
                    "target_type": "session_name",
                    "target_value": "browser-a",
                    "enabled": True,
                    "note": "",
                },
                {
                    "listen_port": 33101,
                    "target_type": "session_name",
                    "target_value": "browser-b",
                    "enabled": True,
                    "note": "",
                },
            ],
        )

        resp = self._post_with_csrf(
            "/dashboard/compat/save",
            data={
                "original_listen_port": "33100",
                "listen_port": "33101",
                "target_type": "session_name",
                "target_value": "browser-a-updated",
                "enabled": "1",
                "note": "collision",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already assigned", resp.text)
        mappings = persistence.load_compat_port_mappings(self._storage)
        self.assertEqual({mapping.listen_port for mapping in mappings}, {33100, 33101})

    def test_compat_delete_removes_mapping(self):
        self._login()
        self._reset_storage()
        persistence.save_compat_port_mappings(
            self._storage,
            [
                {
                    "listen_port": 33100,
                    "target_type": "session_name",
                    "target_value": "browser-a",
                    "enabled": True,
                    "note": "",
                }
            ],
        )

        resp = self._post_with_csrf(
            "/dashboard/compat/33100/delete",
            form_path="/dashboard/compat",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/compat", resp.headers["Location"])
        self.assertEqual(persistence.load_compat_port_mappings(self._storage), [])

    def test_compat_save_ajax_returns_html_partials(self):
        self._login()
        self._reset_storage()
        entry = _make_entry("compat_entry", "compat.example.com", 10001)
        persistence.save_proxy_list(self._storage, [entry])

        resp = self._post_with_csrf(
            "/dashboard/compat/save",
            data={
                "listen_port": "33100",
                "target_type": "entry_key",
                "target_value": "compat_entry",
                "enabled": "1",
                "note": "ajax bridge",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertIn("form_html", payload)
        self.assertIn("mappings_html", payload)
        self.assertIn("compat_entry", payload["mappings_html"])
        self.assertEqual(persistence.load_compat_port_mappings(self._storage)[0].target_value, "compat_entry")

    def test_compat_delete_ajax_returns_html_partials(self):
        self._login()
        self._reset_storage()
        persistence.save_compat_port_mappings(
            self._storage,
            [
                {
                    "listen_port": 33100,
                    "target_type": "session_name",
                    "target_value": "browser-a",
                    "enabled": True,
                    "note": "",
                }
            ],
        )

        resp = self._post_with_csrf(
            "/dashboard/compat/33100/delete",
            form_path="/dashboard/compat",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertIn("form_html", payload)
        self.assertIn("mappings_html", payload)
        self.assertEqual(persistence.load_compat_port_mappings(self._storage), [])

    # ====================================================================
    # Tier 4: Proxy CRUD
    # ====================================================================

    def test_proxies_empty_state(self):
        self._login()
        self._reset_storage()
        resp = self.session.get(self.base_url + "/dashboard/proxies")
        self.assertEqual(resp.status_code, 200)
        # Empty state contains the empty-state icon
        self.assertIn("ti-server-off", resp.text)

    def test_proxy_source_filter_repages_from_full_dataset(self):
        self._login()
        self._reset_storage()
        entries = [
            _make_entry(f"auto_{idx}", f"auto-{idx}.example.com", 11000 + idx, source_tag="auto")
            for idx in range(100)
        ]
        entries.extend(
            [
                _make_entry("manual_1", "manual-one.example.com", 12001, source_tag="manual"),
                _make_entry("manual_2", "manual-two.example.com", 12002, source_tag="manual"),
            ]
        )
        persistence.save_proxy_list(self._storage, entries)

        resp = self.session.get(self.base_url + "/dashboard/proxies")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("manual-one.example.com", resp.text)

        filtered = self.session.get(self.base_url + "/dashboard/proxies?source=manual")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("manual-one.example.com", filtered.text)
        self.assertIn("manual-two.example.com", filtered.text)
        self.assertNotIn("auto-0.example.com", filtered.text)

    def test_proxy_source_filter_empty_result_keeps_filter_tabs(self):
        self._login()
        self._reset_storage()
        persistence.save_proxy_list(
            self._storage,
            [_make_entry("manual_only", "manual-only.example.com", 12001, source_tag="manual")],
        )

        filtered = self.session.get(self.base_url + "/dashboard/proxies?source=auto")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn('data-filter="all"', filtered.text)
        self.assertIn('data-filter="manual"', filtered.text)
        self.assertIn('data-filter="auto"', filtered.text)
        self.assertIn("ti-filter-off", filtered.text)
        self.assertNotIn("ti-server-off", filtered.text)
        self.assertNotIn("manual-only.example.com", filtered.text)

    def test_proxy_country_filter_limits_rows(self):
        self._login()
        self._reset_storage()
        persistence.save_proxy_list(
            self._storage,
            [
                _make_entry("us_only", "us-only.example.com", 12001, tags={"country": "US"}),
                _make_entry("de_only", "de-only.example.com", 12002, tags={"country": "DE"}),
                _make_entry("no_country", "no-country.example.com", 12003),
            ],
        )

        filtered = self.session.get(self.base_url + "/dashboard/proxies?country=DE")

        self.assertEqual(filtered.status_code, 200)
        self.assertIn("de-only.example.com", filtered.text)
        self.assertNotIn("us-only.example.com", filtered.text)
        self.assertNotIn("no-country.example.com", filtered.text)
        self.assertIn('value="DE" selected', filtered.text)

    def test_ajax_country_detection_job_updates_storage(self):
        self._login()
        self._reset_storage()
        persistence.save_proxy_list(
            self._storage,
            [
                _make_entry("detect-us", "detect-us.example.com", 12001),
                _make_entry("detect-de", "detect-de.example.com", 12002),
                _make_entry("keep-fr", "keep-fr.example.com", 12003, tags={"country": "FR"}),
            ],
        )
        csrf_token = self._csrf_token_for("/dashboard/proxies")

        with patch.object(
            proxy_routes,
            "resolve_entry_country_tag",
            side_effect=["US", "DE"],
        ):
            start_resp = self.session.post(
                self.base_url + "/dashboard/proxies/tags/country/detect",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={
                    "csrf_token": csrf_token,
                    "detect_missing_only": "1",
                },
            )
            self.assertEqual(start_resp.status_code, 200)
            start_payload = start_resp.json()
            self.assertTrue(start_payload["ok"])
            self.assertEqual(start_payload["job"]["total"], 2)
            job_id = start_payload["job"]["job_id"]

            status_payload = None
            for _ in range(40):
                status_resp = self.session.get(
                    self.base_url + f"/dashboard/proxies/tags/country/detect/{job_id}",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                self.assertEqual(status_resp.status_code, 200)
                status_payload = status_resp.json()
                if status_payload["job"]["status"] == "completed":
                    break
                time.sleep(0.05)

        self.assertIsNotNone(status_payload)
        self.assertEqual(status_payload["job"]["status"], "completed")
        self.assertEqual(status_payload["job"]["success_count"], 2)
        self.assertEqual(status_payload["job"]["updated_count"], 2)
        saved_entries = {entry.key: entry for entry in persistence.load_proxy_list(self._storage)}
        self.assertEqual(saved_entries["detect-us"].tags, {"country": "US"})
        self.assertEqual(saved_entries["detect-de"].tags, {"country": "DE"})
        self.assertEqual(saved_entries["keep-fr"].tags, {"country": "FR"})

    def test_ajax_country_detection_persists_each_result_as_it_completes(self):
        self._login()
        self._reset_storage()
        persistence.save_proxy_list(
            self._storage,
            [
                _make_entry("detect-us", "detect-us.example.com", 12001),
                _make_entry("detect-de", "detect-de.example.com", 12002),
            ],
        )
        csrf_token = self._csrf_token_for("/dashboard/proxies")

        def _resolve_country(_app_config, entry):
            if entry.key == "detect-us":
                time.sleep(0.05)
                return "US"
            time.sleep(0.35)
            return "DE"

        with patch.object(
            proxy_routes,
            "resolve_entry_country_tag",
            side_effect=_resolve_country,
        ):
            start_resp = self.session.post(
                self.base_url + "/dashboard/proxies/tags/country/detect",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={
                    "csrf_token": csrf_token,
                    "detect_missing_only": "1",
                },
            )
            self.assertEqual(start_resp.status_code, 200)
            job_id = start_resp.json()["job"]["job_id"]

            saw_partial_persist = False
            for _ in range(20):
                saved_entries = {entry.key: entry for entry in persistence.load_proxy_list(self._storage)}
                if (
                    saved_entries["detect-us"].tags == {"country": "US"}
                    and saved_entries["detect-de"].tags == {}
                ):
                    saw_partial_persist = True
                    break
                time.sleep(0.05)

            status_payload = None
            for _ in range(40):
                status_resp = self.session.get(
                    self.base_url + f"/dashboard/proxies/tags/country/detect/{job_id}",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                self.assertEqual(status_resp.status_code, 200)
                status_payload = status_resp.json()
                if status_payload["job"]["status"] == "completed":
                    break
                time.sleep(0.05)

        self.assertTrue(saw_partial_persist)
        self.assertIsNotNone(status_payload)
        self.assertEqual(status_payload["job"]["status"], "completed")
        saved_entries = {entry.key: entry for entry in persistence.load_proxy_list(self._storage)}
        self.assertEqual(saved_entries["detect-us"].tags, {"country": "US"})
        self.assertEqual(saved_entries["detect-de"].tags, {"country": "DE"})

    def test_add_proxy_success(self):
        self._login()
        self._reset_storage()
        resp = self._post_with_csrf(
            "/dashboard/proxies/add",
            data={
                "scheme": "http",
                "host": "new-proxy.example.com",
                "port": "8080",
                "username": "",
                "password": "",
                "prepend_hop": "socks5://gate.example.com:1080",
                "in_random_pool": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/proxies", resp.headers["Location"])
        # Verify entry exists in storage
        entries = persistence.load_proxy_list(self._storage)
        self.assertTrue(
            any(
                len(e.hops) == 2
                and e.first_hop.host == "gate.example.com"
                and e.last_hop.host == "new-proxy.example.com"
                for e in entries
            ),
        )

    def test_add_proxy_invalid_port(self):
        self._login()
        resp = self._post_with_csrf(
            "/dashboard/proxies/add",
            data={
                "scheme": "http",
                "host": "bad-proxy.example.com",
                "port": "99999",
                "username": "",
                "password": "",
            },
        )
        self.assertEqual(resp.status_code, 200)
        # Should re-render form with error message about port
        self.assertIn("bad-proxy.example.com", resp.text)

    def test_edit_proxy_page_shows_copyable_chain_uri(self):
        self._login()
        self._reset_storage()
        hops = (
            UpstreamHop("socks5", "gate.old", 1080, "", ""),
            UpstreamHop("http", "edit.example.com", 10001, "u", "p"),
        )
        entry = UpstreamEntry(
            key=compute_entry_key(hops),
            label="gate.old:1080 -> edit.example.com:10001",
            hops=hops,
            source_tag="manual",
            in_random_pool=True,
        )
        persistence.save_proxy_list(self._storage, [entry])

        resp = self.session.get(self.base_url + f"/dashboard/proxies/{entry.key}/edit")

        self.assertEqual(resp.status_code, 200)
        self.assertIn('data-proxy-summary-card', resp.text)
        self.assertIn('id="proxy-entry-key"', resp.text)
        self.assertIn('id="copy-entry-key-btn"', resp.text)
        self.assertIn(entry.key, resp.text)
        self.assertIn('id="proxy-chain-uri"', resp.text)
        self.assertIn('id="copy-chain-uri-btn"', resp.text)
        self.assertIn(
            "socks5://gate.old:1080 | http://u:p@edit.example.com:10001",
            resp.text,
        )

    def test_edit_proxy(self):
        self._login()
        self._reset_storage()
        # Seed an entry
        entry = UpstreamEntry(
            key="edit_me",
            label="gate.old:1080 -> edit.example.com:10001",
            hops=(
                UpstreamHop("socks5", "gate.old", 1080, "", ""),
                UpstreamHop("http", "edit.example.com", 10001, "", ""),
            ),
            source_tag="manual",
            in_random_pool=True,
        )
        persistence.save_proxy_list(self._storage, [entry])

        # POST edit
        resp = self._post_with_csrf(
            "/dashboard/proxies/edit_me/edit",
            form_path="/dashboard/proxies/edit_me/edit",
            data={
                "scheme": "http",
                "host": "edited.example.com",
                "port": "10002",
                "username": "u",
                "password": "p",
                "prepend_hop": "socks5://gate.new:2080",
                "in_random_pool": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        # Verify updated in storage
        entries = persistence.load_proxy_list(self._storage)
        self.assertTrue(
            any(
                len(e.hops) == 2
                and e.first_hop.host == "gate.new"
                and e.last_hop.host == "edited.example.com"
                and e.last_hop.username == "u"
                for e in entries
            ),
        )

    def test_edit_proxy_rejects_duplicate_configuration(self):
        self._login()
        self._reset_storage()
        first_hops = (_make_hop("first.example.com", 10001),)
        second_hops = (_make_hop("second.example.com", 10002),)
        first = UpstreamEntry(
            key=compute_entry_key(first_hops),
            label="first.example.com:10001",
            hops=first_hops,
            source_tag="manual",
            in_random_pool=True,
        )
        second = UpstreamEntry(
            key=compute_entry_key(second_hops),
            label="second.example.com:10002",
            hops=second_hops,
            source_tag="manual",
            in_random_pool=True,
        )
        persistence.save_proxy_list(self._storage, [first, second])

        resp = self._post_with_csrf(
            f"/dashboard/proxies/{first.key}/edit",
            form_path=f"/dashboard/proxies/{first.key}/edit",
            data={
                "scheme": "socks5",
                "host": "second.example.com",
                "port": "10002",
                "username": "user",
                "password": "pass",
                "prepend_hop": "",
                "in_random_pool": "1",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already exists", resp.text)
        entries = persistence.load_proxy_list(self._storage)
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(1 for entry in entries if entry.key == second.key), 1)

    def test_delete_proxy(self):
        self._login()
        self._reset_storage()
        entry = _make_entry("del_me", "delete.example.com", 10001)
        persistence.save_proxy_list(self._storage, [entry])

        resp = self._post_with_csrf(
            "/dashboard/proxies/del_me/delete",
            form_path="/dashboard/proxies",
        )
        self.assertEqual(resp.status_code, 302)
        entries = persistence.load_proxy_list(self._storage)
        self.assertFalse(any(e.key == "del_me" for e in entries))

    def test_import_proxy_form_renders(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/proxies/import")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("proxy_list_text", resp.text)

    def test_import_proxy_list_adds_manual_entries(self):
        self._login()
        self._reset_storage()
        manual = _make_entry("manual_keep", "manual-keep.example.com", 10001, source_tag="manual")
        auto = _make_entry("auto_keep", "auto-keep.example.com", 10002, source_tag="auto")
        persistence.save_proxy_list(self._storage, [manual, auto])

        resp = self._post_with_csrf(
            "/dashboard/proxies/import",
            form_path="/dashboard/proxies/import",
            data={
                "default_scheme": "socks5",
                "default_username": "fallback-user",
                "default_password": "fallback-pass",
                "proxy_list_text": "\n".join(
                    [
                        "# comment",
                        "import-one.example.com:20001",
                        "http://127.0.0.1:30001 | import-two.example.com:20002",
                    ]
                ),
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/proxies", resp.headers["Location"])

        entries = persistence.load_proxy_list(self._storage)
        self.assertEqual(len(entries), 4)
        imported = [
            entry
            for entry in entries
            if entry.last_hop.host in {"import-one.example.com", "import-two.example.com"}
        ]
        self.assertEqual(len(imported), 2)
        self.assertTrue(all(entry.source_tag == "manual" for entry in imported))
        self.assertTrue(all(entry.in_random_pool for entry in imported))
        first = next(entry for entry in imported if entry.last_hop.host == "import-one.example.com")
        self.assertEqual(first.last_hop.scheme, "socks5")
        self.assertEqual(first.last_hop.username, "fallback-user")
        self.assertEqual(first.last_hop.password, "fallback-pass")
        second = next(entry for entry in imported if entry.last_hop.host == "import-two.example.com")
        self.assertEqual(second.chain_length, 2)
        self.assertEqual(second.hops[0].scheme, "http")
        self.assertEqual(second.last_hop.username, "fallback-user")
        self.assertTrue(any(entry.key == manual.key for entry in entries))
        self.assertTrue(any(entry.key == auto.key for entry in entries))

    def test_import_proxy_list_rejects_invalid_line(self):
        self._login()
        self._reset_storage()
        resp = self._post_with_csrf(
            "/dashboard/proxies/import",
            form_path="/dashboard/proxies/import",
            data={
                "default_scheme": "http",
                "default_username": "",
                "default_password": "",
                "proxy_list_text": "not-a-valid-proxy-line",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Line 1", resp.text)
        self.assertIn("Expected host:port", resp.text)

    def test_import_proxy_list_check_then_commit_saves_only_valid_entries(self):
        self._login()
        self._reset_storage()
        auto = _make_entry("auto_keep", "auto-keep.example.com", 10002, source_tag="auto")
        persistence.save_proxy_list(self._storage, [auto])
        csrf_token = self._csrf_token_for("/dashboard/proxies/import")

        with patch.object(
            proxy_routes,
            "_probe_import_entry",
            side_effect=[(True, ""), (False, "dial timeout")],
        ):
            start_resp = self.session.post(
                self.base_url + "/dashboard/proxies/import/check",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={
                    "csrf_token": csrf_token,
                    "default_scheme": "socks5",
                    "default_username": "fallback-user",
                    "default_password": "fallback-pass",
                    "proxy_list_text": "\n".join(
                        [
                            "check-ok.example.com:21001",
                            "check-bad.example.com:21002",
                        ]
                    ),
                },
            )
            self.assertEqual(start_resp.status_code, 200)
            start_payload = start_resp.json()
            self.assertTrue(start_payload["ok"])
            job_id = start_payload["job"]["job_id"]

            status_payload = None
            for _ in range(40):
                status_resp = self.session.get(
                    self.base_url + f"/dashboard/proxies/import/check/{job_id}",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                self.assertEqual(status_resp.status_code, 200)
                status_payload = status_resp.json()
                if status_payload["job"]["status"] == "completed":
                    break
                time.sleep(0.05)

        self.assertIsNotNone(status_payload)
        self.assertEqual(status_payload["job"]["status"], "completed")
        self.assertEqual(status_payload["job"]["success_count"], 1)
        self.assertEqual(status_payload["job"]["failure_count"], 1)

        commit_resp = self.session.post(
            self.base_url + "/dashboard/proxies/import/commit",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={
                "csrf_token": csrf_token,
                "job_id": job_id,
            },
        )
        self.assertEqual(commit_resp.status_code, 200)
        commit_payload = commit_resp.json()
        self.assertTrue(commit_payload["ok"])
        entries = persistence.load_proxy_list(self._storage)
        self.assertEqual(len(entries), 2)
        self.assertTrue(any(entry.last_hop.host == "check-ok.example.com" for entry in entries))
        self.assertFalse(any(entry.last_hop.host == "check-bad.example.com" for entry in entries))
        self.assertTrue(any(entry.key == auto.key for entry in entries))

    def test_ajax_toggle_pool_returns_json_without_redirect(self):
        self._login()
        self._reset_storage()
        entry = _make_entry("ajax_toggle", "ajax-toggle.example.com", 10001, source_tag="manual")
        persistence.save_proxy_list(self._storage, [entry])
        csrf_token = self._csrf_token_for("/dashboard/proxies")

        resp = self.session.post(
            self.base_url + f"/dashboard/proxies/{entry.key}/toggle-pool",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={"csrf_token": csrf_token},
            allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["entry"]["in_random_pool"])
        saved_entry = persistence.load_proxy_list(self._storage)[0]
        self.assertFalse(saved_entry.in_random_pool)

    # ====================================================================
    # Tier 5: Batch
    # ====================================================================

    def test_batch_generate_form_renders(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/proxies/batch")
        self.assertEqual(resp.status_code, 200)
        # Batch form contains port range inputs
        self.assertIn("port_first", resp.text.lower())

    def test_batch_generate(self):
        self._login()
        self._reset_storage()
        resp = self._post_with_csrf(
            "/dashboard/proxies/batch/generate",
            form_path="/dashboard/proxies/batch",
            data={
                "scheme": "http",
                "host": "batch.example.com",
                "username": "u",
                "password": "p",
                "port_first": "20001",
                "port_last": "20003",
                "prepend_hop": "",
                "cycle_first_hop": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/proxies", resp.headers["Location"])
        # Should have 3 auto entries
        entries = persistence.load_proxy_list(self._storage)
        auto_entries = [e for e in entries if e.source_tag == "auto"]
        self.assertEqual(len(auto_entries), 3)

    def test_batch_clear_auto(self):
        self._login()
        self._reset_storage()
        # Seed mixed entries
        manual = _make_entry("m1", "manual.example.com", 10001, source_tag="manual")
        auto = _make_entry("a1", "auto.example.com", 10002, source_tag="auto")
        persistence.save_proxy_list(self._storage, [manual, auto])

        resp = self._post_with_csrf(
            "/dashboard/proxies/batch/clear",
            form_path="/dashboard/proxies",
        )
        self.assertEqual(resp.status_code, 302)
        entries = persistence.load_proxy_list(self._storage)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].source_tag, "manual")

    def test_batch_delete_selected(self):
        self._login()
        self._reset_storage()
        first = _make_entry("d1", "delete-1.example.com", 10001, source_tag="manual")
        second = _make_entry("d2", "delete-2.example.com", 10002, source_tag="manual")
        third = _make_entry("d3", "keep.example.com", 10003, source_tag="manual")
        persistence.save_proxy_list(self._storage, [first, second, third])

        resp = self._post_with_csrf(
            "/dashboard/proxies/batch/delete",
            form_path="/dashboard/proxies",
            data={"keys": [first.key, second.key]},
        )
        self.assertEqual(resp.status_code, 302)
        entries = persistence.load_proxy_list(self._storage)
        self.assertEqual([entry.key for entry in entries], [third.key])

    def test_batch_toggle_pool_selected(self):
        self._login()
        self._reset_storage()
        first = _make_entry("p1", "pool-1.example.com", 10001, source_tag="manual")
        second = _make_entry("p2", "pool-2.example.com", 10002, source_tag="manual")
        persistence.save_proxy_list(self._storage, [first, second])

        resp = self._post_with_csrf(
            "/dashboard/proxies/batch/toggle-pool",
            form_path="/dashboard/proxies",
            data={"keys": [first.key], "pool_state": "off"},
        )
        self.assertEqual(resp.status_code, 302)
        entries = {entry.key: entry for entry in persistence.load_proxy_list(self._storage)}
        self.assertFalse(entries[first.key].in_random_pool)
        self.assertTrue(entries[second.key].in_random_pool)

    # ====================================================================
    # Tier 6: Logs
    # ====================================================================

    def test_logs_page_renders(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/logs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Test log message", resp.text)

    def test_logs_page_renders_zh_translations(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/logs?lang=zh")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('lang="zh"', resp.text)
        self.assertIn("自动刷新: 5秒", resp.text)
        self.assertIn("清除缓冲区", resp.text)
        self.assertIn("暂无日志", resp.text)
        self.assertIn("全部", resp.text)
        self.assertIn("上次更新", resp.text)
        self.assertIn("刷新失败", resp.text)
        self.assertNotIn("No log entries", resp.text)
        self.assertNotIn("Refresh failed", resp.text)

    def test_logs_api_json(self):
        self._login()
        resp = self.session.get(self.base_url + "/dashboard/logs/api")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("entries", data)
        self.assertIsInstance(data["entries"], list)
        self.assertGreater(len(data["entries"]), 0)

    # ====================================================================
    # Tier 7: Theme / Language
    # ====================================================================

    def test_theme_dark_cookie(self):
        self._login()
        resp = self.session.get(
            self.base_url + "/dashboard?theme=dark",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('data-theme="dark"', resp.text)
        # Theme cookie uses a normal max-age so requests stores it fine
        theme_val = self.session.cookies.get("theme")
        self.assertEqual(theme_val, "dark")

    def test_language_zh_cookie(self):
        self._login()
        resp = self.session.get(
            self.base_url + "/dashboard?lang=zh",
        )
        self.assertEqual(resp.status_code, 200)
        # Page should contain Chinese text (lang="zh" in HTML)
        self.assertIn('lang="zh"', resp.text)
        # Language cookie
        lang_val = self.session.cookies.get("lang")
        self.assertEqual(lang_val, "zh")


if __name__ == "__main__":
    unittest.main()
