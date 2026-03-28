import importlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

config_center = importlib.import_module("config_center")
upstream_pool = importlib.import_module("upstream_pool")
compat_ports = importlib.import_module("compat_ports")
persistence = importlib.import_module("persistence")
proxy_tags = importlib.import_module("proxy_tags")

AppConfig = config_center.AppConfig
UpstreamEntry = upstream_pool.UpstreamEntry
UpstreamHop = upstream_pool.UpstreamHop
CompatPortMapping = compat_ports.CompatPortMapping

save_proxy_list = persistence.save_proxy_list
load_proxy_list = persistence.load_proxy_list
save_config = persistence.save_config
load_config = persistence.load_config
clear_admin_password = persistence.clear_admin_password
save_batch_params = persistence.save_batch_params
load_batch_params = persistence.load_batch_params
save_compat_port_mappings = persistence.save_compat_port_mappings
load_compat_port_mappings = persistence.load_compat_port_mappings
SQLiteStorage = persistence.SQLiteStorage
entry_country_tag = proxy_tags.entry_country_tag
merge_country_tag_updates = proxy_tags.merge_country_tag_updates
normalize_country_tag = proxy_tags.normalize_country_tag


class _BrokenStorage:
    def get(self, state_key):
        raise ConnectionError("storage unavailable")

    def set(self, state_key, value):
        raise ConnectionError("storage unavailable")


def _make_entry(
    key="test_1",
    host="proxy.example.com",
    port=10001,
    source_tag="manual",
    in_random_pool=True,
    tags=None,
):
    hop = UpstreamHop(
        scheme="socks5",
        host=host,
        port=port,
        username="user",
        password="pass",
    )
    return UpstreamEntry(
        key=key,
        label="test",
        hops=(hop,),
        source_tag=source_tag,
        in_random_pool=in_random_pool,
        tags=tags or {},
    )


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.storage = SQLiteStorage(":memory:")

    def tearDown(self):
        self.storage.close()

    def test_save_load_proxy_list(self):
        entries = [
            _make_entry("e1", "host1.com", 10001, "manual", True, {"country": "US"}),
            _make_entry("e2", "host2.com", 10002, "auto", False, {"country": "DE"}),
        ]
        save_proxy_list(self.storage, entries)
        loaded = load_proxy_list(self.storage)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].key, "e1")
        self.assertEqual(loaded[0].first_hop.host, "host1.com")
        self.assertEqual(loaded[0].source_tag, "manual")
        self.assertTrue(loaded[0].in_random_pool)
        self.assertEqual(loaded[0].tags, {"country": "US"})
        self.assertEqual(loaded[1].key, "e2")
        self.assertEqual(loaded[1].first_hop.host, "host2.com")
        self.assertEqual(loaded[1].source_tag, "auto")
        self.assertFalse(loaded[1].in_random_pool)
        self.assertEqual(loaded[1].tags, {"country": "DE"})

    def test_normalize_country_tag(self):
        self.assertEqual(normalize_country_tag(" us "), "US")
        self.assertEqual(normalize_country_tag(None), "")
        self.assertEqual(entry_country_tag(_make_entry(tags={"country": " de "})), "DE")

    def test_merge_country_tag_updates(self):
        entries = [
            _make_entry("e1", "host1.com", 10001),
            _make_entry("e2", "host2.com", 10002, tags={"country": "FR"}),
        ]

        merged, changed = merge_country_tag_updates(
            entries,
            {"e1": " us ", "e2": "fr", "missing": "de"},
        )

        self.assertTrue(changed)
        self.assertEqual(merged[0].tags, {"country": "US"})
        self.assertEqual(merged[1].tags, {"country": "FR"})

    def test_merge_country_tag_updates_reports_no_change(self):
        entry = _make_entry("e1", "host1.com", 10001, tags={"country": "US"})

        merged, changed = merge_country_tag_updates([entry], {"e1": " us "})

        self.assertFalse(changed)
        self.assertEqual(merged[0].tags, {"country": "US"})

    def test_save_load_config(self):
        config = {
            "SALT": "test-salt",
            "RANDOM_POOL_PREFIX": "rnd_",
            "COUNTRY_DETECT_MAX_WORKERS": "6",
            "STATE_DB_PATH": "/tmp/test-proxypool.sqlite3",
            "ADMIN_PASSWORD": "panel-admin",
            "WEB_PORT": "9090",
        }
        save_config(self.storage, config)
        loaded = load_config(self.storage)
        expected = AppConfig.from_mapping(config).persisted_values()

        self.assertEqual(loaded, expected)
        self.assertEqual(loaded["SALT"], "test-salt")
        self.assertEqual(loaded["RANDOM_POOL_PREFIX"], "rnd_")
        self.assertEqual(loaded["COUNTRY_DETECT_MAX_WORKERS"], "6")
        stored_payload = json.loads(self.storage.get(persistence.STATE_KEY_CONFIG))
        self.assertEqual(stored_payload, expected)
        self.assertNotIn("STATE_DB_PATH", stored_payload)
        self.assertNotIn("WEB_PORT", stored_payload)
        self.assertEqual(stored_payload["ADMIN_PASSWORD"], "panel-admin")

    def test_clear_admin_password_returns_storage_to_setup_mode(self):
        save_config(
            self.storage,
            {
                "AUTH_PASSWORD": "proxy-secret",
                "SALT": "stable-salt",
                "ADMIN_PASSWORD": "panel-admin",
            },
        )

        clear_admin_password(self.storage)

        loaded = load_config(self.storage)
        self.assertEqual(loaded["AUTH_PASSWORD"], "proxy-secret")
        self.assertEqual(loaded["SALT"], "stable-salt")
        self.assertEqual(loaded["ADMIN_PASSWORD"], "")

    def test_save_load_batch_params(self):
        params = {
            "scheme": "socks5",
            "host": "proxy.example.com",
            "username": "batch_user",
            "password": "batch_pass",
            "port_first": 20001,
            "port_last": 20100,
        }
        save_batch_params(self.storage, params)
        loaded = load_batch_params(self.storage)

        self.assertEqual(loaded, params)
        self.assertEqual(loaded["scheme"], "socks5")
        self.assertEqual(loaded["port_first"], 20001)
        self.assertEqual(loaded["port_last"], 20100)

    def test_save_load_compat_port_mappings(self):
        mappings = [
            CompatPortMapping(
                listen_port=33101,
                target_type="session_name",
                target_value="browser-a",
                enabled=True,
                note="sticky",
            ),
            CompatPortMapping(
                listen_port=33100,
                target_type="entry_key",
                target_value="entry_abc",
                enabled=False,
                note="fixed",
            ),
        ]

        save_compat_port_mappings(self.storage, mappings)
        loaded = load_compat_port_mappings(self.storage)

        self.assertEqual([item.listen_port for item in loaded], [33100, 33101])
        self.assertEqual(loaded[0].target_type, "entry_key")
        self.assertEqual(loaded[0].target_value, "entry_abc")
        self.assertFalse(loaded[0].enabled)
        self.assertEqual(loaded[1].target_type, "session_name")
        self.assertEqual(loaded[1].target_value, "browser-a")
        self.assertTrue(loaded[1].enabled)

    def test_storage_connection_error_handling(self):
        storage = _BrokenStorage()

        with self.assertLogs("persistence", level="ERROR") as captured:
            # load functions should return safe defaults on storage error
            self.assertEqual(load_proxy_list(storage), [])
            self.assertEqual(load_config(storage), {})
            self.assertEqual(load_batch_params(storage), {})
            self.assertEqual(load_compat_port_mappings(storage), [])

            # save functions should not raise on storage error
            save_proxy_list(storage, [_make_entry()])
            save_config(storage, {"AUTH_PASSWORD": "secret"})
            save_batch_params(storage, {"scheme": "http"})
            save_compat_port_mappings(
                storage,
                [
                    CompatPortMapping(
                        listen_port=33100,
                        target_type="session_name",
                        target_value="session-1",
                    )
                ],
            )

        self.assertGreaterEqual(len(captured.output), 8)


if __name__ == "__main__":
    unittest.main()
