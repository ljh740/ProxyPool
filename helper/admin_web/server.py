"""Admin server runtime helpers."""

import collections
import logging
import threading
import time

from config_center import AppConfig
from persistence import load_proxy_list, open_storage

from . import context

LOGGER = logging.getLogger("web_admin")
DEFAULT_LOG_BUFFER_SIZE = 2000


class ReloadRejectedError(RuntimeError):
    """Raised when persisted admin state cannot be hot-reloaded safely."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RingBufferHandler(logging.Handler):
    """Logging handler backed by a bounded deque."""

    def __init__(self, maxlen=DEFAULT_LOG_BUFFER_SIZE):
        super().__init__()
        self.buffer = collections.deque(maxlen=maxlen)

    def emit(self, record):
        try:
            entry = {
                "timestamp_ms": int(record.created * 1000),
                "timestamp": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(record.created)
                ),
                "level": record.levelname,
                "message": self.format(record),
                "logger": record.name,
            }
            msg = record.getMessage()
            if " user=" in msg and " method=" in msg:
                parts = msg.split(None, 1)
                entry["client_ip"] = parts[0] if parts else "-"
                for field in ("user", "method", "target", "upstream", "result"):
                    marker = f"{field}="
                    idx = msg.find(marker)
                    if idx != -1:
                        rest = msg[idx + len(marker) :]
                        end = len(rest)
                        for next_field in (
                            "user=",
                            "method=",
                            "target=",
                            "upstream=",
                            "result=",
                        ):
                            if next_field == marker:
                                continue
                            pos = rest.find(f" {next_field}")
                            if pos != -1 and pos < end:
                                end = pos
                        entry[field] = rest[:end]
                    else:
                        entry[field] = "-"
                entry.setdefault("username", entry.pop("user", "-"))
            else:
                entry["client_ip"] = "-"
                entry["username"] = "-"
                entry["method"] = "-"
                entry["target"] = "-"
                entry["upstream"] = "-"
                entry["result"] = msg

            self.buffer.appendleft(entry)
        except Exception:
            self.handleError(record)

    def get_entries(self, level=None):
        if not level or level == "ALL":
            return list(self.buffer)
        level_upper = level.upper()
        return [entry for entry in self.buffer if entry.get("level") == level_upper]

    def clear(self):
        self.buffer.clear()


def get_storage(server_ref=None):
    admin_storage = context.get_admin_storage()
    if admin_storage is not None:
        return admin_storage
    ref = server_ref if server_ref is not None else context.get_server_ref()
    if ref is None:
        raise RuntimeError("Admin storage is not initialized")
    router = getattr(ref, "router", None)
    if router is None or getattr(router, "storage", None) is None:
        raise RuntimeError("Proxy server storage is not initialized")
    return router.storage


def trigger_reload(server_ref=None, *, raise_on_error=False):
    """Rebuild ProxyConfig and Router from persisted admin state."""
    ref = server_ref if server_ref is not None else context.get_server_ref()
    if ref is None:
        LOGGER.warning("trigger_reload: no server reference available")
        return

    storage = get_storage(ref)

    try:
        app_config = AppConfig.load(storage)
    except Exception:
        LOGGER.exception("trigger_reload: AppConfig.load() failed -- keeping old config")
        return

    try:
        from proxy_server import ProxyConfig

        new_proxy_config = ProxyConfig.from_app_config(app_config)
        ref.config = new_proxy_config
        LOGGER.info(
            "ProxyConfig reloaded: auth_realm=%s connect_timeout=%s relay_timeout=%s",
            new_proxy_config.auth_realm,
            new_proxy_config.connect_timeout,
            new_proxy_config.relay_timeout,
        )
    except ValueError as exc:
        code = "proxy_config_invalid"
        if "AUTH_PASSWORD" in str(exc):
            code = "auth_password_missing"
        LOGGER.error(
            "trigger_reload: runtime config rejected -- %s",
            exc,
        )
        if raise_on_error:
            raise ReloadRejectedError(code, str(exc)) from exc
        return
    except Exception:
        LOGGER.exception(
            "trigger_reload: ProxyConfig construction failed -- keeping old config"
        )
        if raise_on_error:
            raise ReloadRejectedError(
                "proxy_config_invalid",
                "Failed to rebuild proxy runtime configuration.",
            )
        return

    entries = load_proxy_list(storage)

    try:
        from router import Router
        from upstream_pool import UpstreamPool

        pool = UpstreamPool(source="admin", entries=list(entries))
        new_router = Router(config=app_config, upstream_pool=pool)
        new_router.storage = storage
    except Exception:
        LOGGER.exception("trigger_reload: Router construction failed -- keeping old router")
        if raise_on_error:
            raise ReloadRejectedError(
                "router_invalid",
                "Failed to rebuild router from persisted admin state.",
            )
        return

    ref.router = new_router
    LOGGER.info(
        "Router reloaded: upstream_count=%d",
        new_router.upstream_count,
    )
    try:
        from proxy_server import reload_compat_listeners

        reload_compat_listeners(ref, storage)
    except Exception:
        LOGGER.exception("trigger_reload: compat listener reload failed")
        if raise_on_error:
            raise ReloadRejectedError(
                "compat_reload_failed",
                "Failed to reload compatibility listeners.",
            )


def start_admin_server(app, host="0.0.0.0", port=None, server_ref=None, log_handler=None):
    """Start the admin WSGI app on a daemon thread."""
    context.set_server_ref(server_ref)
    context.set_log_handler(log_handler)

    if server_ref is not None and getattr(getattr(server_ref, "router", None), "storage", None):
        context.set_admin_storage(server_ref.router.storage)
    elif context.get_admin_storage() is None:
        bootstrap_config = AppConfig.from_bootstrap_env()
        context.set_admin_storage(open_storage(bootstrap_config.state_db_path))

    app_config = AppConfig.load(get_storage(server_ref))

    if port is None:
        port = app_config.admin_port

    if not app_config.admin_password:
        LOGGER.info(
            "Admin panel entering setup mode on port %s",
            port,
        )

    def _run():
        try:
            from socketserver import ThreadingMixIn
            from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

            class _NoLookupHandler(WSGIRequestHandler):
                def address_string(self):
                    return self.client_address[0]

                def log_request(self, code="-", size="-"):
                    pass

            class _ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
                daemon_threads = True
                address_family = __import__("socket").AF_INET

                def server_bind(self):
                    import socket

                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self.socket.bind(self.server_address)
                    bind_host, bind_port = self.socket.getsockname()[:2]
                    self.server_name = bind_host
                    self.server_port = bind_port
                    self.setup_environ()

            server = make_server(
                host,
                port,
                app,
                server_class=_ThreadedWSGIServer,
                handler_class=_NoLookupHandler,
            )
            server.serve_forever()
        except Exception:
            LOGGER.exception("Admin web server failed")

    thread = threading.Thread(target=_run, name="web-admin", daemon=True)
    thread.start()
    LOGGER.info("Admin panel started on %s:%s", host, port)
    return thread
