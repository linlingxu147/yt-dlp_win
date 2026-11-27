# -*- coding: utf-8 -*-
"""内嵌浏览器的纯逻辑用例：导航白名单、地址归一整、错误文案。

不启动 GUI、不联网；EmbeddedBrowser 的构造与 close() 也不碰 pywebview。
"""
import importlib.util
import json
import os
import queue
import sys
import tempfile
import threading
import time
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


class TestThemePalette(unittest.TestCase):
    def test_light_and_dark_palettes(self):
        light = embedded.palette_for("light")
        dark = embedded.palette_for("dark")
        self.assertEqual(light["bg"], "#f6f8fa")
        self.assertEqual(dark["bg"], "#161b22")
        self.assertNotEqual(light["text"], dark["text"])
        for key in ("bg", "field", "text", "muted", "border",
                    "hover", "press", "accent", "err"):
            self.assertIn(key, light)
            self.assertIn(key, dark)

    def test_unknown_theme_falls_back_to_light(self):
        self.assertEqual(embedded.palette_for("sepia"), embedded.palette_for("light"))
        self.assertEqual(embedded.palette_for(""), embedded.palette_for("light"))
        self.assertEqual(embedded.palette_for(None), embedded.palette_for("light"))

    def test_palette_is_a_copy(self):
        dark = embedded.palette_for("dark")
        dark["bg"] = "#000000"
        self.assertEqual(embedded.palette_for("dark")["bg"], "#161b22")

    def test_normalize_theme(self):
        self.assertEqual(embedded.normalize_theme(" DARK "), "dark")
        self.assertEqual(embedded.normalize_theme("light"), "light")
        self.assertEqual(embedded.normalize_theme("system"), "")
        self.assertEqual(embedded.normalize_theme(None), "")


class TestStartPage(unittest.TestCase):
    def test_dark_page_declares_color_scheme(self):
        html = embedded.start_page_html("dark")
        self.assertIn('name="color-scheme" content="dark"', html)
        self.assertIn("color-scheme: dark", html)
        self.assertIn("light-dark(", html)

    def test_light_page_and_fallback(self):
        for theme in ("light", "", "system", "weird"):
            html = embedded.start_page_html(theme)
            self.assertIn('name="color-scheme" content="light"', html, theme)
            self.assertIn("color-scheme: light", html, theme)

    def test_page_has_url_form_and_quick_links(self):
        html = embedded.start_page_html("light")
        self.assertIn("<form", html)
        self.assertIn('id="url-input"', html)
        self.assertIn('type="submit"', html)
        self.assertIn('aria-live="polite"', html)
        for site in ("bilibili.com", "douyin.com", "youtube.com"):
            self.assertIn(site, html)

    def test_page_has_no_bridge_and_no_keydown_submit(self):
        """起始页不能依赖 pywebview；回车必须走原生表单提交（输入法安全）。"""
        html = embedded.start_page_html("dark")
        self.assertNotIn("pywebview", html)
        self.assertNotIn("keydown", html)


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
            # 日志/埋点把媒体地址塞进查询串：不是媒体，别按 2B 的假视频收
            ("https://data.bilibili.com/log/web?x=1&url=https://cdn/a.mp4",
             "text/plain; charset=utf-8", 2, False),
            # 路径本身是媒体后缀的 text/plain 才可能是伪装媒体
            ("https://cdn/a.mp4?from=log", "text/plain; charset=utf-8", 999999, True),
        ]
        for url, mime, size, want in cases:
            self.assertIs(rules.accept(url, mime, size), want, (url, mime, size))

    def test_opaque_direct_link_fallback(self):
        """无后缀 + octet-stream 的签名直链：只有传了 opts 才靠体积/分片兜底。"""
        cases = [
            ("https://cdn/videoplayback?id=1", "application/octet-stream", 5 << 20, True),
            ("https://cdn/videoplayback?id=1", "application/octet-stream", 1000, False),
            ("https://cdn/videoplayback?id=1", "", 5 << 20, True),
            ("https://cdn/videoplayback?id=1", "text/plain", 5 << 20, False),
            ("https://cdn/videoplayback?id=1", "image/png", 5 << 20, False),
        ]
        for url, mime, size, want in cases:
            self.assertIs(rules.accept(url, mime, size, {"range": False}), want,
                          (url, mime, size))
        # 带 Content-Range 的分片响应：体积小也认
        self.assertTrue(rules.accept("https://cdn/videoplayback?id=1",
                                     "application/octet-stream", 1024, {"range": True}))
        # 不传 opts：维持旧行为，整条不收
        self.assertFalse(rules.accept("https://cdn/videoplayback?id=1",
                                      "application/octet-stream", 5 << 20))
        # B 站 jinkela 这类前端 JS 被 CDN 标成 octet-stream，体积再大也不是媒体
        self.assertFalse(rules.accept(
            "https://s1.hdslb.com/bfs/static/jinkela/long/fo.js",
            "application/octet-stream", 5 << 20, {"range": False}))

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

    def test_request_referer_becomes_page(self):
        it = embedded.response_item(
            "https://cdn/a.mp4", 200, "video/mp4", 0, "https://page/watch", "UA",
            "https://player.example.com/embed/9")
        self.assertEqual(it["page_url"], "https://player.example.com/embed/9")
        self.assertEqual(it["referer"], "https://player.example.com/embed/9")

    def test_bad_request_referer_falls_back_to_page(self):
        it = embedded.response_item(
            "https://cdn/a.mp4", 200, "video/mp4", 0, "https://page/watch", "UA",
            "javascript:alert(1)")
        self.assertEqual(it["referer"], "https://page/watch")

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

    def test_ranged_octet_stream_is_accepted(self):
        """206 + Content-Range 的无后缀直链：靠分片信号收下。"""
        it = embedded.response_item("https://cdn/videoplayback?id=1", 206,
                                    "application/octet-stream", 1024,
                                    "https://page/watch", "UA", "", True)
        self.assertIsNotNone(it)
        self.assertEqual(it["source"], "内嵌网络")
        # 同样的响应不带 range：体积没过阈值，不收
        self.assertIsNone(embedded.response_item("https://cdn/videoplayback?id=1", 206,
                                                 "application/octet-stream", 1024,
                                                 "https://page/watch", "UA", "", False))


class TestBodySniff(unittest.TestCase):
    """正文嗅探：可读性判定 → 清单/直链解析 → 入队与收尾。"""

    def setUp(self):
        self.b = embedded.EmbeddedBrowser()
        self.b._sniff_q = queue.Queue()
        self.b._sniff_sink = lambda items, page="": True
        self.b._page_url = "https://page.example.com/watch"
        self.b._ua = "UA"
        self.b._page_title = "页"

    def _drain(self, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return self.b._sniff_q.get_nowait()
            except queue.Empty:
                time.sleep(0.02)
        self.fail("等不到正文嗅探结果")

    def test_readable_body_matrix(self):
        ok = [
            ("https://cdn/api", "text/plain", 2048),
            ("https://cdn/api", "application/json; charset=utf-8", 2048),
            ("https://cdn/api", "application/octet-stream", 2048),
            ("https://cdn/api", "application/vnd.apple.mpegurl", 2048),
        ]
        for url, mime, size in ok:
            self.assertTrue(embedded.readable_body(url, mime, size), (mime, size))
        for mime in ("video/mp4", "image/png", "audio/mpeg", "font/woff2"):
            self.assertFalse(embedded.readable_body("https://cdn/api", mime, 2048), mime)
        self.assertFalse(embedded.readable_body("https://cdn/api", "text/plain", 0))
        self.assertFalse(embedded.readable_body("https://cdn/api", "text/plain", 5 << 20))

    def test_sniff_body_manifest(self):
        self.assertEqual(embedded.sniff_body("#EXTM3U\n#EXT-X-VERSION:3\n"), [])
        self.assertEqual(
            embedded.sniff_body("#EXTM3U\n#EXT-X-VERSION:3\n", "https://cdn/play?id=1"),
            [("https://cdn/play?id=1", "m3u8")])
        self.assertEqual(
            embedded.sniff_body('<?xml version="1.0"?><MPD mediaPresentationDuration="PT10S"/>',
                                "https://cdn/play?id=2"),
            [("https://cdn/play?id=2", "mpd")])

    def test_sniff_body_json_with_escaped_slashes(self):
        body = '{"data":{"url":"https:\\/\\/cdn.example.com\\/v.m3u8?token=1"}}'
        self.assertEqual(embedded.sniff_body(body),
                         [("https://cdn.example.com/v.m3u8?token=1", "m3u8")])

    def test_sniff_body_dedupes_and_caps(self):
        body = " ".join('"https://cdn/a%d.mp4"' % i for i in range(20))
        out = embedded.sniff_body(body)
        self.assertEqual(len(out), embedded._BODY_MAX_ITEMS)
        self.assertEqual(len(set(u for u, _ in out)), len(out))

    def test_sniff_record_fields(self):
        rec = embedded.sniff_record("https://cdn/a.m3u8?x=1&amp;y=2", "m3u8", "响应正文",
                                    "https://page/watch", "UA",
                                    "https://player.example.com/embed/9")
        self.assertEqual(rec["url"], "https://cdn/a.m3u8?x=1&y=2")
        self.assertEqual(rec["cat"], "stream")
        self.assertEqual(rec["entry"], "embedded")
        self.assertEqual(rec["source"], "响应正文")
        self.assertEqual(rec["page_url"], "https://player.example.com/embed/9")
        self.assertEqual(rec["referer"], "https://player.example.com/embed/9")
        self.assertIsNone(embedded.sniff_record("ftp://cdn/a.m3u8", "m3u8", "响应正文"))

    def test_manifest_body_end_to_end(self):
        """WebResourceRequested → 后台读正文 → 入队，deferral 必须 Complete。"""
        args = _BodyArgs("https://cdn/play?id=1", mime="text/plain", size=1024,
                         referer="https://player.example.com/embed/9", task=object())
        orig = embedded.task_text
        embedded.task_text = lambda task, timeout=None, limit=None: "#EXTM3U\n#EXT-X-VERSION:3\n"
        try:
            self.b._on_web_resource_requested(None, args)
            item = self._drain()
        finally:
            embedded.task_text = orig
        self.assertEqual(item["url"], "https://cdn/play?id=1")
        self.assertEqual(item["kind"], "m3u8")
        self.assertEqual(item["cat"], "stream")
        self.assertEqual(item["source"], "响应正文")
        self.assertEqual(item["referer"], "https://player.example.com/embed/9")
        self.assertEqual(item["title"], "页")
        self.assertEqual(len(args.deferrals), 1)
        self.assertTrue(args.deferrals[0].completed)
        self.assertEqual(self.b._body_busy, 0)

    def test_skip_when_body_is_not_a_manifest(self):
        args = _BodyArgs("https://cdn/api?id=1", mime="application/json", size=1024,
                         task=object())
        orig = embedded.task_text
        embedded.task_text = lambda task, timeout=None, limit=None: '{"ok":true}'
        try:
            self.b._on_web_resource_requested(None, args)
        finally:
            embedded.task_text = orig
        self.assertTrue(self.b._sniff_q.empty())
        self.assertTrue(args.deferrals[0].completed)
        self.assertEqual(self.b._body_busy, 0)

    def test_no_deferral_for_non_candidates(self):
        for mime, size, url in (("image/png", 4096, "https://cdn/pic.png"),
                                ("video/mp4", 4096, "https://cdn/v.mp4"),
                                ("text/plain", 0, "https://cdn/api"),
                                ("text/plain", 5 << 20, "https://cdn/big")):
            args = _BodyArgs(url, mime=mime, size=size, task=object())
            self.b._on_web_resource_requested(None, args)
            self.assertEqual(args.deferrals, [], (mime, size))
        self.assertEqual(self.b._body_busy, 0)

    def test_empty_body_completes_deferral(self):
        args = _BodyArgs("https://cdn/api", mime="text/plain", size=1024, task=object())
        orig = embedded.task_text
        embedded.task_text = lambda task, timeout=None, limit=None: ""
        try:
            self.b._on_web_resource_requested(None, args)
        finally:
            embedded.task_text = orig
        self.assertTrue(args.deferrals[0].completed)
        self.assertEqual(self.b._body_busy, 0)


class TestDomIngest(unittest.TestCase):
    """DOM 补扫入库：只收 http(s)、图片宽高复核、同页去重、单次上限。"""

    def setUp(self):
        self.b = embedded.EmbeddedBrowser()          # 只构造对象，不创建视图
        self.b._sniff_q = queue.Queue()
        self.b._page_url = "https://page.example.com/watch?v=1"
        self.b._ua = "UA"
        self.b._page_title = "测试页"

    def _drain(self):
        out = []
        while True:
            try:
                out.append(self.b._sniff_q.get_nowait())
            except queue.Empty:
                return out

    def test_http_video_is_accepted(self):
        got = self.b._accept_dom_items([
            {"url": "https://cdn.example.com/v.mp4?x=1&amp;y=2", "kind": "video",
             "w": 0, "h": 0, "title": ""},
        ])
        self.assertEqual(len(got), 1)
        it = got[0]
        self.assertEqual(it["url"], "https://cdn.example.com/v.mp4?x=1&y=2")
        self.assertEqual(it["kind"], "video")
        self.assertEqual(it["cat"], "video")
        self.assertEqual(it["entry"], "embedded")
        self.assertEqual(it["source"], "页面元素")
        self.assertEqual(it["size"], 0)
        self.assertEqual(it["page_url"], self.b._page_url)
        self.assertEqual(it["referer"], self.b._page_url)
        self.assertEqual(it["ua"], "UA")
        self.assertEqual(it["title"], "测试页")
        self.assertEqual(self._drain(), got)         # 也进了合批队列

    def test_image_requires_200px_on_both_sides(self):
        big = {"url": "https://cdn.example.com/big.jpg", "kind": "image",
               "w": 300, "h": 300}
        small = {"url": "https://cdn.example.com/small.jpg", "kind": "image",
                 "w": 100, "h": 100}
        thin = {"url": "https://cdn.example.com/thin.jpg", "kind": "image",
                "w": 1200, "h": 100}
        unknown = {"url": "https://cdn.example.com/no-size.jpg", "kind": "image"}
        got = self.b._accept_dom_items([big, small, thin, unknown])
        self.assertEqual([it["url"] for it in got], [big["url"]])
        self.assertEqual(got[0]["kind"], "image")
        self.assertEqual(got[0]["cat"], "image")
        self.assertEqual(len(self._drain()), 1)

    def test_non_http_and_garbage_are_rejected(self):
        got = self.b._accept_dom_items([
            {"url": "blob:https://page.example.com/abc", "kind": "video"},
            {"url": "data:image/png;base64,AAAA", "kind": "image", "w": 300, "h": 300},
            {"url": "ftp://cdn.example.com/a.mp4", "kind": "video"},
            {"url": ""},
            "junk",
            None,
            {"url": "https://cdn.example.com/ok.mp4", "kind": "video"},
        ])
        self.assertEqual([it["url"] for it in got], ["https://cdn.example.com/ok.mp4"])
        self.assertEqual(self._drain(), got)

    def test_duplicate_url_accepted_once(self):
        item = {"url": "https://cdn.example.com/a.mp4", "kind": "video"}
        got = self.b._accept_dom_items([item, dict(item)])
        self.assertEqual(len(got), 1)
        self.assertEqual(self.b._accept_dom_items([item]), [])   # 同一页再扫也不重复
        self.assertEqual(len(self._drain()), 1)

    def test_per_call_cap_is_300(self):
        items = [{"url": "https://cdn.example.com/%03d.mp4" % i, "kind": "video"}
                 for i in range(420)]
        got = self.b._accept_dom_items(items)
        self.assertEqual(len(got), 300)
        self.assertEqual(len(self._drain()), 300)

    def test_queue_full_drops_item(self):
        self.b._sniff_q = queue.Queue(maxsize=1)
        got = self.b._accept_dom_items([
            {"url": "https://cdn.example.com/a.mp4", "kind": "video"},
            {"url": "https://cdn.example.com/b.mp4", "kind": "video"},
        ])
        self.assertEqual([it["url"] for it in got], ["https://cdn.example.com/a.mp4"])
        self.assertEqual(len(self._drain()), 1)

    def test_non_list_payload_is_refused(self):
        for payload in (None, "x", 42, {"url": "https://cdn.example.com/a.mp4"}):
            self.assertEqual(self.b._accept_dom_items(payload), [], payload)
        self.assertEqual(self._drain(), [])

    def test_web_message_branch_ingests_dom(self):
        args = type("_Args", (), {"WebMessageAsJson": json.dumps({
            "__ytdlp": "dom",
            "items": [{"url": "https://cdn.example.com/m.mp4", "kind": "video",
                       "w": 0, "h": 0, "title": ""}],
        })})()
        self.b._on_web_message(None, args)
        queued = self._drain()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["source"], "页面元素")

    def test_page_change_clears_seen_set(self):
        item = {"url": "https://cdn.example.com/a.mp4", "kind": "video"}
        self.assertEqual(len(self.b._accept_dom_items([item])), 1)

        class _Args:
            Cancel = False
            Uri = "https://other.example.com/page"

        self.b._on_nav_starting(None, _Args())
        self.assertEqual(self.b._dom_seen, set())
        self.assertEqual(len(self.b._accept_dom_items([item])), 1)

    def test_frame_url_becomes_referer(self):
        got = self.b._accept_dom_items(
            [{"url": "https://cdn.example.com/embed.mp4", "kind": "video"}],
            "https://player.example.com/embed/9")
        self.assertEqual(got[0]["page_url"], "https://player.example.com/embed/9")
        self.assertEqual(got[0]["referer"], "https://player.example.com/embed/9")

    def test_bad_frame_url_falls_back_to_top_page(self):
        got = self.b._accept_dom_items(
            [{"url": "https://cdn.example.com/top.mp4", "kind": "video"}], "about:blank")
        self.assertEqual(got[0]["referer"], self.b._page_url)

    def test_web_message_carries_frame(self):
        args = type("_Args", (), {"WebMessageAsJson": json.dumps({
            "__ytdlp": "dom",
            "frame": "https://player.example.com/embed/9",
            "items": [{"url": "https://cdn.example.com/m2.mp4", "kind": "video",
                       "w": 0, "h": 0, "title": ""}],
        })})()
        self.b._on_web_message(None, args)
        self.assertEqual(self._drain()[0]["referer"], "https://player.example.com/embed/9")


class _Headers:
    """假 WebView2 头集合：只要 Contains / GetHeader。"""

    def __init__(self, values=None):
        self._v = {str(k).lower(): str(v) for k, v in (values or {}).items()}

    def Contains(self, name):
        return str(name).lower() in self._v

    def GetHeader(self, name):
        return self._v.get(str(name).lower(), "")


class _Response:
    def __init__(self, status=200, headers=None):
        self.StatusCode = status
        self.Headers = _Headers(headers)


class _Request:
    def __init__(self, uri, headers=None):
        self.Uri = uri
        self.Headers = _Headers(headers)


class _ResourceArgs:
    def __init__(self, uri, status=200, mime="", size="", referer=""):
        self.Request = _Request(uri, {"Referer": referer} if referer else None)
        headers = {}
        if mime:
            headers["Content-Type"] = mime
        if size:
            headers["Content-Length"] = str(size)
        self.Response = _Response(status, headers)


class _Deferral:
    def __init__(self):
        self.completed = False

    def Complete(self):
        self.completed = True


class _BodyResponse(_Response):
    def __init__(self, status=200, headers=None, task=None):
        super().__init__(status, headers)
        self.__task = task

    def GetContentAsync(self):
        return self.__task


class _BodyArgs(_ResourceArgs):
    """WebResourceRequested 的假事件：比 _ResourceArgs 多 deferral 与 GetContentAsync。"""

    def __init__(self, uri, status=200, mime="", size="", referer="", task=None):
        self.Request = _Request(uri, {"Referer": referer} if referer else None)
        headers = {}
        if mime:
            headers["Content-Type"] = mime
        if size:
            headers["Content-Length"] = str(size)
        self.Response = _BodyResponse(status, headers, task)
        self.deferrals = []

    def GetDeferral(self):
        d = _Deferral()
        self.deferrals.append(d)
        return d


class TestHookIngest(unittest.TestCase):
    """正文钩子：只收网络层实锤过的 URL，清单到手后不再收分片。"""

    def setUp(self):
        self.b = embedded.EmbeddedBrowser()
        self.b._sniff_q = queue.Queue()
        self.b._sniff_sink = lambda items, page="": True
        self.b._page_url = "https://page.example.com/watch"
        self.b._ua = "UA"
        self.b._page_title = "页"

    def _drain(self):
        out = []
        while True:
            try:
                out.append(self.b._sniff_q.get_nowait())
            except queue.Empty:
                return out

    def test_only_corroborated_urls_are_kept(self):
        seen = "https://cdn.example.com/a.m3u8"
        self.b._remember_net(seen)
        got = self.b._accept_hook_items([
            {"url": seen, "kind": "m3u8"},
            {"url": "https://cdn.example.com/fake.m3u8", "kind": "m3u8"},
        ])
        self.assertEqual([it["url"] for it in got], [seen])
        self.assertEqual(got[0]["source"], "页面请求")
        self.assertEqual(got[0]["kind"], "m3u8")
        self.assertEqual(got[0]["cat"], "stream")
        self.assertEqual(got[0]["entry"], "embedded")
        self.assertEqual(len(self._drain()), 1)

    def test_json_kind_falls_back_to_guess(self):
        url = "https://cdn.example.com/a.mp4"
        self.b._remember_net(url)
        got = self.b._accept_hook_items([{"url": url, "kind": "json"}])
        self.assertEqual(got[0]["kind"], "video")

    def test_segment_dropped_after_manifest(self):
        seg = "https://cdn.example.com/x.ts"
        self.b._remember_net(seg)
        self.b._manifest_pages.add(self.b._page_url)
        self.assertEqual(self.b._accept_hook_items([{"url": seg}]), [])

    def test_duplicate_once(self):
        url = "https://cdn.example.com/a.m3u8"
        self.b._remember_net(url)
        self.assertEqual(len(self.b._accept_hook_items([{"url": url}])), 1)
        self.assertEqual(self.b._accept_hook_items([{"url": url}]), [])

    def test_frame_url_is_carried(self):
        url = "https://cdn.example.com/b.m3u8"
        self.b._remember_net(url)
        got = self.b._accept_hook_items([{"url": url, "kind": "m3u8"}],
                                        "https://player.example.com/embed/1")
        self.assertEqual(got[0]["referer"], "https://player.example.com/embed/1")

    def test_network_event_records_corroboration_and_referer(self):
        uri = "https://cdn.example.com/play?id=1"
        self.b._on_web_resource_response(None, _ResourceArgs(
            uri, mime="text/plain", referer="https://player.example.com/embed/9"))
        self.assertIn(uri, self.b._net_seen)
        got = self.b._accept_hook_items([{"url": uri, "kind": "m3u8"}],
                                        "https://player.example.com/embed/9")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["referer"], "https://player.example.com/embed/9")
        self.assertEqual(got[0]["cat"], "stream")

    def test_network_item_uses_request_referer(self):
        self.b._on_web_resource_response(None, _ResourceArgs(
            "https://cdn.example.com/a.mp4", mime="video/mp4", size=1024,
            referer="https://player.example.com/embed/9"))
        item = self._drain()[-1]
        self.assertEqual(item["referer"], "https://player.example.com/embed/9")
        self.assertEqual(item["page_url"], "https://player.example.com/embed/9")

    def test_page_change_clears_corroboration(self):
        url = "https://cdn.example.com/a.m3u8"
        self.b._remember_net(url)
        self.b._hook_seen.add(url)

        class _Args:
            Cancel = False
            Uri = "https://other.example.com/page"

        self.b._on_nav_starting(None, _Args())
        self.assertEqual(self.b._net_seen, {})
        self.assertEqual(self.b._hook_seen, set())


class TestPageSniffUi(unittest.TestCase):
    """页面内浮条：条目归属、下载 URL 复核、消息分发。"""

    def setUp(self):
        self.b = embedded.EmbeddedBrowser()
        self.b._page_url = "https://page.example.com/watch"
        self.posted = []
        self.b._post_ui = lambda payload: self.posted.append(payload)

    def test_push_ui_keeps_current_top_page_only(self):
        page = self.b._page_url
        self.b._push_ui([
            {"url": "https://cdn/a.m3u8", "top_url": page, "cat": "stream",
             "kind": "m3u8", "title": "正片", "size": 1024},
            {"url": "https://cdn/old.mp4", "top_url": "https://page.example.com/old",
             "cat": "video"},
            {"url": "https://cdn/b.ts", "top_url": page, "cat": "segment"},
        ])
        got = self.b._ui_items
        self.assertIn("https://cdn/a.m3u8", got)
        self.assertIn("https://cdn/b.ts", got)
        self.assertNotIn("https://cdn/old.mp4", got)
        self.assertTrue(got["https://cdn/b.ts"]["seg"])

    def test_push_ui_posts_snapshot(self):
        self.b._push_ui([{"url": "https://cdn/a.mp4", "top_url": self.b._page_url}])
        self.assertEqual(self.posted[-1]["__ytdlp"], "sniff_ui")
        self.assertEqual(self.posted[-1]["items"][0]["url"], "https://cdn/a.mp4")

    def test_download_only_accepts_tracked_urls(self):
        done = threading.Event()
        calls = []

        def sink(urls, page=""):
            calls.append((urls, page))
            done.set()
            return {"ok": True, "task_ids": [1]}

        self.b._download_sink = sink
        self.b._push_ui([{"url": "https://cdn/a.mp4", "top_url": self.b._page_url}])
        self.b._download_from_ui(["https://cdn/a.mp4", "https://evil.example/x"])
        self.assertTrue(done.wait(2))
        self.assertEqual(calls[0][0], ["https://cdn/a.mp4"])

    def test_download_rejects_unknown_url(self):
        self.b._download_from_ui(["https://evil.example/x"])
        self.assertEqual(self.posted[-1]["__ytdlp"], "sniff_ui_result")
        self.assertFalse(self.posted[-1]["ok"])
        self.assertIn("不在本页", self.posted[-1]["text"])

    def test_web_message_dispatches_dl(self):
        got = []
        self.b._download_from_ui = lambda urls: got.append(urls)

        class Args:
            WebMessageAsJson = '{"__ytdlp":"dl","urls":["https://cdn/a.mp4"]}'
            Source = "https://page.example.com/watch"

        self.b._on_web_message(None, Args())
        self.assertEqual(got, [["https://cdn/a.mp4"]])

    def test_page_download_uses_current_top_url(self):
        done = threading.Event()
        calls = []

        def sink(page):
            calls.append(page)
            done.set()
            return {"ok": True, "task_ids": [7]}

        self.b._page_sink = sink
        self.b._download_page_from_ui()
        self.assertTrue(done.wait(2))
        self.assertEqual(calls, ["https://page.example.com/watch"])
        self.assertTrue(self.posted[-1]["ok"])

    def test_page_download_requires_http_page(self):
        self.b._page_url = "about:blank"
        self.b._download_page_from_ui()
        self.assertFalse(self.posted[-1]["ok"])
        self.assertIn("http(s)", self.posted[-1]["text"])

    def test_page_download_reports_missing_channel(self):
        self.b._page_sink = None
        self.b._download_page_from_ui()
        self.assertFalse(self.posted[-1]["ok"])
        self.assertIn("未接通", self.posted[-1]["text"])

    def test_web_message_dispatches_dlpage(self):
        called = []
        self.b._download_page_from_ui = lambda: called.append(True)

        class Args:
            WebMessageAsJson = '{"__ytdlp":"dlpage"}'
            Source = "https://page.example.com/watch"

        self.b._on_web_message(None, Args())
        self.assertEqual(called, [True])


class TestToolbarDownloadPage(unittest.TestCase):
    """工具栏「下载本页」：地址取 WebView2 当前 Source，回退 _page_url。

    嗅探条目常常只是 DASH 分片，工具栏这个入口负责整段下载。
    """

    def setUp(self):
        self.b = embedded.EmbeddedBrowser()
        self.status = []
        self.b._set_status = (lambda text, bad=False, busy=False:
                              self.status.append((text, bad, busy)))

    def _core_with(self, source):
        class Core:
            Source = source

        class Webv:
            CoreWebView2 = Core()

        self.b._webv = Webv()

    @staticmethod
    def _sink(calls, done, result):
        def sink(page):
            calls.append(page)
            done.set()
            return result
        return sink

    def test_uses_live_source_over_stale_page_url(self):
        done = threading.Event()
        calls = []
        self.b._page_sink = self._sink(calls, done, {"ok": True, "task_ids": [3]})
        self.b._page_url = "https://www.bilibili.com/video/BV1old"
        self._core_with("https://www.bilibili.com/video/BV1new?spm_id_from=333.999")
        self.b.download_page()
        self.assertTrue(done.wait(2))
        self.assertEqual(calls,
                         ["https://www.bilibili.com/video/BV1new?spm_id_from=333.999"])
        self.assertEqual(self.status[0][2], True)          # 先报「正在解析」busy
        self.assertIn("已加入下载队列", self.status[-1][0])
        self.assertFalse(self.status[-1][1])

    def test_falls_back_to_page_url_without_core(self):
        done = threading.Event()
        calls = []
        self.b._page_sink = self._sink(calls, done, {"ok": True, "task_ids": []})
        self.b._page_url = "https://page.example.com/watch"
        self.b.download_page()
        self.assertTrue(done.wait(2))
        self.assertEqual(calls, ["https://page.example.com/watch"])

    def test_rejects_non_http_page(self):
        calls = []
        self.b._page_sink = self._sink(calls, threading.Event(), {"ok": True})
        self._core_with("about:blank")
        self.b.download_page()
        self.assertEqual(calls, [])
        self.assertTrue(self.status[-1][1])
        self.assertIn("http(s)", self.status[-1][0])

    def test_reports_sink_error(self):
        done = threading.Event()
        calls = []
        self.b._page_sink = self._sink(
            calls, done, {"ok": False, "error": "站点不支持"})
        self._core_with("https://www.bilibili.com/video/BV1GJ411x7h7")
        self.b.download_page()
        self.assertTrue(done.wait(2))
        self.assertTrue(self.status[-1][1])
        self.assertIn("站点不支持", self.status[-1][0])

    def test_reports_missing_channel(self):
        self._core_with("https://www.bilibili.com/video/BV1GJ411x7h7")
        self.b.download_page()
        self.assertTrue(self.status[-1][1])
        self.assertIn("未接通", self.status[-1][0])


class _Cookie:
    def __init__(self, name="sid", value="v", domain=".example.com", path="/",
                 secure=True, http_only=True, expires=0, session=False):
        self.Name = name
        self.Value = value
        self.Domain = domain
        self.Path = path
        self.IsSecure = secure
        self.IsHttpOnly = http_only
        self.Expires = expires
        self.IsSession = session


class TestCookies(unittest.TestCase):
    def test_maps_fields_for_cookie_jar(self):
        got = embedded.cookies_from_webview2([_Cookie(expires=1234567890.5)])
        self.assertEqual(got, [{"name": "sid", "value": "v", "domain": ".example.com",
                                "path": "/", "secure": True, "httpOnly": True,
                                "expirationDate": 1234567890.5}])

    def test_session_and_broken_cookies(self):
        got = embedded.cookies_from_webview2([
            _Cookie(expires=-1, session=True),
            _Cookie(name="", value="empty"),
            "junk",
        ])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["expirationDate"], 0)

    def test_datetime_expiry_is_converted(self):
        """新 SDK 的 Expires 是 DateTime（老的是 unix 秒），都要能落成时间戳。"""
        class _When:
            def ToFileTimeUtc(self):
                # 2026-01-01 00:00:00 UTC = 1767225600（.NET 文件时间 = 100ns 计数）
                return int((1767225600 + 11644473600) * 1e7)

        class _NoMethod:
            pass

        got = embedded.cookies_from_webview2([
            _Cookie(expires=_When()),
            _Cookie(expires=_NoMethod()),
        ])
        self.assertEqual(got[0]["expirationDate"], 1767225600)
        self.assertEqual(got[1]["expirationDate"], 0)


class _NamedCookie:
    """记录「字段是在哪个线程上读的」，用来验证读字段确实排在 UI 线程。"""

    Value = "v"
    Domain = ".example.com"
    Path = "/"
    IsSecure = True
    IsHttpOnly = False
    Expires = 0
    IsSession = True

    def __init__(self, name="sid"):
        self._name = name
        self.read_on = []

    @property
    def Name(self):
        self.read_on.append(threading.current_thread().name)
        return self._name


class _FakeCookieTask:
    def __init__(self, cookies, completed=True):
        self.Result = list(cookies)
        self.IsCompleted = completed


class _FakeCookieManager:
    def __init__(self, task):
        self.task = task
        self.calls = []

    def GetCookiesAsync(self, url):
        self.calls.append(url)
        return self.task


class _FakeForm:
    """假 WinForms 窗体：委托交给一个具名线程跑，模拟 UI 线程。"""

    def __init__(self, inline=False, begin_fails=False, thread_name="fake-ui"):
        self.InvokeRequired = not inline
        self.begin_fails = begin_fails
        self.thread_name = thread_name
        self.invoked = 0

    def BeginInvoke(self, action):
        if self.begin_fails:
            raise RuntimeError("BeginInvoke 不可用")
        t = threading.Thread(target=action, name=self.thread_name)
        t.start()
        t.join(timeout=2.0)

    def Invoke(self, action):
        self.invoked += 1
        action()


class _SystemActionStub:
    """测试进程里没有 pythonnet：给 `from System import Action` 一个替身。

    `_ui` / `_post_ui` 都靠 `System.Action` 编组委托，没它就整段被吞掉。
    """

    def setUp(self):
        super().setUp()
        self._saved_system = sys.modules.get("System", None)
        stub = type(sys)("System")
        stub.Action = lambda fn: fn
        sys.modules["System"] = stub

    def tearDown(self):
        if self._saved_system is None:
            sys.modules.pop("System", None)
        else:
            sys.modules["System"] = self._saved_system
        super().tearDown()


class TestExportCookies(_SystemActionStub, unittest.TestCase):
    """export_cookies：内核对象（cookie 字段）只能在 UI 线程读。

    从 worker 线程读 CoreWebView2Cookie 的字段会抛 NotImplementedException
    （COM 接口转换失败），把整罐静默丢光；个别时候还会把窗口挂死。
    """

    def setUp(self):
        super().setUp()
        self.b = embedded.EmbeddedBrowser()
        self.cookie = _NamedCookie()
        self.mgr = _FakeCookieManager(_FakeCookieTask([self.cookie]))

        class _Core:
            def __init__(self, mgr):
                self.CookieManager = mgr

        class _Webv:
            def __init__(self, core):
                self.CoreWebView2 = core

        self.b._webv = _Webv(_Core(self.mgr))
        self.b._form = _FakeForm()
        self.b._core_ready = True

    def test_reads_fields_on_ui_thread(self):
        got = self.b.export_cookies("https://www.douyin.com/video/1", timeout=2.0)
        self.assertEqual([c["name"] for c in got], ["sid"])
        self.assertEqual(got[0]["domain"], ".example.com")
        self.assertEqual(self.mgr.calls, ["https://www.douyin.com/video/1"])
        self.assertEqual(self.cookie.read_on, ["fake-ui"])

    def test_refused_when_view_not_ready(self):
        self.b._core_ready = False
        self.assertEqual(self.b.export_cookies("https://a/"), [])
        self.b._core_ready = True
        self.b._form = None
        self.assertEqual(self.b.export_cookies("https://a/"), [])
        self.b._form = _FakeForm()
        self.b._webv = None
        self.assertEqual(self.b.export_cookies("https://a/"), [])

    def test_only_http_urls(self):
        self.assertEqual(self.b.export_cookies("about:blank"), [])
        self.assertEqual(self.mgr.calls, [])

    def test_inline_ui_thread_refuses_to_wait(self):
        """已经在 UI 线程上时不能等结果，直接返回空（旧行为保持）。"""
        self.b._form = _FakeForm(inline=True)
        self.assertEqual(self.b.export_cookies("https://a/"), [])

    def test_timeout_returns_empty_and_is_bounded(self):
        self.mgr.task = _FakeCookieTask([self.cookie], completed=False)
        t0 = time.time()
        got = self.b.export_cookies("https://a/", timeout=0.2)
        self.assertEqual(got, [])
        self.assertLess(time.time() - t0, 3.0)

    def test_channel_failure_returns_empty(self):
        self.b._post_ui = lambda fn: False
        self.assertEqual(self.b.export_cookies("https://a/"), [])

    def test_begin_invoke_failure_falls_back_to_invoke(self):
        self.b._form = _FakeForm(begin_fails=True)
        got = self.b.export_cookies("https://a/", timeout=2.0)
        self.assertEqual([c["name"] for c in got], ["sid"])
        self.assertEqual(self.b._form.invoked, 2)      # kick + convert 各一次


class TestPostUi(_SystemActionStub, unittest.TestCase):
    """_post_ui：投递到 UI 线程执行、立刻返回；不可用时退回同步 Invoke。"""

    def setUp(self):
        super().setUp()
        self.b = embedded.EmbeddedBrowser()

    def test_no_form_is_false(self):
        self.assertFalse(self.b._post_ui(lambda: None))

    def test_runs_inline_when_already_on_ui_thread(self):
        self.b._form = _FakeForm(inline=True)
        seen = []
        self.assertTrue(self.b._post_ui(lambda: seen.append(1)))
        self.assertEqual(seen, [1])

    def test_posts_with_begin_invoke(self):
        self.b._form = _FakeForm()
        seen = []
        self.assertTrue(self.b._post_ui(lambda: seen.append(threading.current_thread().name)))
        self.assertEqual(seen, ["fake-ui"])

    def test_falls_back_to_invoke(self):
        self.b._form = _FakeForm(begin_fails=True)
        seen = []
        self.assertTrue(self.b._post_ui(lambda: seen.append(1)))
        self.assertEqual(seen, [1])


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

    def test_sink_uses_local_registry_without_service(self):
        """不启用扩展服务也要能登记：直接加载同一份服务端登记表。"""
        self.host._web_mod = None
        self.host.sniff_clear()
        items = [{"url": "https://cdn/a.mp4", "entry": "embedded"}]
        self.assertTrue(self.host.register_embedded_sniff(items, "https://page"))
        d = self.host.sniff_items()
        self.assertFalse(d["svc"])
        self.assertIn("https://cdn/a.mp4", [it["url"] for it in d["items"]])

    def test_download_embedded_sniff_filters_registry(self):
        class FakeApi:
            def sniff_items_json(self):
                return {"items": [{"url": "https://cdn/a.mp4", "entry": "embedded"}]}

        self.host._sniff_api = lambda: FakeApi()
        calls = []
        self.host.sniff_download = (lambda items:
                                    calls.append(items) or {"ok": True, "task_ids": [1]})
        r = self.host.download_embedded_sniff(
            ["https://cdn/a.mp4", "https://evil.example/x"], "https://page")
        self.assertTrue(r["ok"])
        self.assertEqual([it["url"] for it in calls[0]], ["https://cdn/a.mp4"])

    def test_download_embedded_sniff_rejects_unknown(self):
        class FakeApi:
            def sniff_items_json(self):
                return {"items": []}

        self.host._sniff_api = lambda: FakeApi()
        r = self.host.download_embedded_sniff(["https://evil.example/x"])
        self.assertFalse(r["ok"])
        self.assertIn("不在本机嗅探列表", r["error"])

    def test_download_embedded_page_builds_merge_task(self):
        captured = []
        self.host.cfg["outdir"] = self.tmp.name
        self.host._embedded_cookie_file = lambda item: ""
        self.host.queue.submit = lambda spec, execfn: (
            captured.append(spec) or 7)
        r = self.host.download_embedded_page(
            "https://www.bilibili.com/video/BV1xn1Jz8EuW/")
        self.assertTrue(r["ok"])
        self.assertEqual(r["task_ids"], [7])
        spec = captured[0]
        self.assertEqual(spec["source"], "embedded-page")
        self.assertEqual(spec["cfg"]["fmt"], "bv*+ba/b")
        self.assertEqual(spec["cfg"]["mode"], "best")
        self.assertEqual(spec["cfg"]["referer"],
                         "https://www.bilibili.com/video/BV1xn1Jz8EuW/")
        self.assertTrue(spec["no_playlist"])

    def test_download_embedded_page_rejects_bad_url(self):
        self.host.cfg["outdir"] = self.tmp.name
        r = self.host.download_embedded_page("javascript:alert(1)")
        self.assertFalse(r["ok"])
        self.assertIn("无效", r["error"])

    def test_embedded_cookie_jar_falls_back_to_page(self):
        calls = []
        written = []

        class FakeEmbedded:
            def export_cookies(self, url, timeout=2.0):
                calls.append(url)
                return [] if "cdn" in url else [
                    {"name": "sid", "value": "1", "domain": ".page.example.com"}]

        class FakeWeb:
            def write_cookie_jar(self, jar, url):
                written.append((jar, url))
                return "C:/temp/jar.txt"

        self.host.embedded = FakeEmbedded()
        self.host._web_mod = FakeWeb()
        path = self.host._embedded_cookie_file({
            "url": "https://cdn.example.com/a.mp4",
            "page_url": "https://page.example.com/watch"})
        self.assertEqual(path, "C:/temp/jar.txt")
        self.assertEqual(calls, ["https://cdn.example.com/a.mp4",
                                 "https://page.example.com/watch"])
        self.assertEqual(written[0][1], "https://cdn.example.com/a.mp4")

    def test_no_cookies_keeps_old_cookie_path(self):
        class FakeEmbedded:
            def export_cookies(self, url, timeout=2.0):
                return []

        class FakeWeb:
            def write_cookie_jar(self, jar, url):
                return None

        self.host.embedded = FakeEmbedded()
        self.host._web_mod = FakeWeb()
        self.assertEqual(self.host._embedded_cookie_file(
            {"url": "https://cdn.example.com/a.mp4"}), "")

    def test_drop_cookie_file_removes_temp(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            path = f.name
        self.host._drop_cookie_file(path)
        self.assertFalse(os.path.exists(path))


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


class TestNewWindowPolicy(unittest.TestCase):
    """target=_blank / window.open 必须留在内嵌视图里，不能踢到系统浏览器。"""

    def setUp(self):
        self.b = embedded.EmbeddedBrowser()
        self.opened = []
        self.status = []
        self.b.navigate = lambda target: self.opened.append(target)
        self.b._set_status = (lambda text, bad=False, busy=False:
                              self.status.append((text, bad)))

    @staticmethod
    def _args(uri):
        class Args:
            pass
        a = Args()
        a.Handled = False
        a.Uri = uri
        return a

    def test_http_link_navigates_in_place(self):
        a = self._args("https://example.com/watch?v=1")
        self.b._on_new_window(None, a)
        self.assertTrue(a.Handled)
        self.assertEqual(self.opened, ["https://example.com/watch?v=1"])
        self.assertEqual(self.status, [])

    def test_blank_window_is_ignored(self):
        a = self._args("about:blank")
        self.b._on_new_window(None, a)
        self.assertTrue(a.Handled)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.status, [])

    def test_non_http_window_is_blocked(self):
        for uri in ("javascript:alert(1)", "file:///C:/Windows/win.ini",
                    "data:text/html,hi", ""):
            a = self._args(uri)
            self.b._on_new_window(None, a)
            self.assertTrue(a.Handled)
        self.assertEqual(self.opened, [])
        self.assertTrue(any(bad for _, bad in self.status))


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