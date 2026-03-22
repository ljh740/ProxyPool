import contextlib
import io
import os
import runpy
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

import persistence  # noqa: E402


class TestResetAdminPasswordScript(unittest.TestCase):
    def test_script_clears_admin_password(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        try:
            storage = persistence.open_storage(path)
            try:
                persistence.save_config(
                    storage,
                    {
                        "AUTH_PASSWORD": "proxy-secret",
                        "SALT": "stable-salt",
                        "ADMIN_PASSWORD": "panel-admin",
                    },
                )
            finally:
                storage.close()

            old_argv = sys.argv[:]
            stdout = io.StringIO()
            try:
                sys.argv = [
                    "reset_admin_password.py",
                    "--state-db-path",
                    path,
                ]
                with contextlib.redirect_stdout(stdout):
                    runpy.run_path(
                        os.path.join(ROOT, "scripts", "reset_admin_password.py"),
                        run_name="__main__",
                    )
            finally:
                sys.argv = old_argv

            storage = persistence.open_storage(path)
            try:
                config = persistence.load_config(storage)
            finally:
                storage.close()

            self.assertEqual(config["AUTH_PASSWORD"], "proxy-secret")
            self.assertEqual(config["ADMIN_PASSWORD"], "")
            self.assertIn("Admin password cleared", stdout.getvalue())
        finally:
            if os.path.exists(path):
                os.remove(path)
