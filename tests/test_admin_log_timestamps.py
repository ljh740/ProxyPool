import importlib
import logging
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

admin_server = importlib.import_module("admin_web.server")
admin_resources = importlib.import_module("admin_web.resources")


class AdminLogTimestampTests(unittest.TestCase):
    def test_ring_buffer_handler_stores_timestamp_ms(self):
        handler = admin_server.RingBufferHandler(maxlen=2)
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            name="test_admin_logs",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="hello",
            args=(),
            exc_info=None,
        )
        record.created = 1713000000.123

        handler.emit(record)

        entry = handler.get_entries()[0]
        self.assertEqual(entry["timestamp_ms"], 1713000000123)
        self.assertIn("timestamp", entry)

    def test_logs_view_template_exposes_timestamp_ms_attribute(self):
        template = admin_resources.load_jinja_template_source("logs/view.html")

        self.assertIn('class="pp-log-timestamp"', template)
        self.assertIn('data-timestamp-ms="{{ entry.get("timestamp_ms", "") }}"', template)

    def test_logs_script_formats_timestamp_ms_in_browser_timezone(self):
        script = admin_resources.load_template_source("logs/scripts.js")

        self.assertIn("function formatTimestampMs(timestampMs, fallback)", script)
        self.assertIn('renderTimestampCell(entry)', script)
        self.assertIn('applyTimestampFormatting(document);', script)
        self.assertIn('entry.timestamp_ms', script)


if __name__ == "__main__":
    unittest.main()
