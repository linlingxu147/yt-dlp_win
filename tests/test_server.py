"""Web 服务端（YtDlpWeb/server.py）与并发/校验相关的回归用例。

server.py 不是包，用 importlib 按文件路径载入；载入本身没有副作用（没有起服务）。
"""
import importlib.util
import json
import os
import sys
import time
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(os.path.dirname(_HERE), "YtDlpWeb", "server.py")
_spec = importlib.util.spec_from_file_location("ytdlp_web_server", _PATH)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


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

    def test_audio_flag_follows_core_rules(self):
        server.register_sniff_items([
            {"url": "https://x/a-1-30280.m4s"},
            {"url": "https://x/a-1-100026.m4s"},
            {"url": "https://x/a.mp3"},
        ])
        got = {it["url"]: it for it in server.sniff_items_json()["items"]}
        self.assertTrue(got["https://x/a-1-30280.m4s"]["audio"])
        self.assertFalse(got["https://x/a-1-100026.m4s"]["audio"])
        self.assertTrue(got["https://x/a.mp3"]["audio"])

    def test_entry_defaults_to_extension_and_explicit_embedded_kept(self):
        server.register_sniff_items([{"url": "https://cdn/old.mp4"}])
        self.assertEqual(server.SNIFF_ITEMS["https://cdn/old.mp4"]["entry"], "extension")
        server.register_sniff_items([{"url": "https://cdn/new.mp4", "entry": "embedded"}])
        self.assertEqual(server.SNIFF_ITEMS["https://cdn/new.mp4"]["entry"], "embedded")

    def test_entry_keeps_first_source_when_both_report_same_url(self):
        server.register_sniff_items([{"url": "https://cdn/same.mp4", "entry": "embedded"}])
        server.register_sniff_items([{"url": "https://cdn/same.mp4", "entry": "extension"}])
        self.assertEqual(len(server.SNIFF_ITEMS), 1)
        self.assertEqual(server.SNIFF_ITEMS["https://cdn/same.mp4"]["entry"], "embedded")

    def test_cat_inference_and_client_override(self):
        server.register_sniff_items([
            {"url": "https://cdn/a.m3u8", "mime": "application/vnd.apple.mpegurl"},
            {"url": "https://cdn/a-1.ts", "mime": "video/mp2t"},
            {"url": "https://cdn/a.mp4", "mime": "video/mp4"},
            {"url": "https://cdn/a.mp3", "mime": "audio/mpeg"},
            {"url": "https://cdn/a.png", "mime": "image/png"},
            {"url": "https://cdn/page", "mime": "text/html"},
            {"url": "https://cdn/x.bin", "cat": "weird", "mime": "video/mp4"},
            {"url": "https://cdn/y.bin", "cat": "audio", "mime": "text/plain"},
        ])
        got = {it["url"]: it for it in server.sniff_items_json()["items"]}
        self.assertEqual(got["https://cdn/a.m3u8"]["cat"], "stream")
        self.assertEqual(got["https://cdn/a-1.ts"]["cat"], "stream")
        self.assertEqual(got["https://cdn/a.mp4"]["cat"], "video")
        self.assertEqual(got["https://cdn/a.mp3"]["cat"], "audio")
        self.assertEqual(got["https://cdn/a.png"]["cat"], "image")
        self.assertEqual(got["https://cdn/page"]["cat"], "other")
        # 非法 cat 回退推断；合法 cat 原样保留（供新客户端/图片补扫使用）
        self.assertEqual(got["https://cdn/x.bin"]["cat"], "video")
        self.assertEqual(got["https://cdn/y.bin"]["cat"], "audio")

    def test_mime_is_preserved_and_kind_stays_truncated(self):
        mime = "video/mp4; codecs=\"avc1.640028, mp4a.40.2\"; profile=high"
        server.register_sniff_items([{"url": "https://cdn/long.mp4", "kind": mime, "mime": mime}])
        rec = server.SNIFF_ITEMS["https://cdn/long.mp4"]
        self.assertEqual(rec["mime"], mime)
        self.assertEqual(rec["kind"], mime[:16])


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
            svc = server.start_service(port=8799)
        except OSError as e:
            self.skipTest("端口 8799 被占用：%s" % e)
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
            svc = server.start_service(port=8799)
        except OSError as e:
            self.skipTest("端口 8799 被占用：%s" % e)
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
                svc = server.start_service(port=8799)
            except OSError as e:
                self.skipTest("端口 8799 被占用：%s" % e)
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
        server.LAST_EXT_TASK["id"] = None
        with server.SNIFF_LOCK:
            server.SNIFF_STATE.update({"lines": [], "prog": None, "done": True,
                                       "code": None, "token": 0})

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
            return 101          # 桌面宿主返回 task_id（truthy 兼容旧 bool）

        server.set_task_sink(sink)
        st, j = self._post("/api/sniff", {"url": "https://example.com/v.mp4",
                                          "kind": "media", "title": "T"})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(j["task_id"], 101)
        self.assertIn("queue_position", j)
        self.assertEqual(got["url"], "https://example.com/v.mp4")
        self.assertEqual(got["cfg"]["fmt"], "b")
        self.assertFalse(got["cfg"]["thumb"])
        # 镜像 token 对齐 sink 返回的 task_id
        self.assertEqual(server.SNIFF_STATE["token"], 101)
        self.assertEqual(server.LAST_EXT_TASK["id"], 101)
        got["emit"]({"type": "log", "text": "桌面版日志"})
        got["emit"]({"type": "progress", "pct": 42.0, "speed": "1MiB/s"})
        got["emit"]({"type": "done", "code": 0})
        d = server.sniff_log_json()
        self.assertTrue(d["done"])
        self.assertEqual(d["code"], 0)
        self.assertEqual(d["task_id"], 101)
        self.assertIn("桌面版日志", d["lines"])
        self.assertEqual(d["prog"]["pct"], 42.0)
        self.assertEqual(d["title"], "T")

    def test_sink_busy_gives_409_and_restores_done(self):
        server.set_task_sink(lambda task: None)
        st, j = self._post("/api/sniff", {"url": "https://example.com/v.mp4"})
        self.assertEqual(st, 409)
        self.assertTrue(j["busy"])
        self.assertTrue(server.SNIFF_STATE["done"])

    def test_page_submit_carries_cookies_and_keeps_site_settings(self):
        got = {}

        def sink(task):
            got.update(task)
            return 102

        server.set_task_sink(sink)
        base_cfg = {"fmt": "", "thumb": True, "meta": True,
                    "mode": "best"}
        jar = [{"domain": ".douyin.com", "name": "ttwid", "value": "x",
                "path": "/", "secure": True}]
        with mock.patch.object(server, "load_config", return_value=dict(base_cfg)), \
             mock.patch.object(server, "write_cookie_jar",
                               return_value="C:/tmp/douyin_cookies.txt") as w:
            st, j = self._post("/api/sniff", {
                "url": "https://www.douyin.com/video/1", "kind": "page",
                "referer": "https://www.douyin.com/video/1", "ua": "UA",
                "cookie_jar": jar,
            })
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(got["kind"], "page")
        self.assertEqual(got["cfg"]["cookie"], "C:/tmp/douyin_cookies.txt")
        self.assertEqual(got["cfg"]["browser"], "（不使用）")
        self.assertEqual(got["cfg"]["referer"], "https://www.douyin.com/video/1")
        self.assertTrue(got["cfg"]["_referer_explicit"])
        self.assertEqual(got["cfg"]["ua"], "UA")
        # 下载本页走站点提取器：不能强制 fmt=b，也不能关掉封面元数据
        self.assertNotEqual(got["cfg"].get("fmt"), "b")
        self.assertTrue(got["cfg"].get("thumb"))
        w.assert_called_once_with(jar, "https://www.douyin.com/video/1")

    def test_stop_goes_through_hook(self):
        calls = []
        server.set_task_sink(lambda task: True, stop=lambda: calls.append(1) or True)
        st, j = self._post("/api/stop", {})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(calls, [1])

    def test_douyin_modal_url_is_normalized_for_page_task(self):
        got = {}

        def sink(task):
            got.update(task)
            return True

        server.set_task_sink(sink)
        st, j = self._post("/api/sniff", {
            "url": "https://www.douyin.com/jingxuan?modal_id=7679426956026187035",
            "kind": "page",
        })
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(got["url"],
                         "https://www.douyin.com/video/7679426956026187035")


if __name__ == "__main__":
    unittest.main()

class TestSniffClear(unittest.TestCase):
    def test_clear_empties_list(self):
        server.SNIFF_ITEMS.clear()
        server.register_sniff_items([{"url": "https://cdn.example.com/a.mp4", "kind": "mp4"}])
        self.assertEqual(len(server.sniff_items_json()["items"]), 1)
        server.clear_sniff_items()
        self.assertEqual(server.sniff_items_json()["items"], [])


class TestServerQueueFields(unittest.TestCase):
    """独立模式队列语义：接口字段只增不删、第二个任务被接收、停止只作用于指定任务。

    端口用 8799，避开 8765-8775（可能有正在使用的实例）。
    """

    def setUp(self):
        import urllib.error
        import urllib.request
        self.req = urllib.request
        self.err = urllib.error
        try:
            self.svc = server.start_service(port=8799)
        except OSError as e:
            self.skipTest("端口 8799 被占用：%s" % e)
        self.addCleanup(self.svc.shutdown)
        self.addCleanup(self._reset_queue)
        server.TASK_SINK = None
        server.STOP_HOOK = None
        server.QUEUE_STATUS = None
        server.QUEUE.set_max(2)
        self._reset_queue()

    def _reset_queue(self):
        server.QUEUE.stop_all()
        end = time.time() + 5
        while server.QUEUE.any_active() and time.time() < end:
            time.sleep(0.02)
        server.LAST_EXT_TASK["id"] = None
        with server.SNIFF_LOCK:
            server.SNIFF_STATE.update({"lines": [], "prog": None, "done": True,
                                       "code": None, "token": 0})

    def _get(self, path):
        with self.req.urlopen(self.svc.url + path, timeout=5) as r:
            return r.status, json.loads(r.read())

    def _post(self, path, body):
        req = self.req.Request(self.svc.url + path, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.req.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except self.err.HTTPError as e:
            return e.code, json.loads(e.read())

    @staticmethod
    def _block_executor(task):
        """不起子进程的占位执行器：一直跑到被暂停/取消。"""
        while not (task.cancel_requested or task.pause_requested):
            time.sleep(0.02)
        task.exit_code = -1

    def _submit(self, url="https://example.com/v.mp4"):
        return self._post("/api/sniff", {"url": url, "kind": "media"})

    def _wait_state(self, tid, states, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            t = server.QUEUE.get(tid)
            if t is None or t.state in states:
                return t
            time.sleep(0.02)
        self.fail("任务 %s 未在 %ss 内到达 %s" % (tid, timeout, states))

    def test_status_has_queue_fields(self):
        st, data = self._get("/api/status")
        self.assertEqual(st, 200)
        self.assertEqual(data["app"], "yt-dlp-web")
        for key in ("tasks", "running", "queued"):
            self.assertIn(key, data)
        for key in ("busy", "kind", "elapsed"):   # 旧字段保留
            self.assertIn(key, data)
        self.assertFalse(data["busy"])
        self.assertEqual(data["running"], 0)
        self.assertEqual(data["queued"], 0)

    def test_sniff_log_has_task_id_and_keeps_legacy_fields(self):
        st, d = self._get("/api/sniff/log")
        self.assertEqual(st, 200)
        self.assertIn("task_id", d)
        for key in ("busy", "running_kind", "done", "code", "prog", "last_text",
                    "url", "title", "kind", "lines", "hint", "last_error"):
            self.assertIn(key, d)

    def test_sniff_response_adds_task_id_and_queue_position(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            st, j = self._submit()
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        self.assertFalse(j["busy"])
        self.assertEqual(j["url"], "https://example.com/v.mp4")
        self.assertIsInstance(j["task_id"], int)
        self.assertIn("queue_position", j)
        self.assertEqual(server.LAST_EXT_TASK["id"], j["task_id"])
        self.assertEqual(server.SNIFF_STATE["token"], j["task_id"])

    def test_second_task_accepted_not_409(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            st1, j1 = self._submit("https://example.com/a.mp4")
            st2, j2 = self._submit("https://example.com/b.mp4")
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 200)          # 旧单任务语义是 409，队列语义必须接收
        self.assertTrue(j2["ok"])
        self.assertNotEqual(j1["task_id"], j2["task_id"])

    def test_queue_full_gives_409(self):
        with mock.patch.object(server.QUEUE, "submit",
                               side_effect=server.ytdlp_queue.QueueFull("队列已满")):
            st, j = self._submit()
        self.assertEqual(st, 409)
        self.assertFalse(j["ok"])
        self.assertTrue(j["busy"])
        self.assertIn("队列已满", j["error"])

    def test_status_busy_follows_running_task(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            _, j = self._submit()
            tid = j["task_id"]
            end = time.time() + 5
            data = None
            while time.time() < end:
                _, data = self._get("/api/status")
                if data["busy"]:
                    break
                time.sleep(0.02)
        self.assertTrue(data["busy"])
        self.assertEqual(data["kind"], "download")
        self.assertGreaterEqual(data["running"], 1)
        self.assertTrue(any(t["id"] == tid and t["state"] == "running"
                            for t in data["tasks"]))
        self.assertGreaterEqual(data["elapsed"], 0)

    def test_stop_with_id_only_cancels_that_task(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            _, j1 = self._submit("https://example.com/a.mp4")
            _, j2 = self._submit("https://example.com/b.mp4")
            self._wait_state(j1["task_id"], ("running",))
            self._wait_state(j2["task_id"], ("running",))
            st, j = self._post("/api/stop", {"id": j1["task_id"]})
            self.assertEqual(st, 200)
            self.assertTrue(j["ok"])
            self._wait_state(j1["task_id"], ("canceled",))
            t2 = server.QUEUE.get(j2["task_id"])
            self.assertIsNotNone(t2)
            self.assertIn(t2.state, ("queued", "running"))   # 不误杀其它任务

    def test_stop_without_id_cancels_latest_ext_task(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            _, j1 = self._submit("https://example.com/a.mp4")
            _, j2 = self._submit("https://example.com/b.mp4")
            self._wait_state(j2["task_id"], ("running",))
            st, j = self._post("/api/stop", {})
            self.assertEqual(st, 200)
            self.assertTrue(j["ok"])
            self._wait_state(j2["task_id"], ("canceled",))
            t1 = server.QUEUE.get(j1["task_id"])
            self.assertIsNotNone(t1)
            self.assertIn(t1.state, ("queued", "running"))

    def test_stop_accepts_query_id(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            _, j1 = self._submit()
            self._wait_state(j1["task_id"], ("running",))
            st, j = self._post("/api/stop?id=%d" % j1["task_id"], {})
            self.assertEqual(st, 200)
            self.assertTrue(j["ok"])
            self._wait_state(j1["task_id"], ("canceled",))

    def test_fast_finished_task_still_reports_done(self):
        """任务在镜像回填前就结束（如启动即失败）时，轮询不能永远卡在「进行中」。"""
        def quick(task):
            task.exit_code = 0
        with mock.patch.object(server, "_server_executor", quick):
            _, j = self._submit()
        tid = j["task_id"]
        self._wait_state(tid, ("done",))
        end = time.time() + 2
        d = None
        while time.time() < end:
            d = server.sniff_log_json()
            if d["done"]:
                break
            time.sleep(0.02)
        self.assertTrue(d["done"])
        self.assertEqual(d["code"], 0)
        self.assertEqual(d["task_id"], tid)

    def test_executor_retries_with_net_plan_then_succeeds(self):
        """执行器复用自愈阶梯：10054 连接重置 → 换姿势重试 → 成功。"""
        import tempfile
        calls = []

        def fake_run(task, cmd, cwd, on_line, stall_seconds=300):
            calls.append(list(cmd))
            if len(calls) == 1:
                return 1, ("ERROR: unable to download video data: "
                           "('Connection aborted.', ConnectionResetError(10054, 'reset'))"), {}
            return 0, "[download] 100% of 1.00MiB", {}

        with tempfile.TemporaryDirectory() as d:
            task = server.ytdlp_queue.Task(999, {
                "urls": ["https://example.com/v.mp4"], "cfg": {"net_hard": True},
                "outdir": d, "no_playlist": True, "source": "ext"})
            with mock.patch.object(server.ytdlp_queue, "run_process", fake_run):
                server._server_executor(task)
        self.assertEqual(task.exit_code, 0)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])   # 重试换了命令（自愈姿势）

    def test_executor_stops_on_cancel_without_retry(self):
        import tempfile

        def fake_run(task, cmd, cwd, on_line, stall_seconds=300):
            task.cancel_requested = True      # 模拟取消杀进程后的收尾
            return -15, "", {}

        with tempfile.TemporaryDirectory() as d:
            task = server.ytdlp_queue.Task(998, {
                "urls": ["https://example.com/v.mp4"], "cfg": {"net_hard": True},
                "outdir": d, "no_playlist": True, "source": "ext"})
            with mock.patch.object(server.ytdlp_queue, "run_process", fake_run):
                server._server_executor(task)
        self.assertEqual(task.exit_code, -15)  # 取消不再自愈重试

    def test_sniff_log_busy_and_task_id_track_queue(self):
        with mock.patch.object(server, "_server_executor", self._block_executor):
            _, j = self._submit()
            tid = j["task_id"]
            self._wait_state(tid, ("running",))
            d = server.sniff_log_json()
            self.assertTrue(d["busy"])
            self.assertEqual(d["task_id"], tid)
            self.assertFalse(d["done"])
        server.QUEUE.stop_all()
        self._wait_state(tid, ("canceled",))
        d = server.sniff_log_json()
        self.assertFalse(d["busy"])
        self.assertTrue(d["done"])


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
