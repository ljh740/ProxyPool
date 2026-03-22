#!/usr/bin/env python3
"""Clear the persisted admin password and return Web Admin to setup mode."""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

from persistence import (  # noqa: E402
    clear_admin_password,
    open_storage,
    resolve_state_db_path,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Clear the persisted admin password so /setup can be used again."
    )
    parser.add_argument(
        "--state-db-path",
        default="",
        help="SQLite state DB path. Defaults to STATE_DB_PATH or ./data/proxypool.sqlite3.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    state_db_path = resolve_state_db_path(args.state_db_path or None)
    storage = open_storage(state_db_path)
    try:
        clear_admin_password(storage)
    finally:
        storage.close()
    print(
        "Admin password cleared for %s. Open Web Admin and complete /setup again."
        % state_db_path
    )


if __name__ == "__main__":
    main()
