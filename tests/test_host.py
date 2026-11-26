# -*- coding: utf-8 -*-
"""host 单测：注入事件回调，不依赖 pywebview。"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "YtDlpGUI"))


def load_host():
    spec = importlib.util.spec_from_file_location(
        "ytdlp_gui_host", os.path.join(ROOT, "YtDlpGUI", "host.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class PumpTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_ready_keeps_events_queued(self):
        self.host._put({"type": "progress", "pct": 10})
        self.host.pump_once()
        self.assertEqual(self.events, [])

    def test_progress_coalesced_to_latest(self):
        self.host.mark_ready()
        for i in range(10):
            self.host._put({"type": "progress", "pct": i})
        self.host.pump_once()
        prog = [e for e in self.events if e["type"] == "progress"]
        self.assertEqual(len(prog), 1)
        self.assertEqual(prog[0]["pct"], 9)

    def test_logs_batched(self):
        self.host.mark_ready()
        for t in ("a", "b", "c"):
            self.host._put({"type": "log", "text": t, "tag": "ok"})
        self.host.pump_once()
        batches = [e for e in self.events if e["type"] == "logbatch"]
        self.assertEqual(len(batches), 1)
        self.assertEqual([i["text"] for i in batches[0]["items"]], ["a", "b", "c"])

    def test_plain_events_pass_through(self):
        self.host.mark_ready()
        self.host._put({"type": "status", "text": "就绪", "tone": "ok"})
        self.host.pump_once()
        self.assertIn({"type": "status", "text": "就绪", "tone": "ok"}, self.events)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        self.host = self.mod.Host(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_loaded(self):
        self.assertEqual(self.host.cfg["mode"], "best")

    def test_save_roundtrip_keeps_keys(self):
        ok = self.host.save_config({"proxy": "http://127.0.0.1:7890"})
        self.assertTrue(ok)
        again = self.mod.Host(self.path)
        self.assertEqual(again.cfg["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(again.cfg["mode"], "best")

    def test_env_shape(self):
        env = self.host.get_env()
        self.assertIn("has_aria2c", env)
        self.assertIn("yt_dlp_path", env)
        self.assertIn("svc_url", env)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_defaults_and_roundtrip(self):
        self.host.record_history({"urls": ["https://x/v"], "status": "ok", "code": 0})
        again = self.mod.Host(self.host.config_file)
        self.assertEqual(len(again.history), 1)
        rec = again.history[0]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["source"], "gui")
        self.assertIn("ts", rec)

    def test_fifo_cap_500(self):
        for i in range(505):
            self.host.record_history({"urls": ["u%d" % i], "status": "ok", "code": 0})
        self.assertEqual(len(self.host.history), 500)
        self.assertEqual(self.host.history[0]["urls"], ["u5"])

    def test_list_newest_first(self):
        self.host.record_history({"urls": ["a"], "status": "ok", "code": 0})
        self.host.record_history({"urls": ["b"], "status": "failed", "code": 1})
        self.assertEqual(self.host.history_list()[0]["urls"], ["b"])

    def test_corrupt_file_falls_back_empty(self):
        with open(self.host.history_file, "w", encoding="utf-8") as f:
            f.write("{bad json")
        again = self.mod.Host(self.host.config_file)
        self.assertEqual(again.history, [])

    def test_clear(self):
        self.host.record_history({"urls": ["a"], "status": "ok", "code": 0})
        self.host.history_clear()
        self.assertEqual(self.host.history_list(), [])
        again = self.mod.Host(self.host.config_file)
        self.assertEqual(again.history, [])


class FakeProc:
    def __init__(self, code):
        self._code = code
    def poll(self):
        return self._code
    def wait(self):
        return self._code


def fake_iter(lines):
    def it(proc, abort=None, keepalive=None, **kw):
        for ln in lines:
            yield ln
    return it


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        # 测试不许真的弹资源管理器：临时目录随后会被删，Explorer 会留下「位置不可用」弹窗
        self.cfg = dict(self.host.cfg, outdir=os.path.join(self.tmp.name, "out"),
                        open_after=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_download(self, lines, code):
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(code)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter(lines)):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()

    def test_success_emits_done_and_history(self):
        self._run_download(["[download] Destination: v.mp4"], 0)
        done = [e for e in self.events if e["type"] == "done"]
        self.assertEqual(done[0]["code"], 0)
        self.assertEqual(self.host.history[0]["status"], "ok")
        self.assertIsNone(self.host.task_kind)

    def test_unsafe_extra_arg_blocked(self):
        cfg = dict(self.cfg, extra="--exec rm")
        r = self.host.start_download(["https://x/v"], cfg)
        self.assertFalse(r["ok"])
        self.assertIn("禁用", r["error"])

    def test_busy_gate(self):
        self.host.task_kind = "detect"
        r = self.host.start_download(["https://x/v"], self.cfg)
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])

    def test_net_heal_retries_once(self):
        calls = []
        def spawn2(cmd, cwd=None):
            calls.append(cmd)
            return FakeProc(1 if len(calls) == 1 else 0)
        lines = ["ERROR: unable to download: ConnectionResetError 10054"]
        with mock.patch.object(self.mod, "spawn", side_effect=spawn2), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter(lines)):
            r = self.host.start_download(["https://x/v"], dict(self.cfg, net_hard=True))
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
            self.host.pump_once()      # 路由 done → 自愈起新 worker
            self.host._worker.join(5)
            self.host.pump_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.host.history[-1]["status"], "ok")

    def test_stop_marks_stopped_history(self):
        def stopped_iter(proc, abort=None, keepalive=None, **kw):
            self.host.user_stopped = True   # 模拟用户点了停止
            yield "ERROR: aborted"
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(1)), \
             mock.patch.object(self.mod, "iter_lines_watch", stopped_iter):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["status"], "stopped")

    def test_submit_server_task_busy(self):
        self.host.task_kind = "download"
        self.assertFalse(self.host.submit_server_task({"urls": ["https://x"]}))

    def test_sniff_source_recorded(self):
        task = {"urls": ["https://x/v"], "cfg": dict(self.cfg),
                "emit": None, "no_playlist": True, "source": "sniff"}
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([])):
            self.host.start_external(task)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["source"], "sniff")

    def test_external_task_mirrors_and_records_ext_source(self):
        emitted = []
        task = {"urls": ["https://x/v"], "cfg": dict(self.cfg),
                "emit": emitted.append, "no_playlist": True}
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([])):
            self.host.start_external(task)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["source"], "ext")
        self.assertTrue(any(e.get("type") == "done" for e in emitted))


class DetectTroubleTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        self.cfg = dict(self.host.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_collects_rows(self):
        row = json.dumps({"id": "x", "title": "T", "duration": 60,
                          "webpage_url": "https://x/v"})
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([row])):
            r = self.host.detect(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(len(self.host.results), 1)
        self.assertEqual(self.host.results[0]["title"], "T")
        self.assertTrue(any(e["type"] == "detdone" for e in self.events))

    def test_formats_returns_lines_event(self):
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch",
                               fake_iter(["ID EXT RES", "137 mp4 1080p"])):
            r = self.host.fetch_formats("https://x/v", self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        fmt = [e for e in self.events if e["type"] == "formats"]
        self.assertEqual(fmt[0]["code"], 0)
        self.assertIn("137 mp4 1080p", fmt[0]["lines"])

    COOKIE_ERR = ("ERROR: Could not copy Chrome cookie database. "
                  "See https://github.com/yt-dlp/yt-dlp/issues/7271 for more info")

    def _cookie_cfg(self):
        return dict(self.cfg, browser="edge", browser_profile="Default")

    def _iter_queue(self, batches):
        queue = list(batches)

        def it(proc, abort=None, keepalive=None, **kw):
            for ln in queue.pop(0):
                yield ln
        return it

    def test_detect_retries_without_cookies_when_db_locked(self):
        row = json.dumps({"id": "x", "title": "T", "webpage_url": "https://x/v"})
        calls = []

        def spawn2(cmd, cwd=None):
            calls.append(cmd)
            return FakeProc(1 if len(calls) == 1 else 0)

        it = self._iter_queue([[self.COOKIE_ERR], [row]])
        with mock.patch.object(self.mod, "spawn", side_effect=spawn2), \
             mock.patch.object(self.mod, "iter_lines_watch", it):
            r = self.host.detect(["https://x/v"], self._cookie_cfg())
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(len(calls), 2)
        self.assertIn("--cookies-from-browser", calls[0])
        self.assertNotIn("--cookies-from-browser", calls[1])
        self.assertEqual(len(self.host.results), 1)
        done = [e for e in self.events if e["type"] == "detdone"]
        self.assertEqual(done[-1]["code"], 0)
        logs = " ".join(i["text"] for b in self.events if b["type"] == "logbatch"
                        for i in b["items"])
        self.assertIn("自动重试", logs)

    def test_formats_retries_without_cookies_when_db_locked(self):
        calls = []

        def spawn2(cmd, cwd=None):
            calls.append(cmd)
            return FakeProc(1 if len(calls) == 1 else 0)

        it = self._iter_queue([[self.COOKIE_ERR],
                               ["ID EXT RES", "137 mp4 1080p"]])
        with mock.patch.object(self.mod, "spawn", side_effect=spawn2), \
             mock.patch.object(self.mod, "iter_lines_watch", it):
            r = self.host.fetch_formats("https://x/v", self._cookie_cfg())
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(len(calls), 2)
        self.assertIn("--cookies-from-browser", calls[0])
        self.assertNotIn("--cookies-from-browser", calls[1])
        fmt = [e for e in self.events if e["type"] == "formats"][-1]
        self.assertEqual(fmt["code"], 0)
        self.assertIn("137 mp4 1080p", fmt["lines"])

    def test_copy_cmd_text(self):
        r = self.host.copy_cmd(["https://x/v"], self.cfg)
        self.assertTrue(r["ok"])
        self.assertIn("yt-dlp", r["text"])

    def test_cookie_lock_line_becomes_one_warning(self):
        err = ("ERROR: Could not copy Chrome cookie database. See "
               "https://github.com/yt-dlp/yt-dlp/issues/7271 for more info")
        self.host._handle_line(err)
        self.host._handle_line(err)
        self.host.pump_once()
        logs = [i for b in self.events if b["type"] == "logbatch"
                for i in b["items"]]
        hits = [i for i in logs if "浏览器 Cookies" in i["text"]]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["tag"], "warn")

    def test_other_error_lines_stay_red(self):
        self.host._handle_line("ERROR: unable to download: HTTP Error 403")
        self.host.pump_once()
        logs = [i for b in self.events if b["type"] == "logbatch"
                for i in b["items"]]
        self.assertTrue(any(i["tag"] == "err" for i in logs))

    def test_trouble_applies_fix(self):
        row = json.dumps({"id": "x", "title": "T"})
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([row])):
            r = self.host.troubleshoot(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertTrue(any(e["type"] == "trodone" for e in self.events))


class PreviewPlayTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()

    def tearDown(self):
        self.tmp.cleanup()

    def test_thumb_returns_data_url(self):
        png = os.path.join(self.tmp.name, "t.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        # 字面公网 IP：is_public_http_url 无需 DNS 即可判定公网
        with mock.patch.object(self.mod, "fetch_thumb", return_value=(png, None)):
            r = self.host.get_thumb("https://93.184.216.34/x.png", {})
        self.assertTrue(r["ok"])
        self.assertTrue(r["data_url"].startswith("data:image/png;base64,"))

    def test_thumb_private_url_rejected_without_network(self):
        r = self.host.get_thumb("https://127.0.0.1/x.png", {})
        self.assertFalse(r["ok"])

    def test_thumb_failure_degrades(self):
        with mock.patch.object(self.mod, "fetch_thumb", return_value=("", "403")):
            r = self.host.get_thumb("https://93.184.216.34/x.png", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["err"], "403")


class SniffServiceTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sniff_download_overrides_cfg(self):
        captured = {}
        def sink(task):
            captured.update(task)
            return True
        self.host.submit_server_task = sink
        items = [{"url": "https://cdn/x.m3u8", "referer": "https://p/", "ua": "UA"}]
        r = self.host.sniff_download(items)
        self.assertTrue(r["ok"])
        self.assertEqual(captured["cfg"]["fmt"], "b")
        self.assertFalse(captured["cfg"]["thumb"])
        self.assertEqual(captured["cfg"]["referer"], "https://p/")
        self.assertTrue(captured["no_playlist"])

    def test_sniff_download_busy(self):
        self.host.task_kind = "download"
        r = self.host.sniff_download([{"url": "https://cdn/x"}])
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])

    def test_sniff_items_without_service(self):
        d = self.host.sniff_items()
        self.assertEqual(d["items"], [])
        self.assertFalse(d["svc"])

    def test_sniff_preview_requires_service(self):
        r = self.host.sniff_preview("https://cdn/x")
        self.assertFalse(r["ok"])

    def test_sniff_play_clip_without_clip(self):
        r = self.host.sniff_play_clip()
        self.assertFalse(r["ok"])

    def test_set_ext_service_lifecycle_with_stub(self):
        class StubRunner:
            def reserve(self, kind): return True
            def finish(self): pass
        class StubMod:
            RUNNER = StubRunner()
            def set_config_file(self, p): pass
            def start_service(self):
                class Svc:
                    url = "http://127.0.0.1:8799"
                    def shutdown(self): pass
                return Svc()
            def set_task_sink(self, sink, stop=None): pass
        self.host.load_web_server = lambda: StubMod()
        r = self.host.set_ext_service(True)
        self.assertTrue(r["ok"])
        self.assertIsNotNone(self.host.web_svc)
        self.host.pump_once()
        self.assertTrue(any(e["type"] == "svc" and e["ok"] for e in self.events))
        self.host.set_ext_service(False)
        self.assertIsNone(self.host.web_svc)
        self.assertFalse(self.host.cfg["ext_service"])


class UpdateMirrorFlowTest(unittest.TestCase):
    def test_official_failure_without_mirror_logs_hint(self):
        mod = load_host()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        host = mod.Host(os.path.join(tmp.name, "config.json"))
        cmd = [sys.executable, "-c", "raise SystemExit(1)"]
        host._update_run(cmd, proxy="", mirror="")
        events = []
        while not host.q.empty():
            events.append(host.q.get())
        logs = "".join(e.get("text", "") for e in events if e.get("type") == "log")
        self.assertIn("更新镜像", logs)

    def test_official_failure_falls_back_to_mirror(self):
        mod = load_host()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = os.path.join(tmp.name, "runtime")
        os.makedirs(os.path.join(rt, "_internal"))
        with open(os.path.join(rt, "yt-dlp.exe"), "wb") as f:
            f.write(b"old-exe")
        with open(os.path.join(rt, "_internal", "old.txt"), "wb") as f:
            f.write(b"old-internal")

        mirror = os.path.join(tmp.name, "mirror")
        os.makedirs(mirror)
        zpath = os.path.join(mirror, "yt-dlp_win.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("yt-dlp.exe", b"new-exe")
            zf.writestr("_internal/new.txt", b"new-internal")
        with open(zpath, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(mirror, "SHA2-256SUMS"), "w", encoding="utf-8") as f:
            f.write("%s  yt-dlp_win.zip\n" % digest)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                p = os.path.join(mirror, os.path.basename(self.path))
                if not os.path.isfile(p):
                    self.send_error(404)
                    return
                with open(p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        host = mod.Host(os.path.join(tmp.name, "config.json"))
        cmd = [sys.executable, "-c", "raise SystemExit(1)"]
        with mock.patch.object(mod.core, "RUNTIME_DIR", rt), \
                mock.patch.object(mod.updater, "verify_version",
                                  return_value=(True, "test-2026", "")):
            host._update_run(cmd, proxy="",
                             mirror="http://127.0.0.1:%d" % srv.server_port)

        events = []
        while not host.q.empty():
            events.append(host.q.get())
        self.assertTrue(any(e.get("type") == "trodone" and e.get("ok") == 1
                            for e in events), events)
        with open(os.path.join(rt, "yt-dlp.exe"), "rb") as f:
            self.assertEqual(f.read(), b"new-exe")
        self.assertTrue(os.path.isfile(os.path.join(rt, "_internal", "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(rt, "_internal", "old.txt")))
        backups = [n for n in os.listdir(rt) if n.startswith(".update_backup_")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(rt, backups[0], "yt-dlp.exe"), "rb") as f:
            self.assertEqual(f.read(), b"old-exe")


if __name__ == "__main__":
    unittest.main()