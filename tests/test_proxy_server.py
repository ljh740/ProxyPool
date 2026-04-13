import importlib
import os
import socket
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

proxy_server = importlib.import_module("proxy_server")
upstream_pool = importlib.import_module("upstream_pool")
config_center = importlib.import_module("config_center")
compat_ports = importlib.import_module("compat_ports")

AppConfig = config_center.AppConfig
ProxyConfig = proxy_server.ProxyConfig
encode_socks_address = proxy_server.encode_socks_address
parse_basic_credentials = proxy_server.parse_basic_credentials
resolve_hop_host = proxy_server.resolve_hop_host
resolve_target = proxy_server.resolve_target
should_send_absolute_form = proxy_server.should_send_absolute_form
split_host_port = proxy_server.split_host_port
UpstreamEntry = upstream_pool.UpstreamEntry
UpstreamHop = upstream_pool.UpstreamHop
CompatPortMapping = compat_ports.CompatPortMapping


def _build_socks5_greeting(methods):
    return bytes([proxy_server.SOCKS_VERSION, len(methods), *methods])


def _build_socks5_auth(username, password):
    username_bytes = username.encode("utf-8")
    password_bytes = password.encode("utf-8")
    return (
        bytes([proxy_server.SOCKS_AUTH_VERSION, len(username_bytes)])
        + username_bytes
        + bytes([len(password_bytes)])
        + password_bytes
    )


def _build_socks5_connect_request(host, port, *, command=proxy_server.SOCKS_CMD_CONNECT):
    return bytes([proxy_server.SOCKS_VERSION, command, 0x00]) + encode_socks_address(
        host,
        port,
    )


def _recv_available(sock):
    chunks = []
    sock.settimeout(0.1)
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


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

    def make_entry(
        self,
        key="entry_1",
        host="proxy.example.com",
        port=10001,
        scheme="socks5",
    ):
        return UpstreamEntry(
            key=key,
            label=f"{host}:{port}",
            hops=(UpstreamHop(scheme, host, port, "user", "pass"),),
        )

    def make_server(self, *, config=None, route_entry=None):
        server = MagicMock()
        server.config = config or self.make_config()
        server.router = MagicMock()
        server.router.route_entry.return_value = route_entry
        return server

    def run_handler(self, payload, *, server=None, handler_class=None):
        handler_class = handler_class or proxy_server.ProxyRequestHandler
        server = server or self.make_server()
        client_sock, server_sock = socket.socketpair()
        try:
            client_sock.sendall(payload)
            handler_class(server_sock, ("127.0.0.1", 12345), server)
            return _recv_available(client_sock)
        finally:
            client_sock.close()
            try:
                server_sock.close()
            except OSError:
                pass

    def test_parse_basic_credentials_success(self):
        header = "Basic dXNlcjpwYXNz"
        self.assertEqual(parse_basic_credentials(header), ("user", "pass"))

    def test_parse_basic_credentials_rejects_invalid_value(self):
        self.assertIsNone(parse_basic_credentials("Bearer token"))
        self.assertIsNone(parse_basic_credentials("Basic !!!"))

    def test_authenticate_rejects_unconfigured_server_password(self):
        handler = proxy_server.ProxyRequestHandler.__new__(
            proxy_server.ProxyRequestHandler
        )
        handler.server = MagicMock()
        handler.server.config = ProxyConfig(
            listen_host="0.0.0.0",
            listen_port=3128,
            auth_password="",
            auth_realm="Proxy",
            connect_timeout=5.0,
            connect_retries=3,
            relay_timeout=30.0,
            loopback_host_mode="auto",
            host_loopback_address="host.docker.internal",
            running_in_docker=False,
        )

        with self.assertRaises(proxy_server.ClientError) as ctx:
            handler.authenticate(
                [("Proxy-Authorization", "Basic dXNlcjpzZWNyZXQ=")]
            )

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("AUTH_PASSWORD is not configured", ctx.exception.body)

    def test_proxy_handler_dispatches_http_requests_to_http_session(self):
        server = self.make_server()
        client_sock, server_sock = socket.socketpair()
        try:
            client_sock.sendall(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
            with patch.object(
                proxy_server.HttpProxyRequestHandler,
                "handle",
                autospec=True,
            ) as http_handle:
                proxy_server.ProxyRequestHandler(
                    server_sock,
                    ("127.0.0.1", 12345),
                    server,
                )
        finally:
            client_sock.close()
            try:
                server_sock.close()
            except OSError:
                pass

        http_handle.assert_called_once()

    def test_proxy_handler_accepts_socks5_connect_requests_on_main_port(self):
        server = self.make_server(route_entry=self.make_entry())
        payload = (
            _build_socks5_greeting(
                [proxy_server.SOCKS_METHOD_NO_AUTH, proxy_server.SOCKS_METHOD_USERNAME_PASSWORD]
            )
            + _build_socks5_auth("user-a", "secret")
            + _build_socks5_connect_request("example.com", 443)
        )
        upstream_socket = MagicMock()

        with patch.object(
            proxy_server,
            "open_upstream_tunnel",
            return_value=upstream_socket,
        ) as open_tunnel_mock, patch.object(
            proxy_server,
            "relay_bidirectional",
        ) as relay_mock:
            response = self.run_handler(payload, server=server)

        self.assertEqual(
            response,
            bytes(
                [
                    proxy_server.SOCKS_VERSION,
                    proxy_server.SOCKS_METHOD_USERNAME_PASSWORD,
                    proxy_server.SOCKS_AUTH_VERSION,
                    proxy_server.SOCKS_AUTH_STATUS_SUCCESS,
                ]
            )
            + proxy_server.build_socks5_reply(proxy_server.SOCKS_REPLY_SUCCEEDED),
        )
        server.router.route_entry.assert_called_once_with("user-a")
        open_tunnel_mock.assert_called_once_with(
            server.config,
            server.router.route_entry.return_value,
            "example.com",
            443,
        )
        relay_mock.assert_called_once()
        upstream_socket.close.assert_called_once()

    def test_proxy_handler_rejects_socks5_when_username_password_not_offered(self):
        server = self.make_server()

        response = self.run_handler(
            _build_socks5_greeting([proxy_server.SOCKS_METHOD_NO_AUTH]),
            server=server,
        )

        self.assertEqual(
            response,
            bytes(
                [
                    proxy_server.SOCKS_VERSION,
                    proxy_server.SOCKS_METHOD_NO_ACCEPTABLE,
                ]
            ),
        )
        server.router.route_entry.assert_not_called()

    def test_proxy_handler_rejects_socks5_invalid_credentials(self):
        server = self.make_server(route_entry=self.make_entry())
        payload = _build_socks5_greeting(
            [proxy_server.SOCKS_METHOD_USERNAME_PASSWORD]
        ) + _build_socks5_auth("user-a", "wrong-secret")

        response = self.run_handler(payload, server=server)

        self.assertEqual(
            response,
            bytes(
                [
                    proxy_server.SOCKS_VERSION,
                    proxy_server.SOCKS_METHOD_USERNAME_PASSWORD,
                    proxy_server.SOCKS_AUTH_VERSION,
                    proxy_server.SOCKS_AUTH_STATUS_FAILURE,
                ]
            ),
        )
        server.router.route_entry.assert_not_called()

    def test_proxy_handler_rejects_socks5_unsupported_command(self):
        server = self.make_server(route_entry=self.make_entry())
        payload = (
            _build_socks5_greeting([proxy_server.SOCKS_METHOD_USERNAME_PASSWORD])
            + _build_socks5_auth("user-a", "secret")
            + _build_socks5_connect_request("example.com", 443, command=0x02)
        )

        response = self.run_handler(payload, server=server)

        self.assertEqual(
            response,
            bytes(
                [
                    proxy_server.SOCKS_VERSION,
                    proxy_server.SOCKS_METHOD_USERNAME_PASSWORD,
                    proxy_server.SOCKS_AUTH_VERSION,
                    proxy_server.SOCKS_AUTH_STATUS_SUCCESS,
                ]
            )
            + proxy_server.build_socks5_reply(
                proxy_server.SOCKS_REPLY_COMMAND_NOT_SUPPORTED
            ),
        )
        server.router.route_entry.assert_not_called()

    def test_split_host_port_supports_default(self):
        self.assertEqual(split_host_port("example.com", 80), ("example.com", 80))
        self.assertEqual(split_host_port("example.com:443", 80), ("example.com", 443))
        self.assertEqual(
            split_host_port("[2001:db8::1]:8443", 80), ("2001:db8::1", 8443)
        )

    def test_resolve_target_for_connect(self):
        host, port, forward_target, connect_tunnel = resolve_target(
            "CONNECT", "ipinfo.io:443", []
        )
        self.assertEqual(
            (host, port, forward_target, connect_tunnel),
            ("ipinfo.io", 443, "ipinfo.io:443", True),
        )

    def test_resolve_target_for_http_request(self):
        headers = [("Host", "ipinfo.io")]
        host, port, forward_target, connect_tunnel = resolve_target(
            "GET", "http://ipinfo.io/json", headers
        )
        self.assertEqual(
            (host, port, forward_target, connect_tunnel),
            ("ipinfo.io", 80, "/json", False),
        )

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

    def test_should_send_absolute_form_for_chained_http_final_hop(self):
        request = type("Request", (), {"connect_tunnel": False})()
        entry = UpstreamEntry(
            key="upstream_1",
            label="chain",
            hops=(
                UpstreamHop("http", "127.0.0.1", 30001, "", ""),
                UpstreamHop("http", "proxy.example.com", 8080, "user", "pass"),
            ),
        )
        self.assertTrue(should_send_absolute_form(request, entry))

    def test_should_not_send_absolute_form_for_connect_requests(self):
        request = type("Request", (), {"connect_tunnel": True})()
        entry = UpstreamEntry(
            key="upstream_1",
            label="chain",
            hops=(UpstreamHop("http", "proxy.example.com", 8080, "user", "pass"),),
        )
        self.assertFalse(should_send_absolute_form(request, entry))

    def test_should_not_send_absolute_form_for_socks_final_hop(self):
        request = type("Request", (), {"connect_tunnel": False})()
        entry = UpstreamEntry(
            key="upstream_1",
            label="chain",
            hops=(
                UpstreamHop("http", "127.0.0.1", 30001, "", ""),
                UpstreamHop("socks5", "proxy.example.com", 1080, "user", "pass"),
            ),
        )
        self.assertFalse(should_send_absolute_form(request, entry))

    def test_build_router_uses_explicit_persisted_entries(self):
        app_config = AppConfig.from_mapping({
            "AUTH_PASSWORD": "secret",
        })
        entry = UpstreamEntry(
            key="entry_1",
            label="proxy.example.com:10001",
            hops=(UpstreamHop("socks5", "proxy.example.com", 10001, "user", "pass"),),
        )

        router = proxy_server.build_router(app_config, [entry])

        self.assertEqual(router.upstream_pool.source, "admin")
        self.assertEqual(router.upstream_count, 1)
        self.assertEqual(router.get_entry("entry_1").first_hop.host, "proxy.example.com")

    def test_build_router_accepts_empty_persisted_entries(self):
        app_config = AppConfig.from_mapping({
            "AUTH_PASSWORD": "secret",
        })

        router = proxy_server.build_router(app_config, [])

        self.assertEqual(router.upstream_pool.source, "admin")
        self.assertEqual(router.upstream_count, 0)

    def test_compat_handler_routes_session_name(self):
        handler = proxy_server.CompatProxyRequestHandler.__new__(
            proxy_server.CompatProxyRequestHandler
        )
        handler.server = MagicMock()
        handler.server.mapping = CompatPortMapping(
            listen_port=33100,
            target_type="session_name",
            target_value="browser-a",
        )
        expected_entry = MagicMock()
        handler.server.router = MagicMock()
        handler.server.router.route_entry.return_value = expected_entry

        username = handler.authenticate([])
        resolved = handler.resolve_upstream_entry(username)

        self.assertEqual(username, "browser-a")
        self.assertIs(resolved, expected_entry)
        handler.server.router.route_entry.assert_called_once_with("browser-a")

    def test_compat_handler_rejects_missing_entry_key(self):
        handler = proxy_server.CompatProxyRequestHandler.__new__(
            proxy_server.CompatProxyRequestHandler
        )
        handler.server = MagicMock()
        handler.server.mapping = CompatPortMapping(
            listen_port=33100,
            target_type="entry_key",
            target_value="missing-entry",
        )
        handler.server.router = MagicMock()
        handler.server.router.get_entry.return_value = None

        with self.assertRaises(proxy_server.ClientError) as ctx:
            handler.resolve_upstream_entry("missing-entry")

        self.assertEqual(ctx.exception.status_code, 503)

    def test_reload_compat_listeners_normalizes_non_dict_registry(self):
        parent_server = MagicMock()
        parent_server.compat_listeners = MagicMock()
        parent_server.router = MagicMock()
        storage = MagicMock()

        with patch("persistence.load_compat_port_mappings", return_value=[]):
            registry = proxy_server.reload_compat_listeners(parent_server, storage)

        self.assertEqual(registry, {})
        self.assertEqual(parent_server.compat_listeners, {})

    def test_reload_compat_listeners_starts_and_stops_servers(self):
        parent_server = MagicMock()
        parent_server.config.listen_host = "0.0.0.0"
        parent_server.router = MagicMock()
        storage = MagicMock()
        mapping = CompatPortMapping(
            listen_port=33100,
            target_type="session_name",
            target_value="browser-a",
        )
        fake_server = MagicMock()
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True

        with patch("persistence.load_compat_port_mappings", return_value=[mapping]), \
             patch.object(proxy_server, "CompatTCPServer", return_value=fake_server) as compat_server_cls, \
             patch.object(proxy_server.threading, "Thread", return_value=fake_thread):
            registry = proxy_server.reload_compat_listeners(parent_server, storage)

        self.assertIn(33100, registry)
        compat_server_cls.assert_called_once_with(
            ("0.0.0.0", 33100),
            proxy_server.CompatProxyRequestHandler,
            parent_server,
            mapping,
        )
        fake_thread.start.assert_called_once()

        with patch("persistence.load_compat_port_mappings", return_value=[]):
            registry = proxy_server.reload_compat_listeners(parent_server, storage)

        self.assertEqual(registry, {})
        fake_server.shutdown.assert_called_once()
        fake_server.server_close.assert_called_once()
        fake_thread.join.assert_called_once()

    def test_reload_compat_listeners_stops_removed_servers_in_parallel(self):
        parent_server = MagicMock()
        parent_server.config.listen_host = "0.0.0.0"
        parent_server.router = MagicMock()
        storage = MagicMock()
        parent_server.compat_listeners = {}

        for port in (33100, 33101, 33102, 33103):
            listener = MagicMock()
            listener.mapping = CompatPortMapping(
                listen_port=port,
                target_type="session_name",
                target_value="browser-%s" % port,
            )
            parent_server.compat_listeners[port] = listener

        def slow_stop(listener):
            del listener
            time.sleep(0.1)

        with patch("persistence.load_compat_port_mappings", return_value=[]), \
             patch.object(proxy_server, "_stop_compat_listener", side_effect=slow_stop) as stop_mock:
            started_at = time.perf_counter()
            registry = proxy_server.reload_compat_listeners(parent_server, storage)
            elapsed = time.perf_counter() - started_at

        self.assertEqual(registry, {})
        self.assertEqual(stop_mock.call_count, 4)
        self.assertLess(elapsed, 0.25)

    def test_main_bootstraps_router_from_storage_proxy_list(self):
        app_config = AppConfig.from_mapping({
            "PROXY_HOST": "127.0.0.1",
            "PROXY_PORT": "3128",
            "AUTH_PASSWORD": "secret",
            "ADMIN_PASSWORD": "admin-secret",
        })
        proxy_entry = UpstreamEntry(
            key="persisted_1",
            label="proxy.example.com:10001",
            hops=(UpstreamHop("socks5", "proxy.example.com", 10001, "user", "pass"),),
        )
        storage = MagicMock()
        storage.get.side_effect = lambda key: None

        fake_server = MagicMock()
        fake_server.router = None
        fake_server.serve_forever.side_effect = KeyboardInterrupt()
        fake_server.server_close.return_value = None

        with patch.object(proxy_server.AppConfig, "from_bootstrap_env", return_value=app_config), \
             patch.object(proxy_server.AppConfig, "load", return_value=app_config), \
             patch("persistence.open_storage", return_value=storage), \
             patch.object(proxy_server, "build_router", wraps=proxy_server.build_router) as build_router_mock, \
             patch("persistence.load_proxy_list", return_value=[proxy_entry]) as load_proxy_list_mock, \
             patch("persistence.save_proxy_list") as save_proxy_list_mock, \
             patch.object(proxy_server, "ThreadedTCPServer", return_value=fake_server), \
             patch("web_admin.start_admin_server"):
            proxy_server.main()

        build_router_mock.assert_called_once_with(app_config, [proxy_entry])
        load_proxy_list_mock.assert_called_once_with(storage)
        save_proxy_list_mock.assert_called_once_with(storage, [])


if __name__ == "__main__":
    unittest.main()
