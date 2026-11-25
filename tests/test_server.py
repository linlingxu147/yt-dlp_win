"""Web 服务端（YtDlpWeb/server.py）与并发/校验相关的回归用例。

server.py 不是包，用 importlib 按文件路径载入；载入本身没有副作用（没有起服务）。
"""
import importlib.util
import os
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()