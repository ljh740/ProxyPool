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
        self.assertEqual(pool.entries[-1].port, 10003)

    def test_parse_upstream_line_supports_uri(self):
        entry = parse_upstream_line(
            "socks5://user:pass@dc.decodo.com:10001",
            0,
            "http",
            "",
            "",
        )
        self.assertEqual(entry.scheme, "socks5")
        self.assertEqual(entry.host, "dc.decodo.com")
        self.assertEqual(entry.port, 10001)
        self.assertEqual(entry.username, "user")
        self.assertEqual(entry.password, "pass")

    def test_parse_upstream_line_supports_colon_format(self):
        entry = parse_upstream_line(
            "dc.decodo.com:10001:user:pass",
            1,
            "socks5",
            "",
            "",
        )
        self.assertEqual(entry.scheme, "socks5")
        self.assertEqual(entry.key, "upstream_2")
        self.assertEqual(entry.password, "pass")

    def test_load_upstream_pool_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, "upstreams.txt")
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write("# comment\n")
                handle.write("socks5://u1:p1@dc.decodo.com:10001\n")
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
        self.assertEqual(pool.entries[1].port, 10002)

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
        self.assertEqual(pool.entries[0].username, "default-user")
        self.assertEqual(pool.entries[0].password, "default-pass")
        self.assertEqual(pool.entries[1].username, "user2")
        self.assertEqual(pool.entries[1].password, "pass2")


if __name__ == "__main__":
    unittest.main()
