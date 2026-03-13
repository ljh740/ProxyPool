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
from dataclasses import dataclass
from typing import List, Tuple
from urllib.parse import urlsplit

from auth import check_password

BUFFER_SIZE = 65536
MAX_HEADER_LINE = 8192
MAX_HEADER_LINES = 200
SUPPORTED_UPSTREAM_SCHEMES = {"http", "socks5", "socks5h"}
FILTERED_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "upgrade",
}
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


@dataclass
class ProxyConfig:
    listen_host: str
    listen_port: int
    auth_password: str
    auth_realm: str
    connect_timeout: float
    connect_retries: int
    relay_timeout: float

    @classmethod
    def from_env(cls):
        config = cls(
            listen_host=os.getenv("PROXY_HOST", "0.0.0.0"),
            listen_port=env_int("PROXY_PORT", 3128),
            auth_password=os.getenv("AUTH_PASSWORD", ""),
            auth_realm=os.getenv("AUTH_REALM", "Proxy"),
            connect_timeout=env_float("UPSTREAM_CONNECT_TIMEOUT", 20.0),
            connect_retries=max(1, env_int("UPSTREAM_CONNECT_RETRIES", 3)),
            relay_timeout=env_float("RELAY_TIMEOUT", 120.0),
        )

        if not config.auth_password:
            raise ValueError("AUTH_PASSWORD must be configured")
        if "\\~" in os.getenv("UP_PASS", ""):
            LOGGER.warning(
                "UP_PASS contains literal '\\~'. In .env files that usually means a shell escape was copied literally."
            )
        return config


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


def build_router():
    from router import Router

    return Router()


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
            raise ClientError(431, "Request Header Fields Too Large", "Header line is too long.")
        if line in (b"\r\n", b"\n"):
            break
        try:
            decoded = line.decode("iso-8859-1")
            key, value = decoded.split(":", 1)
        except ValueError as exc:
            raise ClientError(400, "Bad Request", "Malformed header line.") from exc
        headers.append((key.strip(), value.strip()))
    else:
        raise ClientError(431, "Request Header Fields Too Large", "Too many header lines.")

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
            raise ClientError(400, "Bad Request", "Use CONNECT for non-HTTP upstream requests.")
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


def open_socks5_tunnel(config, upstream_entry, dest_host, dest_port):
    sock = socket.create_connection((upstream_entry.host, upstream_entry.port), config.connect_timeout)
    sock.settimeout(config.connect_timeout)

    if upstream_entry.username or upstream_entry.password:
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
        username = upstream_entry.username.encode("utf-8")
        password = upstream_entry.password.encode("utf-8")
        if len(username) > 255 or len(password) > 255:
            raise UpstreamError("SOCKS5 credentials exceed protocol limits")
        payload = (
            bytes([0x01, len(username)])
            + username
            + bytes([len(password)])
            + password
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

    sock.settimeout(None)
    return sock


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


def open_http_tunnel(config, upstream_entry, dest_host, dest_port):
    sock = socket.create_connection((upstream_entry.host, upstream_entry.port), config.connect_timeout)
    sock.settimeout(config.connect_timeout)
    headers = [
        f"CONNECT {dest_host}:{dest_port} HTTP/1.1",
        f"Host: {dest_host}:{dest_port}",
        "Proxy-Connection: Keep-Alive",
    ]
    if upstream_entry.username or upstream_entry.password:
        headers.append(
            f"Proxy-Authorization: {build_basic_authorization(upstream_entry.username, upstream_entry.password)}"
        )
    payload = ("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1")
    sock.sendall(payload)

    response = read_http_headers_from_socket(sock).decode("iso-8859-1", errors="replace")
    status_line = response.splitlines()[0] if response else ""
    parts = status_line.split(None, 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise UpstreamError("Invalid HTTP CONNECT response from upstream")
    status_code = int(parts[1])
    if status_code != 200:
        raise UpstreamError(f"HTTP upstream CONNECT failed with status {status_code}")

    sock.settimeout(None)
    return sock


def open_upstream_tunnel(config, upstream_entry, dest_host, dest_port):
    last_error = None
    for attempt in range(config.connect_retries):
        try:
            if upstream_entry.scheme in {"socks5", "socks5h"}:
                return open_socks5_tunnel(config, upstream_entry, dest_host, dest_port)
            if upstream_entry.scheme == "http":
                return open_http_tunnel(config, upstream_entry, dest_host, dest_port)
            raise UpstreamError(f"Unsupported upstream scheme {upstream_entry.scheme}")
        except (OSError, UpstreamError) as exc:
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
    raise UpstreamError(f"Failed to connect upstream {upstream_entry.key}: {last_error}")


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
            raise ClientError(400, "Bad Request", "Unexpected EOF while reading request body.")
        upstream_socket.sendall(chunk)
        remaining -= len(chunk)


def stream_chunked(rfile, upstream_socket):
    while True:
        line = rfile.readline(MAX_HEADER_LINE + 1)
        if not line:
            raise ClientError(400, "Bad Request", "Unexpected EOF while reading chunked body.")
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
                    raise ClientError(400, "Bad Request", "Unexpected EOF after chunk trailer.")
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
        super().__init__(server_address, handler_class)


class ProxyRequestHandler(socketserver.StreamRequestHandler):
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
            upstream_entry = self.server.router.route_entry(username)
            if upstream_entry is None:
                raise ClientError(503, "Service Unavailable", "No upstream port available.")

            if request.connect_tunnel:
                self.handle_connect(request, username, upstream_entry)
            else:
                self.handle_http_request(request, username, upstream_entry)
        except ClientError as exc:
            self.send_error_response(exc.status_code, exc.reason, exc.body, exc.headers)
            self.log_request_result(request, username, upstream_entry, f"client_error:{exc.status_code}")
        except UpstreamError as exc:
            self.send_error_response(502, "Bad Gateway", str(exc))
            self.log_request_result(request, username, upstream_entry, f"upstream_error:{exc}")
        except Exception as exc:
            LOGGER.exception("Unhandled proxy error")
            self.send_error_response(500, "Internal Server Error", "Unhandled proxy error.")
            self.log_request_result(request, username, upstream_entry, f"internal_error:{exc}")

    def authenticate(self, headers):
        credentials = parse_basic_credentials(header_value(headers, "Proxy-Authorization"))
        if not credentials:
            raise ClientError(
                407,
                "Proxy Authentication Required",
                "Proxy authentication required.",
                headers=[("Proxy-Authenticate", f'Basic realm="{self.server.config.auth_realm}"')],
            )
        username, password = credentials
        if not username or not check_password(password, self.server.config.auth_password):
            raise ClientError(
                407,
                "Proxy Authentication Required",
                "Invalid proxy credentials.",
                headers=[("Proxy-Authenticate", f'Basic realm="{self.server.config.auth_realm}"')],
            )
        return username

    def handle_connect(self, request, username, upstream_entry):
        upstream_socket = open_upstream_tunnel(
            self.server.config,
            upstream_entry,
            request.host,
            request.port,
        )
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\nConnection: close\r\n\r\n")
            self.wfile.flush()
            self.connection.settimeout(None)
            relay_bidirectional(self.connection, upstream_socket, self.server.config.relay_timeout)
            self.log_request_result(request, username, upstream_entry, "ok_tunnel")
        finally:
            upstream_socket.close()

    def handle_http_request(self, request, username, upstream_entry):
        headers = replace_header(filter_headers(request.headers), "Connection", "close")
        if upstream_entry.scheme == "http":
            headers = replace_header(
                headers,
                "Proxy-Authorization",
                build_basic_authorization(
                    upstream_entry.username,
                    upstream_entry.password,
                ),
            )
            request_bytes = format_request(
                request.method,
                rebuild_absolute_target(request, headers),
                request.version,
                headers,
            )
            upstream_socket = socket.create_connection(
                (upstream_entry.host, upstream_entry.port),
                self.server.config.connect_timeout,
            )
        else:
            request_bytes = format_request(
                request.method,
                request.forward_target,
                request.version,
                headers,
            )
            upstream_socket = open_socks5_tunnel(
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
            upstream_display = f"{upstream_entry.scheme}://{upstream_entry.host}:{upstream_entry.port} ({upstream_entry.key})"
        LOGGER.info(
            "%s user=%s method=%s target=%s upstream=%s result=%s",
            self.client_address[0],
            username,
            request.method,
            request.target,
            upstream_display,
            result,
        )


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
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    config = ProxyConfig.from_env()
    router = build_router()
    server = ThreadedTCPServer((config.listen_host, config.listen_port), ProxyRequestHandler, config, router)
    LOGGER.info(
        "Listening on %s:%s with upstream_source=%s upstream_count=%s",
        config.listen_host,
        config.listen_port,
        router.upstream_pool.source,
        router.upstream_pool.count,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
