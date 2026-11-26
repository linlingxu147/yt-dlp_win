# -*- coding: utf-8 -*-
"""内嵌浏览器的纯逻辑用例：导航白名单、地址归一整、错误文案。

不启动 GUI、不联网；EmbeddedBrowser 的构造与 close() 也不碰 pywebview。
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_PATH = os.path.join(_ROOT, "YtDlpGUI", "embedded.py")
_spec = importlib.util.spec_from_file_location("ytdlp_gui_embedded", _PATH)
embedded = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = embedded
_spec.loader.exec_module(embedded)

import sniff_rules as rules  # noqa: E402


def load_host():
    spec = importlib.util.spec_from_file_location(
        "ytdlp_gui_host_embedded_test", os.path.join(_ROOT, "YtDlpGUI", "host.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestNavigationPolicy(unittest.TestCase):
    def test_http_and_https_are_allowed(self):
        for url in ("http://example.com", "https://example.com/a?b=1#c",
                    "HTTPS://EXAMPLE.COM", "http://127.0.0.1:8799/x"):
            self.assertTrue(embedded.is_allowed_navigation(url), url)

    def test_blank_is_allowed_for_initial_load(self):
        self.assertTrue(embedded.is_allowed_navigation("about:blank"))
        self.assertTrue(embedded.is_allowed_navigation("  ABOUT:BLANK  "))

    def test_dangerous_and_local_schemes_are_blocked(self):
        for url in ("file:///C:/Windows/win.ini", "javascript:alert(1)",
                    "data:text/html,<b>x</b>", "blob:https://example.com/x",
                    "ftp://example.com/a.mp4", "chrome://settings",
                    "mailto:a@b.com", "", "   ", "http://", "https://"):
            self.assertFalse(embedded.is_allowed_navigation(url), url)


class TestNormalizeAddress(unittest.TestCase):
    def test_bare_domain_gets_https(self):
        self.assertEqual(embedded.normalize_address("example.com/watch?v=1"),
                         "https://example.com/watch?v=1")
        self.assertEqual(embedded.normalize_address("  example.com  "),
                         "https://example.com")

    def test_existing_scheme_is_kept(self):
        self.assertEqual(embedded.normalize_address("http://example.com/a"),
                         "http://example.com/a")
        self.assertEqual(embedded.normalize_address("localhost:3000"),
                         "https://localhost:3000")

    def test_pseudo_schemes_are_not_prefixed(self):
        for text in ("javascript:alert(1)", "data:text/html,x", "file:///C:/x",
                     "about:config", "mailto:a@b.com"):
            self.assertEqual(embedded.normalize_address(text), "", text)

    def test_garbage_is_rejected(self):
        for text in ("", "   ", "hello world", "打开视频", "about:blank"):
            if text.strip() == "about:blank":
                self.assertEqual(embedded.normalize_address(text), "about:blank")
            else:
                self.assertEqual(embedded.normalize_address(text), "", text)


class TestSharedSniffRules(unittest.TestCase):
    def test_rule_sources_are_loaded_from_js(self):
        self.assertIn("mime", rules.SOURCES)
        self.assertEqual(rules.MIN_MIME_BYTES, 204800)

    def test_accept_matrix(self):
        cases = [
            ("https://cdn/a.m3u8", "application/vnd.apple.mpegurl", 0, True),
            ("https://cdn/a.mp4", "video/mp4", 100, True),
            ("https://cdn/a.mp4?x=1", "", 0, True),
            ("https://cdn/a.mp4#frag", "text/html", 999999, True),
            ("https://cdn/a.m3u8?token=1#x", "", 0, True),
            ("https://cdn/a.ts", "", 0, True),
            ("https://cdn/page", "text/html", 999999, False),
            ("https://cdn/pic", "image/png", 999999, False),
            ("https://cdn/sound", "audio/mpeg", 1000, False),
            ("https://cdn/sound", "audio/mpeg", 300000, True),
            ("https://cdn/cover.png", "image/png", 0, False),
            ("ftp://cdn/a.mp4", "video/mp4", 0, False),
        ]
        for url, mime, size, want in cases:
            self.assertIs(rules.accept(url, mime, size), want, (url, mime, size))

    def test_classify(self):
        self.assertEqual(rules.classify("https://cdn/a.m3u8", ""), "stream")
        self.assertEqual(rules.classify("https://cdn/a.ts", ""), "stream")
        self.assertEqual(rules.classify("https://cdn/a.m4s", ""), "stream")
        self.assertEqual(rules.classify("https://x/a-1-30280.m4s", ""), "audio")
        self.assertEqual(rules.classify("https://cdn/a.mp3", ""), "audio")
        self.assertEqual(rules.classify("https://cdn/a.mp4", "video/mp4"), "video")
        self.assertEqual(rules.classify("https://cdn/a.png", ""), "image")
        self.assertEqual(rules.classify("https://cdn/page", "text/html"), "other")
        self.assertEqual(rules.classify("https://cdn/page", "video/mp4"), "video")

    def test_manifest_segment_audio_flags(self):
        self.assertTrue(rules.is_manifest("https://cdn/a.m3u8?x=1"))
        self.assertTrue(rules.is_manifest("https://cdn/a.mpd#t=1"))
        self.assertFalse(rules.is_manifest("https://cdn/a.mp4"))
        self.assertTrue(rules.is_segment("https://cdn/a-1.ts?x=1"))
        self.assertFalse(rules.is_segment("https://cdn/a.mp4"))
        self.assertTrue(rules.is_audio("https://x/a-1-30280.m4s"))
        self.assertFalse(rules.is_audio("https://x/a-1-100026.m4s"))
        self.assertTrue(rules.is_audio("https://x/a.ogg"))
        self.assertEqual(rules.guess_kind("https://x/a.m3u8"), "m3u8")
        self.assertEqual(rules.guess_kind("https://x/a.mpd"), "mpd")

    def test_normalize_url(self):
        self.assertEqual(rules.normalize_url("  https://x/a.mp4?x=1&amp;y=2  "),
                         "https://x/a.mp4?x=1&y=2")


class TestResponseItems(unittest.TestCase):
    def test_manifest_becomes_embedded_stream_item(self):
        it = embedded.response_item(
            "https://cdn/a.m3u8", 200, "application/vnd.apple.mpegurl", 0,
            "https://page/watch", "UA")
        self.assertIsNotNone(it)
        self.assertEqual(it["entry"], "embedded")
        self.assertEqual(it["cat"], "stream")
        self.assertEqual(it["mime"], "application/vnd.apple.mpegurl")
        self.assertEqual(it["source"], "内嵌网络")
        self.assertEqual(it["page_url"], "https://page/watch")
        self.assertEqual(it["referer"], "https://page/watch")
        self.assertEqual(it["ua"], "UA")

    def test_filters_bad_status_mime_and_scheme(self):
        self.assertIsNone(embedded.response_item("https://cdn/a.m3u8", 404, "", 0))
        self.assertIsNone(embedded.response_item("https://cdn/page", 200, "text/html", 999999))
        self.assertIsNone(embedded.response_item("https://cdn/pic", 200, "image/png", 999999))
        self.assertIsNone(embedded.response_item("ftp://cdn/a.mp4", 200, "video/mp4", 0))
        self.assertIsNone(embedded.response_item("https://cdn/a.mp4", "oops", "", 0))

    def test_batch_dedupe_keeps_last(self):
        items = embedded.dedupe_items([
            {"url": "https://cdn/a.mp4", "size": 1},
            {"url": "https://cdn/a.mp4", "size": 2},
            "junk",
            {"url": "https://cdn/b.mp4"},
            {"url": ""},
        ])
        self.assertEqual(len(items), 2)
        got = {it["url"]: it for it in items}
        self.assertEqual(got["https://cdn/a.mp4"]["size"], 2)


class TestHostSniffSink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mod = load_host()
        self.host = mod.Host(os.path.join(self.tmp.name, "config.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sink_registers_into_web_module(self):
        calls = []

        class FakeWeb:
            def register_sniff_items(self, items, page_url=""):
                calls.append((items, page_url))

        self.host._web_mod = FakeWeb()
        items = [{"url": "https://cdn/a.mp4", "entry": "embedded"}]
        self.assertTrue(self.host.register_embedded_sniff(items, "https://page"))
        self.assertEqual(calls, [(items, "https://page")])

    def test_sink_without_service_refuses(self):
        self.host._web_mod = None
        self.assertFalse(self.host.register_embedded_sniff([{"url": "https://cdn/a.mp4"}]))


class TestNavErrorText(unittest.TestCase):
    def test_known_network_errors(self):
        self.assertIn("超时", embedded.describe_nav_error("Timeout"))
        self.assertIn("域名解析失败",
                      embedded.describe_nav_error("HostNameNotResolved"))
        self.assertIn("连不上服务器",
                      embedded.describe_nav_error("ServerUnreachable"))

    def test_certificate_errors(self):
        for name in ("CertificateExpired", "CertificateCommonNameIsIncorrect",
                     "CertificateIsInvalid"):
            self.assertIn("证书", embedded.describe_nav_error(name))

    def test_cancel_is_silent(self):
        self.assertEqual(embedded.describe_nav_error("OperationCanceled"), "")
        self.assertEqual(embedded.describe_nav_error(""), "")

    def test_unknown_error_keeps_raw_name(self):
        text = embedded.describe_nav_error("SomethingNew")
        self.assertIn("SomethingNew", text)


class TestControllerShape(unittest.TestCase):
    def test_construct_and_close_without_gui(self):
        b = embedded.EmbeddedBrowser()
        self.assertIsNone(b._form)
        self.assertEqual(b.last_status, "")
        b.close()                      # 无视图时必须是空操作
        self.assertIsNone(b._form)

    def test_open_without_window_is_refused(self):
        b = embedded.EmbeddedBrowser()
        r = b.open(None)
        self.assertFalse(r["ok"])
        self.assertIn("主窗口", r["error"])

    def test_not_serializable_by_pywebview(self):
        """主窗口 js_api 会递归反射 host 的公开属性，控制器必须显式挡住。"""
        self.assertIs(embedded.EmbeddedBrowser._serializable, False)


class TestHostLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mod = load_host()
        self.host = mod.Host(os.path.join(self.tmp.name, "config.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_closes_embedded_window(self):
        closed = []

        class FakeBrowser:
            def close(self):
                closed.append(True)

        self.host.embedded = FakeBrowser()
        self.host.shutdown()
        self.assertEqual(closed, [True])
        self.assertIsNone(self.host.embedded)

    def test_open_without_embedded_module_reports_error(self):
        sys.modules["embedded"] = None          # 让 import 抛 ImportError
        try:
            r = self.host.open_embedded_browser()
            self.assertFalse(r["ok"])
            self.assertIn("内嵌浏览器", r["error"])
        finally:
            sys.modules.pop("embedded", None)

    def test_open_without_main_window_is_refused(self):
        sys.modules["embedded"] = embedded      # 复用本测试已加载的实现
        try:
            r = self.host.open_embedded_browser(None)
        finally:
            sys.modules.pop("embedded", None)
        self.assertFalse(r["ok"])
        self.assertIn("主窗口", r["error"])


if __name__ == "__main__":
    unittest.main()