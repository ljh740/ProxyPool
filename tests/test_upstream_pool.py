import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

from upstream_pool import build_range_pool, load_upstream_pool_from_env, parse_upstream_line


class UpstreamPoolTests(unittest.TestCase):
    def test_build_range_pool_uses_port_range(self):
        with patch.dict(
            os.environ,
            {
                "UPSTREAM_SCHEME": "socks5",
                "UPSTREAM_HOST": "dc.decodo.com",
                "UP_USER": "user",
                "UP_PASS": "pass",
                "PORT_FIRST": "10001",
                "PORT_LAST": "10003",
            },
            clear=False,
        ):
            pool = build_range_pool()

        self.assertEqual(pool.source, "range")
        self.assertEqual(pool.count, 3)
        self.assertEqual(pool.entries[0].key, "10001")
        self.assertEqual(pool.entries[-1].first_hop.port, 10003)

    def test_parse_upstream_line_supports_uri(self):
        entry = parse_upstream_line(
            "socks5://user:pass@dc.decodo.com:10001",
            0,
            "http",
            "",
            "",
        )
        hop = entry.first_hop
        self.assertEqual(hop.scheme, "socks5")
        self.assertEqual(hop.host, "dc.decodo.com")
        self.assertEqual(hop.port, 10001)
        self.assertEqual(hop.username, "user")
        self.assertEqual(hop.password, "pass")

    def test_parse_upstream_line_supports_colon_format(self):
        entry = parse_upstream_line(
            "dc.decodo.com:10001:user:pass",
            1,
            "socks5",
            "",
            "",
        )
        self.assertEqual(entry.first_hop.scheme, "socks5")
        self.assertEqual(entry.key, "upstream_2")
        self.assertEqual(entry.first_hop.password, "pass")

    def test_parse_upstream_line_supports_chain_mode(self):
        entry = parse_upstream_line(
            "http://127.0.0.1:30001 | dc.decodo.com:10001",
            0,
            "socks5",
            "default-user",
            "default-pass",
        )
        self.assertEqual(entry.chain_length, 2)
        self.assertEqual(entry.hops[0].scheme, "http")
        self.assertEqual(entry.hops[0].username, "")
        self.assertEqual(entry.hops[1].scheme, "socks5")
        self.assertEqual(entry.hops[1].username, "default-user")
        self.assertEqual(entry.hops[1].password, "default-pass")

    def test_load_upstream_pool_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, "upstreams.txt")
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write("# comment\n")
                handle.write("http://127.0.0.1:30001 | socks5://u1:p1@dc.decodo.com:10001\n")
                handle.write("dc.decodo.com:10002:u2:p2\n")

            with patch.dict(
                os.environ,
                {
                    "UPSTREAM_LIST_FILE": file_path,
                    "UPSTREAM_SCHEME": "socks5",
                },
                clear=False,
            ):
                pool = load_upstream_pool_from_env()

        self.assertEqual(pool.source, "file")
        self.assertEqual(pool.count, 2)
        self.assertEqual(pool.entries[0].chain_length, 2)
        self.assertEqual(pool.entries[1].first_hop.port, 10002)

    def test_load_upstream_pool_from_inline_text(self):
        with patch.dict(
            os.environ,
            {
                "UPSTREAM_LIST_FILE": "",
                "UPSTREAM_LIST": "dc.decodo.com:10001\ndc.decodo.com:10002:user2:pass2\n",
                "UPSTREAM_SCHEME": "socks5",
                "UP_USER": "default-user",
                "UP_PASS": "default-pass",
            },
            clear=False,
        ):
            pool = load_upstream_pool_from_env()

        self.assertEqual(pool.source, "inline")
        self.assertEqual(pool.count, 2)
        self.assertEqual(pool.entries[0].first_hop.username, "default-user")
        self.assertEqual(pool.entries[0].first_hop.password, "default-pass")
        self.assertEqual(pool.entries[1].first_hop.username, "user2")
        self.assertEqual(pool.entries[1].first_hop.password, "pass2")


if __name__ == "__main__":
    unittest.main()
