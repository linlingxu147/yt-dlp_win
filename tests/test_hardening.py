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


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


class _FakeProc(object):
    """替身进程：communicate() 直接回预置输出，可选在「跑」的时候造个文件。"""

    def __init__(self, out=b"", rc=0, side=None):
        self._out = out
        self.returncode = rc
        self._side = side

    def communicate(self, timeout=None):
        if self._side:
            self._side()
        return self._out, b""


class TestStreamPreviewHelpers(unittest.TestCase):
    def test_headers_carry_referer_ua_and_cookie(self):
        h = core.stream_http_headers({
            "referer": "https://site/watch",
            "ua": "UA/1.0",
            "cookie_header": "k=v",
        })
        self.assertEqual(h, "Referer: https://site/watch\r\nUser-Agent: UA/1.0\r\nCookie: k=v\r\n")

    def test_headers_are_empty_without_any_value(self):
        self.assertEqual(core.stream_http_headers({}), "")
        self.assertEqual(core.stream_http_headers(None), "")

    def test_header_injection_is_scrubbed(self):
        # 换行被抹掉后整段退回成「一个 Referer 值」的一部分，不会多出头部行
        h = core.stream_http_headers({"referer": "https://a/\r\nX-Evil: 1"})
        self.assertEqual(h.count("\r\n"), 1)          # 只有结尾那一个
        self.assertTrue(h.startswith("Referer: "))
        line = h.split("\r\n")[0]
        self.assertNotIn("\r", line)
        self.assertNotIn("\n", line)

    def test_browser_can_play_only_native_containers(self):
        for url in ("https://cdn/a.mp4", "https://cdn/a.M4V?x=1", "https://cdn/a.webm",
                    "https://cdn/a.mp3", "https://cdn/a.m4a"):
            self.assertTrue(core.browser_can_play_url(url), url)
        for url in ("https://cdn/a.m3u8", "https://cdn/a.mpd", "https://cdn/a.flv",
                    "https://cdn/stream", "not a url"):
            self.assertFalse(core.browser_can_play_url(url), url)

    def test_duration_is_parsed_from_ffprobe_csv(self):
        with mock.patch.object(core, "spawn", return_value=_FakeProc(b"1800.00059\n")):
            self.assertAlmostEqual(core.stream_duration("https://cdn/a.m3u8"), 1800.00059)

    def test_duration_is_none_for_live_streams(self):
        with mock.patch.object(core, "spawn", return_value=_FakeProc(b"N/A\n")):
            self.assertIsNone(core.stream_duration("https://cdn/a.m3u8"))
        with mock.patch.object(core, "spawn", return_value=_FakeProc(b"", rc=1)):
            self.assertIsNone(core.stream_duration("https://cdn/a.m3u8"))

    def test_frame_requires_a_non_empty_file(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "t.png")
        with mock.patch.object(core, "spawn", return_value=_FakeProc()):
            # 进程说成功，但没产出文件 —— 不能当成有封面
            self.assertFalse(core.grab_stream_frame("https://cdn/a.m3u8", {}, out))
        with mock.patch.object(core, "spawn",
                               return_value=_FakeProc(side=lambda: _write(out, b"\x89PNG"))):
            self.assertTrue(core.grab_stream_frame("https://cdn/a.m3u8", {}, out))

    def test_clip_falls_back_to_video_only(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "t.mp4")
        first = _FakeProc(rc=1)                                     # -c copy 失败
        second = _FakeProc(side=lambda: _write(out, b"xxxx"))  # 只留视频轨成功
        with mock.patch.object(core, "spawn", side_effect=[first, second]) as sp:
            self.assertTrue(core.grab_stream_clip("https://cdn/a.m3u8", {}, out))
        self.assertEqual(sp.call_count, 2)
        self.assertIn("-an", sp.call_args_list[1][0][0])

    def test_stream_tool_timeout_kills_process(self):
        proc = _FakeProc()
        proc.communicate = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1))
        with mock.patch.object(core, "spawn", return_value=proc), \
             mock.patch.object(core, "kill_tree") as kt:
            ok, msg = core._run_stream_tool(["ffmpeg"], 1)
        self.assertFalse(ok)
        self.assertEqual(msg, "超时")
        kt.assert_called_once_with(proc)

    def test_missing_ffmpeg_is_reported_not_raised(self):
        with mock.patch.object(core, "spawn", side_effect=FileNotFoundError()):
            ok, msg = core._run_stream_tool(["ffmpeg"], 5)
        self.assertFalse(ok)
        self.assertIn("找不到 ffmpeg", msg)


class TestToolProbe(unittest.TestCase):
    """find_tool 找不到时原样返回名字，曾被当成「装好了」用；which_tool 才是可判定的。"""

    def test_which_tool_finds_bundled_binary(self):
        self.assertTrue(core.which_tool("ffmpeg.exe"))

    def test_which_tool_returns_empty_when_absent(self):
        self.assertEqual(core.which_tool("definitely-not-a-real-tool-xyz.exe"), "")


class TestSegmentRecognition(unittest.TestCase):
    """HLS/DASH 分片不能当正片下载，清单才是入口。"""

    def test_segments(self):
        for url in ("https://x/a/seg-1.ts", "https://x/a/seg-1.ts?token=1",
                    "https://x/v/1.m4s", "https://x/v/1.f4v", "https://x/v/1.m2ts",
                    "https://x/a/seg.ts#t=3"):
            self.assertTrue(core.looks_like_segment(url), url)

    def test_non_segments(self):
        for url in ("https://x/v.mp4", "https://x/p.m3u8", "https://x/v.mp4?x=.ts",
                    "https://x/mp4", ""):
            self.assertFalse(core.looks_like_segment(url), url)

    def test_manifests(self):
        for url in ("https://x/p.m3u8", "https://x/p.mpd", "https://x/p.m3u8?a=1"):
            self.assertTrue(core.looks_like_manifest(url), url)
        for url in ("https://x/v.mp4", "https://x/a.ts", ""):
            self.assertFalse(core.looks_like_manifest(url), url)


class TestConcurrencyDefaults(unittest.TestCase):
    """-N 4 是实测甜点（本机 4.88→6.36 MiB/s，再高反而回落）。"""

    def test_defaults(self):
        self.assertTrue(core.DEFAULT_CONFIG["conc"])
        self.assertEqual(str(core.DEFAULT_CONFIG["conc_n"]), "4")

    def test_schema3_migration_upgrades_untouched_config(self):
        out, _warns = core.coerce_config({"schema_version": 2, "conc": False, "conc_n": "8"})
        self.assertTrue(out["conc"])
        self.assertEqual(str(out["conc_n"]), "4")
        self.assertEqual(int(out["schema_version"]), core.CONFIG_SCHEMA_VERSION)

    def test_schema3_migration_keeps_user_tuned_value(self):
        out, _warns = core.coerce_config({"schema_version": 2, "conc": True, "conc_n": "12"})
        self.assertEqual(str(out["conc_n"]), "12")


class TestAria2cFallback(unittest.TestCase):
    """没装 aria2c 时以前会拼出必然失败的 --external-downloader。"""

    def test_missing_aria2c_drops_external_downloader_and_warns(self):
        warns = []
        with mock.patch.object(core, "which_tool", return_value=""):
            cmd = core.build_download_cmd(dict(core.DEFAULT_CONFIG, aria=True),
                                          ["https://x/v.mp4"], warn=warns)
        self.assertNotIn("--external-downloader", cmd)
        self.assertTrue(any("aria2c" in w for w in warns), warns)

    def test_present_aria2c_still_used(self):
        warns = []
        with mock.patch.object(core, "which_tool", return_value=r"C:\bin\aria2c.exe"):
            cmd = core.build_download_cmd(dict(core.DEFAULT_CONFIG, aria=True),
                                          ["https://x/v.mp4"], warn=warns)
        self.assertIn("--external-downloader", cmd)
        self.assertEqual(warns, [])


if __name__ == "__main__":
    unittest.main()