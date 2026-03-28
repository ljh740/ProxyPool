"""Browser-level E2E tests for the ProxyPool Web Admin.

Uses ``playwright.sync_api`` + ``unittest.TestCase`` to exercise the
full admin WSGI app via a real headless Chromium browser. Reuses the same
wsgiref + in-memory storage fixture pattern from ``test_e2e.py``.

These tests cover browser-only behaviour that HTTP-level tests cannot
reach: JS interactions, DOM rendering, toast notifications, theme
toggle, sidebar navigation, batch checkbox logic, etc.
"""

import importlib
import logging
import os
import re
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import sync_playwright

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

UpstreamEntry = upstream_pool.UpstreamEntry
UpstreamHop = upstream_pool.UpstreamHop
compute_entry_key = upstream_pool.compute_entry_key
ProxyConfig = proxy_server.ProxyConfig
RingBufferHandler = web_admin.RingBufferHandler

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
    def log_message(self, format, *args):  # noqa: A002
        pass


# ---------------------------------------------------------------------------
# Browser E2E Test Suite
# ---------------------------------------------------------------------------


class TestE2EBrowser(unittest.TestCase):
    """Playwright browser-level E2E tests for the Web Admin panel."""

    _server = None
    _thread = None
    _storage = None
    _playwright = None
    _browser = None

    @classmethod
    def _cleanup_class_resources(cls):
        if cls._browser:
            cls._browser.close()
            cls._browser = None
        if cls._playwright:
            cls._playwright.stop()
            cls._playwright = None
        if cls._server:
            cls._server.shutdown()
            cls._server = None
        if cls._storage:
            cls._storage.close()
            cls._storage = None
        web_admin._server_ref = cls._original_server_ref
        web_admin._log_handler = cls._original_log_handler

    @classmethod
    def _probe_browser(cls, browser):
        for title in ("probe-one", "probe-two"):
            context = browser.new_context()
            try:
                page = context.new_page()
                page.goto(f"data:text/html,<title>{title}</title>")
                if page.title() != title:
                    raise RuntimeError("unexpected probe title: %s" % page.title())
            finally:
                context.close()

    @classmethod
    def _launch_browser(cls):
        browser_name = os.environ.get("PLAYWRIGHT_BROWSER", "chromium")
        browser_type = getattr(cls._playwright, browser_name)
        launch_options = {
            "headless": not os.environ.get("HEADED"),
            "slow_mo": 300 if os.environ.get("HEADED") else 0,
        }
        fallback_arg_sets = [None]
        if browser_name == "chromium":
            fallback_arg_sets.append(["--single-process"])

        launch_errors = []
        for args in fallback_arg_sets:
            try:
                browser = (
                    browser_type.launch(**launch_options)
                    if args is None
                    else browser_type.launch(**launch_options, args=args)
                )
            except Exception as exc:
                launch_errors.append((args, exc))
                continue
            try:
                cls._probe_browser(browser)
                return browser
            except Exception as exc:
                launch_errors.append((args, exc))
                browser.close()

        arg_descriptions = [
            "default" if args is None else "args=%s" % " ".join(args)
            for args, _ in launch_errors
        ]
        message = "failed to launch %s via %s" % (browser_name, ", ".join(arg_descriptions))
        raise RuntimeError(message) from launch_errors[-1][1]

    @classmethod
    def setUpClass(cls):
        # 1) Initialize in-memory admin storage
        cls._storage = _MemoryStorage()

        # 2) Log handler with test entries
        cls._log_handler = RingBufferHandler(maxlen=100)
        cls._log_handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test_e2e_browser_fixture")
        logger.addHandler(cls._log_handler)
        logger.setLevel(logging.DEBUG)
        logger.info("Browser test log INFO")
        logger.warning("Browser test log WARNING")
        logger.error("Browser test log ERROR")
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

        # 5) Seed initial config
        persistence.save_config(
            cls._storage,
            {
                "AUTH_PASSWORD": "test-secret",
                "ADMIN_PASSWORD": TEST_PASSWORD,
            },
        )

        # 6) Start WSGI server on random port
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

        # 7) Launch Playwright + Chromium
        cls._playwright = sync_playwright().start()
        try:
            cls._browser = cls._launch_browser()
        except Exception:
            cls._cleanup_class_resources()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class_resources()

    def setUp(self):
        self._reset_storage()
        self._reload_patcher = patch.object(web_admin, "trigger_reload")
        self._reload_patcher.start()
        self._context = self._browser.new_context()
        self.page = self._context.new_page()

    def tearDown(self):
        self._context.close()
        self._reload_patcher.stop()

    # --- helpers ---

    def _login(self):
        """Navigate to /login, fill password, submit, wait for dashboard."""
        self.page.goto(f"{self.base_url}/login")
        self.page.fill("#password", TEST_PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard**")

    def _reset_storage(self, *, admin_password=TEST_PASSWORD):
        self._storage.clear()
        persistence.save_config(
            self._storage,
            {
                "AUTH_PASSWORD": "test-secret",
                "ADMIN_PASSWORD": admin_password,
            },
        )

    # ====================================================================
    # Tier 1: Login Flow
    # ====================================================================

    def test_setup_flow_browser(self):
        """First-boot setup renders and submits successfully."""
        self._reset_storage(admin_password="")

        self.page.goto(f"{self.base_url}/setup")
        self.assertTrue(self.page.is_visible("#password"))
        self.assertTrue(self.page.is_visible("#confirm_password"))
        self.page.fill("#password", TEST_PASSWORD)
        self.page.fill("#confirm_password", TEST_PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard**")
        self.assertIn("/dashboard", self.page.url)

    def test_login_redirects_to_setup_when_admin_password_missing(self):
        """Login page should not render before setup is completed."""
        self._reset_storage(admin_password="")

        self.page.goto(f"{self.base_url}/login")

        self.page.wait_for_url("**/setup**")
        self.assertIn("/setup", self.page.url)

    def test_login_flow_browser(self):
        """Full browser login: fill form -> submit -> arrive at dashboard."""
        self.page.goto(f"{self.base_url}/login")
        # Login page should have the password field and submit button
        self.assertTrue(self.page.is_visible("#password"))
        self.page.fill("#password", TEST_PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard**")
        # Dashboard should render stat cards
        self.assertIn("/dashboard", self.page.url)
        self.assertTrue(self.page.locator(".pp-stat-value").count() > 0)

    def test_login_wrong_password_shows_error(self):
        """Wrong password redirects back to login with error toast."""
        self.page.goto(f"{self.base_url}/login")
        self.page.fill("#password", "wrong-password")
        self.page.click('button[type="submit"]')
        # Should stay on /login with error query param triggering toast
        self.page.wait_for_url("**/login**")
        # The toast JS fires on page load when error= is in URL
        toast = self.page.locator(".pp-toast.show")
        toast.wait_for(timeout=3000)
        self.assertTrue(toast.count() > 0)

    # ====================================================================
    # Tier 1b: Dashboard Interactions
    # ====================================================================

    def test_dashboard_stat_cards_render(self):
        """Dashboard renders stat cards with proxy count and log count."""
        self._login()
        self._reset_storage()
        # Seed some proxies so the count is non-zero
        entries = [
            _make_entry("dash1", "dash-a.example.com", 13001),
            _make_entry("dash2", "dash-b.example.com", 13002),
        ]
        persistence.save_proxy_list(self._storage, entries)
        self.page.goto(f"{self.base_url}/dashboard")
        # Stat values should render
        stat_values = self.page.locator(".pp-stat-value")
        self.assertGreaterEqual(stat_values.count(), 2)
        # Proxy count card should show "2"
        texts = [stat_values.nth(i).text_content() for i in range(stat_values.count())]
        self.assertIn("2", texts)

    def test_dashboard_stat_card_click_navigates(self):
        """Click stat cards on dashboard to navigate to respective pages."""
        self._login()
        content = self.page.locator(".pp-content")

        # Click the Configuration card -> /dashboard/config
        content.locator('a[href="/dashboard/config"]').click()
        self.page.wait_for_url("**/dashboard/config**")
        self.assertIn("/dashboard/config", self.page.url)

        # Go back to dashboard
        self.page.goto(f"{self.base_url}/dashboard")
        # Click the Proxies card -> /dashboard/proxies
        content.locator('a[href="/dashboard/proxies"]').click()
        self.page.wait_for_url("**/dashboard/proxies**")
        self.assertIn("/dashboard/proxies", self.page.url)

        # Go back to dashboard
        self.page.goto(f"{self.base_url}/dashboard")
        # Click the Logs card -> /dashboard/logs
        content.locator('a[href="/dashboard/logs"]').click()
        self.page.wait_for_url("**/dashboard/logs**")
        self.assertIn("/dashboard/logs", self.page.url)

    def test_dashboard_server_info_displays(self):
        """Dashboard shows server info grid with listen address and status."""
        self._login()
        self.page.goto(f"{self.base_url}/dashboard")
        info_grid = self.page.locator(".pp-info-grid")
        self.assertTrue(info_grid.is_visible())
        # Should display listen address and runtime status
        grid_text = info_grid.text_content()
        self.assertIn("0.0.0.0", grid_text)
        self.assertIn("Running", grid_text)

    # ====================================================================
    # Tier 2: Sidebar Navigation
    # ====================================================================

    def test_sidebar_navigation(self):
        """Click sidebar links and verify URL + content changes."""
        self._login()

        # Navigate to Configuration via sidebar
        self.page.click('a[href="/dashboard/config"]')
        self.page.wait_for_url("**/dashboard/config**")
        self.assertIn("/dashboard/config", self.page.url)
        self.assertTrue(self.page.locator("#config-form").count() > 0)

        # Navigate to Proxies via sidebar
        self.page.click('a[href="/dashboard/proxies"]')
        self.page.wait_for_url("**/dashboard/proxies**")
        self.assertIn("/dashboard/proxies", self.page.url)

        # Navigate to Logs via sidebar
        self.page.click('a[href="/dashboard/logs"]')
        self.page.wait_for_url("**/dashboard/logs**")
        self.assertIn("/dashboard/logs", self.page.url)
        self.assertTrue(self.page.locator("#log-table-body").count() > 0)

    def test_sidebar_logout(self):
        """Click logout in sidebar -> redirected to login page."""
        self._login()
        self.page.click('a[href="/logout"]')
        self.page.wait_for_url("**/login**")
        self.assertIn("/login", self.page.url)

    # ====================================================================
    # Tier 3: Theme Toggle
    # ====================================================================

    def test_theme_toggle_button(self):
        """Click theme button -> data-theme attribute toggles to dark."""
        self._login()
        html = self.page.locator("html")
        # Default is light
        self.page.click(".pp-theme-btn")
        self.assertEqual(html.get_attribute("data-theme"), "dark")
        # Click again -> back to light
        self.page.click(".pp-theme-btn")
        self.assertEqual(html.get_attribute("data-theme"), "light")
        # Verify cookie was set
        cookies = self._context.cookies()
        theme_cookie = next((c for c in cookies if c["name"] == "theme"), None)
        self.assertIsNotNone(theme_cookie)

    def test_theme_persists_across_pages(self):
        """Set dark theme -> navigate to another page -> dark theme persists."""
        self._login()
        # Set dark theme via query param (reliable approach)
        self.page.goto(f"{self.base_url}/dashboard?theme=dark")
        html_el = self.page.locator("html")
        self.assertEqual(html_el.get_attribute("data-theme"), "dark")
        # Navigate to config page
        self.page.click('a[href="/dashboard/config"]')
        self.page.wait_for_url("**/dashboard/config**")
        # Theme should persist
        self.assertEqual(
            self.page.locator("html").get_attribute("data-theme"), "dark",
        )

    def test_dark_theme_tables_use_dark_row_backgrounds(self):
        """Dark theme keeps proxy/log tables on dark row tokens, not light Tabler defaults."""
        self._login()
        self._reset_storage()
        entries = [
            _make_entry("dark1", "dark-one.example.com", 13001),
            _make_entry("dark2", "dark-two.example.com", 13002),
        ]
        persistence.save_proxy_list(self._storage, entries)

        self.page.goto(f"{self.base_url}/dashboard/proxies?theme=dark")
        html = self.page.locator("html")
        self.assertEqual(html.get_attribute("data-theme"), "dark")
        self.assertEqual(html.get_attribute("data-bs-theme"), "dark")

        proxy_second_row = self.page.locator(".pp-proxy-table tbody tr").nth(1)
        proxy_second_cell = proxy_second_row.locator("td").nth(1)
        self.assertEqual(
            proxy_second_row.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(36, 50, 68)",
        )
        self.assertEqual(
            proxy_second_cell.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(36, 50, 68)",
        )

        proxy_second_row.locator(".row-check").check()
        self.assertIn(
            "pp-selected-row",
            proxy_second_row.get_attribute("class") or "",
        )
        self.page.wait_for_timeout(250)
        self.assertEqual(
            proxy_second_row.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(28, 58, 92)",
        )

        self.page.goto(f"{self.base_url}/dashboard/logs")
        log_second_row = self.page.locator("#log-table-body tr").nth(1)
        log_second_cell = log_second_row.locator("td").nth(1)
        self.assertEqual(
            log_second_row.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(36, 50, 68)",
        )
        self.assertEqual(
            log_second_cell.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(36, 50, 68)",
        )

    # ====================================================================
    # Tier 4: Config Form
    # ====================================================================

    def test_config_form_submit_shows_toast(self):
        """Submit config form -> success toast appears."""
        self._login()
        self._reset_storage()
        self.page.goto(f"{self.base_url}/dashboard/config")
        # Fill a required field (AUTH_PASSWORD)
        self.page.fill("#AUTH_PASSWORD", "new-secret-from-browser")
        # Submit the form
        self.page.click('#config-form button[type="submit"]')
        self.page.wait_for_timeout(750)
        self.assertIn("/dashboard/config", self.page.url)
        self.assertGreater(self.page.locator(".pp-toast").count(), 0)
        self.assertEqual(
            persistence.load_config(self._storage)["AUTH_PASSWORD"],
            "new-secret-from-browser",
        )
        self.assertEqual(
            persistence.load_config(self._storage)["ADMIN_PASSWORD"],
            TEST_PASSWORD,
        )

    def test_config_collapsible_groups(self):
        """Advanced group is collapsed by default -> click to expand."""
        self._login()
        self.page.goto(f"{self.base_url}/dashboard/config")
        # Advanced group card-body should be hidden (display:none)
        advanced_body = self.page.locator("#group-advanced .card-body")
        self.assertFalse(advanced_body.is_visible())
        # Click the header to expand
        self.page.click("#group-advanced .card-header")
        # Now body should be visible
        self.assertTrue(advanced_body.is_visible())

    # ====================================================================
    # Tier 5: Proxy CRUD via Browser
    # ====================================================================

    def test_add_proxy_via_form(self):
        """Fill add-proxy form -> submit -> new proxy appears in list."""
        self._login()
        self._reset_storage()
        self.page.goto(f"{self.base_url}/dashboard/proxies/add")
        self.page.fill("#host", "browser-test.example.com")
        self.page.fill("#port", "9090")
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard/proxies**")
        # Verify the new proxy is visible in the table
        self.assertTrue(
            self.page.locator("text=browser-test.example.com").count() > 0,
        )

    def test_edit_proxy_page_shows_copyable_chain_uri(self):
        """Edit page exposes the full proxy chain in a copyable read-only field."""
        self._login()
        self._reset_storage()
        hops = (
            UpstreamHop("socks5", "browser-gate.example.com", 1080, "", ""),
            UpstreamHop("http", "browser-edit.example.com", 10001, "u", "p"),
        )
        entry = UpstreamEntry(
            key=compute_entry_key(hops),
            label="browser-gate.example.com:1080 -> browser-edit.example.com:10001",
            hops=hops,
            source_tag="manual",
            in_random_pool=True,
        )
        persistence.save_proxy_list(self._storage, [entry])

        self.page.goto(f"{self.base_url}/dashboard/proxies/{entry.key}/edit")

        self.assertEqual(self.page.locator("#proxy-entry-key").input_value(), entry.key)
        self.assertEqual(
            self.page.locator("#proxy-chain-uri").input_value(),
            "socks5://browser-gate.example.com:1080 | http://u:p@browser-edit.example.com:10001",
        )
        self.assertTrue(self.page.locator("#copy-entry-key-btn").is_visible())
        self.assertTrue(self.page.locator("#copy-chain-uri-btn").is_visible())

        self.page.click("#copy-entry-key-btn")
        self.page.wait_for_function(
            "() => document.getElementById('copy-entry-key-btn').dataset.copyState === 'copied'"
        )

    def test_import_proxy_list_via_form(self):
        """Fill import form -> submit -> imported proxies appear as manual entries."""
        self._login()
        self._reset_storage()
        auto_entry = _make_entry("auto_existing", "auto-existing.example.com", 12000, source_tag="auto")
        persistence.save_proxy_list(self._storage, [auto_entry])

        self.page.goto(f"{self.base_url}/dashboard/proxies")
        self.page.click('a[href="/dashboard/proxies/import"]')
        self.page.wait_for_url("**/dashboard/proxies/import")
        self.page.select_option("#default_scheme", "socks5")
        self.page.fill("#default_username", "fallback-user")
        self.page.fill("#default_password", "fallback-pass")
        self.page.fill(
            "#proxy_list_text",
            "\n".join(
                [
                    "browser-import.example.com:21001",
                    "http://127.0.0.1:30001 | browser-chain.example.com:21002",
                ]
            ),
        )
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard/proxies**")

        imported_row = self.page.locator(
            'tr[data-source="manual"] td.pp-proxy-label:has-text("browser-import.example.com")'
        )
        chain_row = self.page.locator(
            'tr[data-source="manual"] td.pp-proxy-label:has-text("browser-chain.example.com")'
        )
        self.assertTrue(
            imported_row.is_visible()
        )
        self.assertTrue(
            chain_row.is_visible()
        )
        saved_entries = {entry.last_hop.host: entry for entry in persistence.load_proxy_list(self._storage)}
        self.assertEqual(saved_entries["browser-import.example.com"].source_tag, "manual")
        self.assertEqual(saved_entries["browser-import.example.com"].last_hop.scheme, "socks5")
        self.assertEqual(saved_entries["browser-import.example.com"].last_hop.username, "fallback-user")
        self.assertEqual(saved_entries["browser-chain.example.com"].chain_length, 2)
        self.assertEqual(saved_entries["auto-existing.example.com"].source_tag, "auto")

    def test_import_proxy_list_with_connectivity_check(self):
        """Pre-check imported proxies, then save only the valid manual entries."""
        self._login()
        self._reset_storage()
        auto_entry = _make_entry("auto_existing", "auto-existing.example.com", 12000, source_tag="auto")
        persistence.save_proxy_list(self._storage, [auto_entry])

        with patch.object(
            proxy_routes,
            "_probe_import_entry",
            side_effect=[(True, ""), (False, "dial timeout")],
        ):
            self.page.goto(f"{self.base_url}/dashboard/proxies/import")
            self.page.check("#probe_before_import")
            self.page.select_option("#default_scheme", "socks5")
            self.page.fill("#default_username", "fallback-user")
            self.page.fill("#default_password", "fallback-pass")
            self.page.fill(
                "#proxy_list_text",
                "\n".join(
                    [
                        "checked-ok.example.com:22001",
                        "checked-bad.example.com:22002",
                    ]
                ),
            )
            self.page.click('button[type="submit"]')
            self.page.wait_for_selector("#import-check-modal.show")
            self.page.wait_for_function(
                "() => {"
                " const btn = document.getElementById('import-check-commit');"
                " return btn && !btn.disabled;"
                " }"
            )
            self.assertTrue(self.page.locator("text=checked-ok.example.com").count() > 0)
            self.assertTrue(self.page.locator("text=checked-bad.example.com").count() > 0)
            self.page.click("#import-check-commit")
            self.page.wait_for_url("**/dashboard/proxies**")

        saved_entries = {entry.last_hop.host: entry for entry in persistence.load_proxy_list(self._storage)}
        self.assertIn("checked-ok.example.com", saved_entries)
        self.assertNotIn("checked-bad.example.com", saved_entries)
        self.assertEqual(saved_entries["checked-ok.example.com"].source_tag, "manual")
        self.assertEqual(saved_entries["auto-existing.example.com"].source_tag, "auto")

    def test_delete_proxy_with_confirm(self):
        """Click delete -> browser confirm dialog -> proxy removed."""
        self._login()
        self._reset_storage()
        entry = _make_entry("del_browser", "del-browser.example.com", 7777)
        persistence.save_proxy_list(self._storage, [entry])
        self.page.goto(f"{self.base_url}/dashboard/proxies")
        # Verify entry is present
        self.assertTrue(
            self.page.locator("text=del-browser.example.com").count() > 0,
        )
        # Set up dialog handler to accept the confirm
        self.page.on("dialog", lambda dialog: dialog.accept())
        # Click the delete button for this entry
        delete_form = self.page.locator(
            'form[action="/dashboard/proxies/del_browser/delete"]'
        )
        delete_form.locator('button[type="submit"]').click()
        self.page.wait_for_url("**/dashboard/proxies**")
        # Proxy should be gone
        self.assertEqual(
            self.page.locator("text=del-browser.example.com").count(), 0,
        )

    def test_proxy_table_source_filter(self):
        """Filter tabs request a filtered page across the full dataset."""
        self._login()
        self._reset_storage()
        entries = [
            _make_entry(f"a_filt_{idx}", f"auto-filt-{idx}.example.com", 11000 + idx,
                        source_tag="auto")
            for idx in range(100)
        ]
        entries.extend([
            _make_entry("m_filt_1", "manual-filt-one.example.com", 12001,
                        source_tag="manual"),
            _make_entry("m_filt_2", "manual-filt-two.example.com", 12002,
                        source_tag="manual"),
        ])
        persistence.save_proxy_list(self._storage, entries)
        self.page.goto(f"{self.base_url}/dashboard/proxies")

        self.assertEqual(
            self.page.locator("text=manual-filt-one.example.com").count(),
            0,
        )

        # Click "Manual" filter
        with self.page.expect_navigation(
            url=re.compile(r".*/dashboard/proxies\?source=manual$")
        ):
            self.page.click('[data-filter="manual"]')
        self.assertTrue(
            self.page.locator(
                'td.pp-proxy-label:has-text("manual-filt-one.example.com")'
            ).is_visible()
        )
        self.assertTrue(
            self.page.locator(
                'td.pp-proxy-label:has-text("manual-filt-two.example.com")'
            ).is_visible()
        )
        self.assertEqual(
            self.page.locator("text=auto-filt-0.example.com").count(),
            0,
        )

        # Click "Auto" filter
        with self.page.expect_navigation(
            url=re.compile(r".*/dashboard/proxies\?source=auto$")
        ):
            self.page.click('[data-filter="auto"]')
        self.assertTrue(
            self.page.locator(
                'td.pp-proxy-label:has-text("auto-filt-0.example.com")'
            ).is_visible()
        )
        self.assertEqual(
            self.page.locator("text=manual-filt-one.example.com").count(),
            0,
        )

        # Click "All" filter
        with self.page.expect_navigation(
            url=re.compile(r".*/dashboard/proxies$")
        ):
            self.page.click('[data-filter="all"]')
        self.assertTrue(
            self.page.locator(
                'td.pp-proxy-label:has-text("auto-filt-0.example.com")'
            ).is_visible()
        )

    def test_pool_toggle_uses_ajax_without_navigation(self):
        """Row-level random-pool toggle updates in place without a page refresh."""
        self._login()
        self._reset_storage()
        entry = _make_entry("ajax_pool", "ajax-pool.example.com", 12009, source_tag="manual")
        persistence.save_proxy_list(self._storage, [entry])
        self.page.goto(f"{self.base_url}/dashboard/proxies")
        current_url = self.page.url
        button = self.page.locator(
            'form[action="/dashboard/proxies/ajax_pool/toggle-pool"] .pp-pool-toggle-btn'
        )
        self.assertEqual(button.text_content().strip(), "ON")
        button.click()
        self.page.wait_for_function(
            "() => {"
            " const btn = document.querySelector("
            "   'form[action=\"/dashboard/proxies/ajax_pool/toggle-pool\"] .pp-pool-toggle-btn'"
            " );"
            " return btn && btn.textContent.trim() === 'OFF';"
            " }"
        )
        self.assertEqual(self.page.url, current_url)
        self.assertFalse(persistence.load_proxy_list(self._storage)[0].in_random_pool)

    def test_country_detect_button_updates_tag_badge(self):
        """Country detection button refreshes the row and shows the detected tag."""
        self._login()
        self._reset_storage()
        entry = _make_entry("detect_country", "detect-country.example.com", 12010, source_tag="manual")
        persistence.save_proxy_list(self._storage, [entry])
        self.page.goto(f"{self.base_url}/dashboard/proxies")

        with patch.object(proxy_routes, "resolve_entry_country_tag", return_value="US"):
            self.page.click('button.pp-detect-country-btn[data-key="detect_country"]')
            self.page.wait_for_selector("text=country: US", timeout=5000)

        saved_entry = persistence.load_proxy_list(self._storage)[0]
        self.assertEqual(saved_entry.tags, {"country": "US"})

    def test_detect_missing_country_works_on_empty_filtered_view(self):
        """Missing-country detection still works when the current filter has no visible rows."""
        self._login()
        self._reset_storage()
        entry = _make_entry("manual_missing", "manual-missing.example.com", 12011, source_tag="manual")
        persistence.save_proxy_list(self._storage, [entry])
        self.page.goto(f"{self.base_url}/dashboard/proxies?source=auto")

        self.assertEqual(self.page.locator("#select-all").count(), 0)
        self.assertTrue(self.page.locator("#detect-missing-country-btn").is_visible())

        with patch.object(proxy_routes, "resolve_entry_country_tag", return_value="US"):
            self.page.click("#detect-missing-country-btn")
            self.page.wait_for_url(
                re.compile(r".*/dashboard/proxies\?source=auto.*msg=.*"),
                timeout=5000,
            )

        saved_entry = persistence.load_proxy_list(self._storage)[0]
        self.assertEqual(saved_entry.tags, {"country": "US"})

    # ====================================================================
    # Tier 6: Batch Operations
    # ====================================================================

    def test_batch_checkbox_select_all(self):
        """Select-all checkbox selects all rows + updates selected count."""
        self._login()
        self._reset_storage()
        entries = [
            _make_entry("ba1", "batch-a.example.com", 12001),
            _make_entry("ba2", "batch-b.example.com", 12002),
        ]
        persistence.save_proxy_list(self._storage, entries)
        self.page.goto(f"{self.base_url}/dashboard/proxies")

        # Selected count should be 0 initially
        count_text = self.page.locator("#selected-count").text_content()
        self.assertEqual(count_text, "0")

        # Click select-all
        self.page.click("#select-all")
        # All row checkboxes should be checked
        row_checks = self.page.locator(".row-check")
        for i in range(row_checks.count()):
            self.assertTrue(row_checks.nth(i).is_checked())
        # Selected count should match
        count_text = self.page.locator("#selected-count").text_content()
        self.assertEqual(count_text, "2")

        # Uncheck select-all -> all unchecked, count back to 0
        self.page.click("#select-all")
        for i in range(row_checks.count()):
            self.assertFalse(row_checks.nth(i).is_checked())
        count_text = self.page.locator("#selected-count").text_content()
        self.assertEqual(count_text, "0")

    def test_batch_generate_flow(self):
        """Fill batch generate form -> submit -> proxies appear in list."""
        self._login()
        self._reset_storage()
        self.page.goto(f"{self.base_url}/dashboard/proxies/batch")
        self.page.fill("#host", "batch-gen.example.com")
        self.page.fill("#port_first", "20001")
        self.page.fill("#port_last", "20003")
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard/proxies**")
        # Should have 3 entries in the table
        rows = self.page.locator('tr[data-source="auto"]')
        self.assertEqual(rows.count(), 3)

    def test_batch_toggle_pool_flow(self):
        """Selecting rows and clicking batch pool toggle updates proxy state."""
        self._login()
        self._reset_storage()
        entries = [
            _make_entry("bt1", "toggle-a.example.com", 12001),
            _make_entry("bt2", "toggle-b.example.com", 12002),
        ]
        persistence.save_proxy_list(self._storage, entries)
        self.page.goto(f"{self.base_url}/dashboard/proxies")

        self.page.locator(".row-check").nth(0).check()
        self.page.click('button[data-batch-action="toggle-pool"][data-pool-state="off"]')
        self.page.wait_for_url("**/dashboard/proxies**")

        saved_entries = {entry.key: entry for entry in persistence.load_proxy_list(self._storage)}
        self.assertFalse(saved_entries["bt1"].in_random_pool)
        self.assertTrue(saved_entries["bt2"].in_random_pool)

    def test_batch_delete_flow(self):
        """Selecting rows and clicking batch delete removes the chosen proxies."""
        self._login()
        self._reset_storage()
        entries = [
            _make_entry("bd1", "delete-a.example.com", 12001),
            _make_entry("bd2", "delete-b.example.com", 12002),
            _make_entry("bd3", "keep-c.example.com", 12003),
        ]
        persistence.save_proxy_list(self._storage, entries)
        self.page.goto(f"{self.base_url}/dashboard/proxies")

        self.page.locator(".row-check").nth(0).check()
        self.page.locator(".row-check").nth(1).check()
        self.page.on("dialog", lambda dialog: dialog.accept())
        self.page.click('button[data-batch-action="delete"]')
        self.page.wait_for_url("**/dashboard/proxies**")

        remaining = persistence.load_proxy_list(self._storage)
        self.assertEqual([entry.key for entry in remaining], ["bd3"])

    # ====================================================================
    # Tier 7: Language Switch
    # ====================================================================

    def test_language_switch_updates_ui(self):
        """Click ZH language button -> sidebar labels switch to Chinese."""
        self._login()
        self.page.goto(f"{self.base_url}/dashboard?lang=en")
        with self.page.expect_navigation(
            url=re.compile(r".*/dashboard\?lang=zh$")
        ):
            self.page.click('a.pp-lang-btn[href*="lang=zh"]')
        self.page.wait_for_load_state("networkidle")
        # HTML lang attribute should be "zh"
        self.assertEqual(
            self.page.locator("html").get_attribute("lang"), "zh",
        )
        # Sidebar should contain Chinese text for "Dashboard" -> "仪表盘"
        sidebar_text = self.page.locator(".pp-sidebar-nav").text_content()
        self.assertIn("\u4eea\u8868\u76d8", sidebar_text)  # "仪表盘"

    # ====================================================================
    # Tier 8: Log Viewer
    # ====================================================================

    def test_logs_level_filter_dropdown(self):
        """Change level filter dropdown -> JS fetch updates table via AJAX."""
        self._login()
        self.page.goto(f"{self.base_url}/dashboard/logs")
        # Initially table should have log entries
        tbody = self.page.locator("#log-table-body")
        initial_rows = tbody.locator("tr").count()
        self.assertGreater(initial_rows, 0)

        # Select ERROR filter
        self.page.select_option("#level-filter", "ERROR")
        # Wait for the AJAX fetch to update the table
        self.page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('#log-table-body tr');
                return rows.length > 0 && Array.from(rows).every(
                    r => r.querySelector('.pp-log-error') !== null
                );
            }""",
            timeout=5000,
        )
        # All visible rows should be ERROR level
        error_rows = tbody.locator("tr")
        for i in range(error_rows.count()):
            badge = error_rows.nth(i).locator(".pp-badge")
            self.assertIn("ERROR", badge.text_content())


if __name__ == "__main__":
    unittest.main()
