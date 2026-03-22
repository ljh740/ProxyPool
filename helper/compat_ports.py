#!/usr/bin/env python3
"""Compatibility port mappings for no-auth local listeners."""

from dataclasses import dataclass

COMPAT_PORT_MIN = 33100
COMPAT_PORT_MAX = 33199

TARGET_TYPE_ENTRY_KEY = "entry_key"
TARGET_TYPE_SESSION_NAME = "session_name"
TARGET_TYPES = (TARGET_TYPE_ENTRY_KEY, TARGET_TYPE_SESSION_NAME)


@dataclass(frozen=True)
class CompatPortMapping:
    listen_port: int
    target_type: str
    target_value: str
    enabled: bool = True
    note: str = ""

    def __post_init__(self):
        listen_port = int(self.listen_port)
        target_type = str(self.target_type).strip()
        target_value = str(self.target_value).strip()
        note = str(self.note).strip()

        if listen_port < COMPAT_PORT_MIN or listen_port > COMPAT_PORT_MAX:
            raise ValueError(
                "listen_port must be between %d and %d"
                % (COMPAT_PORT_MIN, COMPAT_PORT_MAX)
            )
        if target_type not in TARGET_TYPES:
            raise ValueError("target_type must be one of %s" % ", ".join(TARGET_TYPES))
        if not target_value:
            raise ValueError("target_value is required")

        object.__setattr__(self, "listen_port", listen_port)
        object.__setattr__(self, "target_type", target_type)
        object.__setattr__(self, "target_value", target_value)
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "note", note)

    def to_dict(self):
        return {
            "listen_port": self.listen_port,
            "target_type": self.target_type,
            "target_value": self.target_value,
            "enabled": self.enabled,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("compat port mapping must be a dict")
        return cls(
            listen_port=data.get("listen_port"),
            target_type=data.get("target_type"),
            target_value=data.get("target_value"),
            enabled=data.get("enabled", True),
            note=data.get("note", ""),
        )
