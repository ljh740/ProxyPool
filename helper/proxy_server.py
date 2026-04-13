#!/usr/bin/env python3

import base64
import binascii
import ipaddress
import logging
import os
import selectors
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass
from typing import Callable, List, Mapping, Tuple
from urllib.parse import urlsplit

from auth import check_password
from compat_ports import TARGET_TYPE_ENTRY_KEY
from config_center import AppConfig

BUFFER_SIZE = 65536
MAX_HEADER_LINE = 8192
MAX_HEADER_LINES = 200
SUPPORTED_UPSTREAM_SCHEMES = {"http", "socks5", "socks5h"}
SOCKS_VERSION = 0x05
SOCKS_AUTH_VERSION = 0x01
SOCKS_METHOD_NO_AUTH = 0x00
SOCKS_METHOD_USERNAME_PASSWORD = 0x02
SOCKS_METHOD_NO_ACCEPTABLE = 0xFF
SOCKS_CMD_CONNECT = 0x01
SOCKS_ATYP_IPV4 = 0x01
SOCKS_ATYP_DOMAIN = 0x03
SOCKS_ATYP_IPV6 = 0x04
SOCKS_REPLY_SUCCEEDED = 0x00
SOCKS_REPLY_GENERAL_FAILURE = 0x01
SOCKS_REPLY_COMMAND_NOT_SUPPORTED = 0x07
SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED = 0x08
SOCKS_AUTH_STATUS_SUCCESS = 0x00
SOCKS_AUTH_STATUS_FAILURE = 0x01
FILTERED_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "upgrade",
}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
LOGGER = logging.getLogger("proxy_server")


class ProxyError(Exception):
    pass


class ClientError(ProxyError):
    def __init__(self, status_code: int, reason: str, body: str, headers=None):
        super().__init__(body)
        self.status_code = status_code
        self.reason = reason
        self.body = body
        self.headers = headers or []


class UpstreamError(ProxyError):
    pass


class Socks5ProtocolError(ProxyError):
    def __init__(self, message: str, *, reply_code: int | None = None):
        super().__init__(message)
        self.reply_code = reply_code


@dataclass
class ProxyConfig:
    listen_host: str
    listen_port: int
    auth_password: str
    auth_realm: str
    connect_timeout: float
    connect_retries: int
    relay_timeout: float
    loopback_host_mode: str
    host_loopback_address: str
    running_in_docker: bool

    @classmethod
    def from_app_config(cls, app_config: AppConfig, *, strict: bool = True):
        config = cls(
            listen_host=app_config.proxy_host,
            listen_port=app_config.proxy_port,
            auth_password=app_config.auth_password,
            auth_realm=app_config.auth_realm,
            connect_timeout=app_config.upstream_connect_timeout,
            connect_retries=app_config.upstream_connect_retries,
            relay_timeout=app_config.relay_timeout,
            loopback_host_mode=app_config.rewrite_loopback_to_host,
            host_loopback_address=app_config.host_loopback_address,
            running_in_docker=is_running_in_docker(),
        )

        if not config.auth_password:
            if strict:
                raise ValueError("AUTH_PASSWORD must be configured")
            LOGGER.warning(
                "AUTH_PASSWORD is not configured — all proxy connections will be rejected. "
                "Set it via the web admin Config Center."
            )
        if config.loopback_host_mode not in {"auto", "always", "off"}:
            raise ValueError("REWRITE_LOOPBACK_TO_HOST must be auto, always, or off")
        return config

    @classmethod
    def from_env(cls):
        return cls.from_app_config(AppConfig.from_bootstrap_env())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]):
        """Construct a ProxyConfig from flat env-keyed values."""
        return cls.from_app_config(AppConfig.from_mapping(data))


@dataclass
class ProxyRequest:
    method: str
    target: str
    version: str
    headers: List[Tuple[str, str]]
    host: str
    port: int
    forward_target: str
    connect_tunnel: bool


@dataclass
class CompatListenerHandle:
    mapping: object
    server: socketserver.BaseServer
    thread: threading.Thread


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def is_running_in_docker():
    return os.path.exists("/.dockerenv")


def should_rewrite_first_hop_loopback(config):
    if config.loopback_host_mode == "always":
        return True
    if config.loopback_host_mode == "off":
        return False
    return config.running_in_docker


def resolve_hop_host(config, hop, hop_index):
    if hop_index == 0 and should_rewrite_first_hop_loopback(config):
        if hop.host.lower() in LOOPBACK_HOSTS:
            return config.host_loopback_address
    return hop.host


def should_send_absolute_form(request, upstream_entry):
    return (not request.connect_tunnel) and upstream_entry.last_hop.scheme == "http"


def build_router(app_config: AppConfig, proxy_entries):
    from router import Router
    from upstream_pool import UpstreamPool

    return Router(
        config=app_config,
        upstream_pool=UpstreamPool(source="admin", entries=list(proxy_entries)),
    )


def header_value(headers, name):
    needle = name.lower()
    for key, value in headers:
        if key.lower() == needle:
            return value
    return None


def replace_header(headers, name, value):
    needle = name.lower()
    updated = [(key, current) for key, current in headers if key.lower() != needle]
    updated.append((name, value))
    return updated


def filter_headers(headers):
    return [
        (key, value)
        for key, value in headers
        if key.lower() not in FILTERED_REQUEST_HEADERS
    ]


def parse_basic_credentials(header):
    if not header:
        return None
    try:
        scheme, payload = header.split(None, 1)
    except ValueError:
        return None
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(payload.strip(), validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def build_basic_authorization(username, password):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def detect_inbound_protocol(sock):
    try:
        first_byte = sock.recv(1, socket.MSG_PEEK)
    except OSError:
        return "http"
    if not first_byte:
        return None
    if first_byte[0] == SOCKS_VERSION:
        return "socks5"
    return "http"


def read_request(rfile):
    request_line = rfile.readline(MAX_HEADER_LINE + 1)
    if not request_line:
        return None
    if len(request_line) > MAX_HEADER_LINE:
        raise ClientError(414, "Request-URI Too Long", "Request line is too long.")

    try:
        method, target, version = request_line.decode("iso-8859-1").strip().split()
    except ValueError as exc:
        raise ClientError(400, "Bad Request", "Malformed request line.") from exc

    headers = []
    for _ in range(MAX_HEADER_LINES):
        line = rfile.readline(MAX_HEADER_LINE + 1)
        if not line:
            break
        if len(line) > MAX_HEADER_LINE:
            raise ClientError(
                431, "Request Header Fields Too Large", "Header line is too long."
            )
        if line in (b"\r\n", b"\n"):
            break
        try:
            decoded = line.decode("iso-8859-1")
            key, value = decoded.split(":", 1)
        except ValueError as exc:
            raise ClientError(400, "Bad Request", "Malformed header line.") from exc
        headers.append((key.strip(), value.strip()))
    else:
        raise ClientError(
            431, "Request Header Fields Too Large", "Too many header lines."
        )

    host, port, forward_target, connect_tunnel = resolve_target(method, target, headers)
    return ProxyRequest(
        method=method.upper(),
        target=target,
        version=version,
        headers=headers,
        host=host,
        port=port,
        forward_target=forward_target,
        connect_tunnel=connect_tunnel,
    )


def resolve_target(method, target, headers):
    if method.upper() == "CONNECT":
        host, port = split_host_port(target, 443)
        return host, port, target, True

    parsed = urlsplit(target)
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        if scheme != "http":
            raise ClientError(
                400, "Bad Request", "Use CONNECT for non-HTTP upstream requests."
            )
        host = parsed.hostname
        if not host:
            raise ClientError(400, "Bad Request", "Missing target host.")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return host, port, path, False

    host_header = header_value(headers, "Host")
    if not host_header:
        raise ClientError(400, "Bad Request", "Missing Host header.")
    host, port = split_host_port(host_header, 80)
    path = target or "/"
    return host, port, path, False


def split_host_port(value, default_port):
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ClientError(400, "Bad Request", "Malformed IPv6 host.")
        host = value[1:end]
        remainder = value[end + 1 :]
        if remainder.startswith(":"):
            return host, int(remainder[1:])
        return host, default_port

    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return host, int(port_text)
    return value, default_port


def format_authority(host, port):
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def encode_socks_address(host, port):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        encoded_host = host.encode("idna")
        if len(encoded_host) > 255:
            raise UpstreamError("Destination host is too long for SOCKS5")
        return bytes([0x03, len(encoded_host)]) + encoded_host + port.to_bytes(2, "big")

    if address.version == 4:
        return bytes([0x01]) + address.packed + port.to_bytes(2, "big")
    return bytes([0x04]) + address.packed + port.to_bytes(2, "big")


def recv_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise UpstreamError("Unexpected EOF from upstream proxy")
        chunks.extend(chunk)
    return bytes(chunks)


def read_exact_from_stream(stream, size, message):
    data = stream.read(size)
    if data is None or len(data) < size:
        raise Socks5ProtocolError(message)
    return data


def read_socks5_methods(stream):
    header = read_exact_from_stream(stream, 2, "Incomplete SOCKS5 greeting.")
    version, method_count = header
    if version != SOCKS_VERSION:
        raise Socks5ProtocolError(f"Unsupported SOCKS version {version}.")
    return read_exact_from_stream(
        stream,
        method_count,
        "Incomplete SOCKS5 authentication methods.",
    )


def read_socks5_username_password(stream):
    version = read_exact_from_stream(
        stream,
        1,
        "Incomplete SOCKS5 username/password version.",
    )[0]
    if version != SOCKS_AUTH_VERSION:
        raise Socks5ProtocolError(
            f"Unsupported SOCKS5 auth version {version}.",
            reply_code=SOCKS_REPLY_GENERAL_FAILURE,
        )
    username_length = read_exact_from_stream(
        stream,
        1,
        "Incomplete SOCKS5 username length.",
    )[0]
    username = read_exact_from_stream(
        stream,
        username_length,
        "Incomplete SOCKS5 username payload.",
    )
    password_length = read_exact_from_stream(
        stream,
        1,
        "Incomplete SOCKS5 password length.",
    )[0]
    password = read_exact_from_stream(
        stream,
        password_length,
        "Incomplete SOCKS5 password payload.",
    )
    try:
        return username.decode("utf-8"), password.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Socks5ProtocolError(
            "SOCKS5 credentials must be valid UTF-8.",
            reply_code=SOCKS_REPLY_GENERAL_FAILURE,
        ) from exc


def read_socks5_address(stream, atyp):
    if atyp == SOCKS_ATYP_IPV4:
        return str(ipaddress.ip_address(read_exact_from_stream(stream, 4, "Incomplete SOCKS5 IPv4 address.")))
    if atyp == SOCKS_ATYP_IPV6:
        return str(ipaddress.ip_address(read_exact_from_stream(stream, 16, "Incomplete SOCKS5 IPv6 address.")))
    if atyp == SOCKS_ATYP_DOMAIN:
        length = read_exact_from_stream(stream, 1, "Incomplete SOCKS5 domain length.")[0]
        domain = read_exact_from_stream(stream, length, "Incomplete SOCKS5 domain payload.")
        try:
            return domain.decode("idna")
        except UnicodeDecodeError as exc:
            raise Socks5ProtocolError(
                "SOCKS5 destination host is not valid IDNA.",
                reply_code=SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED,
            ) from exc
    raise Socks5ProtocolError(
        f"Unsupported SOCKS5 address type {atyp}.",
        reply_code=SOCKS_REPLY_ADDRESS_TYPE_NOT_SUPPORTED,
    )


def read_socks5_request(stream):
    header = read_exact_from_stream(stream, 4, "Incomplete SOCKS5 request header.")
    version, command, _reserved, atyp = header
    if version != SOCKS_VERSION:
        raise Socks5ProtocolError(
            f"Unsupported SOCKS5 request version {version}.",
            reply_code=SOCKS_REPLY_GENERAL_FAILURE,
        )
    if command != SOCKS_CMD_CONNECT:
        raise Socks5ProtocolError(
            "Only SOCKS5 CONNECT is supported.",
            reply_code=SOCKS_REPLY_COMMAND_NOT_SUPPORTED,
        )
    host = read_socks5_address(stream, atyp)
    port = int.from_bytes(
        read_exact_from_stream(stream, 2, "Incomplete SOCKS5 destination port."),
        "big",
    )
    target = format_authority(host, port)
    return ProxyRequest(
        method="CONNECT",
        target=target,
        version="SOCKS5",
        headers=[],
        host=host,
        port=port,
        forward_target=target,
        connect_tunnel=True,
    )


def build_socks5_reply(reply_code, bind_host="0.0.0.0", bind_port=0):
    return bytes([SOCKS_VERSION, reply_code, 0x00]) + encode_socks_address(
        bind_host,
        bind_port,
    )


def discard_socks_reply_address(sock, atyp):
    if atyp == 0x01:
        recv_exact(sock, 4 + 2)
        return
    if atyp == 0x04:
        recv_exact(sock, 16 + 2)
        return
    if atyp == 0x03:
        length = recv_exact(sock, 1)[0]
        recv_exact(sock, length + 2)
        return
    raise UpstreamError(f"Unsupported SOCKS5 address type {atyp}")


def open_first_hop_socket(config, hop):
    host = resolve_hop_host(config, hop, 0)
    sock = socket.create_connection((host, hop.port), config.connect_timeout)
    sock.settimeout(config.connect_timeout)
    return sock


def establish_socks5_tunnel(sock, hop, dest_host, dest_port):
    if hop.username or hop.password:
        methods = bytes([0x05, 0x01, 0x02])
    else:
        methods = bytes([0x05, 0x01, 0x00])
    sock.sendall(methods)
    version, method = recv_exact(sock, 2)
    if version != 0x05:
        raise UpstreamError("Invalid SOCKS5 greeting from upstream")
    if method == 0xFF:
        raise UpstreamError("SOCKS5 upstream rejected all auth methods")
    if method == 0x02:
        username = hop.username.encode("utf-8")
        password = hop.password.encode("utf-8")
        if len(username) > 255 or len(password) > 255:
            raise UpstreamError("SOCKS5 credentials exceed protocol limits")
        payload = (
            bytes([0x01, len(username)]) + username + bytes([len(password)]) + password
        )
        sock.sendall(payload)
        auth_version, status = recv_exact(sock, 2)
        if auth_version != 0x01 or status != 0x00:
            raise UpstreamError("SOCKS5 upstream authentication failed")
    elif method != 0x00:
        raise UpstreamError(f"SOCKS5 upstream chose unsupported auth method {method}")

    request = bytes([0x05, 0x01, 0x00]) + encode_socks_address(dest_host, dest_port)
    sock.sendall(request)
    version, reply, _reserved, atyp = recv_exact(sock, 4)
    if version != 0x05:
        raise UpstreamError("Invalid SOCKS5 connect response")
    discard_socks_reply_address(sock, atyp)
    if reply != 0x00:
        raise UpstreamError(f"SOCKS5 upstream connect failed with code {reply}")


def read_http_headers_from_socket(sock):
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(1)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_HEADER_LINE * MAX_HEADER_LINES:
            raise UpstreamError("Upstream HTTP response headers too large")
    return bytes(buffer)


def establish_http_tunnel(sock, hop, dest_host, dest_port):
    headers = [
        f"CONNECT {dest_host}:{dest_port} HTTP/1.1",
        f"Host: {dest_host}:{dest_port}",
        "Proxy-Connection: Keep-Alive",
    ]
    if hop.username or hop.password:
        headers.append(
            f"Proxy-Authorization: {build_basic_authorization(hop.username, hop.password)}"
        )
    payload = ("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1")
    sock.sendall(payload)

    response = read_http_headers_from_socket(sock).decode(
        "iso-8859-1", errors="replace"
    )
    status_line = response.splitlines()[0] if response else ""
    parts = status_line.split(None, 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise UpstreamError("Invalid HTTP CONNECT response from upstream")
    status_code = int(parts[1])
    if status_code != 200:
        raise UpstreamError(f"HTTP upstream CONNECT failed with status {status_code}")


def open_upstream_tunnel(
    config, upstream_entry, dest_host, dest_port, stop_before_last_hop=False
):
    last_error = None
    for attempt in range(config.connect_retries):
        sock = None
        try:
            first_hop = upstream_entry.first_hop
            sock = open_first_hop_socket(config, first_hop)

            for hop_index, hop in enumerate(upstream_entry.hops):
                is_last_hop = hop_index + 1 == upstream_entry.chain_length
                if is_last_hop and stop_before_last_hop:
                    break
                if hop_index + 1 < upstream_entry.chain_length:
                    next_hop = upstream_entry.hops[hop_index + 1]
                    next_host = next_hop.host
                    next_port = next_hop.port
                else:
                    next_host = dest_host
                    next_port = dest_port

                if hop.scheme in {"socks5", "socks5h"}:
                    establish_socks5_tunnel(sock, hop, next_host, next_port)
                    continue
                if hop.scheme == "http":
                    establish_http_tunnel(sock, hop, next_host, next_port)
                    continue
                raise UpstreamError(f"Unsupported upstream scheme {hop.scheme}")

            sock.settimeout(None)
            return sock
        except (OSError, UpstreamError) as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            last_error = exc
            if attempt + 1 >= config.connect_retries:
                break
            LOGGER.warning(
                "Retrying upstream %s after connect error (%s/%s): %s",
                upstream_entry.key,
                attempt + 1,
                config.connect_retries,
                exc,
            )
    raise UpstreamError(
        f"Failed to connect upstream {upstream_entry.key}: {last_error}"
    )


def relay_bidirectional(left, right, timeout):
    selector = selectors.DefaultSelector()
    sockets = (left, right)
    for sock in sockets:
        sock.setblocking(False)
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            events = selector.select(timeout)
            if not events:
                raise UpstreamError("Relay timed out")
            for key, _mask in events:
                source = key.fileobj
                target = key.data
                try:
                    chunk = source.recv(BUFFER_SIZE)
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                target.sendall(chunk)
    finally:
        selector.close()
        for sock in sockets:
            sock.setblocking(True)


def relay_one_way(source, target):
    while True:
        chunk = source.recv(BUFFER_SIZE)
        if not chunk:
            return
        target.sendall(chunk)


def stream_exact(rfile, upstream_socket, size):
    remaining = size
    while remaining > 0:
        chunk = rfile.read(min(BUFFER_SIZE, remaining))
        if not chunk:
            raise ClientError(
                400, "Bad Request", "Unexpected EOF while reading request body."
            )
        upstream_socket.sendall(chunk)
        remaining -= len(chunk)


def stream_chunked(rfile, upstream_socket):
    while True:
        line = rfile.readline(MAX_HEADER_LINE + 1)
        if not line:
            raise ClientError(
                400, "Bad Request", "Unexpected EOF while reading chunked body."
            )
        upstream_socket.sendall(line)
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise ClientError(400, "Bad Request", "Invalid chunk size.") from exc
        if size == 0:
            while True:
                trailer = rfile.readline(MAX_HEADER_LINE + 1)
                if not trailer:
                    raise ClientError(
                        400, "Bad Request", "Unexpected EOF after chunk trailer."
                    )
                upstream_socket.sendall(trailer)
                if trailer in (b"\r\n", b"\n"):
                    return
        stream_exact(rfile, upstream_socket, size + 2)


def forward_request_body(rfile, upstream_socket, headers):
    content_length = header_value(headers, "Content-Length")
    transfer_encoding = header_value(headers, "Transfer-Encoding")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise ClientError(400, "Bad Request", "Invalid Content-Length.") from exc
        if length > 0:
            stream_exact(rfile, upstream_socket, length)
        return
    if transfer_encoding and transfer_encoding.lower() == "chunked":
        stream_chunked(rfile, upstream_socket)


def format_request(method, target, version, headers):
    lines = [f"{method} {target} {version}"]
    for key, value in headers:
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, config, router):
        self.config = config
        self.router = router
        self.compat_listeners = {}
        super().__init__(server_address, handler_class)


class CompatTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, parent_server, mapping):
        self.parent_server = parent_server
        self.mapping = mapping
        super().__init__(server_address, handler_class)

    @property
    def config(self):
        return self.parent_server.config

    @property
    def router(self):
        return self.parent_server.router


class HttpProxyRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(self.server.config.connect_timeout)
        request = None
        username = "-"
        upstream_entry = None
        try:
            request = read_request(self.rfile)
            if request is None:
                return
            username = self.authenticate(request.headers)
            upstream_entry = self.resolve_upstream_entry(username)
            if upstream_entry is None:
                raise ClientError(
                    503, "Service Unavailable", "No upstream port available."
                )

            if request.connect_tunnel:
                self.handle_connect(request, username, upstream_entry)
            else:
                self.handle_http_request(request, username, upstream_entry)
        except ClientError as exc:
            self.send_error_response(exc.status_code, exc.reason, exc.body, exc.headers)
            self.log_request_result(
                request, username, upstream_entry, f"client_error:{exc.status_code}"
            )
        except UpstreamError as exc:
            self.send_error_response(502, "Bad Gateway", str(exc))
            self.log_request_result(
                request, username, upstream_entry, f"upstream_error:{exc}"
            )
        except Exception as exc:
            LOGGER.exception("Unhandled proxy error")
            self.send_error_response(
                500, "Internal Server Error", "Unhandled proxy error."
            )
            self.log_request_result(
                request, username, upstream_entry, f"internal_error:{exc}"
            )

    def resolve_upstream_entry(self, username):
        return self.server.router.route_entry(username)

    def authenticate(self, headers):
        if not self.server.config.auth_password:
            raise ClientError(
                503,
                "Service Unavailable",
                (
                    "Proxy server AUTH_PASSWORD is not configured. "
                    "Set it in Web Admin > Configuration before using the proxy."
                ),
            )
        credentials = parse_basic_credentials(
            header_value(headers, "Proxy-Authorization")
        )
        if not credentials:
            raise ClientError(
                407,
                "Proxy Authentication Required",
                "Proxy authentication required.",
                headers=[
                    (
                        "Proxy-Authenticate",
                        f'Basic realm="{self.server.config.auth_realm}"',
                    )
                ],
            )
        username, password = credentials
        if not username or not check_password(
            password, self.server.config.auth_password
        ):
            raise ClientError(
                407,
                "Proxy Authentication Required",
                "Invalid proxy credentials.",
                headers=[
                    (
                        "Proxy-Authenticate",
                        f'Basic realm="{self.server.config.auth_realm}"',
                    )
                ],
            )
        return username

    def handle_connect(self, request, username, upstream_entry):
        self.relay_connect_tunnel(
            request,
            username,
            upstream_entry,
            on_established=self._write_http_connect_success,
        )

    def handle_http_request(self, request, username, upstream_entry):
        headers = replace_header(filter_headers(request.headers), "Connection", "close")
        final_hop = upstream_entry.last_hop
        if should_send_absolute_form(request, upstream_entry):
            if final_hop.username or final_hop.password:
                headers = replace_header(
                    headers,
                    "Proxy-Authorization",
                    build_basic_authorization(
                        final_hop.username,
                        final_hop.password,
                    ),
                )
            request_bytes = format_request(
                request.method,
                rebuild_absolute_target(request, headers),
                request.version,
                headers,
            )
            if upstream_entry.chain_length == 1:
                upstream_socket = open_first_hop_socket(self.server.config, final_hop)
            else:
                upstream_socket = open_upstream_tunnel(
                    self.server.config,
                    upstream_entry,
                    request.host,
                    request.port,
                    stop_before_last_hop=True,
                )
        else:
            request_bytes = format_request(
                request.method,
                request.forward_target,
                request.version,
                headers,
            )
            upstream_socket = open_upstream_tunnel(
                self.server.config,
                upstream_entry,
                request.host,
                request.port,
            )

        try:
            upstream_socket.settimeout(self.server.config.relay_timeout)
            upstream_socket.sendall(request_bytes)
            forward_request_body(self.rfile, upstream_socket, request.headers)
            relay_one_way(upstream_socket, self.connection)
            self.log_request_result(request, username, upstream_entry, "ok_http")
        finally:
            upstream_socket.close()

    def relay_connect_tunnel(
        self,
        request,
        username,
        upstream_entry,
        *,
        on_established: Callable[[], None],
    ):
        upstream_socket = open_upstream_tunnel(
            self.server.config,
            upstream_entry,
            request.host,
            request.port,
        )
        try:
            on_established()
            self.connection.settimeout(None)
            relay_bidirectional(
                self.connection, upstream_socket, self.server.config.relay_timeout
            )
            self.log_request_result(request, username, upstream_entry, "ok_tunnel")
        finally:
            upstream_socket.close()

    def _write_http_connect_success(self):
        self.wfile.write(
            b"HTTP/1.1 200 Connection established\r\nConnection: close\r\n\r\n"
        )
        self.wfile.flush()

    def send_error_response(self, status_code, reason, body, extra_headers=None):
        payload = body.encode("utf-8")
        lines = [
            f"HTTP/1.1 {status_code} {reason}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(payload)}",
            "Connection: close",
        ]
        for key, value in extra_headers or []:
            lines.append(f"{key}: {value}")
        response = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + payload
        try:
            self.wfile.write(response)
            self.wfile.flush()
        except OSError:
            pass

    def log_request_result(self, request, username, upstream_entry, result):
        if request is None:
            return
        upstream_display = "-"
        if upstream_entry is not None:
            upstream_display = f"{upstream_entry.display} ({upstream_entry.key})"
        LOGGER.info(
            "%s user=%s method=%s target=%s upstream=%s result=%s",
            self.client_address[0],
            username,
            request.method,
            request.target,
            upstream_display,
            result,
        )


class ProxyRequestHandler(HttpProxyRequestHandler):
    def handle(self):
        protocol = detect_inbound_protocol(self.connection)
        if protocol is None:
            return
        if protocol == "socks5":
            self.handle_socks5_session()
            return
        super().handle()

    def handle_socks5_session(self):
        self.connection.settimeout(self.server.config.connect_timeout)
        request = None
        username = "-"
        upstream_entry = None
        try:
            methods = read_socks5_methods(self.rfile)
            if not self.server.config.auth_password:
                self.send_socks5_method_selection(SOCKS_METHOD_NO_ACCEPTABLE)
                return
            if SOCKS_METHOD_USERNAME_PASSWORD not in methods:
                self.send_socks5_method_selection(SOCKS_METHOD_NO_ACCEPTABLE)
                return
            self.send_socks5_method_selection(SOCKS_METHOD_USERNAME_PASSWORD)

            username, password = read_socks5_username_password(self.rfile)
            if not username or not check_password(
                password,
                self.server.config.auth_password,
            ):
                self.send_socks5_auth_status(SOCKS_AUTH_STATUS_FAILURE)
                return
            self.send_socks5_auth_status(SOCKS_AUTH_STATUS_SUCCESS)

            request = read_socks5_request(self.rfile)
            upstream_entry = self.resolve_upstream_entry(username)
            if upstream_entry is None:
                self.send_socks5_reply(SOCKS_REPLY_GENERAL_FAILURE)
                self.log_request_result(
                    request,
                    username,
                    upstream_entry,
                    "client_error:no_upstream",
                )
                return
            self.handle_socks5_connect(request, username, upstream_entry)
        except Socks5ProtocolError as exc:
            if exc.reply_code is not None:
                self.send_socks5_reply(exc.reply_code)
            self.log_request_result(
                request,
                username,
                upstream_entry,
                f"client_error:socks5:{exc}",
            )
        except UpstreamError as exc:
            self.send_socks5_reply(SOCKS_REPLY_GENERAL_FAILURE)
            self.log_request_result(
                request,
                username,
                upstream_entry,
                f"upstream_error:{exc}",
            )
        except Exception as exc:
            LOGGER.exception("Unhandled SOCKS5 proxy error")
            self.send_socks5_reply(SOCKS_REPLY_GENERAL_FAILURE)
            self.log_request_result(
                request,
                username,
                upstream_entry,
                f"internal_error:{exc}",
            )

    def handle_socks5_connect(self, request, username, upstream_entry):
        self.relay_connect_tunnel(
            request,
            username,
            upstream_entry,
            on_established=lambda: self.send_socks5_reply(SOCKS_REPLY_SUCCEEDED),
        )

    def send_socks5_method_selection(self, method):
        self.wfile.write(bytes([SOCKS_VERSION, method]))
        self.wfile.flush()

    def send_socks5_auth_status(self, status):
        self.wfile.write(bytes([SOCKS_AUTH_VERSION, status]))
        self.wfile.flush()

    def send_socks5_reply(self, reply_code):
        try:
            self.wfile.write(build_socks5_reply(reply_code))
            self.wfile.flush()
        except OSError:
            pass


class CompatProxyRequestHandler(HttpProxyRequestHandler):
    def authenticate(self, headers):
        del headers
        return self.server.mapping.target_value

    def resolve_upstream_entry(self, username):
        del username
        mapping = self.server.mapping
        router = self.server.router
        if mapping.target_type == TARGET_TYPE_ENTRY_KEY:
            upstream_entry = router.get_entry(mapping.target_value)
            if upstream_entry is None:
                raise ClientError(
                    503,
                    "Service Unavailable",
                    "Configured compatibility target is unavailable.",
                )
            return upstream_entry
        return router.route_entry(mapping.target_value)


def _compat_listener_registry(parent_server):
    registry = getattr(parent_server, "compat_listeners", None)
    if not isinstance(registry, dict):
        registry = {}
        parent_server.compat_listeners = registry
    return registry


def _stop_compat_listener(listener):
    try:
        listener.server.shutdown()
    except Exception:
        LOGGER.exception(
            "Failed to stop compat listener on port %s",
            listener.mapping.listen_port,
        )
    finally:
        try:
            listener.server.server_close()
        except Exception:
            LOGGER.exception(
                "Failed to close compat listener on port %s",
                listener.mapping.listen_port,
            )
        if listener.thread.is_alive():
            listener.thread.join(timeout=1.0)


def _stop_compat_listeners(listeners):
    workers = []
    for listener in listeners:
        worker = threading.Thread(
            target=_stop_compat_listener,
            args=(listener,),
            name="compat-stop-%s" % listener.mapping.listen_port,
            daemon=True,
        )
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join()


def close_compat_listeners(parent_server):
    if parent_server is None:
        return
    registry = _compat_listener_registry(parent_server)
    listeners = []
    for port, listener in list(registry.items()):
        registry.pop(port, None)
        listeners.append(listener)
    _stop_compat_listeners(listeners)


def reload_compat_listeners(parent_server, storage=None):
    if parent_server is None:
        return {}

    registry = _compat_listener_registry(parent_server)
    if storage is None:
        router = getattr(parent_server, "router", None)
        storage = getattr(router, "storage", None)

    from persistence import load_compat_port_mappings

    desired = {
        mapping.listen_port: mapping
        for mapping in load_compat_port_mappings(storage)
        if mapping.enabled
    }

    listeners_to_stop = []
    for port, listener in list(registry.items()):
        mapping = desired.get(port)
        if mapping is None or listener.mapping != mapping:
            registry.pop(port, None)
            listeners_to_stop.append(listener)
    _stop_compat_listeners(listeners_to_stop)

    bind_host = parent_server.config.listen_host
    for port, mapping in desired.items():
        if port in registry:
            continue
        try:
            compat_server = CompatTCPServer(
                (bind_host, port),
                CompatProxyRequestHandler,
                parent_server,
                mapping,
            )
            thread = threading.Thread(
                target=compat_server.serve_forever,
                name="compat-port-%s" % port,
                daemon=True,
            )
            thread.start()
            registry[port] = CompatListenerHandle(
                mapping=mapping,
                server=compat_server,
                thread=thread,
            )
            LOGGER.info(
                "Compat listener started on %s:%s -> %s:%s",
                bind_host,
                port,
                mapping.target_type,
                mapping.target_value,
            )
        except Exception:
            LOGGER.exception("Failed to start compat listener on port %s", port)
    return registry


def rebuild_absolute_target(request, headers):
    host_header = header_value(headers, "Host")
    if request.target.startswith("http://"):
        return request.target
    if not host_header:
        host_header = request.host
        if request.port != 80:
            host_header = f"{host_header}:{request.port}"
    return f"http://{host_header}{request.forward_target}"


def main():
    bootstrap_config = AppConfig.from_bootstrap_env()
    logging.basicConfig(
        level=bootstrap_config.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    from persistence import (
        STATE_KEY_PROXY_LIST,
        load_proxy_list,
        open_storage,
        save_proxy_list,
    )

    storage = open_storage(bootstrap_config.state_db_path)
    app_config = AppConfig.load(storage)
    logging.getLogger().setLevel(app_config.log_level)
    if storage.get(STATE_KEY_PROXY_LIST) is None:
        save_proxy_list(storage, [])
    config = ProxyConfig.from_app_config(app_config, strict=False)
    proxy_entries = load_proxy_list(storage)

    try:
        router = build_router(app_config, proxy_entries)
        router.storage = storage
    except Exception:
        LOGGER.warning(
            "Failed to build router from persisted proxy list — starting with empty upstream pool. "
            "Configure proxies via the web admin panel, then reload.",
            exc_info=True,
        )
        from router import Router
        from upstream_pool import UpstreamPool

        router = Router(
            config=app_config,
            upstream_pool=UpstreamPool(source="admin", entries=[]),
        )
        router.storage = storage
    server = ThreadedTCPServer(
        (config.listen_host, config.listen_port), ProxyRequestHandler, config, router
    )
    LOGGER.info(
        "Listening on %s:%s with upstream_source=%s upstream_count=%s",
        config.listen_host,
        config.listen_port,
        router.upstream_pool.source,
        router.upstream_pool.count,
    )
    reload_compat_listeners(server, storage)

    # Start web admin panel as daemon thread
    log_handler = None
    try:
        from web_admin import RingBufferHandler, start_admin_server

        log_handler = RingBufferHandler(2000)
        logging.getLogger().addHandler(log_handler)

        start_admin_server(
            host="0.0.0.0",
            port=app_config.admin_port,
            server_ref=server,
            log_handler=log_handler,
        )
        if app_config.admin_password:
            LOGGER.info("Admin panel started on port %s", app_config.admin_port)
        else:
            LOGGER.info(
                "Admin panel started on port %s in setup mode; visit /setup to create the admin password",
                app_config.admin_port,
            )
    except Exception:
        LOGGER.exception("Failed to start admin panel")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down")
    finally:
        close_compat_listeners(server)
        server.server_close()


if __name__ == "__main__":
    main()
