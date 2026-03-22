import importlib
import logging
import os
import sys
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

config_center = importlib.import_module("config_center")
upstream_pool = importlib.import_module("upstream_pool")
proxy_server = importlib.import_module("proxy_server")
persistence = importlib.import_module("persistence")
compat_ports = importlib.import_module("compat_ports")
admin_auth = importlib.import_module("admin_web.auth")

# Import web_admin with os.environ patches so it does not fail on missing
# server references during import.
web_admin = importlib.import_module("web_admin")

AppConfig = config_center.AppConfig
UpstreamEntry = upstream_pool.UpstreamEntry
UpstreamHop = upstream_pool.UpstreamHop
UpstreamPool = upstream_pool.UpstreamPool
compute_entry_key = upstream_pool.compute_entry_key
ProxyConfig = proxy_server.ProxyConfig
CompatPortMapping = compat_ports.CompatPortMapping
RingBufferHandler = web_admin.RingBufferHandler
ReloadRejectedError = web_admin.ReloadRejectedError
trigger_reload = web_admin.trigger_reload
validate_config_form = config_center.validate_config_form
save_config = persistence.save_config
load_config = persistence.load_config
save_proxy_list = persistence.save_proxy_list
load_proxy_list = persistence.load_proxy_list
save_compat_port_mappings = persistence.save_compat_port_mappings
load_compat_port_mappings = persistence.load_compat_port_mappings


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


class TestWebAdmin(unittest.TestCase):

    def test_compute_entry_key(self):
        hop = UpstreamHop(
            scheme="socks5", host="dc.decodo.com", port=10001,
            username="user", password="pass",
        )
        key = compute_entry_key((hop,))
        # Key should be a 12-char hex string derived from md5
        self.assertEqual(len(key), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in key))

        # Same hops should produce the same key (stable/deterministic)
        key2 = compute_entry_key((hop,))
        self.assertEqual(key, key2)

        # Different hops should produce a different key
        hop2 = UpstreamHop(
            scheme="http", host="other.com", port=8080,
            username="", password="",
        )
        key3 = compute_entry_key((hop2,))
        self.assertNotEqual(key, key3)

    def test_upstream_entry_source_tag(self):
        hop = UpstreamHop("http", "example.com", 8080, "", "")
        # Default source_tag should be 'manual'
        entry_default = UpstreamEntry(
            key="e1", label="test", hops=(hop,),
        )
        self.assertEqual(entry_default.source_tag, "manual")

        # Explicit source_tag='auto'
        entry_auto = UpstreamEntry(
            key="e2", label="test", hops=(hop,), source_tag="auto",
        )
        self.assertEqual(entry_auto.source_tag, "auto")

    def test_upstream_entry_in_random_pool(self):
        hop = UpstreamHop("http", "example.com", 8080, "", "")
        # Default in_random_pool should be True
        entry_default = UpstreamEntry(
            key="e1", label="test", hops=(hop,),
        )
        self.assertTrue(entry_default.in_random_pool)

        # Explicit in_random_pool=False
        entry_excluded = UpstreamEntry(
            key="e2", label="test", hops=(hop,), in_random_pool=False,
        )
        self.assertFalse(entry_excluded.in_random_pool)

    def test_proxy_config_from_dict(self):
        data = {
            "PROXY_HOST": "0.0.0.0",
            "PROXY_PORT": "3128",
            "AUTH_PASSWORD": "test-secret",
            "AUTH_REALM": "TestProxy",
            "UPSTREAM_CONNECT_TIMEOUT": "10.0",
            "UPSTREAM_CONNECT_RETRIES": "5",
            "RELAY_TIMEOUT": "60.0",
            "REWRITE_LOOPBACK_TO_HOST": "off",
            "HOST_LOOPBACK_ADDRESS": "172.17.0.1",
        }
        config = ProxyConfig.from_dict(data)

        self.assertEqual(config.listen_host, "0.0.0.0")
        self.assertEqual(config.listen_port, 3128)
        self.assertEqual(config.auth_password, "test-secret")
        self.assertEqual(config.auth_realm, "TestProxy")
        self.assertEqual(config.connect_timeout, 10.0)
        self.assertEqual(config.connect_retries, 5)
        self.assertEqual(config.relay_timeout, 60.0)
        self.assertEqual(config.loopback_host_mode, "off")
        self.assertEqual(config.host_loopback_address, "172.17.0.1")

    def test_app_config_load_prefers_saved_values_and_keeps_legacy_admin_port(self):
        environ = {
            "PROXY_HOST": "env-host",
            "PROXY_PORT": "3128",
            "AUTH_PASSWORD": "env-secret",
            "AUTH_REALM": "Proxy",
            "UPSTREAM_CONNECT_TIMEOUT": "20.0",
            "UPSTREAM_CONNECT_RETRIES": "3",
            "RELAY_TIMEOUT": "120.0",
            "REWRITE_LOOPBACK_TO_HOST": "auto",
            "HOST_LOOPBACK_ADDRESS": "host.docker.internal",
            "LOG_LEVEL": "INFO",
            "SALT": "env-salt",
            "STATE_DB_PATH": "/tmp/bootstrap.sqlite3",
            "ROUTER_DEBUG_LOG": "",
            "WEB_PORT": "9090",
        }
        app_config = AppConfig.from_sources(
            {"AUTH_PASSWORD": "saved-secret"},
            environ=environ,
            generate_runtime_secrets=True,
        )

        self.assertEqual(app_config.auth_password, "saved-secret")
        self.assertEqual(app_config.state_db_path, "/tmp/bootstrap.sqlite3")
        self.assertEqual(app_config.admin_port, 9090)
        self.assertEqual(app_config.admin_password, "")
        self.assertEqual(app_config.runtime_values()["WEB_PORT"], "9090")

    def test_app_config_load_falls_back_to_runtime_env_when_storage_is_empty(self):
        environ = {
            "PROXY_HOST": "env-host",
            "AUTH_PASSWORD": "env-secret",
            "STATE_DB_PATH": "/tmp/runtime.sqlite3",
            "WEB_PORT": "8088",
        }

        app_config = AppConfig.load(storage=None, environ=environ)

        self.assertEqual(app_config.proxy_host, "env-host")
        self.assertEqual(app_config.auth_password, "env-secret")
        self.assertEqual(app_config.state_db_path, "/tmp/runtime.sqlite3")
        self.assertEqual(app_config.admin_password, "")
        self.assertEqual(app_config.admin_port, 8088)

    def test_bootstrap_only_keys_cover_final_boundary(self):
        self.assertEqual(
            AppConfig.bootstrap_only_keys(),
            {"STATE_DB_PATH", "ADMIN_PORT", "WEB_PORT"},
        )

    def test_app_config_load_generates_missing_auth_password_and_salt(self):
        storage = _make_test_storage()

        app_config = AppConfig.load(
            storage=storage,
            environ={"STATE_DB_PATH": "/tmp/runtime.sqlite3"},
        )
        persisted = load_config(storage)

        self.assertTrue(app_config.auth_password)
        self.assertTrue(app_config.salt)
        self.assertEqual(app_config.admin_password, "")
        self.assertEqual(persisted["AUTH_PASSWORD"], app_config.auth_password)
        self.assertEqual(persisted["SALT"], app_config.salt)
        self.assertEqual(persisted["ADMIN_PASSWORD"], "")

    def test_ring_buffer_handler(self):
        handler = RingBufferHandler(maxlen=5)
        handler.setFormatter(logging.Formatter("%(message)s"))

        # Create a test logger that uses our handler
        logger = logging.getLogger("test_ring_buffer")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        # Emit some records
        logger.info("message 1")
        logger.warning("message 2")
        logger.error("message 3")

        # get_entries returns all entries (newest first)
        entries = handler.get_entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["level"], "ERROR")
        self.assertEqual(entries[2]["level"], "INFO")

        # Filter by level
        warnings = handler.get_entries(level="WARNING")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["level"], "WARNING")

        # Clear
        handler.clear()
        self.assertEqual(len(handler.get_entries()), 0)

        # maxlen enforcement: emit 7 records, only 5 should be kept
        for i in range(7):
            logger.info("overflow %d", i)
        self.assertEqual(len(handler.get_entries()), 5)

        # Clean up
        logger.removeHandler(handler)


def _make_test_storage():
    """Create a lightweight in-memory storage double."""
    return _MemoryStorage()


def _make_hop(host="proxy.example.com", port=10001, scheme="socks5"):
    return UpstreamHop(
        scheme=scheme, host=host, port=port,
        username="user", password="pass",
    )


def _make_entry(key="test_1", host="proxy.example.com", port=10001):
    hop = _make_hop(host=host, port=port)
    return UpstreamEntry(
        key=key, label="test", hops=(hop,),
        source_tag="manual", in_random_pool=True,
    )


def _make_server_ref(config=None, router=None):
    """Create a mock server object with config and router attributes."""
    ref = MagicMock()
    if config is not None:
        ref.config = config
    else:
        ref.config = ProxyConfig(
            listen_host="0.0.0.0", listen_port=3128,
            auth_password="old-secret", auth_realm="Proxy",
            connect_timeout=20.0, connect_retries=3,
            relay_timeout=120.0, loopback_host_mode="auto",
            host_loopback_address="host.docker.internal",
            running_in_docker=False,
        )
    if router is not None:
        ref.router = router
    else:
        ref.router = MagicMock()
        ref.router.storage = None
    return ref


class _ProxyTablePlacementParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.stack = []
        self.saw_proxy_table = False
        self.proxy_table_inside_content = False

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        classes = frozenset(filter(None, attr_map.get("class", "").split()))
        if tag == "table" and "pp-proxy-table" in classes:
            self.saw_proxy_table = True
            self.proxy_table_inside_content = any(
                ancestor["tag"] == "div" and "pp-content" in ancestor["classes"]
                for ancestor in self.stack
            )
        self.stack.append({"tag": tag, "classes": classes})

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break


class TestProxyFormHelpers(unittest.TestCase):

    def test_prepend_hop_value_serializes_all_but_last_hop(self):
        hops = (
            UpstreamHop("socks5", "gate-1.example.com", 1080, "", ""),
            UpstreamHop("http", "gate-2.example.com", 2080, "user", "pa:ss"),
            UpstreamHop("http", "target.example.com", 8080, "final", "secret"),
        )

        value = web_admin._prepend_hop_value(hops)

        self.assertEqual(
            value,
            "socks5://gate-1.example.com:1080, http://user:pa%3Ass@gate-2.example.com:2080",
        )


class TestAdminAuthHelpers(unittest.TestCase):

    def test_signed_cookie_roundtrip(self):
        headers = admin_auth.build_signed_cookie_headers(
            "admin_session",
            "authenticated",
            secret_key="test-secret",
        )

        self.assertEqual(len(headers), 1)
        header = headers[0]
        self.assertIn("HttpOnly", header)
        self.assertTrue(header.startswith("admin_session="))
        value = header.split("=", 1)[1].split(";", 1)[0].strip('"')
        self.assertTrue(
            admin_auth.is_authenticated_cookie(
                value,
                secret_key="test-secret",
                cookie_name="admin_session",
                cookie_value="authenticated",
            )
        )

    def test_signed_cookie_rejects_tampering(self):
        header = admin_auth.build_signed_cookie_headers(
            "admin_session",
            "authenticated",
            secret_key="test-secret",
        )[0]
        value = header.split("=", 1)[1].split(";", 1)[0].strip('"')
        tampered = value[:-1] + ("A" if value[-1] != "A" else "B")

        self.assertFalse(
            admin_auth.is_authenticated_cookie(
                tampered,
                secret_key="test-secret",
                cookie_name="admin_session",
                cookie_value="authenticated",
            )
        )


class TestAdminThemeStyles(unittest.TestCase):

    def test_tabler_page_includes_centered_layout_and_dark_theme_overrides(self):
        html = web_admin._tabler_page(
            "Admin",
            "<div>content</div>",
            active_nav=None,
            theme="dark",
        )

        self.assertIn("--pp-table-row-alt: #243244;", html)
        self.assertIn("--pp-button-bg: #223044;", html)
        self.assertIn('data-bs-theme="dark"', html)
        self.assertIn("background-color: inherit !important;", html)
        self.assertIn("box-shadow: none !important;", html)
        self.assertIn(".btn-group > .btn.active {", html)
        self.assertIn(".form-check-input:checked {", html)
        self.assertIn("width: min(100%, 1440px);", html)
        self.assertIn("margin: 0 auto;", html)
        self.assertIn(".pp-proxy-toolbar {", html)
        self.assertIn(".pp-proxy-summary-item {", html)
        self.assertIn('--tblr-table-striped-bg: var(--pp-table-row-alt);', html)
        self.assertIn('html.setAttribute("data-bs-theme",next);', html)

    def test_toggle_locale_url_preserves_current_query_params(self):
        url = web_admin._toggle_locale_url(
            "/dashboard/proxies",
            [("source", "manual"), ("page", "2"), ("theme", "dark")],
            "en",
        )

        self.assertEqual(
            url,
            "/dashboard/proxies?source=manual&page=2&theme=dark&lang=zh",
        )

    def test_proxy_list_template_uses_structured_layout_classes(self):
        self.assertIn('class="pp-page-header pp-proxy-header"', web_admin._PROXY_LIST_PAGE)
        self.assertIn('class="pp-toolbar pp-proxy-toolbar"', web_admin._PROXY_LIST_PAGE)
        self.assertIn('class="pp-proxy-summary"', web_admin._PROXY_LIST_PAGE)
        self.assertIn('class="table table-vcenter table-striped card-table pp-proxy-table"', web_admin._PROXY_LIST_PAGE)
        self.assertIn("pp-selected-row", web_admin._PROXY_LIST_SCRIPTS)

    def test_proxy_list_table_stays_inside_content_rail(self):
        translations = dict(web_admin.get_translations("en"))
        translations["proxies_clear_confirm"] = "Confirm delete?"
        translations["proxies_showing"] = "Showing 1-1 of 1"
        content = web_admin.template(
            web_admin._PROXY_LIST_PAGE,
            entries=[_make_entry()],
            total=1,
            manual_count=1,
            auto_count=0,
            active_filter="all",
            filter_urls={
                "all": "/dashboard/proxies",
                "manual": "/dashboard/proxies?source=manual",
                "auto": "/dashboard/proxies?source=auto",
            },
            page=1,
            total_pages=1,
            page_start=1,
            page_end=1,
            page_range=[1],
            page_urls={1: "/dashboard/proxies"},
            prev_page_url="/dashboard/proxies",
            next_page_url="/dashboard/proxies",
            i=web_admin._TranslationNamespace(translations),
        )
        html = web_admin._tabler_page(
            "Proxy List",
            content,
            active_nav="nav_proxies",
            theme="dark",
        )

        parser = _ProxyTablePlacementParser()
        parser.feed(html)

        self.assertTrue(parser.saw_proxy_table)
        self.assertTrue(parser.proxy_table_inside_content)

    def test_compat_template_renderer_creates_csrf_tokens_without_request_context(self):
        content = web_admin.template(
            web_admin._PROXY_FORM_PAGE,
            title="Proxy Form",
            action_url="/dashboard/proxies/add",
            submit_label="Save",
            schemes=["http"],
            scheme="http",
            host="",
            port="",
            username="",
            password="",
            prepend_hop="",
            in_random_pool=True,
            error="",
            i=web_admin._TranslationNamespace(dict(web_admin.get_translations("en"))),
        )

        self.assertIn('name="csrf_token"', content)
        self.assertNotIn('name="csrf_token" value=""', content)


class TestTriggerReload(unittest.TestCase):
    """Tests for the hardened trigger_reload path."""

    def test_reload_updates_proxy_config(self):
        """trigger_reload should update server.config with new ProxyConfig."""
        storage = _make_test_storage()
        entries = [_make_entry("e1"), _make_entry("e2", port=10002)]
        save_proxy_list(storage, entries)
        save_config(storage, {
            "AUTH_PASSWORD": "new-secret",
            "RELAY_TIMEOUT": "60.0",
            "UPSTREAM_CONNECT_TIMEOUT": "5.0",
        })

        server_ref = _make_server_ref()
        server_ref.router.storage = storage

        trigger_reload(server_ref)

        # ProxyConfig should be replaced with new values
        new_config = server_ref.config
        self.assertIsInstance(new_config, ProxyConfig)
        self.assertEqual(new_config.auth_password, "new-secret")
        self.assertEqual(new_config.relay_timeout, 60.0)
        self.assertEqual(new_config.connect_timeout, 5.0)

    def test_reload_rebuilds_router(self):
        """trigger_reload should rebuild the router with updated entries."""
        storage = _make_test_storage()
        entries = [_make_entry("e1"), _make_entry("e2", port=10002)]
        save_proxy_list(storage, entries)
        save_config(storage, {"AUTH_PASSWORD": "secret"})

        server_ref = _make_server_ref()
        server_ref.router.storage = storage

        trigger_reload(server_ref)

        new_router = server_ref.router
        self.assertEqual(new_router.upstream_count, 2)
        self.assertIn(new_router.route("sticky-user"), {"e1", "e2"})

    def test_reload_no_server_ref(self):
        """trigger_reload should log and return when no server reference."""
        # Should not raise
        trigger_reload(server_ref=None)

    def test_reload_without_storage_raises(self):
        """trigger_reload should fail fast when the storage layer is missing."""
        server_ref = _make_server_ref()
        server_ref.router.storage = None

        with self.assertRaises(RuntimeError):
            trigger_reload(server_ref)

    def test_reload_no_entries_rebuilds_empty_router(self):
        """trigger_reload should rebuild the router even when no proxy entries exist."""
        storage = _make_test_storage()
        save_config(storage, {"AUTH_PASSWORD": "secret"})
        save_proxy_list(storage, [])

        server_ref = _make_server_ref()
        server_ref.router.storage = storage

        trigger_reload(server_ref)

        self.assertEqual(server_ref.router.upstream_pool.source, "admin")
        self.assertEqual(server_ref.router.upstream_count, 0)

    def test_reload_also_refreshes_compat_listeners(self):
        storage = _make_test_storage()
        save_config(storage, {"AUTH_PASSWORD": "secret"})
        save_proxy_list(storage, [])
        save_compat_port_mappings(
            storage,
            [
                CompatPortMapping(
                    listen_port=33100,
                    target_type="session_name",
                    target_value="browser-a",
                )
            ],
        )

        server_ref = _make_server_ref()
        server_ref.router.storage = storage

        with patch.object(proxy_server, "reload_compat_listeners") as compat_reload_mock:
            trigger_reload(server_ref)

        compat_reload_mock.assert_called_once_with(server_ref, storage)

    def test_reload_bad_config_reinitializes_runtime_config(self):
        """trigger_reload should rebuild runtime config when persisted JSON is invalid."""
        storage = _make_test_storage()
        # Poison the config with invalid JSON
        storage.set(persistence.STATE_KEY_CONFIG, "not-valid-json")

        server_ref = _make_server_ref()
        server_ref.router.storage = storage
        old_config = server_ref.config
        old_router = server_ref.router

        trigger_reload(server_ref)

        self.assertIsNot(server_ref.config, old_config)
        self.assertIsNot(server_ref.router, old_router)
        self.assertTrue(server_ref.config.auth_password)
        self.assertNotEqual(server_ref.config.auth_password, "old-secret")
        self.assertEqual(
            load_config(storage)["AUTH_PASSWORD"],
            server_ref.config.auth_password,
        )

    def test_reload_missing_auth_password_generates_new_secret(self):
        storage = _make_test_storage()
        save_config(storage, {"AUTH_PASSWORD": ""})
        save_proxy_list(storage, [])

        server_ref = _make_server_ref()
        server_ref.router.storage = storage

        trigger_reload(server_ref, raise_on_error=True)

        self.assertTrue(server_ref.config.auth_password)
        self.assertEqual(
            load_config(storage)["AUTH_PASSWORD"],
            server_ref.config.auth_password,
        )


class TestConfigSaveReloadRoundtrip(unittest.TestCase):
    """Tests for the config save -> load -> reload cycle."""

    def test_config_roundtrip_preserves_values(self):
        """save_config -> load_config roundtrip preserves all runtime values."""
        storage = _make_test_storage()
        original = {
            "SALT": "roundtrip-salt",
            "AUTH_PASSWORD": "roundtrip-secret",
            "RELAY_TIMEOUT": "45.0",
            "RANDOM_POOL_PREFIX": "rnd_",
        }
        save_config(storage, original)
        loaded = load_config(storage)

        self.assertEqual(loaded["SALT"], "roundtrip-salt")
        self.assertEqual(loaded["AUTH_PASSWORD"], "roundtrip-secret")
        self.assertEqual(loaded["RELAY_TIMEOUT"], "45.0")
        self.assertEqual(loaded["RANDOM_POOL_PREFIX"], "rnd_")

    def test_proxy_list_roundtrip_preserves_entries(self):
        """save_proxy_list -> load_proxy_list preserves entry data."""
        storage = _make_test_storage()
        entries = [
            _make_entry("e1", "host1.com", 10001),
            _make_entry("e2", "host2.com", 10002),
        ]
        save_proxy_list(storage, entries)
        loaded = load_proxy_list(storage)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].key, "e1")
        self.assertEqual(loaded[0].first_hop.host, "host1.com")
        self.assertEqual(loaded[1].key, "e2")
        self.assertEqual(loaded[1].first_hop.port, 10002)

    def test_full_reload_cycle(self):
        """Config + entries save -> trigger_reload -> server state updated."""
        storage = _make_test_storage()

        # Save runtime config
        save_config(storage, {
            "AUTH_PASSWORD": "cycle-secret",
            "RANDOM_POOL_PREFIX": "rnd_",
            "RELAY_TIMEOUT": "30.0",
        })

        # Save proxy entries
        entries = [_make_entry("e1"), _make_entry("e2", port=10002)]
        save_proxy_list(storage, entries)

        # Setup server ref
        server_ref = _make_server_ref()
        server_ref.router.storage = storage

        trigger_reload(server_ref)

        # Verify ProxyConfig updated
        self.assertEqual(server_ref.config.auth_password, "cycle-secret")
        self.assertEqual(server_ref.config.relay_timeout, 30.0)

        # Verify Router updated
        self.assertEqual(server_ref.router.random_pool_prefix, "rnd_")
        self.assertEqual(server_ref.router.upstream_count, 2)
        self.assertIn(server_ref.router.route("sticky-user"), {"e1", "e2"})


def _make_valid_config_form(**overrides):
    form = {
        "PROXY_HOST": "0.0.0.0",
        "PROXY_PORT": "3128",
        "AUTH_PASSWORD": "secret",
        "AUTH_REALM": "Proxy",
        "UPSTREAM_CONNECT_TIMEOUT": "20.0",
        "UPSTREAM_CONNECT_RETRIES": "3",
        "RELAY_TIMEOUT": "120.0",
        "REWRITE_LOOPBACK_TO_HOST": "auto",
        "HOST_LOOPBACK_ADDRESS": "host.docker.internal",
        "LOG_LEVEL": "INFO",
        "SALT": "test-salt",
        "ROUTER_DEBUG_LOG": "",
    }
    form.update(overrides)
    return form


class TestValidateConfigForm(unittest.TestCase):
    """Tests for config form validation edge cases."""

    def test_valid_form_passes(self):
        form = _make_valid_config_form()
        clean, errors = validate_config_form(form)
        self.assertEqual(errors, [])
        self.assertEqual(clean["AUTH_PASSWORD"], "secret")

    def test_empty_auth_password_rejected(self):
        form = _make_valid_config_form(AUTH_PASSWORD="")
        clean, errors = validate_config_form(form)
        self.assertTrue(any("required" in e.lower() for e in errors))

    def test_invalid_port_rejected(self):
        form = _make_valid_config_form(PROXY_PORT="99999")
        clean, errors = validate_config_form(form)
        self.assertTrue(any("65535" in e for e in errors))

    def test_negative_relay_timeout_rejected(self):
        form = _make_valid_config_form(RELAY_TIMEOUT="-5.0")
        clean, errors = validate_config_form(form)
        self.assertTrue(any("positive" in e.lower() for e in errors))

    def test_invalid_loopback_mode_rejected(self):
        form = _make_valid_config_form(REWRITE_LOOPBACK_TO_HOST="invalid_mode")
        clean, errors = validate_config_form(form)
        self.assertTrue(any("auto" in e and "always" in e for e in errors))

    def test_retries_below_one_rejected(self):
        form = _make_valid_config_form(UPSTREAM_CONNECT_RETRIES="0")
        clean, errors = validate_config_form(form)
        self.assertTrue(any("at least 1" in e.lower() for e in errors))

    def test_admin_dashboard_css_includes_dashboard_layout_selectors(self):
        css = web_admin.admin_resources.load_static_source("css/admin.css")
        for selector in (
            ".pp-stat-label",
            ".pp-stat-value",
            ".pp-info-grid",
            ".pp-info-item-label",
            ".pp-info-item-value",
            ".pp-badge-info",
            ".pp-badge-warning",
        ):
            self.assertIn(selector, css)


if __name__ == "__main__":
    unittest.main()
