import importlib
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
HELPER_DIR = os.path.join(ROOT, "helper")
if HELPER_DIR not in sys.path:
    sys.path.insert(0, HELPER_DIR)

i18n = importlib.import_module("i18n")
web_admin = importlib.import_module("web_admin")
config_center = importlib.import_module("config_center")


class TestI18nLocaleLoading(unittest.TestCase):
    """Test locale dictionary loading from JSON files."""

    def test_en_locale_loaded(self):
        locales = i18n.available_locales()
        self.assertIn("en", locales)

    def test_zh_locale_loaded(self):
        locales = i18n.available_locales()
        self.assertIn("zh", locales)

    def test_at_least_two_locales_available(self):
        self.assertGreaterEqual(len(i18n.available_locales()), 2)

    def test_locales_have_matching_keys(self):
        en = i18n.get_translations("en")
        zh = i18n.get_translations("zh")
        # zh should have all keys that en has
        missing = set(en.keys()) - set(zh.keys())
        self.assertEqual(missing, set(),
                         "zh locale is missing keys: %s" % missing)

    def test_locale_files_valid_json(self):
        locale_dir = i18n._locale_dir()
        for filename in os.listdir(locale_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(locale_dir, filename)
            with open(filepath, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIsInstance(data, dict,
                                 "%s should contain a dict" % filename)
            self.assertGreater(len(data), 0,
                               "%s should not be empty" % filename)


class TestTranslationLookup(unittest.TestCase):
    """Test the t() translation function."""

    def test_basic_english_lookup(self):
        result = i18n.t("nav_dashboard", "en")
        self.assertEqual(result, "Dashboard")

    def test_basic_chinese_lookup(self):
        result = i18n.t("nav_dashboard", "zh")
        self.assertNotEqual(result, "Dashboard")
        self.assertNotEqual(result, "nav_dashboard")

    def test_fallback_to_default_locale(self):
        # Non-existent locale should fall back to default (en)
        result = i18n.t("nav_dashboard", "xx")
        self.assertEqual(result, "Dashboard")

    def test_missing_key_returns_key(self):
        result = i18n.t("nonexistent_key_12345", "en")
        self.assertEqual(result, "nonexistent_key_12345")

    def test_none_locale_uses_default(self):
        result = i18n.t("nav_dashboard", None)
        self.assertEqual(result, "Dashboard")

    def test_interpolation(self):
        result = i18n.t("proxies_deleted_count", "en", count=5)
        self.assertEqual(result, "Deleted 5 proxies")

    def test_interpolation_chinese(self):
        result = i18n.t("proxies_deleted_count", "zh", count=5)
        self.assertIn("5", result)

    def test_interpolation_missing_param_no_error(self):
        # Should not raise even if placeholder is not provided
        result = i18n.t("proxies_deleted_count", "en")
        self.assertIsInstance(result, str)

    def test_batch_generated_interpolation(self):
        result = i18n.t("batch_generated", "en", count=100, first=10001, last=10100)
        self.assertIn("100", result)
        self.assertIn("10001", result)
        self.assertIn("10100", result)


class TestGetTranslations(unittest.TestCase):
    """Test get_translations() full dict retrieval."""

    def test_en_returns_dict(self):
        result = i18n.get_translations("en")
        self.assertIsInstance(result, dict)
        self.assertIn("app_name", result)
        self.assertEqual(result["app_name"], "ProxyPool")

    def test_zh_overlay_on_en_base(self):
        result = i18n.get_translations("zh")
        # Should have all en keys as base
        en = i18n.get_translations("en")
        for key in en:
            self.assertIn(key, result)

    def test_unknown_locale_returns_en_base(self):
        result = i18n.get_translations("xx")
        en = i18n.get_translations("en")
        self.assertEqual(result, en)


class TestDetectLocale(unittest.TestCase):
    """Test locale detection from request signals."""

    def test_query_param_highest_priority(self):
        result = i18n.detect_locale(query_lang="zh", cookie_lang="en")
        self.assertEqual(result, "zh")

    def test_cookie_fallback(self):
        result = i18n.detect_locale(query_lang=None, cookie_lang="zh")
        self.assertEqual(result, "zh")

    def test_accept_language_fallback(self):
        result = i18n.detect_locale(
            query_lang=None, cookie_lang=None,
            accept_header="zh-CN,zh;q=0.9,en;q=0.8",
        )
        self.assertEqual(result, "zh")

    def test_accept_language_quality_ordering(self):
        result = i18n.detect_locale(
            query_lang=None, cookie_lang=None,
            accept_header="en;q=0.9,zh;q=1.0",
        )
        self.assertEqual(result, "zh")

    def test_unsupported_locale_falls_back(self):
        result = i18n.detect_locale(query_lang="fr")
        self.assertEqual(result, i18n.DEFAULT_LOCALE)

    def test_empty_signals_return_default(self):
        result = i18n.detect_locale()
        self.assertEqual(result, i18n.DEFAULT_LOCALE)

    def test_accept_language_with_no_supported(self):
        result = i18n.detect_locale(
            accept_header="fr-FR,de;q=0.8",
        )
        self.assertEqual(result, i18n.DEFAULT_LOCALE)


class TestParseAcceptLanguage(unittest.TestCase):
    """Test Accept-Language header parsing."""

    def test_simple_header(self):
        result = i18n._parse_accept_language("en")
        self.assertEqual(result, "en")

    def test_header_with_region(self):
        result = i18n._parse_accept_language("zh-CN")
        self.assertEqual(result, "zh")

    def test_header_with_quality(self):
        result = i18n._parse_accept_language("en;q=0.5,zh;q=0.9")
        self.assertEqual(result, "zh")

    def test_no_supported_returns_none(self):
        result = i18n._parse_accept_language("fr-FR,de;q=0.8")
        self.assertIsNone(result)

    def test_empty_header(self):
        result = i18n._parse_accept_language("")
        self.assertIsNone(result)


class TestTranslationNamespace(unittest.TestCase):
    """Test the _TranslationNamespace used in templates."""

    def test_dot_access(self):
        ns = web_admin._TranslationNamespace({"nav_dashboard": "Dashboard"})
        self.assertEqual(ns.nav_dashboard, "Dashboard")

    def test_missing_key_returns_key_name(self):
        ns = web_admin._TranslationNamespace({})
        self.assertEqual(ns.nonexistent_key, "nonexistent_key")

    def test_i18n_ns_builder(self):
        ns = web_admin._i18n_ns("en")
        self.assertEqual(ns.app_name, "ProxyPool")
        self.assertEqual(ns.nav_dashboard, "Dashboard")


class TestI18nKeyCompleteness(unittest.TestCase):
    """Verify that admin-facing i18n keys exist in both locales."""

    @classmethod
    def setUpClass(cls):
        admin_root = os.path.join(HELPER_DIR, "admin_web")
        html_pattern = re.compile(r"\bi\.([A-Za-z0-9_]+)\b")
        py_pattern = re.compile(r"\bt\(\s*[\"']([A-Za-z0-9_]+)[\"']")

        required_keys = set()
        for dirpath, _, filenames in os.walk(admin_root):
            for filename in filenames:
                if not filename.endswith((".html", ".py")):
                    continue

                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()

                if filename.endswith(".html"):
                    required_keys.update(html_pattern.findall(content))
                if filename.endswith(".py"):
                    required_keys.update(py_pattern.findall(content))

        cls.required_keys = sorted(required_keys)

    def test_en_has_all_required_keys(self):
        translations = i18n.get_translations("en")
        missing = [key for key in self.required_keys if key not in translations]
        self.assertEqual(
            missing,
            [],
            "en locale missing required admin keys: %s" % missing,
        )

    def test_zh_has_all_required_keys(self):
        translations = i18n.get_translations("zh")
        missing = [key for key in self.required_keys if key not in translations]
        self.assertEqual(
            missing,
            [],
            "zh locale missing required admin keys: %s" % missing,
        )


class TestAdminTemplateTranslationRegression(unittest.TestCase):
    """Guard against reintroducing known hardcoded admin UI strings."""

    def test_known_admin_templates_do_not_contain_hardcoded_english(self):
        checks = {
            os.path.join(
                HELPER_DIR, "admin_web", "templates", "logs", "scripts.js"
            ): [
                "No log entries",
                "Buffer: ",
                "Last updated: ",
                "Refresh failed",
            ],
            os.path.join(
                HELPER_DIR, "admin_web", "jinja_templates", "proxies", "list.html"
            ): [
                ">ON<",
                ">OFF<",
            ],
        }

        for path, forbidden_tokens in checks.items():
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            for token in forbidden_tokens:
                self.assertNotIn(
                    token,
                    content,
                    "%s should not contain hardcoded admin UI token %r"
                    % (path, token),
                )


class TestConfigPageI18nCoverage(unittest.TestCase):
    """Verify config-page generated translation keys exist in both locales."""

    ERROR_KEYS = [
        "config_error_required",
        "config_error_integer",
        "config_error_between",
        "config_error_at_least",
        "config_error_non_negative",
        "config_error_number",
        "config_error_positive",
        "config_error_one_of",
    ]

    @classmethod
    def setUpClass(cls):
        keys = set(cls.ERROR_KEYS)
        for field in config_center.CONFIG_PAGE_FIELDS:
            env_key = field.env_key.lower()
            keys.add("config_field_%s_label" % env_key)
            keys.add("config_field_%s_help" % env_key)
            for option in field.options:
                keys.add("config_option_%s_%s" % (env_key, option.lower()))
        cls.required_keys = sorted(keys)

    def test_en_has_generated_config_keys(self):
        translations = i18n.get_translations("en")
        missing = [key for key in self.required_keys if key not in translations]
        self.assertEqual(missing, [], "en locale missing config keys: %s" % missing)

    def test_zh_has_generated_config_keys(self):
        translations = i18n.get_translations("zh")
        missing = [key for key in self.required_keys if key not in translations]
        self.assertEqual(missing, [], "zh locale missing config keys: %s" % missing)


if __name__ == "__main__":
    unittest.main()
