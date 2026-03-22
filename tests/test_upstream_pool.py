import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

upstream_pool = importlib.import_module("upstream_pool")

parse_upstream_line = upstream_pool.parse_upstream_line
build_list_entries = upstream_pool.build_list_entries


class UpstreamPoolTests(unittest.TestCase):
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
        self.assertEqual(entry.key, upstream_pool.compute_entry_key(entry.hops))
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

    def test_parse_upstream_line_keeps_pipe_in_password_without_chain_split(self):
        entry = parse_upstream_line(
            "dc.decodo.com:10001:user:pa|ss",
            0,
            "socks5",
            "",
            "",
        )
        self.assertEqual(entry.chain_length, 1)
        self.assertEqual(entry.first_hop.password, "pa|ss")

    def test_parse_upstream_line_uri_inherits_missing_password(self):
        entry = parse_upstream_line(
            "socks5://user@dc.decodo.com:10001",
            0,
            "socks5",
            "default-user",
            "default-pass",
        )
        self.assertEqual(entry.first_hop.username, "user")
        self.assertEqual(entry.first_hop.password, "default-pass")

    def test_parse_upstream_line_uri_inherits_missing_username(self):
        entry = parse_upstream_line(
            "socks5://:pass@dc.decodo.com:10001",
            0,
            "socks5",
            "default-user",
            "default-pass",
        )
        self.assertEqual(entry.first_hop.username, "default-user")
        self.assertEqual(entry.first_hop.password, "pass")

    def test_build_list_entries_supports_comments_and_multiple_formats(self):
        entries = build_list_entries(
            [
                "# comment",
                "http://127.0.0.1:30001 | socks5://u1:p1@dc.decodo.com:10001",
                "dc.decodo.com:10002:u2:p2",
            ],
            "socks5",
            "",
            "",
        )

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].chain_length, 2)
        self.assertEqual(entries[1].first_hop.port, 10002)

    def test_compute_entry_key_changes_when_credentials_change(self):
        hop_a = upstream_pool.UpstreamHop("socks5", "proxy.example.com", 10001, "user-a", "pass")
        hop_b = upstream_pool.UpstreamHop("socks5", "proxy.example.com", 10001, "user-b", "pass")

        self.assertNotEqual(
            upstream_pool.compute_entry_key((hop_a,)),
            upstream_pool.compute_entry_key((hop_b,)),
        )

    def test_build_list_entries_keep_same_key_after_reordering(self):
        first = parse_upstream_line(
            "dc.decodo.com:10001:user-a:pass-a",
            0,
            "socks5",
            "",
            "",
        )
        second = parse_upstream_line(
            "dc.decodo.com:10001:user-a:pass-a",
            5,
            "socks5",
            "",
            "",
        )

        self.assertEqual(first.key, second.key)


if __name__ == "__main__":
    unittest.main()
