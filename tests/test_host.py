# -*- coding: utf-8 -*-
"""host 单测：注入事件回调，不依赖 pywebview。"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        self.cfg = dict(self.host.cfg, outdir=os.path.join(self.tmp.name, "out"))

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


if __name__ == "__main__":
    unittest.main()