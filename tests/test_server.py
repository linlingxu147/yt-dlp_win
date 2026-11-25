"""Web 服务端（YtDlpWeb/server.py）与并发/校验相关的回归用例。

server.py 不是包，用 importlib 按文件路径载入；载入本身没有副作用（没有起服务）。
"""
import importlib.util
import os
import threading
import time
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(os.path.dirname(_HERE), "YtDlpWeb", "server.py")
_spec = importlib.util.spec_from_file_location("ytdlp_web_server", _PATH)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


class TestValidateConfig(unittest.TestCase):
    def test_unc_and_device_paths_rejected(self):
        for v in ("\\\\server\\share", "//server/share"):
            self.assertTrue(server.validate_config_value("outdir", v), v)

    def test_template_cannot_escape_outdir(self):
        for v in ("../x", "..\\x", "C:\\x\\%(title)s.%(ext)s", "\\\\srv\\x"):
            self.assertTrue(server.validate_config_value("tpl", v), v)
        self.assertIsNone(server.validate_config_value("tpl", "%(title)s.%(ext)s"))
        self.assertIsNone(server.validate_config_value("outdir", "D:\\media\\dl"))
        self.assertIsNone(server.validate_config_value("outdir", "downloads"))


class TestRunnerReserve(unittest.TestCase):
    def test_only_one_of_many_concurrent_requests_wins(self):
        runner = server.Runner()
        wins = []

        def grab():
            if runner.reserve("download"):
                wins.append(1)

        threads = [threading.Thread(target=grab) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(runner.kind, "download")
        runner.finish()
        self.assertIsNone(runner.kind)

    def test_launch_adopts_own_placeholder_and_rejects_foreign_one(self):
        runner = server.Runner()
        self.assertTrue(runner.reserve("detect"))
        self.assertFalse(runner.launch(["x"], None, "download", lambda e: None, adopt=True))
        # 自己占的位可以被兑现（用假 proc，只为验证闸门逻辑）
        class _P:
            pid = 1
        with mock.patch.object(server, "spawn", return_value=_P()):
            self.assertTrue(runner.launch(["x"], None, "detect", lambda e: None, adopt=True))
        runner.finish()

    def test_failed_spawn_releases_slot(self):
        runner = server.Runner()
        self.assertTrue(runner.reserve("download"))
        with mock.patch.object(server, "spawn", side_effect=OSError("boom")):
            try:
                runner.launch(["x"], None, "download", None, adopt=True)
            except Exception:
                pass
        self.assertIsNone(runner.kind)


class TestSniffTokenIsolation(unittest.TestCase):
    def test_stale_task_output_is_ignored(self):
        server.SNIFF_STATE.update({"token": 7, "lines": [], "prog": None, "done": False})
        server.sniff_emit({"type": "log", "text": "old"}, token=6)
        self.assertEqual(server.SNIFF_STATE["lines"], [])
        server.sniff_emit({"type": "log", "text": "new"}, token=7)
        self.assertEqual(server.SNIFF_STATE["lines"], ["new"])

    def test_untokened_write_still_allowed(self):
        server.SNIFF_STATE.update({"token": 7, "lines": [], "prog": None, "done": False})
        server.sniff_emit({"type": "log", "text": "x"})
        self.assertEqual(server.SNIFF_STATE["lines"], ["x"])


class TestThumbTokenTtl(unittest.TestCase):
    def test_expired_token_is_dropped(self):
        server.THUMB_TOKENS.clear()
        token = server.register_thumb("https://cdn.example/a.jpg")
        self.assertIsNotNone(server.thumb_url_for(token))
        url, ts = server.THUMB_TOKENS[token]
        server.THUMB_TOKENS[token] = (url, ts - server.THUMB_TOKEN_TTL - 1)
        self.assertIsNone(server.thumb_url_for(token))
        self.assertNotIn(token, server.THUMB_TOKENS)


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


class TestSniffItems(unittest.TestCase):
    def setUp(self):
        server.SNIFF_ITEMS.clear()
        server.PREVIEW_TOKENS.clear()

    def tearDown(self):
        server.SNIFF_ITEMS.clear()
        server.PREVIEW_TOKENS.clear()

    def test_non_http_and_garbage_are_dropped(self):
        kept = server.register_sniff_items([
            {"url": "ftp://cdn/a.mp4"}, {"url": ""}, "junk", {}, None,
            {"url": "https://cdn/a.mp4"},
        ])
        self.assertEqual(kept, 1)
        self.assertIn("https://cdn/a.mp4", server.SNIFF_ITEMS)

    def test_same_url_updates_instead_of_duplicating(self):
        server.register_sniff_items([{"url": "https://cdn/a.mp4", "size": 10}])
        first_ts = server.SNIFF_ITEMS["https://cdn/a.mp4"]["first_ts"]
        server.register_sniff_items([{"url": "https://cdn/a.mp4", "size": 99}])
        self.assertEqual(len(server.SNIFF_ITEMS), 1)
        rec = server.SNIFF_ITEMS["https://cdn/a.mp4"]
        self.assertEqual(rec["size"], 99)
        self.assertEqual(rec["first_ts"], first_ts)   # 首次出现时间不该被覆盖

    def test_capacity_is_capped(self):
        for i in range(server.SNIFF_MAX_ITEMS + 5):
            server.register_sniff_items([{"url": "https://cdn/%d.mp4" % i}])
        self.assertEqual(len(server.SNIFF_ITEMS), server.SNIFF_MAX_ITEMS)

    def test_items_are_newest_first(self):
        server.register_sniff_items([{"url": "https://cdn/a.mp4"}])
        time.sleep(0.01)
        server.register_sniff_items([{"url": "https://cdn/b.mp4"}])
        items = server.sniff_items_json()["items"]
        self.assertEqual(items[0]["url"], "https://cdn/b.mp4")

    def test_segment_and_manifest_flags_follow_core_rules(self):
        """网页版靠这两个字段把 HLS 分片折叠掉，判定口径必须和核心一致。"""
        server.register_sniff_items([
            {"url": "https://cdn/a.m3u8"}, {"url": "https://cdn/a-1.ts"},
            {"url": "https://cdn/a.mp4"},
        ])
        got = {it["url"]: it for it in server.sniff_items_json()["items"]}
        self.assertTrue(got["https://cdn/a.m3u8"]["manifest"])
        self.assertFalse(got["https://cdn/a.m3u8"]["seg"])
        self.assertTrue(got["https://cdn/a-1.ts"]["seg"])
        self.assertFalse(got["https://cdn/a.mp4"]["seg"])
        self.assertFalse(got["https://cdn/a.mp4"]["manifest"])


class TestPreviewTokens(unittest.TestCase):
    def setUp(self):
        server.PREVIEW_TOKENS.clear()

    def tearDown(self):
        server.PREVIEW_TOKENS.clear()

    def test_preview_path_is_stable_and_kind_specific(self):
        p1 = server._preview_path("https://cdn/a.m3u8", "thumb")
        p2 = server._preview_path("https://cdn/a.m3u8", "thumb")
        c = server._preview_path("https://cdn/a.m3u8", "clip")
        self.assertEqual(p1, p2)
        self.assertTrue(p1.endswith(".png"))
        self.assertTrue(c.endswith(".mp4"))
        self.assertNotEqual(p1, c)
        self.assertTrue(os.path.isdir(server.PREVIEW_DIR))

    def test_wrong_kind_is_rejected(self):
        token = server._preview_token("C:/tmp/x.png", "thumb")
        self.assertIsNone(server.preview_token_lookup(token, "clip")[0])
        self.assertEqual(server.preview_token_lookup(token, "thumb")[0], "C:/tmp/x.png")

    def test_unknown_token_is_none(self):
        self.assertIsNone(server.preview_token_lookup("nope")[0])

    def test_expired_token_is_dropped(self):
        token = server._preview_token("C:/tmp/x.png", "thumb")
        path, kind, ts = server.PREVIEW_TOKENS[token]
        server.PREVIEW_TOKENS[token] = (path, kind, ts - server.PREVIEW_TOKEN_TTL - 1)
        self.assertIsNone(server.preview_token_lookup(token)[0])
        self.assertNotIn(token, server.PREVIEW_TOKENS)


class TestBuildSniffPreview(unittest.TestCase):
    def setUp(self):
        server.SNIFF_ITEMS.clear()
        server.PREVIEW_TOKENS.clear()
        self._urls = []

    def tearDown(self):
        for url in self._urls:
            for kind in ("thumb", "clip"):
                p = server._preview_path(url, kind)
                try:
                    os.remove(p)
                except OSError:
                    pass
        server.SNIFF_ITEMS.clear()
        server.PREVIEW_TOKENS.clear()

    def _url(self, ext):
        url = "https://cdn.example/%s.%s" % (uuid.uuid4().hex, ext)
        self._urls.append(url)
        return url

    def test_unregistered_url_is_refused(self):
        r = server.build_sniff_preview(self._url("m3u8"))
        self.assertFalse(r["ok"])
        self.assertIn("不在本机嗅探列表", r["error"])

    def test_native_mp4_gets_thumb_and_duration_without_clip(self):
        url = self._url("mp4")
        server.register_sniff_items([{"url": url, "referer": "https://site/watch"}])
        seen = {}

        def fake_dur(u, cfg=None, timeout=20):
            seen["dur_cfg"] = cfg
            return 65.5

        def fake_frame(u, cfg, out, at=1.0, timeout=None):
            seen["frame"] = (u, cfg, at)
            _write(out, b"\x89PNG\r\n\x1a\n")
            return True

        def fake_clip(*a, **k):
            seen["clip"] = True
            return False

        with mock.patch.object(server.core, "stream_duration", fake_dur), \
             mock.patch.object(server.core, "grab_stream_frame", fake_frame), \
             mock.patch.object(server.core, "grab_stream_clip", fake_clip):
            r = server.build_sniff_preview(url)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["native"])
        self.assertAlmostEqual(r["duration"], 65.5)
        self.assertTrue(r["thumb"])
        self.assertEqual(r["clip"], "")
        self.assertNotIn("clip", seen)
        # 预览请求带上了条目里的 Referer（防盗链靠它）
        self.assertEqual(seen["dur_cfg"].get("referer"), "https://site/watch")

    def test_hls_clip_is_grabbed_and_framed_locally(self):
        url = self._url("m3u8")
        server.register_sniff_items([{"url": url}])
        order = []

        def fake_clip(u, cfg, out, seconds=6, timeout=None):
            order.append(("clip", u))
            _write(out, b"xxxx")
            return True

        def fake_frame(u, cfg, out, at=1.0, timeout=None):
            order.append(("frame", u))
            _write(out, b"\x89PNG")
            return True

        with mock.patch.object(server.core, "stream_duration", lambda *a, **k: 120.0), \
             mock.patch.object(server.core, "grab_stream_clip", fake_clip), \
             mock.patch.object(server.core, "grab_stream_frame", fake_frame):
            r = server.build_sniff_preview(url)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["native"])
        self.assertTrue(r["clip"])
        self.assertTrue(r["thumb"])
        # 截帧读的是本地片段，网络只走一遍
        frame_src = [u for kind, u in order if kind == "frame"][0]
        self.assertNotEqual(frame_src, url)
        self.assertTrue(frame_src.endswith(".mp4"))

    def test_nothing_grabbed_is_reported_as_failure(self):
        url = self._url("m3u8")
        server.register_sniff_items([{"url": url}])
        with mock.patch.object(server.core, "stream_duration", lambda *a, **k: None), \
             mock.patch.object(server.core, "grab_stream_clip", lambda *a, **k: False), \
             mock.patch.object(server.core, "grab_stream_frame", lambda *a, **k: False):
            r = server.build_sniff_preview(url)
        self.assertFalse(r["ok"])
        self.assertIn("抓不到画面", r["error"])


if __name__ == "__main__":
    unittest.main()