"""针对 2026-11 审阅修复的回归用例。

每条用例对应一处真实缺陷，失败时能直接指回问题而不是「某处行为变了」。
"""
import contextlib
import io
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "YtDlpGUI"))

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

    def __init__(self, out=b"", err=b"", rc=0, side=None):
        self._out = out
        self._err = err
        self.returncode = rc
        self._side = side

    def communicate(self, timeout=None):
        if self._side:
            self._side()
        return self._out, self._err


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

    def test_duration_is_parsed_from_ffmpeg_stderr(self):
        out = b"  Duration: 00:30:00.00, start: 0.000000, bitrate: 123 kb/s\n"
        with mock.patch.object(core, "spawn", return_value=_FakeProc(err=out)) as sp:
            self.assertAlmostEqual(core.stream_duration("https://cdn/a.m3u8"), 1800.0)
        self.assertIn("ffmpeg.exe", sp.call_args[0][0][0])
        self.assertNotIn("ffprobe", " ".join(sp.call_args[0][0]))

    def test_duration_supports_fractional_seconds(self):
        out = b"  Duration: 00:00:01.50, start: 0.000000, bitrate: 1 kb/s\n"
        with mock.patch.object(core, "spawn", return_value=_FakeProc(err=out)):
            self.assertAlmostEqual(core.stream_duration("https://cdn/a.m3u8"), 1.5)

    def test_duration_is_none_for_live_streams(self):
        live = b"  Duration: N/A, start: 0.000000, bitrate: N/A\n"
        with mock.patch.object(core, "spawn", return_value=_FakeProc(err=live, rc=1)):
            self.assertIsNone(core.stream_duration("https://cdn/a.m3u8"))
        with mock.patch.object(core, "spawn", return_value=_FakeProc(rc=1)):
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

    @unittest.skipUnless(core.which_tool("ffmpeg.exe"), "需要随包 ffmpeg（runtime/）")
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


class TestAudioRecognition(unittest.TestCase):
    """B 站 DASH 音频轨是单条 m4s（302xx），嗅探列表要能单独标注/隐藏。"""

    def test_bilibili_audio_streams(self):
        for url in ("https://x/43630595935-1-30280.m4s",
                    "https://x/43736042152_qe1-1-30216.m4s?token=1",
                    "https://x/a-30232.m4s#t=1",
                    "https://x/a_30251.m4s"):
            self.assertTrue(core.looks_like_audio(url), url)

    def test_plain_audio_extensions(self):
        for url in ("https://x/a.m4a", "https://x/a.MP3?x=1", "https://x/a.aac",
                    "https://x/a.flac", "https://x/a.wav", "https://x/a.opus"):
            self.assertTrue(core.looks_like_audio(url), url)

    def test_video_and_manifests_are_not_audio(self):
        for url in ("https://x/43630595935-1-100026.m4s",
                    "https://x/a-30080.m4s", "https://x/a.mp4",
                    "https://x/p.m3u8", "https://x/a.ts",
                    "https://data.bilibili.com/log/web?x=.m4s",
                    "", "not a url"):
            self.assertFalse(core.looks_like_audio(url), url)


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


class TestCookieDatabaseFallback(unittest.TestCase):
    """浏览器开着时 Chromium 会独占锁住自己的 Cookies 数据库（yt-dlp issue #7271）。

    真实报错：ERROR: Could not copy Chrome cookie database.
    读取必然失败，不能让整个下载跟着失败——先按「不带登录态」重试一次。
    """

    ERR = ("ERROR: Could not copy Chrome cookie database. See "
           "https://github.com/yt-dlp/yt-dlp/issues/7271 for more info")

    def test_copy_failure_is_recognised(self):
        self.assertTrue(core.cookie_copy_failed(self.ERR))
        self.assertFalse(core.cookie_copy_failed("HTTP Error 403: Forbidden"))

    def test_copy_failure_shows_a_dedicated_diagnosis(self):
        hits = core.diagnose(self.ERR)
        self.assertTrue(hits, hits)
        self.assertEqual(hits[0]["code"], "cookies")
        self.assertIn("Cookies", hits[0]["title"])

    def test_plan_drops_browser_cookies_exactly_once(self):
        name, extra = core.next_cookie_plan(self.ERR, set(), {"browser": "edge"})
        self.assertEqual(name, "no-cookie")
        self.assertEqual(extra["browser"], "（不使用）")
        self.assertIsNone(core.next_cookie_plan(self.ERR, {"no-cookie"}, {"browser": "edge"}))
        self.assertIsNone(core.next_cookie_plan(self.ERR, set(), {"browser": ""}))

    def test_plan_is_offered_before_the_network_ladder(self):
        # 这不是网络问题，is_net_failure 认不出来，所以必须单独判一次
        self.assertFalse(core.is_net_failure(self.ERR))

    def test_sniff_jar_wins_over_the_browser_database(self):
        with tempfile.TemporaryDirectory() as d:
            jar = os.path.join(d, "sniff_cookies.txt")
            with io.open(jar, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
            cfg = dict(core.DEFAULT_CONFIG, browser="edge", browser_profile="Default",
                       cookie=jar, _sniff_cookie_file=jar, outdir=d)
            cmd = core.build_download_cmd(cfg, ["https://x/v.mp4"])
        self.assertNotIn("--cookies-from-browser", cmd)
        i = cmd.index("--cookies")
        self.assertEqual(cmd[i + 1], jar)

    def test_browser_database_still_used_when_no_jar_was_sniffed(self):
        cfg = dict(core.DEFAULT_CONFIG, browser="edge", browser_profile="Default")
        cmd = core.build_download_cmd(cfg, ["https://x/v.mp4"])
        self.assertIn("--cookies-from-browser", cmd)
        self.assertIn("edge:Default", cmd)


class TestHostQueue(unittest.TestCase):
    """T5：host 下载走共享队列，逐链接拆任务（fake submit 注入，不起真实下载）。"""

    def make_host(self, tmp):
        import importlib
        host_mod = importlib.import_module("host")
        cfg = os.path.join(tmp, "config.json")
        core.save_config(cfg, {"outdir": tmp, "open_after": False, "queue_n": "2"})
        return host_mod.Host(cfg)

    def test_start_download_splits_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = self.make_host(tmp)
            calls = []
            h.queue.submit = lambda spec, exe: calls.append(spec) or len(calls)
            r = h.start_download(["http://e/a", "http://e/b"], {"outdir": tmp})
            self.assertTrue(r["ok"])
            self.assertEqual(len(r["task_ids"]), 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["urls"], ["http://e/a"])
            self.assertEqual(calls[1]["urls"], ["http://e/b"])
            self.assertEqual(calls[0]["source"], "gui")
            h.queue.close()


class TestAuxGate(unittest.TestCase):
    """T6：探测/排障/格式表共用 aux 单闸、与下载并行；更新 yt-dlp 独占。"""

    def make_host(self, tmp):
        import importlib
        host_mod = importlib.import_module("host")
        cfg = os.path.join(tmp, "config.json")
        core.save_config(cfg, {"outdir": tmp, "open_after": False})
        h = host_mod.Host(cfg)
        h.mark_ready()
        return host_mod, h

    def _active_download(self, h, tmp):
        """挂一个阻塞 executor 的下载任务，保证 any_active() 稳定为 True。"""
        gate = threading.Event()

        def exe(task):
            gate.wait(5)
            task.exit_code = 0

        tid = h.queue.submit({"urls": ["http://e/a"], "cfg": {"outdir": tmp},
                              "outdir": tmp, "source": "gui"}, exe)
        return gate, tid

    def _wait_done(self, h, tid, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            t = h.queue.get(tid)
            if t is not None and t.state not in ("queued", "running", "paused"):
                return t
            time.sleep(0.02)
        self.fail("任务 %s 未在超时内结束" % tid)

    def test_update_blocked_while_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            gate, tid = self._active_download(h, tmp)
            try:
                r = h.update_ytdlp("", "")
                self.assertFalse(r["ok"])
            finally:
                gate.set()
                self._wait_done(h, tid)
                h.queue.close()

    def test_update_blocked_while_aux_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            h.aux_kind = "detect"
            with mock.patch.object(mod, "spawn",
                                   side_effect=RuntimeError("测试禁止起进程")):
                r = h.update_ytdlp("", "")
            self.assertFalse(r["ok"])
            if h._worker is not None:
                h._worker.join(5)
            h.queue.close()

    def test_updating_blocks_aux_ops(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            h.updating = True
            cfg = dict(h.cfg, outdir=tmp)
            with mock.patch.object(mod, "spawn",
                                   side_effect=RuntimeError("测试禁止起进程")):
                for call in (lambda: h.detect(["https://x/v"], cfg),
                             lambda: h.troubleshoot(["https://x/v"], cfg),
                             lambda: h.fetch_formats("https://x/v", cfg)):
                    r = call()
                    self.assertFalse(r["ok"])
                    self.assertTrue(r.get("busy"))
                    if h._worker is not None:
                        h._worker.join(5)
                        h._worker = None
            h.queue.close()

    def test_aux_single_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            h.aux_kind = "detect"
            with mock.patch.object(mod, "spawn",
                                   side_effect=RuntimeError("测试禁止起进程")):
                r = h.fetch_formats("https://x/v", dict(h.cfg, outdir=tmp))
            self.assertFalse(r["ok"])
            self.assertTrue(r.get("busy"))
            if h._worker is not None:
                h._worker.join(5)
            h.queue.close()

    def test_detect_runs_parallel_with_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            gate, tid = self._active_download(h, tmp)

            class Proc:
                def poll(self): return 0
                def wait(self): return 0

            row = json.dumps({"id": "x", "title": "T", "webpage_url": "https://x/v"})

            def it(proc, abort=None, **kw):
                yield row

            try:
                with mock.patch.object(mod, "spawn", return_value=Proc()), \
                     mock.patch.object(mod, "iter_lines_watch", it):
                    r = h.detect(["https://x/v"], dict(h.cfg, outdir=tmp))
                    self.assertTrue(r["ok"], r)
                    h._worker.join(5)
                h.pump_once()
                self.assertEqual(len(h.results), 1)
                self.assertIsNone(h.aux_kind)      # 收尾只清 aux 闸
                self.assertTrue(h.queue.any_active())  # 下载不受影响
            finally:
                gate.set()
                self._wait_done(h, tid)
                h.queue.close()

    def test_done_callbacks_clear_own_gate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            h.aux_kind = "detect"
            h.updating = True
            h.on_detect_done(0)
            self.assertIsNone(h.aux_kind)
            self.assertTrue(h.updating)            # 不许顺手清掉更新闸
            h.updating = True
            h.on_trodone(1, "update")
            self.assertFalse(h.updating)
            h.aux_kind = "trouble"
            h.on_trodone(0, "trouble")
            self.assertIsNone(h.aux_kind)
            h.queue.close()

    def test_stop_handles_aux_update_and_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            self.assertFalse(h.stop())             # 空闲：无可停
            h.aux_kind = "detect"
            self.assertTrue(h.stop())              # 辅助无子进程也要能叫停
            self.assertTrue(h.user_stopped)
            h.user_stopped = False
            h.aux_kind = None
            h.updating = True
            self.assertTrue(h.stop())              # 镜像更新无子进程
            self.assertTrue(h.user_stopped)
            h.queue.close()

    def test_task_payload_truthful(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            h.aux_kind = "formats"
            p = h._task_payload()
            self.assertEqual(p["kind"], "formats")
            self.assertTrue(p["busy"])
            self.assertFalse(p["downloading"])
            h.aux_kind = None
            h.updating = True
            p = h._task_payload()
            self.assertEqual(p["kind"], "update")
            self.assertTrue(p["busy"])
            h.queue.close()


class TestHistoryLock(unittest.TestCase):
    """history_clear/history_list 与 record_history 共用一把锁，避免并发丢记录。"""

    def test_clear_waits_for_history_lock(self):
        import importlib
        host_mod = importlib.import_module("host")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            core.save_config(cfg, {"outdir": tmp, "open_after": False})
            h = host_mod.Host(cfg)
            done = threading.Event()

            def clear():
                h.history_clear()
                done.set()

            h._history_lock.acquire()
            t = threading.Thread(target=clear, daemon=True)
            t.start()
            try:
                self.assertFalse(done.wait(0.2))   # 锁未放行前不得清完
            finally:
                h._history_lock.release()
            t.join(5)
            self.assertTrue(done.is_set())
            self.assertEqual(h.history, [])
            h.queue.close()


class TestTaskControls(unittest.TestCase):
    """T7：暂停/继续/取消 API；退出停止保留部分下载记 stopped，取消清理记 canceled。"""

    def make_host(self, tmp, queue_n="1"):
        import importlib
        host_mod = importlib.import_module("host")
        cfg = os.path.join(tmp, "config.json")
        core.save_config(cfg, {"outdir": tmp, "open_after": False, "queue_n": queue_n})
        h = host_mod.Host(cfg)
        h.mark_ready()
        return host_mod, h

    def _blocking_exec(self, name="v.mp4", started=None):
        """模拟 run_process：轮询 pause/cancel 请求提前返回，不起真实子进程。"""
        def exe(task):
            task.pstate["file"] = name
            if started is not None:
                started.set()
            end = time.time() + 5
            while time.time() < end:
                if task.pause_requested or task.cancel_requested:
                    task.exit_code = -1
                    return
                time.sleep(0.02)
            task.exit_code = 0
        return exe

    def _wait_state(self, h, tid, states, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            t = h.queue.get(tid)
            if t is not None and t.state in states:
                return t
            time.sleep(0.02)
        self.fail("任务 %s 未在超时内到达 %s" % (tid, states))

    def _submit(self, h, tmp, url="http://e/a", exe=None):
        return h.queue.submit({"urls": [url], "cfg": {"outdir": tmp},
                               "outdir": tmp, "source": "gui"},
                              exe or self._blocking_exec())

    def test_pause_resume_cancel_api(self):
        import importlib
        api_mod = importlib.import_module("api")
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp, queue_n="2")
            a = api_mod.Api(h)
            tid = self._submit(h, tmp)
            try:
                self._wait_state(h, tid, ("running",))
                self.assertTrue(a.pause_task(tid)["ok"])
                self._wait_state(h, tid, ("paused",))
                self.assertTrue(a.resume_task(tid)["ok"])
                self._wait_state(h, tid, ("running",))
                self.assertTrue(a.cancel_task(tid)["ok"])
                self._wait_state(h, tid, ("canceled",))
                self.assertFalse(a.pause_task(9999)["ok"])
                self.assertFalse(a.resume_task(tid)["ok"])
            finally:
                h.queue.stop_all()
                h.queue.close()

    def test_stop_all_api_counts_and_keeps_partials(self):
        import importlib
        api_mod = importlib.import_module("api")
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            a = api_mod.Api(h)
            part = os.path.join(tmp, "v.mp4.part")
            _write(part, b"x")
            started = threading.Event()
            tid = self._submit(h, tmp, exe=self._blocking_exec(started=started))
            self.assertTrue(started.wait(5))
            tid2 = self._submit(h, tmp, url="http://e/b")
            try:
                r = a.stop_all()
                self.assertTrue(r["ok"])
                self.assertEqual(r["stopped"], 2)
                self._wait_state(h, tid, ("canceled",))
                self.assertTrue(os.path.isfile(part))      # 全部停止＝可续传，不清残留
                recs = [r for r in h.history if r.get("task_id") == tid]
                self.assertEqual(len(recs), 1)
                self.assertEqual(recs[0]["status"], "stopped")
                self.assertFalse(any(r.get("task_id") == tid2 for r in h.history))
            finally:
                h.queue.close()

    def test_shutdown_keeps_partials_and_records_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            part = os.path.join(tmp, "v.mp4.part")
            _write(part, b"x")
            started = threading.Event()
            tid = self._submit(h, tmp, exe=self._blocking_exec(started=started))
            self.assertTrue(started.wait(5))
            tid2 = self._submit(h, tmp, url="http://e/b")   # queue_n=1 → 排队
            h.shutdown()
            self._wait_state(h, tid, ("canceled",))
            self.assertTrue(os.path.isfile(part))           # 退出保留 .part 供继续下载
            recs = [r for r in h.history if r.get("task_id") == tid]
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["status"], "stopped")
            self.assertEqual(recs[0]["urls"], ["http://e/a"])
            self.assertEqual(recs[0]["outdir"], tmp)
            self.assertEqual(recs[0]["source"], "gui")
            # 排队任务直接丢弃：不记历史
            self.assertEqual(h.queue.get(tid2).state, "canceled")
            self.assertFalse(any(r.get("task_id") == tid2 for r in h.history))

    def test_api_cancel_purges_partials_and_records_canceled(self):
        import importlib
        api_mod = importlib.import_module("api")
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            a = api_mod.Api(h)
            part = os.path.join(tmp, "v.mp4.part")
            _write(part, b"x")
            started = threading.Event()
            tid = self._submit(h, tmp, exe=self._blocking_exec(started=started))
            self.assertTrue(started.wait(5))
            self.assertTrue(a.cancel_task(tid)["ok"])
            self._wait_state(h, tid, ("canceled",))
            self.assertFalse(os.path.exists(part))          # 取消必须清残留
            recs = [r for r in h.history if r.get("task_id") == tid]
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["status"], "canceled")
            self.assertEqual(recs[0]["urls"], ["http://e/a"])
            h.queue.close()

    def test_cancel_finished_task_returns_false_no_history(self):
        import importlib
        api_mod = importlib.import_module("api")
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            a = api_mod.Api(h)

            def finish(task):
                task.exit_code = 0
            tid = self._submit(h, tmp, exe=finish)
            self._wait_state(h, tid, ("done",))
            before = len(h.history)
            self.assertFalse(a.cancel_task(tid)["ok"])      # 已结束 → 取消失败
            self.assertEqual(len(h.history), before)        # 不得多记假 canceled
            self.assertFalse(any(r.get("task_id") == tid for r in h.history))
            self.assertFalse(a.cancel_task(9999)["ok"])
            h.queue.close()

    def test_spawn_failure_on_canceled_task_records_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            t = mod.ytdlp_queue.Task(1, {
                "urls": ["http://e/a"], "outdir": tmp,
                "cfg": dict(core.DEFAULT_CONFIG, outdir=tmp)})
            t.cancel_requested = True   # 取消已记历史，启动失败分支不得再走 _task_done
            with mock.patch.object(mod.ytdlp_queue, "run_process",
                                   return_value=(None, "", {})):
                h._exec_download(t)
            self.assertFalse(any(r.get("task_id") == t.id for r in h.history))
            h.queue.close()

    def test_ext_cookie_cleaned_on_pause_early_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            jar = os.path.join(tmp, "sniff_cookies.txt")
            _write(jar, b"# Netscape HTTP Cookie File\n")
            for flag in ("pause_requested", "cancel_requested"):
                _write(jar, b"# Netscape HTTP Cookie File\n")
                t = mod.ytdlp_queue.Task(1, {
                    "urls": ["http://e/a"], "outdir": tmp,
                    "cfg": dict(core.DEFAULT_CONFIG, outdir=tmp),
                    "ext_cookie_file": jar})
                setattr(t, flag, True)
                with mock.patch.object(mod.ytdlp_queue, "run_process",
                                       return_value=(1, "", {})):
                    h._exec_download(t)
                self.assertFalse(os.path.exists(jar), flag)
            h.queue.close()

    def test_update_run_always_emits_trodone(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, h = self.make_host(tmp)
            h.updating = True
            with mock.patch.object(mod, "spawn",
                                   side_effect=RuntimeError("测试禁止起进程")), \
                 mock.patch.object(h, "_update_via_mirror",
                                   side_effect=RuntimeError("镜像初始化炸了")):
                h._update_run(["yt-dlp", "-U"], "", "https://mirror.example")
            evs = []
            while not h.q.empty():
                evs.append(h.q.get_nowait())
            self.assertTrue(any(e.get("type") == "trodone" and e.get("task") == "update"
                                for e in evs), evs)
            h.queue.close()


if __name__ == "__main__":
    unittest.main()