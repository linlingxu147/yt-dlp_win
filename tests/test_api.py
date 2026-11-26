# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class ApiTest(unittest.TestCase):
    def setUp(self):
        host_mod = load_module("ytdlp_gui_host", "YtDlpGUI/host.py")
        api_mod = load_module("ytdlp_gui_api", "YtDlpGUI/api.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.host = host_mod.Host(os.path.join(self.tmp.name, "config.json"))
        self.api = api_mod.Api(self.host)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsafe_extra_blocked(self):
        cfg = dict(self.host.cfg, extra="--exec calc", outdir=self.tmp.name)
        r = self.api.start_download(["https://x/v"], cfg)
        self.assertFalse(r["ok"])

    def test_busy_semantics(self):
        self.host.task_kind = "download"
        r = self.api.start_download(["https://x/v"], dict(self.host.cfg))
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])

    def test_open_url_rejects_non_http(self):
        self.assertFalse(self.api.open_url("file:///C:/x")["ok"])
        self.assertFalse(self.api.open_url("javascript:alert(1)")["ok"])

    def test_play_now_no_path_param(self):
        with self.assertRaises(TypeError):
            self.api.play_now(path="C:/x")

    def test_sniff_play_clip_no_path_param(self):
        with self.assertRaises(TypeError):
            self.api.sniff_play_clip(path="C:/x")

    def test_get_config_shape(self):
        r = self.api.get_config()
        self.assertTrue(r["ok"])
        self.assertIn("cfg", r)
        self.assertIn("env", r)
        self.assertIn("modes", r)

    def test_history_redownload_uses_current_cfg(self):
        self.host.record_history({"urls": ["https://x/v"], "status": "ok", "code": 0})
        captured = {}
        def fake_start(urls, cfg, no_playlist=False):
            captured["urls"] = urls
            return {"ok": True}
        self.host.start_download = fake_start
        r = self.api.history_redownload(0)
        self.assertTrue(r["ok"])
        self.assertEqual(captured["urls"], ["https://x/v"])

    def test_push_without_window_silent(self):
        self.api.push({"type": "status", "text": "x"})  # 不抛异常即通过

    def test_host_is_not_reflected_into_js_api(self):
        """pywebview 会递归遍历 js_api 的公开属性；Host 整棵对象图不能暴露。"""
        self.assertIs(self.host._serializable, False)

    def test_bind_window_marks_window_non_serializable(self):
        class FakeWindow:
            pass

        w = FakeWindow()
        self.api.bind_window(w)
        self.assertIs(self.api.window, w)
        self.assertIs(w._serializable, False)

    def test_internal_error_wrapped(self):
        def boom(*a, **kw):
            raise RuntimeError("炸了")
        self.host.detect = boom
        r = self.api.detect(["https://x"], {})
        self.assertFalse(r["ok"])
        self.assertIn("内部错误", r["error"])


if __name__ == "__main__":
    unittest.main()