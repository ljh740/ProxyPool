"""Shared runtime state for the admin web subsystem."""


class AdminRuntimeState:
    """Mutable admin runtime state shared across routes and helpers."""

    def __init__(self):
        self.server_ref = None
        self.admin_storage = None
        self.log_handler = None


_STATE = AdminRuntimeState()


def get_server_ref():
    return _STATE.server_ref


def set_server_ref(value):
    _STATE.server_ref = value


def get_admin_storage():
    return _STATE.admin_storage


def set_admin_storage(value):
    _STATE.admin_storage = value


def get_log_handler():
    return _STATE.log_handler


def set_log_handler(value):
    _STATE.log_handler = value
