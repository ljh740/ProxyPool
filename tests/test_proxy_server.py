import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

from proxy_server import (
    ProxyConfig,
    encode_socks_address,
    parse_basic_credentials,
    resolve_hop_host,
    resolve_target,
    split_host_port,
)
from upstream_pool import UpstreamHop


class ProxyServerTests(unittest.TestCase):
    def make_config(self, loopback_host_mode="auto", running_in_docker=True):
        return ProxyConfig(
            listen_host="0.0.0.0",
            listen_port=3128,
            auth_password="secret",
            auth_realm="Proxy",
            connect_timeout=5.0,
            connect_retries=3,
            relay_timeout=30.0,
            loopback_host_mode=loopback_host_mode,
            host_loopback_address="host.docker.internal",
            running_in_docker=running_in_docker,
        )

    def test_parse_basic_credentials_success(self):
        header = "Basic dXNlcjpwYXNz"
        self.assertEqual(parse_basic_credentials(header), ("user", "pass"))

    def test_parse_basic_credentials_rejects_invalid_value(self):
        self.assertIsNone(parse_basic_credentials("Bearer token"))
        self.assertIsNone(parse_basic_credentials("Basic !!!"))

    def test_split_host_port_supports_default(self):
        self.assertEqual(split_host_port("example.com", 80), ("example.com", 80))
        self.assertEqual(split_host_port("example.com:443", 80), ("example.com", 443))
        self.assertEqual(split_host_port("[2001:db8::1]:8443", 80), ("2001:db8::1", 8443))

    def test_resolve_target_for_connect(self):
        host, port, forward_target, connect_tunnel = resolve_target("CONNECT", "ipinfo.io:443", [])
        self.assertEqual((host, port, forward_target, connect_tunnel), ("ipinfo.io", 443, "ipinfo.io:443", True))

    def test_resolve_target_for_http_request(self):
        headers = [("Host", "ipinfo.io")]
        host, port, forward_target, connect_tunnel = resolve_target("GET", "http://ipinfo.io/json", headers)
        self.assertEqual((host, port, forward_target, connect_tunnel), ("ipinfo.io", 80, "/json", False))

    def test_encode_socks_address_for_domain(self):
        payload = encode_socks_address("ipinfo.io", 443)
        self.assertEqual(payload[:2], bytes([0x03, len("ipinfo.io")]))
        self.assertEqual(payload[-2:], (443).to_bytes(2, "big"))

    def test_resolve_hop_host_rewrites_first_hop_loopback_in_docker(self):
        config = self.make_config()
        hop = UpstreamHop("http", "127.0.0.1", 30001, "", "")
        self.assertEqual(resolve_hop_host(config, hop, 0), "host.docker.internal")

    def test_resolve_hop_host_keeps_later_loopback_hops(self):
        config = self.make_config()
        hop = UpstreamHop("http", "127.0.0.1", 30001, "", "")
        self.assertEqual(resolve_hop_host(config, hop, 1), "127.0.0.1")

    def test_resolve_hop_host_can_disable_rewrite(self):
        config = self.make_config(loopback_host_mode="off")
        hop = UpstreamHop("http", "localhost", 30001, "", "")
        self.assertEqual(resolve_hop_host(config, hop, 0), "localhost")


if __name__ == "__main__":
    unittest.main()
