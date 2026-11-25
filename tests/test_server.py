"""Web 服务端（YtDlpWeb/server.py）与并发/校验相关的回归用例。

server.py 不是包，用 importlib 按文件路径载入；载入本身没有副作用（没有起服务）。
"""
import importlib.util
import json
import os
import sys
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


class TestServiceLifecycle(unittest.TestCase):
    """桌面版内嵌服务靠的就是这两个入口：start_service() 启动、Service.shutdown() 收摊。"""

    def test_start_serves_status_and_shutdown_releases_port(self):
        import json
        import socket
        import urllib.request

        try:
            svc = server.start_service()
        except OSError as e:
            self.skipTest("端口都被占用：%s" % e)
        try:
            body = json.loads(urllib.request.urlopen(svc.url + "/api/status", timeout=5).read())
            self.assertEqual(body["app"], "yt-dlp-web")
            self.assertIn("busy", body)
            port = svc.port
        finally:
            svc.shutdown()
        # 关掉之后端口必须放出来，否则下次启动会被自己的残影挡住
        with socket.socket() as sock:
            sock.settimeout(1.0)
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)

    def test_shutdown_is_idempotent(self):
        try:
            svc = server.start_service()
        except OSError as e:
            self.skipTest("端口都被占用：%s" % e)
        svc.shutdown()
        svc.shutdown()

    def test_set_config_file_points_both_readers_at_one_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "sub", "config.json")
            old = server.CONFIG_FILE
            try:
                server.set_config_file(target)
                self.assertEqual(server.CONFIG_FILE, target)
                self.assertTrue(os.path.isdir(os.path.dirname(target)))
            finally:
                server.set_config_file(old)


    def test_status_still_answers_without_stderr(self):
        """pythonw 启动时 sys.stderr 是 None：写日志失败绝不能把响应变成空回复。

        这是真实踩过的坑——扩展报「找不到服务」，浏览器访问是 ERR_EMPTY_RESPONSE。
        """
        import json
        import urllib.request

        saved = sys.stderr
        try:
            sys.stderr = None
            try:
                svc = server.start_service()
            except OSError as e:
                self.skipTest("端口都被占用：%s" % e)
            try:
                body = json.loads(urllib.request.urlopen(svc.url + "/api/status", timeout=5).read())
                self.assertEqual(body["app"], "yt-dlp-web")
            finally:
                svc.shutdown()
        finally:
            sys.stderr = saved


class TestPortProbe(unittest.TestCase):
    """Windows 上 SO_REUSEADDR 允许绑定别人已在监听的端口，先探一下才不会撞车。"""

    def _listen(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_listening_port_is_reported_as_taken(self):
        self.assertTrue(server._port_taken(self._listen()))

    def test_free_port_is_not_reported_as_taken(self):
        port = self._listen()
        self.assertFalse(server._port_taken(port + 1000))

    def test_pick_port_refuses_the_port_someone_already_listens_on(self):
        port = self._listen()
        p, httpd, skipped = server.pick_port(port)
        self.assertIsNone(p)
        self.assertIsNone(httpd)
        self.assertEqual(skipped, [port])


class TestTaskSink(unittest.TestCase):
    """桌面版是宿主时，下载交还桌面版执行：服务端只接请求、镜像状态。"""

    def setUp(self):
        import json
        import urllib.request
        import urllib.error
        self.json = json
        self.req = urllib.request
        self.err = urllib.error
        try:
            self.svc = server.start_service(port=8899)
        except OSError as e:
            self.skipTest("端口 8899 被占用：%s" % e)
        self.addCleanup(self.svc.shutdown)
        self.addCleanup(server.set_task_sink, None, None)
        self.addCleanup(server.RUNNER.finish)

    def _post(self, path, body):
        req = self.req.Request(self.svc.url + path, data=self.json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.req.urlopen(req, timeout=5) as r:
                return r.status, self.json.loads(r.read())
        except self.err.HTTPError as e:
            return e.code, self.json.loads(e.read())

    def test_sniff_task_is_handed_to_sink_and_state_mirrored(self):
        got = {}

        def sink(task):
            got.update(task)
            return server.RUNNER.reserve("download")

        server.set_task_sink(sink)
        st, j = self._post("/api/sniff", {"url": "https://example.com/v.mp4",
                                          "kind": "media", "title": "T"})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(got["url"], "https://example.com/v.mp4")
        self.assertEqual(got["cfg"]["fmt"], "b")
        self.assertTrue(got["cfg"]["play_while"])
        self.assertFalse(got["cfg"]["thumb"])
        # 桌面版占住槽位，服务端 busy 跟着一致
        self.assertTrue(server.RUNNER.busy())
        got["emit"]({"type": "log", "text": "桌面版日志"})
        got["emit"]({"type": "progress", "pct": 42.0, "speed": "1MiB/s"})
        got["emit"]({"type": "done", "code": 0})
        server.RUNNER.finish()
        d = server.sniff_log_json()
        self.assertTrue(d["done"])
        self.assertEqual(d["code"], 0)
        self.assertIn("桌面版日志", d["lines"])
        self.assertEqual(d["prog"]["pct"], 42.0)
        self.assertEqual(d["title"], "T")

    def test_sink_busy_gives_409_and_restores_done(self):
        server.RUNNER.reserve("download")
        server.set_task_sink(lambda task: False)
        st, j = self._post("/api/sniff", {"url": "https://example.com/v.mp4"})
        self.assertEqual(st, 409)
        self.assertTrue(j["busy"])
        self.assertTrue(server.SNIFF_STATE["done"])

    def test_stop_goes_through_hook(self):
        calls = []
        server.set_task_sink(lambda task: True, stop=lambda: calls.append(1) or True)
        st, j = self._post("/api/stop", {})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()

class TestSniffClear(unittest.TestCase):
    def test_clear_empties_list(self):
        server.SNIFF_ITEMS.clear()
        server.register_sniff_items([{"url": "https://cdn.example.com/a.mp4", "kind": "mp4"}])
        self.assertEqual(len(server.sniff_items_json()["items"]), 1)
        server.clear_sniff_items()
        self.assertEqual(server.sniff_items_json()["items"], [])


class TestUiRoutesRetired(unittest.TestCase):
    def test_web_ui_and_ui_only_apis_are_gone(self):
        import urllib.error
        import urllib.request
        svc = server.start_service(port=8896)
        try:
            def code(method, path, body=None):
                req = urllib.request.Request(
                    svc.url + path, method=method,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code
            for method, path, body in (("GET", "/", None), ("GET", "/index.html", None),
                                       ("GET", "/api/config", None),
                                       ("GET", "/api/thumb?id=x", None),
                                       ("POST", "/api/config", {}),
                                       ("POST", "/api/detect", {"urls": []}),
                                       ("POST", "/api/download", {"urls": []}),
                                       ("POST", "/api/open", {})):
                self.assertEqual(code(method, path, body), 404, path)
            self.assertEqual(code("GET", "/api/status"), 200)
        finally:
            svc.shutdown()
