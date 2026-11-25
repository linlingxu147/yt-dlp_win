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


if __name__ == "__main__":
    unittest.main()