"""针对 2026-11 审阅修复的回归用例。

每条用例对应一处真实缺陷，失败时能直接指回问题而不是「某处行为变了」。
"""
import contextlib
import io
import ipaddress
import os
import subprocess
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

import ytdlp_core as core


class TestSsrfNets(unittest.TestCase):
    def test_mapped_and_special_ranges_are_blocked(self):
        for raw in ("::ffff:127.0.0.1", "::ffff:10.0.0.1", "100.64.0.1", "198.18.0.1",
                    "240.0.0.1", "255.255.255.255", "0.0.0.0", "::", "2001:db8::1",
                    "64:ff9b::7f00:1"):
            self.assertTrue(core._ip_is_blocked(ipaddress.ip_address(raw)), raw)

    def test_public_addresses_still_pass(self):
        for raw in ("93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertFalse(core._ip_is_blocked(ipaddress.ip_address(raw)), raw)


class TestThumbRedirect(unittest.TestCase):
    def test_redirect_to_private_is_refused(self):
        handler = core._ThumbRedirectHandler()
        req = mock.MagicMock()
        with self.assertRaises(Exception) as ctx:
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/steal")
        self.assertIn("非公网", str(ctx.exception))

    def test_redirect_to_public_is_followed(self):
        handler = core._ThumbRedirectHandler()
        req = urllib.request.Request("https://cdn.example.com/a.jpg")
        with mock.patch("ytdlp_core.is_public_http_url", return_value=True):
            new = handler.redirect_request(req, None, 302, "Found", {},
                                           "https://cdn.example.com/b.jpg")
        self.assertIn("cdn.example.com", new.full_url)


class TestThumbCredentials(unittest.TestCase):
    def test_direct_headers_drop_cookie_and_custom(self):
        cfg = {"ua": "UA/1", "referer": "https://a/", "cookie_header": "sid=1",
               "headers": "Authorization: Bearer x\nX-Token: y"}
        h = core.thumb_direct_headers(cfg)
        joined = " ".join("%s: %s" % kv for kv in h.items()).lower()
        self.assertNotIn("bearer", joined)
        self.assertNotIn("sid=1", joined)
        self.assertNotIn("x-token", joined)

    def test_ytdlp_fallback_keeps_full_headers(self):
        cfg = {"ua": "UA/1", "cookie_header": "sid=1"}
        joined = " ".join("%s: %s" % kv for kv in core.thumb_headers(cfg).items()).lower()
        self.assertIn("sid=1", joined)


class TestThumbContent(unittest.TestCase):
    def _resp(self, payload):
        r = mock.MagicMock()
        r.read.return_value = payload
        r.__enter__ = mock.Mock(return_value=r)
        r.__exit__ = mock.Mock(return_value=False)
        return r

    def test_html_is_not_cached_as_cover(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("ytdlp_core._open_thumb", return_value=self._resp(b"<html>nope")):
                path, err = core.fetch_thumb("https://cdn.example/a.jpg", {}, cache_dir=d)
        self.assertEqual(path, "")
        self.assertIn("不是图片", err)
        self.assertFalse(os.path.exists(core.thumb_cache_path("https://cdn.example/a.jpg", d)))

    def test_stale_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as d:
            u = "https://cdn.example/a.jpg"
            p = core.thumb_cache_path(u, d)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\xff\xd8\xffOLD")
            old = time.time() - core.THUMB_CACHE_TTL - 60
            os.utime(p, (old, old))
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("ytdlp_core.thumb_cache_path", return_value=p), \
                 mock.patch("ytdlp_core._open_thumb",
                            return_value=self._resp(b"\xff\xd8\xffNEW")) as op:
                path, err = core.fetch_thumb(u, {}, cache_dir=d)
            op.assert_called_once()
            self.assertEqual(path, p)
            self.assertIsNone(err)
            with open(p, "rb") as f:
                self.assertIn(b"NEW", f.read())


class TestConfigRobustness(unittest.TestCase):
    def test_unicode_digit_does_not_crash(self):
        cfg = {"retries": "\u00b2"}          # '²'.isdigit() 为 True，int() 会抛
        out = dict(cfg)
        core.coerce_types(out)          # 就地校正
        self.assertEqual(out["retries"], core.DEFAULT_CONFIG["retries"])

    def test_bom_config_keeps_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with io.open(p, "w", encoding="utf-8-sig") as f:
                f.write('{"outdir": "D:/keep", "retries": 7}')
            cfg = core.load_config(p)
        self.assertEqual(cfg["outdir"], "D:/keep")
        self.assertEqual(str(cfg["retries"]), "7")

    @unittest.skipUnless(os.name == "nt", "盘根判断只在 Windows 上有意义")
    def test_drive_root_contains_everything(self):
        self.assertTrue(core.path_inside(r"C:\x\y.txt", "C:\\"))
        self.assertFalse(core.path_inside(r"D:\x\y.txt", "C:\\"))

    def test_write_lock_is_released_and_stale_one_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            core.atomic_write_text(p, "{}")
            self.assertFalse(os.path.exists(p + ".lock"))
            with io.open(p + ".lock", "w") as f:
                f.write("stale")
            old = time.time() - 120
            os.utime(p + ".lock", (old, old))
            core.atomic_write_text(p, '{"a": 1}')
            self.assertIn("a", io.open(p, encoding="utf-8").read())


class TestFallbackAttribution(unittest.TestCase):
    def test_ignores_older_files_created_by_someone_else(self):
        with tempfile.TemporaryDirectory() as d:
            stale = os.path.join(d, "thumb_stale.jpg")
            with open(stale, "wb") as f:
                f.write(b"\xff\xd8\xff")
            old = time.time() - 3600
            os.utime(stale, (old, old))

            def fake_spawn(cmd, cwd):
                with open(os.path.join(d, "thumb_mine.jpg"), "wb") as f:
                    f.write(b"\xff\xd8\xffMINE")
                m = mock.MagicMock()
                m.returncode = 0
                m.communicate.return_value = (b"", b"")
                return m

            with mock.patch("ytdlp_core.spawn", side_effect=fake_spawn):
                path, err = core.fetch_thumb_via_ytdlp("https://a/x", {}, cache_dir=d)
        self.assertIsNone(err)
        self.assertTrue(path.endswith("thumb_mine.jpg"), path)

    def test_timeout_kills_and_reports(self):
        proc = mock.MagicMock()
        proc.communicate.side_effect = [subprocess.TimeoutExpired("yt-dlp", 60), (b"", b"")]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("ytdlp_core.spawn", return_value=proc), \
             mock.patch("ytdlp_core.kill_tree") as kill:
            path, err = core.fetch_thumb_via_ytdlp("https://a/x", {}, cache_dir=d)
        kill.assert_called_once_with(proc)
        self.assertEqual(path, "")
        self.assertIn("超时", err)


class TestWatchdogCleanup(unittest.TestCase):
    def test_generator_close_still_kills_process(self):
        class FakeOut:
            def __init__(self):
                self.chunks = [b"hello\n", b"", b""]

            def read1(self, n):
                return self.chunks.pop(0) if self.chunks else b""

        proc = mock.MagicMock()
        proc.stdout = FakeOut()
        proc.poll.return_value = None
        gen = core.iter_lines_watch(proc, stall_seconds=999)
        with mock.patch("ytdlp_core.kill_tree") as kill:
            next(gen)
            gen.close()          # 生成器已启动过，close() 才会走到 GeneratorExit 分支
        kill.assert_called_with(proc)

    def test_carriage_return_keeps_it_alive_without_spamming_lines(self):
        lines = []

        class FakeOut:
            def __init__(self):
                self.chunks = [b"[download]  1%\r", b"[download]  2%\r", b"done\n", b""]

            def read1(self, n):
                return self.chunks.pop(0)

        proc = mock.MagicMock()
        proc.stdout = FakeOut()
        proc.poll.return_value = 0
        for ln in core.iter_lines_watch(proc, stall_seconds=999):
            lines.append(ln)
        # \r 只续命、不产出行：整段最后才按 \n 切成一行，避免 ffmpeg 进度刷屏
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("done"), lines)


class TestProbeCmd(unittest.TestCase):
    def test_probe_is_single_url_and_keeps_access_args(self):
        cfg = dict(core.DEFAULT_CONFIG)
        cfg.update({"ua": "UA/2", "referer": "https://site/"})
        cmd = core.build_probe_cmd(cfg, "https://site/v", "plain")
        self.assertIn("--no-playlist", cmd)
        self.assertNotIn("--playlist-items", cmd)
        self.assertIn("UA/2", cmd)
        self.assertIn("https://site/", cmd)

    def test_update_switch_is_rejected(self):
        for bad in ("--update", "-U", "--postprocessor-args", "--parse-metadata=x"):
            self.assertTrue(core.is_unsafe_arg(bad), bad)
        self.assertFalse(core.is_unsafe_arg("--no-mtime"))


class TestFormatLine(unittest.TestCase):
    def test_storyboard_row_is_skipped(self):
        row = "sb0  mhtml 48x27  1 |      1.00KiB   storyboard"
        self.assertIsNone(core.parse_format_line(row))

    def test_progress_filename_with_pipe_survives(self):
        ev = core.parse_progress_line(
            "PROG|downloading|10|100|N/A|1MiB/s|00:10|a|b.mp4")
        self.assertEqual(ev["file"], "a|b.mp4")


if __name__ == "__main__":
    unittest.main()