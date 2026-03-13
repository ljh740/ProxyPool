import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

from proxy_server import encode_socks_address, parse_basic_credentials, resolve_target, split_host_port


class ProxyServerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
