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
        self.host.queue.close()
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
        self.host.queue.close()
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
        self.host.queue.close()
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
    """下载走队列：fake 进程注入到 core（run_process 用 core.spawn/iter_lines_watch）。"""

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
        self.host.queue.close()
        self.tmp.cleanup()

    def _wait_task(self, tid, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            t = self.host.queue.get(tid)
            if t is not None and t.state not in ("queued", "running"):
                return t
            time.sleep(0.02)
        self.fail("任务 %s 未在超时内结束" % tid)

    def _run_download(self, lines, code):
        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(code)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter(lines)):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            t = self._wait_task(r["task_ids"][0])
        self.host.pump_once()
        return t

    def test_success_emits_done_and_history(self):
        t = self._run_download(["[download] Destination: v.mp4"], 0)
        done = [e for e in self.events if e["type"] == "done"]
        self.assertEqual(done[0]["code"], 0)
        self.assertEqual(done[0]["task_id"], t.id)
        self.assertEqual(self.host.history[0]["status"], "ok")
        self.assertEqual(self.host.history[0]["task_id"], t.id)
        self.assertEqual(t.state, "done")
        self.assertIsNone(self.host.aux_kind)

    def test_unsafe_extra_arg_blocked(self):
        cfg = dict(self.cfg, extra="--exec rm")
        r = self.host.start_download(["https://x/v"], cfg)
        self.assertFalse(r["ok"])
        self.assertIn("禁用", r["error"])

    def test_update_blocks_download(self):
        self.host.updating = True
        r = self.host.start_download(["https://x/v"], self.cfg)
        self.assertFalse(r["ok"])

    def test_aux_kind_does_not_block_download(self):
        self.host.aux_kind = "detect"
        t = self._run_download([], 0)
        self.assertEqual(t.state, "done")

    def test_queue_full_reports_error(self):
        calls = []
        def submit(spec, exe):
            calls.append(spec)
            if len(calls) > 1:
                raise self.mod.ytdlp_queue.QueueFull("队列已满（200 个未完成任务）")
            return len(calls)
        self.host.queue.submit = submit
        r = self.host.start_download(["https://x/a", "https://x/b"], self.cfg)
        self.assertFalse(r["ok"])
        self.assertIn("队列已满", r["error"])
        # 部分入队：已排上的任务保留，提示里带数量（前端 toast 直接显示）
        self.assertEqual(r["task_ids"], [1])
        self.assertIn("已加入 1 个任务", r["error"])

    def test_duplicate_urls_in_one_call_create_one_task(self):
        """同一批里重复的链接只入队一次（并发写同名文件会互相覆盖）。"""
        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter([])):
            r = self.host.start_download(["https://x/v", "https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.assertEqual(len(r["task_ids"]), 1)
            self._wait_task(r["task_ids"][0])
        tasks = self.host.queue.snapshot()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["urls"], ["https://x/v"])

    def test_url_already_downloading_is_skipped(self):
        """活动任务已占用同链接+同目录：再次提交被跳过；换目录不算重复。"""
        started, release = threading.Event(), threading.Event()

        def blocking_iter(proc, abort=None, keepalive=None, **kw):
            started.set()
            release.wait(5)
            yield "[download] Destination: v.mp4"

        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", blocking_iter):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.assertTrue(started.wait(5))
            again = self.host.start_download(["https://x/v"], self.cfg)
            self.assertFalse(again["ok"])
            self.assertIn("队列中", again["error"])
            other = dict(self.cfg, outdir=os.path.join(self.tmp.name, "out2"))
            elsewhere = self.host.start_download(["https://x/v"], other)
            self.assertTrue(elsewhere["ok"], elsewhere)
            release.set()
            self._wait_task(r["task_ids"][0])
            self._wait_task(elsewhere["task_ids"][0])

    def test_submit_server_task_skips_active_duplicate(self):
        gate = threading.Event()

        def exe(task):
            gate.wait(5)
            task.exit_code = 0

        tid = self.host.queue.submit(
            {"urls": ["https://x/v"], "cfg": dict(self.cfg),
             "outdir": self.cfg["outdir"], "source": "ext"}, exe)
        try:
            self.assertFalse(self.host.submit_server_task(
                {"url": "https://x/v", "cfg": dict(self.cfg)}))
        finally:
            gate.set()
            self._wait_task(tid)

    def test_net_heal_retries_within_same_task(self):
        calls = []
        def spawn2(cmd, cwd=None):
            calls.append(cmd)
            return FakeProc(1 if len(calls) == 1 else 0)
        lines = ["ERROR: unable to download: ConnectionResetError 10054"]
        with mock.patch.object(self.mod.core, "spawn", side_effect=spawn2), \
             mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter(lines)):
            r = self.host.start_download(["https://x/v"], dict(self.cfg, net_hard=True))
            self.assertTrue(r["ok"], r)
            t = self._wait_task(r["task_ids"][0])
        self.host.pump_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(t.state, "done")
        self.assertEqual(self.host.history[-1]["status"], "ok")
        self.assertEqual(len(self.host.history), 1)   # 自愈不另记一条历史

    def test_pause_marks_task_paused(self):
        started, release = threading.Event(), threading.Event()

        def blocking_iter(proc, abort=None, keepalive=None, **kw):
            started.set()
            release.wait(5)
            yield "ERROR: aborted"

        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(1)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", blocking_iter):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            tid = r["task_ids"][0]
            self.assertTrue(started.wait(5))
            self.host.queue.pause(tid)
            release.set()
            t = self._wait_task(tid)
        self.assertEqual(t.state, "paused")

    def test_task_event_reports_downloading(self):
        started, release = threading.Event(), threading.Event()

        def blocking_iter(proc, abort=None, keepalive=None, **kw):
            started.set()
            release.wait(5)
            yield "[download] Destination: v.mp4"

        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", blocking_iter):
            r = self.host.start_download(["https://x/v"], self.cfg)
            tid = r["task_ids"][0]
            self.assertTrue(started.wait(5))
            self.host.pump_once()
            task_evs = [e for e in self.events if e["type"] == "task"]
            self.assertTrue(task_evs[-1]["downloading"])
            self.assertFalse(task_evs[-1]["busy"])
            release.set()
            self._wait_task(tid)
        end = time.time() + 3
        task_evs = []
        while time.time() < end:
            self.host.pump_once()
            task_evs = [e for e in self.events if e["type"] == "task"]
            if task_evs and not task_evs[-1]["downloading"]:
                break
            time.sleep(0.02)
        self.assertFalse(task_evs[-1]["downloading"])

    def test_open_after_opens_once_per_outdir(self):
        self.host.open_outdir = mock.Mock()
        cfg = dict(self.cfg, open_after=True, queue_n="1")
        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter([])):
            r = self.host.start_download(["https://x/a", "https://x/b"], cfg)
            self.assertTrue(r["ok"], r)
            for tid in r["task_ids"]:
                self._wait_task(tid)
        self.assertEqual(self.host.open_outdir.call_count, 1)

    def test_update_refused_while_download_active(self):
        gate = threading.Event()
        def exe(task):
            gate.wait(5)
            task.exit_code = 0
        tid = self.host.queue.submit(
            {"urls": ["https://x/v"], "cfg": dict(self.cfg),
             "outdir": self.cfg["outdir"], "source": "gui"}, exe)
        try:
            r = self.host.update_ytdlp("", "")
            self.assertFalse(r["ok"])
            self.assertTrue(r["busy"])
        finally:
            gate.set()
            self._wait_task(tid)

    def test_submit_server_task_refused_while_updating(self):
        self.host.updating = True
        self.assertFalse(self.host.submit_server_task({"urls": ["https://x"]}))

    def test_sniff_source_recorded(self):
        task = {"urls": ["https://x/v"], "cfg": dict(self.cfg),
                "emit": None, "no_playlist": True, "source": "sniff"}
        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter([])):
            tid = self.host.start_external(task)
            self.assertTrue(tid)
            self._wait_task(tid)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["source"], "sniff")

    def test_external_task_mirrors_and_records_ext_source(self):
        emitted = []
        task = {"urls": ["https://x/v"], "cfg": dict(self.cfg),
                "emit": emitted.append, "no_playlist": True}
        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter([])):
            tid = self.host.start_external(task)
            self.assertTrue(tid)
            self._wait_task(tid)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["source"], "ext")
        self.assertTrue(any(e.get("type") == "done" for e in emitted))

    def test_stop_ext_task_cancels_only_that_task(self):
        """扩展停止按任务作用域：只取消指定任务，不误杀 GUI 任务、不 stop_all。"""
        started, release = threading.Event(), threading.Event()
        entered = []

        def blocking_iter(proc, abort=None, keepalive=None, **kw):
            entered.append(1)
            if len(entered) >= 2:
                started.set()
            release.wait(5)
            yield "[download] Destination: v.mp4"

        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", blocking_iter):
            r = self.host.start_download(["https://x/gui"], self.cfg)
            self.assertTrue(r["ok"], r)
            ext_tid = self.host.submit_server_task(
                {"url": "https://x/ext", "cfg": dict(self.cfg)})
            self.assertTrue(ext_tid)
            self.assertTrue(started.wait(5))
            ok = self.host.stop_ext_task(ext_tid)
            release.set()
            ext = self._wait_task(ext_tid)
            gui = self._wait_task(r["task_ids"][0])
        self.assertTrue(ok)
        self.assertEqual(ext.state, "canceled")
        self.assertEqual(gui.state, "done")          # GUI 任务不受影响
        self.assertTrue(any(h.get("task_id") == ext_tid and h["status"] == "canceled"
                            for h in self.host.history))

    def test_stop_ext_task_rejects_unknown_and_invalid_ids(self):
        self.assertFalse(self.host.stop_ext_task(None))
        self.assertFalse(self.host.stop_ext_task(999))
        self.assertFalse(self.host.stop_ext_task("abc"))

    def test_stop_ext_task_emits_done_to_extension(self):
        """运行中被扩展停止：镜像要收到 done，扩展面板不能永远卡在「进行中」。"""
        emitted = []
        started, release = threading.Event(), threading.Event()

        def blocking_iter(proc, abort=None, keepalive=None, **kw):
            started.set()
            release.wait(5)
            yield "[download] Destination: v.mp4"

        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", blocking_iter):
            tid = self.host.submit_server_task(
                {"url": "https://x/ext", "cfg": dict(self.cfg), "emit": emitted.append})
            self.assertTrue(tid)
            self.assertTrue(started.wait(5))
            self.assertTrue(self.host.stop_ext_task(tid))
            release.set()
            t = self._wait_task(tid)
        self.assertEqual(t.state, "canceled")
        self.assertTrue(any(e.get("type") == "done" for e in emitted))

    def test_cancel_queued_ext_task_emits_done(self):
        """排队中被扩展停止：执行器不会跑，done 由取消路径补发。"""
        self.host.queue.set_max(1)
        gate = threading.Event()

        def blocker(task):
            gate.wait(5)
            task.exit_code = 0

        gui_tid = self.host.queue.submit(
            {"urls": ["https://x/gui"], "cfg": dict(self.cfg),
             "outdir": self.cfg["outdir"], "source": "gui"}, blocker)
        emitted = []
        try:
            ext_tid = self.host.submit_server_task(
                {"url": "https://x/ext", "cfg": dict(self.cfg), "emit": emitted.append})
            self.assertTrue(ext_tid)
            end = time.time() + 5
            while time.time() < end and self.host.queue.get(gui_tid).state != "running":
                time.sleep(0.02)
            self.assertEqual(self.host.queue.get(ext_tid).state, "queued")
            self.assertTrue(self.host.stop_ext_task(ext_tid))
            self.assertEqual(self.host.queue.get(ext_tid).state, "canceled")
            self.assertTrue(any(e.get("type") == "done" for e in emitted))
        finally:
            gate.set()
            self._wait_task(gui_tid)

    def test_remove_task_clears_row_but_keeps_history(self):
        """删除已结束任务：从列表摘掉，历史记录照留（失败任务还要靠它续传）。"""
        t = self._run_download(["[download] Destination: v.mp4"], 1)   # 失败任务
        tid = t.id
        self.assertEqual(t.state, "failed")
        self.assertTrue(any(h.get("task_id") == tid for h in self.host.history))
        self.assertTrue(self.host.remove_task(tid))
        self.assertIsNone(self.host.queue.get(tid))
        self.assertFalse(self.host.remove_task(tid))            # 已删过
        self.assertFalse(self.host.remove_task("abc"))          # 非法任务号
        self.assertTrue(any(h.get("task_id") == tid for h in self.host.history))

    def test_remove_task_rejects_active(self):
        """下载中/排队中的任务不能直接删除：先取消，否则槽位计数会对不上。"""
        started, release = threading.Event(), threading.Event()

        def blocking_iter(proc, abort=None, keepalive=None, **kw):
            started.set()
            release.wait(5)
            yield "[download] Destination: v.mp4"

        with mock.patch.object(self.mod.core, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod.core, "iter_lines_watch", blocking_iter):
            r = self.host.start_download(["https://x/gui"], self.cfg)
            tid = r["task_ids"][0]
            self.assertTrue(started.wait(5))
            self.assertFalse(self.host.remove_task(tid))
            self.assertIsNotNone(self.host.queue.get(tid))
            release.set()
            self._wait_task(tid)
        self.assertTrue(self.host.remove_task(tid))             # 结束后就能删
        self.assertIsNone(self.host.queue.get(tid))

    def test_remove_finished_tasks_bulk(self):
        t = self._run_download(["[download] Destination: v.mp4"], 0)
        out = self.host.remove_finished_tasks()
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 1)
        self.assertEqual(self.host.queue.snapshot()["tasks"], [])
        self.assertEqual(self.host.remove_finished_tasks()["removed"], 0)
        self.assertEqual(t.state, "done")   # 摘掉列表不影响任务对象本身的终态


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
        self.host.queue.close()
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
        self.host.queue.close()
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
        self.host.queue.close()
        self.tmp.cleanup()

    def test_sniff_download_overrides_cfg(self):
        captured = []
        self.host.queue.submit = lambda spec, exe: captured.append(spec) or len(captured)
        items = [{"url": "https://cdn/x.m3u8", "referer": "https://p/", "ua": "UA"}]
        r = self.host.sniff_download(items)
        self.assertTrue(r["ok"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["cfg"]["fmt"], "b")
        self.assertFalse(captured[0]["cfg"]["thumb"])
        self.assertEqual(captured[0]["cfg"]["referer"], "https://p/")
        self.assertTrue(captured[0]["no_playlist"])
        self.assertEqual(captured[0]["source"], "sniff")

    def test_sniff_download_splits_items(self):
        captured = []
        self.host.queue.submit = lambda spec, exe: captured.append(spec) or len(captured)
        r = self.host.sniff_download([{"url": "https://cdn/a"}, {"url": "https://cdn/b"}])
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["task_ids"]), 2)
        self.assertEqual([c["urls"] for c in captured],
                         [["https://cdn/a"], ["https://cdn/b"]])

    def test_sniff_download_skips_active_duplicate(self):
        """嗅探下载同样去重：活动任务已占用同链接+同目录时拒绝再入队。"""
        outdir = os.path.join(self.tmp.name, "out")
        self.host.cfg["outdir"] = outdir
        gate = threading.Event()

        def exe(task):
            gate.wait(5)
            task.exit_code = 0

        tid = self.host.queue.submit(
            {"urls": ["https://cdn/x"], "cfg": dict(self.host.cfg),
             "outdir": outdir, "source": "gui"}, exe)
        try:
            r = self.host.sniff_download([{"url": "https://cdn/x"}])
            self.assertFalse(r["ok"])
            self.assertIn("队列中", r["error"])
        finally:
            gate.set()
            end = time.time() + 5
            while time.time() < end and self.host.queue.get(tid).state not in (
                    "done", "failed"):
                time.sleep(0.02)

    def test_sniff_download_blocked_while_updating(self):
        self.host.updating = True
        r = self.host.sniff_download([{"url": "https://cdn/x"}])
        self.assertFalse(r["ok"])

    def test_sniff_items_work_without_service(self):
        self.host.sniff_clear()
        self.host.register_embedded_sniff(
            [{"url": "https://cdn/a.mp4", "entry": "embedded"}], "https://page")
        d = self.host.sniff_items()
        self.assertFalse(d["svc"])
        self.assertEqual([it["url"] for it in d["items"]], ["https://cdn/a.mp4"])

    def test_web_server_module_is_cached(self):
        mod = self.host.load_web_server()
        self.assertIsNotNone(mod)
        self.assertIs(mod, self.host.load_web_server())

    def test_sniff_preview_unknown_url(self):
        r = self.host.sniff_preview("https://cdn/x")
        self.assertFalse(r["ok"])
        self.assertIn("不在本机嗅探列表", r["error"])

    def test_sniff_play_clip_without_clip(self):
        r = self.host.sniff_play_clip()
        self.assertFalse(r["ok"])

    def test_set_ext_service_lifecycle_with_stub(self):
        captured = {}

        class StubMod:
            def set_config_file(self, p): pass
            def start_service(self):
                class Svc:
                    url = "http://127.0.0.1:8799"
                    def shutdown(self): pass
                return Svc()
            def set_task_sink(self, sink, stop=None, queue_status=None):
                captured.update(sink=sink, stop=stop, queue_status=queue_status)
        self.host.load_web_server = lambda: StubMod()
        r = self.host.set_ext_service(True)
        self.assertTrue(r["ok"])
        self.assertIsNotNone(self.host.web_svc)
        # T9 接线：sink 入 host 队列、stop 按任务作用域、状态快照来自 host 队列
        self.assertEqual(captured["sink"], self.host.submit_server_task)
        self.assertEqual(captured["stop"], self.host.stop_ext_task)
        self.assertEqual(captured["queue_status"], self.host.queue.snapshot)
        self.host.pump_once()
        self.assertTrue(any(e["type"] == "svc" and e["ok"] for e in self.events))
        self.host.set_ext_service(False)
        self.assertIsNone(self.host.web_svc)
        self.assertFalse(self.host.cfg["ext_service"])
        self.assertIsNone(captured["sink"])
        self.assertIsNone(captured["stop"])
        self.assertIsNone(captured["queue_status"])


class EmbeddedLinkTest(unittest.TestCase):
    """页面直接触发的下载（DownloadStarting）与 API「下载本页」的 WebView2 凭据。"""

    PAGE = "https://www.douyin.com/video/7679426956026187035"
    COOKIES = [{"name": "sid", "value": "v", "domain": ".douyin.com", "path": "/"}]

    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        self.host.sniff_clear()
        self.host.cfg.update({"outdir": os.path.join(self.tmp.name, "out"),
                              "open_after": False})
        self.jars = []

    def tearDown(self):
        for p in self.jars:
            self.host._drop_cookie_file(p)
        self.host.sniff_clear()
        self.host.queue.close()
        self.tmp.cleanup()

    def _capture(self):
        captured = []
        self.host.queue.submit = lambda spec, exe: captured.append(spec) or len(captured)
        return captured

    def _fake_view(self, calls):
        """只实现 export_cookies 的内嵌浏览器替身（记录被问过哪些地址）。"""
        cookies = list(self.COOKIES)

        class View:
            def export_cookies(self, url, timeout=2.0):
                calls.append(url)
                return list(cookies)
        self.host.embedded = View()

    def _remember_jar(self, spec):
        jar = str((spec.get("cfg") or {}).get("_sniff_cookie_file") or "")
        if jar:
            self.jars.append(jar)
        return jar

    def test_link_download_submits_direct_link(self):
        captured = self._capture()
        r = self.host.download_embedded_link("https://cdn.example.com/files/a.zip",
                                             self.PAGE)
        self.assertTrue(r["ok"])
        spec = captured[0]
        self.assertEqual(spec["urls"], ["https://cdn.example.com/files/a.zip"])
        self.assertEqual(spec["cfg"]["fmt"], "b")
        self.assertEqual(spec["cfg"]["referer"], self.PAGE)
        self.assertTrue(spec["cfg"]["_referer_explicit"])
        self.assertEqual(spec["source"], "embedded-link")

    def test_link_download_rejects_non_http(self):
        self._capture()
        r = self.host.download_embedded_link("blob:https://www.douyin.com/9f0c", self.PAGE)
        self.assertFalse(r["ok"])
        self.assertIn("无效", r["error"])

    def test_link_download_falls_back_to_url_as_referer(self):
        captured = self._capture()
        r = self.host.download_embedded_link("https://cdn.example.com/a.zip", "about:blank")
        self.assertTrue(r["ok"])
        self.assertEqual(captured[0]["cfg"]["referer"], "https://cdn.example.com/a.zip")

    def test_link_download_uses_webview_cookies(self):
        captured = self._capture()
        calls = []
        self._fake_view(calls)
        r = self.host.download_embedded_link("https://cdn.example.com/a.zip", self.PAGE)
        self.assertTrue(r["ok"])
        jar = self._remember_jar(captured[0])
        self.assertTrue(jar and os.path.isfile(jar))
        self.assertEqual(captured[0]["cfg"]["_sniff_cookie_file"], jar)
        self.assertEqual(captured[0]["ext_cookie_file"], jar)
        self.assertEqual(calls, ["https://cdn.example.com/a.zip"])

    def test_api_page_task_uses_webview_cookies(self):
        captured = self._capture()
        calls = []
        self._fake_view(calls)
        tid = self.host.submit_server_task({"url": self.PAGE, "kind": "page",
                                            "cfg": {"fmt": "bv*+ba/b"}})
        self.assertTrue(tid)
        cfg = captured[0]["cfg"]
        jar = self._remember_jar(captured[0])
        self.assertTrue(jar)
        self.assertEqual(cfg["cookie"], jar)
        self.assertEqual(cfg["browser"], "（不使用）")
        self.assertEqual(calls, [self.PAGE])

    def test_api_page_task_normalizes_douyin_modal_url(self):
        # 抖音精选弹窗页地址换成标准视频页再入队：yt-dlp 不认 jingxuan 形式，
        # 队列/历史里也该是能直接看懂的 /video/<id>
        captured = self._capture()
        tid = self.host.submit_server_task({
            "url": "https://www.douyin.com/jingxuan?modal_id=7679426956026187035",
            "kind": "page", "cfg": {}})
        self.assertTrue(tid)
        self.assertEqual(captured[0]["urls"], [self.PAGE])

    def test_api_page_task_keeps_supplied_cookies(self):
        captured = self._capture()
        calls = []
        self._fake_view(calls)
        tid = self.host.submit_server_task({"url": self.PAGE, "kind": "page",
                                            "cfg": {"cookie": "C:/tmp/ext.txt"}})
        self.assertTrue(tid)
        self.assertEqual(captured[0]["cfg"]["cookie"], "C:/tmp/ext.txt")
        self.assertEqual(calls, [])

    def test_api_media_task_does_not_borrow_webview_cookies(self):
        captured = self._capture()
        calls = []
        self._fake_view(calls)
        tid = self.host.submit_server_task({"url": "https://cdn.example.com/a.mp4",
                                            "kind": "media", "cfg": {}})
        self.assertTrue(tid)
        self.assertNotIn("cookie", captured[0]["cfg"])
        self.assertEqual(calls, [])

    def test_api_page_task_without_view_is_untouched(self):
        captured = self._capture()
        self.host.embedded = None
        tid = self.host.submit_server_task({"url": self.PAGE, "kind": "page", "cfg": {}})
        self.assertTrue(tid)
        self.assertNotIn("cookie", captured[0]["cfg"])


class PageFallbackTest(unittest.TestCase):
    """页面解析失败后的兜底：改用本页嗅探到的整段媒体（Host._page_fallback）。"""

    PAGE = "https://www.douyin.com/video/7679426956026187035"
    MEDIA = "https://v26-web.douyinvod.com/abc/media-video-avc1/"
    PAGE_ERR = ["ERROR: [Douyin] 7679426956026187035: Fresh cookies "
                "(not necessarily logged in) are needed"]

    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        self.host.sniff_clear()
        # download_embedded_page 只认 self.cfg，测试把输出目录挪到临时目录、别弹资源管理器
        self.host.cfg.update({"outdir": os.path.join(self.tmp.name, "out"),
                              "open_after": False})

    def tearDown(self):
        self.host.sniff_clear()
        self.host.queue.close()
        self.tmp.cleanup()

    def _register(self, items, page=None):
        self.assertTrue(self.host.register_embedded_sniff(items, page or self.PAGE))

    def _patched(self, codes):
        """spawn 按 codes 依次返回退出码（不够就 0），行输出固定为抖音那条报错。"""
        seq = list(codes)

        def spawn(cmd, cwd=None):
            return FakeProc(seq.pop(0) if seq else 0)
        return mock.patch.object(self.mod.core, "spawn", side_effect=spawn), \
            mock.patch.object(self.mod.core, "iter_lines_watch", fake_iter(self.PAGE_ERR))

    def _settled(self, tid, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            t = self.host.queue.get(tid)
            if t is not None and t.state not in ("queued", "running"):
                return t
            time.sleep(0.02)
        self.fail("任务 %s 未在超时内结束" % tid)

    def _wait_count(self, n, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            tasks = self.host.queue.snapshot()["tasks"]
            if len(tasks) >= n:
                return tasks
            time.sleep(0.02)
        self.fail("队列里没出现第 %d 个任务" % n)

    def _log_text(self, tid):
        t = self.host.queue.get(tid)
        return "".join(str(i.get("text") or "") for i in (t.log_ring if t else []))

    def test_media_item_helper(self):
        self.assertTrue(self.mod.full_media_item(
            {"url": "https://cdn/a.mp4", "cat": "video"}))
        self.assertTrue(self.mod.full_media_item(
            {"url": "https://cdn/x/", "kind": "type=video/mp4"}))
        self.assertFalse(self.mod.full_media_item(
            {"url": "https://cdn/a-1-30011.m4s", "seg": True}))
        self.assertFalse(self.mod.full_media_item(
            {"url": "https://cdn/a.m4s", "audio": True}))
        self.assertFalse(self.mod.full_media_item(
            {"url": "https://cdn/a.jpg", "cat": "image"}))
        self.assertTrue(self.mod.same_page("https://a/v/1", "https://a/v/1?spm=1"))
        self.assertFalse(self.mod.same_page("https://a/v/1", "https://a/v/2"))
        self.assertFalse(self.mod.same_page("", "https://a/v/1"))
        self.assertEqual(self.mod.site_of("https://www.douyin.com/video/1?a=1"), "douyin.com")
        self.assertEqual(self.mod.site_of("https://v26-web.douyinvod.com/x"), "v26-web.douyinvod.com")
        self.assertTrue(self.mod.same_site("https://www.douyin.com/video/1", "https://www.douyin.com/"))
        self.assertTrue(self.mod.same_site("https://a.b.example/x", "https://example/y"))
        self.assertFalse(self.mod.same_site("https://www.douyin.com/video/1", "https://other.example/1"))

    def test_failed_page_download_falls_back_to_page_media(self):
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4",
                         "mime": "type=video/mp4", "entry": "embedded",
                         "size": 1336658}])
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            self.assertTrue(r["ok"], r)
            t1 = self._settled(r["task_ids"][0])
            tasks = self._wait_count(2)
            fb = [t for t in tasks if t["id"] != t1.id][0]
            self._settled(fb["id"])
        self.assertEqual(t1.state, "failed")
        self.assertEqual(t1.source, "embedded-page")
        self.assertEqual(fb["urls"], [self.MEDIA])
        self.assertEqual(fb["source"], "sniff")
        self.assertIn("[回退]", self._log_text(t1.id))
        self.assertEqual(fb["state"], "done")
        # 失败的那条照旧进历史；兜底任务自己也记一条
        self.assertEqual([h["status"] for h in self.host.history], ["failed", "ok"])

    def test_page_match_ignores_query_string(self):
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658,
                         "page_url": self.PAGE + "?spm_id_from=333.999.0.0"}],
                       page="")
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
            self._wait_count(2)
        self.assertIn("[回退]", self._log_text(t1.id))

    def test_same_page_understands_douyin_modal_form(self):
        # 精选弹窗页与标准视频页是同一个作品：「下载本页」交给 yt-dlp 的是
        # /video/<id>，而弹窗页里登记下来的条目记的是 /jingxuan?modal_id=<id>
        modal = "https://www.douyin.com/jingxuan?modal_id=7679426956026187035"
        self.assertTrue(self.mod.same_page(modal, self.PAGE))
        self.assertTrue(self.mod.same_page(self.PAGE, modal))
        self.assertFalse(self.mod.same_page(modal, "https://www.douyin.com/video/1"))
        self.assertFalse(self.mod.same_page(modal, "https://other.example/video/1"))

    def test_page_fallback_from_modal_page_counts_as_this_page(self):
        # 弹窗页登记的条目要算「本页」，不能退化成「同站点」
        modal = "https://www.douyin.com/jingxuan?modal_id=7679426956026187035"
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658}],
                       page=modal)
        items, exact = self.host._page_media_items(self.PAGE)
        self.assertTrue(exact)
        self.assertEqual([it["url"] for it in items], [self.MEDIA])
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
            self._wait_count(2)
        log = self._log_text(t1.id)
        self.assertIn("改用本页嗅探到的整段媒体", log)
        self.assertNotIn("同站点", log)

    def test_page_fallback_prefers_biggest_over_static_asset(self):
        # 抖音页自己的静态素材（`uuu_265.mp4`）也是整段 MP4，体积小得多
        self._register([{"url": "https://lf-douyin-pc-web.douyinstatic.com/obj/uuu_265.mp4",
                         "cat": "video", "size": 194560},
                        {"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658}])
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
            tasks = self._wait_count(2)
            fb = [t for t in tasks if t["id"] != t1.id][0]
        self.assertEqual(fb["urls"], [self.MEDIA])

    def test_page_fallback_needs_known_size(self):
        # 体积未知就是没有证据：宁可不下，也不给个几百 KB 的占位素材
        self._register([{"url": "https://lf-douyin-pc-web.douyinstatic.com/obj/uuu_265.mp4",
                         "cat": "video"}])
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
        self.assertEqual(len(self.host.queue.snapshot()["tasks"]), 1)
        self.assertIn("没有可用的整段媒体", self._log_text(t1.id))

    def test_page_fallback_skips_segments_and_audio(self):
        self._register([{"url": "https://cdn/a-1-30011.m4s", "mime": "video/mp4"},
                        {"url": "https://cdn/a-1-30280.m4s", "mime": "audio/mp4"}])
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
        self.assertEqual(t1.state, "failed")
        self.assertEqual(len(self.host.queue.snapshot()["tasks"]), 1)
        self.assertNotIn("改用本页嗅探到的整段媒体", self._log_text(t1.id))

    def test_page_fallback_uses_same_site_item(self):
        # 抖音把视频放在信息流弹层里播：媒体登记的页面是 https://www.douyin.com/
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1068455}],
                       page="https://www.douyin.com/")
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
            tasks = self._wait_count(2)
            fb = [t for t in tasks if t["id"] != t1.id][0]
        self.assertEqual(fb["urls"], [self.MEDIA])
        self.assertIn("同站点", self._log_text(t1.id))

    def test_page_fallback_ignores_other_pages(self):
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658}],
                       page="https://other.example/video/1")
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.download_embedded_page(self.PAGE)
            t1 = self._settled(r["task_ids"][0])
        self.assertEqual(len(self.host.queue.snapshot()["tasks"]), 1)
        self.assertIn("没有可用的整段媒体", self._log_text(t1.id))

    def test_desktop_task_falls_back_for_its_own_page(self):
        # 桌面端粘贴的页面链接失败时，只要内嵌浏览器开过同一页并嗅探到整段媒体，也兜底
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658}])
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.start_download([self.PAGE], dict(self.host.cfg))
            self.assertTrue(r["ok"], r)
            t1 = self._settled(r["task_ids"][0])
            tasks = self._wait_count(2)
            fb = [t for t in tasks if t["id"] != t1.id][0]
        self.assertEqual(t1.source, "gui")
        self.assertEqual(t1.state, "failed")
        self.assertEqual(fb["urls"], [self.MEDIA])
        self.assertEqual(fb["source"], "sniff")
        self.assertIn("改用本页嗅探到的整段媒体", self._log_text(t1.id))

    def test_desktop_task_ignores_sibling_page_items(self):
        # 同站点但不同页面（信息流首页）的条目不能顶替桌面端粘贴的那条链接
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658}],
                       page="https://www.douyin.com/")
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.start_download([self.PAGE], dict(self.host.cfg))
            t1 = self._settled(r["task_ids"][0])
        self.assertEqual(t1.state, "failed")
        self.assertEqual(len(self.host.queue.snapshot()["tasks"]), 1)
        self.assertIn("没有可用的整段媒体", self._log_text(t1.id))

    def test_sniff_task_failure_does_not_fall_back(self):
        self._register([{"url": self.MEDIA, "kind": "type=video/mp4", "size": 1336658}])
        items = self.host._sniff_api().sniff_items_json()["items"]
        p1, p2 = self._patched([1, 0])
        with p1, p2:
            r = self.host.sniff_download(items)
            self.assertTrue(r["ok"], r)
            t1 = self._settled(r["task_ids"][0])
        self.assertEqual(t1.source, "sniff")
        self.assertEqual(len(self.host.queue.snapshot()["tasks"]), 1)
        self.assertNotIn("[回退]", self._log_text(t1.id))


class EmbeddedQueueTest(unittest.TestCase):
    """内嵌浏览器右上角进度小条：摘要字段与「点开下载队列」。"""

    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        self.host.cfg.update({"outdir": os.path.join(self.tmp.name, "out"),
                              "open_after": False})

    def tearDown(self):
        self.host.queue.close()
        self.tmp.cleanup()

    def test_summary_fields_on_empty_queue(self):
        s = self.host.queue_summary()
        for key in ("running", "queued", "failed", "done", "pct", "speed", "label"):
            self.assertIn(key, s)
        self.assertEqual((s["running"], s["queued"], s["failed"], s["done"]), (0,) * 4)
        self.assertEqual((s["pct"], s["speed"], s["label"]), (0, "", ""))

    def test_summary_reads_running_task(self):
        self.host.queue.snapshot = lambda: {
            "running": 1, "queued": 2, "paused": 0, "max": 3,
            "tasks": [{"state": "done"}, {"state": "failed"},
                      {"state": "running", "pct": 42.6, "speed": "1.2 MB/s",
                       "title": "片子"}]}
        s = self.host.queue_summary()
        self.assertEqual(s["running"], 1)
        self.assertEqual(s["queued"], 2)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["done"], 1)
        self.assertEqual(s["pct"], 43)
        self.assertEqual(s["speed"], "1.2 MB/s")
        self.assertEqual(s["label"], "片子")

    def test_summary_falls_back_to_url_label(self):
        self.host.queue.snapshot = lambda: {
            "running": 1, "queued": 0,
            "tasks": [{"state": "running", "pct": None,
                       "urls": ["https://cdn.example.com/a.mp4"]}]}
        s = self.host.queue_summary()
        self.assertEqual(s["label"], "https://cdn.example.com/a.mp4")
        self.assertEqual(s["pct"], 0)

    def test_open_queue_without_browser(self):
        self.host.embedded = None
        r = self.host.open_embedded_queue()
        self.assertFalse(r["ok"])
        self.assertIn("内嵌浏览器", r["error"])

    def test_open_queue_returns_and_switches_tab(self):
        calls = []

        class View:
            def return_to_app(self):
                calls.append(True)

        self.host.embedded = View()
        r = self.host.open_embedded_queue()
        self.host.pump_once()
        self.assertTrue(r["ok"])
        self.assertEqual(calls, [True])
        self.assertIn({"type": "open_tab", "tab": "download"}, self.events)

    def test_open_browser_passes_both_queue_sinks(self):
        got = {}

        class Fake:
            def __init__(self, *args, **kw):
                got["args"] = args

            def open(self, window=None, theme=""):
                return {"ok": True}

        class Mod:
            EmbeddedBrowser = Fake

        sys.modules["embedded"] = Mod()
        try:
            r = self.host.open_embedded_browser(None)
        finally:
            sys.modules.pop("embedded", None)
        self.assertTrue(r["ok"])
        self.assertEqual(got["args"][:4],
                         (self.host.register_embedded_sniff,
                          self.host.download_embedded_sniff,
                          self.host.download_embedded_page,
                          self.host.download_embedded_link))
        self.assertEqual(got["args"][4:],
                         (self.host.queue_summary, self.host.open_embedded_queue,
                          self.host.embedded_queue_action))

    def test_summary_lists_recent_tasks_newest_first(self):
        rows = [{"id": i, "state": "done", "pct": 100, "title": "第%d条" % i,
                 "urls": ["https://cdn.example.com/%d.mp4" % i]} for i in range(1, 11)]
        self.host.queue.snapshot = lambda: {"running": 0, "queued": 0, "tasks": rows}
        s = self.host.queue_summary()
        self.assertEqual(s["total"], 10)
        self.assertEqual(len(s["tasks"]), self.mod.Host.QUEUE_ROWS_MAX)
        ids = [t["id"] for t in s["tasks"]]
        self.assertEqual(ids, [10, 9, 8, 7, 6, 5, 4, 3])
        first = s["tasks"][0]
        for key in ("id", "label", "state", "pct", "speed", "eta", "size", "file"):
            self.assertIn(key, first)
        self.assertEqual(first["label"], "第10条")

    def test_summary_row_label_falls_back_to_url(self):
        self.host.queue.snapshot = lambda: {
            "running": 0, "queued": 0,
            "tasks": [{"id": 1, "state": "failed",
                       "urls": ["https://cdn.example.com/very/long/path/a.mp4"]}]}
        row = self.host.queue_summary()["tasks"][0]
        self.assertEqual(row["label"], "https://cdn.example.com/very/long/path/a.mp4")
        self.assertEqual(row["pct"], 0)

    def _running_task(self, wait=5.0):
        """造一条真的在跑的任务：执行器按队列契约收一个 task，暂停/取消时退出。"""
        release = threading.Event()

        def exe(task):
            while not release.is_set():
                if task.cancel_requested or task.pause_requested:
                    return
                task.pstate["pct"] = min(95.0, float(task.pstate.get("pct") or 0.0) + 5.0)
                task.pstate["speed"] = "1.00MiB/s"
                time.sleep(0.05)
            task.exit_code = 0        # 与真实执行器一致：正常跑完才记 done

        tid = self.host.queue.submit({"urls": ["https://cdn.example.com/a.mp4"],
                                      "cfg": {}, "outdir": self.host.cfg["outdir"],
                                      "source": "gui"}, exe)
        for _ in range(int(wait / 0.01)):
            task = self.host.queue.get(tid)
            if task is not None and task.state == "running" and task.pstate.get("pct"):
                break
            time.sleep(0.01)
        return tid, release

    def _wait_state(self, tid, states, wait=5.0):
        for _ in range(int(wait / 0.01)):
            task = self.host.queue.get(tid)
            if task is not None and task.state in states:
                return task.state
            time.sleep(0.01)
        task = self.host.queue.get(tid)
        return task.state if task is not None else None

    def test_queue_action_unknown_action_rejected(self):
        r = self.host.embedded_queue_action("drop", 1)
        self.assertFalse(r["ok"])
        self.assertIn("不支持", r["error"])

    def test_queue_action_bad_id_rejected(self):
        r = self.host.embedded_queue_action("cancel", "abc")
        self.assertFalse(r["ok"])
        self.assertIn("编号", r["error"])

    def test_queue_action_missing_task_rejected(self):
        r = self.host.embedded_queue_action("cancel", 999)
        self.assertFalse(r["ok"])
        self.assertIn("不在队列", r["error"])

    def test_queue_action_pause_and_resume(self):
        tid, release = self._running_task()
        try:
            r = self.host.embedded_queue_action("pause", tid)
            self.assertTrue(r["ok"])
            self.assertEqual(self._wait_state(tid, ("paused",)), "paused")
            r = self.host.embedded_queue_action("resume", tid)
            self.assertTrue(r["ok"])
            self.assertIn(self._wait_state(tid, ("running", "queued")),
                          ("running", "queued"))
        finally:
            release.set()
            self.host.queue.close()

    def test_queue_action_cancel_running_task(self):
        tid, release = self._running_task()
        try:
            r = self.host.embedded_queue_action("cancel", tid)
            self.assertTrue(r["ok"])
            self.assertEqual(self._wait_state(tid, ("canceled",)), "canceled")
        finally:
            release.set()
            self.host.queue.close()

    def test_queue_action_cancel_finished_removes_row_only(self):
        tid, release = self._running_task()
        release.set()
        self.assertEqual(self._wait_state(tid, ("done",)), "done")
        r = self.host.embedded_queue_action("cancel", tid)
        self.assertTrue(r["ok"])
        self.assertIsNone(self.host.queue.get(tid))


class UpdateMirrorFlowTest(unittest.TestCase):
    def test_official_failure_without_mirror_logs_hint(self):
        mod = load_host()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        host = mod.Host(os.path.join(tmp.name, "config.json"))
        self.addCleanup(host.queue.close)
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
        self.addCleanup(host.queue.close)
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