# -*- coding: utf-8 -*-
"""ytdlp_core 的回归测试（标准库 unittest，无第三方依赖）。

运行：
    E:\\python\\python.exe -m unittest discover -s tests -v
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ytdlp_core as core  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = None


def load_web_server():
    """按需加载网页版服务端模块（只取纯函数，不起端口）。"""
    global _SERVER
    if _SERVER is None:
        spec = importlib.util.spec_from_file_location(
            "ytdlp_web_server_under_test", os.path.join(ROOT, "YtDlpWeb", "server.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _SERVER = mod
    return _SERVER


class TestHeaders(unittest.TestCase):
    def test_glued_headers_are_split(self):
        # 就是这次在 YtDlpGUI\\config.json 里发现的那份脏数据
        dirty = ("Origin: https://ex.com\nX-Token: abcOrigin: https://ex.com\nX-Token: abc")
        self.assertEqual(core.normalize_headers(dirty),
                         "Origin: https://ex.com\nX-Token: abc")

    def test_value_with_url_is_not_split(self):
        self.assertEqual(core.normalize_headers("Referer: https://a.com/x?y=1"),
                         "Referer: https://a.com/x?y=1")

    def test_duplicate_headers_keep_last(self):
        self.assertEqual(core.normalize_headers("X-A: 1\nX-A: 2"), "X-A: 2")

    def test_zero_width_and_crlf_removed(self):
        self.assertEqual(core.normalize_headers("X-A:\u200b 1\r\n\r\nX-B: 2"),
                         "X-A: 1\nX-B: 2")

    def test_junk_lines_dropped(self):
        self.assertEqual(core.normalize_headers("no colon here\nX-A: 1"), "X-A: 1")

    def test_cookie_header_survives(self):
        self.assertEqual(core.normalize_headers("Cookie: a=b; c=d"), "Cookie: a=b; c=d")


class TestUrls(unittest.TestCase):
    def test_bare_domain_gets_scheme(self):
        good, bad = core.sanitize_urls(["www.example.com/v/1"])
        self.assertEqual(good, ["https://www.example.com/v/1"])
        self.assertEqual(bad, [])

    def test_dash_prefixed_is_rejected(self):
        good, bad = core.sanitize_urls(["--exec", "rm -rf /", "https://a.com/x"])
        self.assertEqual(good, ["https://a.com/x"])
        self.assertEqual(bad, ["--exec", "rm -rf /"])

    def test_comments_and_blanks_dropped(self):
        good, bad = core.sanitize_urls(["", "   ", "# 注释", "https://a.com"])
        self.assertEqual(good, ["https://a.com"])
        self.assertEqual(bad, [])

    def test_html_entities_and_zero_width(self):
        self.assertEqual(core.clean_media_url("https://a.com/x?a=1&amp;b=2\u200b"),
                         "https://a.com/x?a=1&b=2")


class TestUnsafeArgs(unittest.TestCase):
    def test_unsafe_detected(self):
        self.assertEqual(core.unsafe_arg_in("--exec echo hi"), "--exec")
        self.assertEqual(core.unsafe_arg_in("--proxy http://127.0.0.1:7890"), "")

    def test_safe_extra_args_drops_value_too(self):
        self.assertEqual(core.safe_extra_args("--exec calc --no-mtime"), ["--no-mtime"])


class TestCommandBuilders(unittest.TestCase):
    def _cfg(self, **kw):
        cfg = core.default_config()
        cfg["yt_dlp_path"] = "yt-dlp.exe"
        cfg["outdir"] = tempfile.gettempdir()
        cfg.update(kw)
        return cfg

    def test_progress_template_is_used(self):
        cmd = core.build_download_cmd(self._cfg(), ["https://a.com/v"])
        self.assertIn("--progress-template", cmd)
        self.assertIn(core.PROGRESS_TEMPLATE, cmd)
        # --print 会隐含 --quiet/--simulate，绝不能出现在下载命令里
        self.assertNotIn("--print", cmd)

    def test_urls_separated_by_dashdash(self):
        cmd = core.build_download_cmd(self._cfg(), ["https://a.com/v", "-x"])
        self.assertEqual(cmd[-2:], ["--", "https://a.com/v"])

    def test_unsafe_extra_is_filtered(self):
        cmd = core.build_download_cmd(self._cfg(extra="--exec calc --no-mtime"), ["https://a.com"])
        self.assertIn("--no-mtime", cmd)
        self.assertNotIn("--exec", cmd)
        self.assertNotIn("calc", cmd)

    def test_conc_is_clamped(self):
        cmd = core.build_download_cmd(self._cfg(conc=True, conc_n="999"), ["https://a.com"])
        self.assertEqual(cmd[cmd.index("-N") + 1], "32")

    def test_access_args_include_headers_and_cookies(self):
        cfg = self._cfg(headers="Origin: https://a.com", browser="edge", browser_profile="Default",
                        ua="UA", referer="https://a.com/", retries="10")
        cmd = core.add_access_args([], cfg)
        self.assertIn("--cookies-from-browser", cmd)
        self.assertIn("edge:Default", cmd)
        self.assertIn("--add-header", cmd)
        self.assertIn("Origin: https://a.com", cmd)

    def test_formats_cmd_has_access_args(self):
        cfg = self._cfg(ua="UA", referer="https://a.com/", browser="（不使用）")
        cmd = core.build_formats_cmd(cfg, "https://a.com/v")
        self.assertIn("--user-agent", cmd)
        self.assertIn("--referer", cmd)
        self.assertEqual(cmd[-1], "https://a.com/v")

    def test_detect_cmd_has_ignore_errors(self):
        cmd = core.build_detect_cmd(self._cfg(proxy="http://127.0.0.1:7890"), ["https://a.com"])
        self.assertIn("-i", cmd)
        self.assertIn("--proxy", cmd)


class TestProgressParsing(unittest.TestCase):
    def test_template_line(self):
        line = ("PROG|downloading|524288|1048576|NA|262144.0|2|"
                "C:/d/My Video.f137.mp4")
        p = core.parse_progress_line(line)
        self.assertEqual(p["pct"], 50.0)
        self.assertEqual(p["size"], "1.0 MB")
        self.assertEqual(p["speed"], "256.0 KB/s")
        self.assertEqual(p["eta"], "2")
        self.assertEqual(p["file"], "My Video.mp4")

    def test_template_unknown_total_with_estimate(self):
        p = core.parse_progress_line("PROG|downloading|500|NA|1000|NA|NA|a.mp4")
        self.assertEqual(p["pct"], 50.0)

    def test_finished_marks_100(self):
        p = core.parse_progress_line("PROG|finished|1000|NA|NA|NA|NA|a.mp4")
        self.assertEqual(p["pct"], 100.0)

    def test_legacy_regex_fallback(self):
        p = core.parse_progress_line("[download]  12.5% of  10.00MiB at 1.00MiB/s ETA 00:10")
        self.assertEqual(p["pct"], 12.5)
        self.assertEqual(p["speed"], "1.00MiB/s")

    def test_non_progress_line(self):
        self.assertIsNone(core.parse_progress_line("[download] Destination: C:/d/a.mp4"))

    def test_progress_event_tracks_files(self):
        st = core.new_progress_state()
        # 第一次看到文件名时回 "file"，界面据此刷新「正在下载」标签
        self.assertEqual(core.progress_event(st, "PROG|downloading|1|2|NA|NA|NA|a.mp4")["kind"], "file")
        self.assertEqual(core.progress_event(st, "PROG|finished|2|2|NA|NA|NA|a.mp4")["kind"], "progress")
        self.assertEqual(st["done"], {"a"})
        self.assertEqual(core.progress_event(st, "[download] Destination: C:/d/b.m4a")["kind"], "file")
        self.assertEqual(st["file"], "b.m4a")

    def test_item_line(self):
        st = core.new_progress_state()
        ev = core.progress_event(st, "[download] Downloading item 3 of 9")
        self.assertEqual(ev["kind"], "item")
        self.assertEqual((st["item_i"], st["item_total"]), (3, 9))

    def test_classify(self):
        self.assertEqual(core.classify_line("ERROR: boom"), "err")
        self.assertEqual(core.classify_line("[Merger] Merging formats"), "ok")
        self.assertIsNone(core.classify_line("plain"))


class TestDiagnose(unittest.TestCase):
    def test_403_hits(self):
        hits = core.diagnose("ERROR: unable to download webpage: HTTP Error 403: Forbidden")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["code"], "403")
        self.assertEqual(hits[0]["fix"], {"ua": True, "referer": True})

    def test_drm_and_unknown(self):
        self.assertEqual(core.diagnose("Widevine DRM protected")[0]["code"], "drm")
        self.assertEqual(core.diagnose("ERROR: something odd")[0]["code"], "unknown")

    def test_empty(self):
        self.assertEqual(core.diagnose(""), [])


class TestNetPlans(unittest.TestCase):
    def test_403_is_not_retried(self):
        self.assertFalse(core.is_net_failure("HTTP Error 403: Forbidden"))

    def test_400_retries_are_capped(self):
        with mock.patch.object(core, "impersonate_available", return_value=True):
            cfg = {"referer": "https://a.com/", "headers": ""}
            self.assertEqual(core.next_net_plan("HTTP Error 400", set(), cfg)[0], "no-referer")
            self.assertEqual(core.next_net_plan("HTTP Error 400", {"no-referer"}, cfg)[0], "origin")
            nxt = core.next_net_plan("HTTP Error 400", {"no-referer", "origin"}, cfg)
            self.assertIn(nxt[0], ("impersonate", "chunk"))

    def test_reset_goes_to_impersonate(self):
        with mock.patch.object(core, "impersonate_available", return_value=True):
            name, plan = core.next_net_plan("ConnectionResetError: [WinError 10054]", set(), {})
            self.assertEqual(name, "impersonate")
            self.assertEqual(plan["impersonate"], "Chrome")

    def test_without_curl_cffi_skips_impersonate(self):
        with mock.patch.object(core, "impersonate_available", return_value=False):
            name, plan = core.next_net_plan("ConnectionResetError: [WinError 10054]", set(), {})
            self.assertEqual(name, "chunk")
            self.assertNotIn("impersonate", plan)

    def test_400_origin_comes_from_the_download_url(self):
        """没填 Referer 时，Origin 只能从这次要下的地址本身推，不能凭空造。"""
        with mock.patch.object(core, "impersonate_available", return_value=True):
            cfg = {"referer": "", "headers": ""}
            name, plan = core.next_net_plan(
                "ERROR: HTTP Error 400: Bad Request", {"no-referer"}, cfg,
                url="https://cdn.example.com/v/1?sign=abc")
        self.assertEqual(name, "origin")
        self.assertIn("Origin: https://cdn.example.com", plan["headers"])
        self.assertNotIn("cdn.example.com/v/1", plan["headers"])   # 只取站点根，不带路径

    def test_400_without_referer_skips_no_referer(self):
        with mock.patch.object(core, "impersonate_available", return_value=True):
            name, plan = core.next_net_plan("ERROR: HTTP Error 400", set(),
                                            {"referer": "", "headers": ""},
                                            url="https://a.com/v")
            self.assertEqual(name, "origin")
            # 连地址都拿不到站点根时，就别再折腾请求头了
            none = core.next_net_plan("ERROR: HTTP Error 400", set(),
                                      {"referer": "", "headers": ""}, url="not-a-url")
            self.assertIn(none[0], ("impersonate", "chunk"))

    def test_400_keeps_existing_headers(self):
        with mock.patch.object(core, "impersonate_available", return_value=True):
            _, plan = core.next_net_plan("ERROR: HTTP Error 400", {"no-referer"},
                                         {"referer": "https://a.com/", "headers": "X-A: 1"},
                                         url="https://b.com/v")
        self.assertTrue(plan["headers"].startswith("X-A: 1"))
        self.assertIn("Origin: https://a.com", plan["headers"])


class TestFormatLine(unittest.TestCase):
    def test_row(self):
        row = core.parse_format_line(
            "137          mp4   1920x1080   1080p  2000k , avc1.640028  2.5MiB video only")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "137")
        self.assertEqual(row["ext"], "mp4")

    def test_heading_is_skipped(self):
        self.assertIsNone(core.parse_format_line("ID  EXT   RESOLUTION FPS"))
        self.assertIsNone(core.parse_format_line("------"))
        self.assertIsNone(core.parse_format_line(""))


class TestConfigIO(unittest.TestCase):
    def test_roundtrip_and_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            cfg = core.default_config()
            cfg["headers"] = "Origin: https://a.com\nX-Token: 1Origin: https://a.com"
            self.assertTrue(core.save_config(path, cfg))
            self.assertTrue(os.path.isfile(path + ".tmp") is False)
            back = core.load_config(path)
            self.assertEqual(back["headers"], "Origin: https://a.com\nX-Token: 1")
            self.assertEqual(back["schema_version"], core.CONFIG_SCHEMA_VERSION)

    def test_legacy_open_key_migrates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"open": False}, f)
            warns = []
            cfg = core.load_config(path, warn=warns.append)
            self.assertFalse(cfg["open_after"])
            self.assertTrue(any("open" in w for w in warns))

    def test_broken_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            warns = []
            cfg = core.load_config(path, warn=warns.append)
            self.assertEqual(cfg["mode"], core.MODE_KEYS[0])
            self.assertTrue(warns)

    def test_save_failure_is_reported(self):
        warns = []
        ok = core.save_config(os.path.join("\0bad", "config.json"),
                              core.default_config(), warn=warns.append)
        self.assertFalse(ok)
        self.assertTrue(warns)


class TestMisc(unittest.TestCase):
    def test_apply_probe_fix(self):
        cfg = core.default_config()
        touched = core.apply_probe_fix(cfg, "https://a.com/v/1", {"ua": True, "referer": True})
        self.assertEqual(cfg["referer"], "https://a.com/")
        self.assertIn("User-Agent", touched)

    def test_command_text_quotes(self):
        self.assertEqual(core.command_text(["a", "b c"]), 'a "b c"')

    def test_referer_and_origin(self):
        self.assertEqual(core.referer_of("https://a.com/v"), "https://a.com/")
        self.assertEqual(core.origin_of("https://a.com/v"), "https://a.com")
        self.assertEqual(core.referer_of(""), "")

    def test_context_menu_labels_present(self):
        # 诊断规则的文案是给用户看的，别被误删
        for pat, code, title, advice, fix in core.DIAG_RULES:
            self.assertTrue(title and advice and isinstance(fix, dict))

    def test_iter_lines_blank_handling(self):
        """默认跳过空行；格式表/更新日志用 keep_blank=True 保持行数一致。"""
        class FakeProc:
            def __init__(self, lines):
                self.stdout = [ln.encode("utf-8") for ln in lines]

        lines = ["a\n", "\n", "b\r\n"]
        self.assertEqual(list(core.iter_lines(FakeProc(lines))), ["a", "b"])
        self.assertEqual(list(core.iter_lines(FakeProc(lines), keep_blank=True)), ["a", "", "b"])


class TestUrlScrubbing(unittest.TestCase):
    """诊断看的是报错措辞，不是链接本身：地址里的 ?region= / &timeout= 曾把正常下载判成故障。"""

    def test_scrub_replaces_urls(self):
        self.assertEqual(core.scrub_urls("see https://a.com/v?x=1 done"), "see  <url>  done")

    def test_query_params_do_not_trigger_rules(self):
        self.assertEqual(core.diagnose(
            "[info] Extracting URL: https://a.com/watch?region=1&timeout=30&referer=x"), [])

    def test_paren_wrapped_url_is_scrubbed(self):
        # URL_RE 不吞右括号，括号后仍有内容时不能误判成防盗链
        self.assertEqual(
            core.diagnose("[download] 100% of 1.0MiB (https://a.com/v?referer=x) ETA 00:00"), [])

    def test_real_errors_still_hit(self):
        self.assertEqual(core.diagnose(
            "ERROR: unable to download https://a.com/v: HTTP Error 403: Forbidden")[0]["code"], "403")

    def test_is_net_failure_ignores_url_text(self):
        self.assertFalse(core.is_net_failure("[download] Destination: https://a.com/v?timeout=30"))
        self.assertTrue(core.is_net_failure("ERROR: [Errno 10054] 远程主机强迫关闭了一个现有的连接"))
        self.assertFalse(core.is_net_failure("ERROR: HTTP Error 403: Forbidden https://a.com/x"))


class TestDiagnoseFalsePositives(unittest.TestCase):
    def test_version_notice_is_not_bot_check(self):
        # "Confirm you are on the latest version" 里含 confirm/bot 附近的词，曾经误报人机验证
        out = core.diagnose("ERROR: If you are using a VPN, confirm you are on the latest version")
        self.assertNotIn("bot", [h["code"] for h in out])

    def test_region_query_is_not_geo(self):
        out = core.diagnose("ERROR: fragment not found https://a.com/m3u8?region=US&geo=1")
        self.assertNotIn("geo", [h["code"] for h in out])

    def test_400_has_its_own_rule(self):
        hit = core.diagnose("ERROR: HTTP Error 400: Bad Request")[0]
        self.assertEqual(hit["code"], "400")
        self.assertTrue(hit["advice"])

    def test_max_hits_is_respected(self):
        blob = "\n".join(["HTTP Error 403: Forbidden", "HTTP Error 429", "timed out", "has DRM"])
        self.assertLessEqual(len(core.diagnose(blob, max_hits=2)), 2)


class TestModeKeys(unittest.TestCase):
    def test_keys_are_ascii_and_unique(self):
        self.assertEqual(len(set(core.MODE_KEYS)), len(core.MODE_KEYS))
        for k in core.MODE_KEYS:
            self.assertTrue(k.isascii() and k.replace("_", "").isalnum())
        self.assertTrue(set(core.MODE_FMT) <= set(core.MODE_KEYS))

    def test_labels_line_up_with_keys(self):
        self.assertEqual([k for k, _ in core.MODE_LABELS], core.MODE_KEYS)
        self.assertEqual([v for _, v in core.MODE_LABELS], core.MODE_CHOICES)
        self.assertEqual(len(core.MODE_NAME), len(core.MODE_KEYS))

    def test_legacy_labels_migrate(self):
        for key, label in core.MODE_LABELS:
            self.assertEqual(core.mode_key(label), key)

    def test_unknown_falls_back_to_best(self):
        self.assertEqual(core.mode_key(""), "best")
        self.assertEqual(core.mode_key("只有画质"), "best")
        self.assertEqual(core.mode_key(None), "best")

    def test_coerce_config_migrates_chinese_mode(self):
        cfg, _ = core.coerce_config({"mode": "仅音频（转 MP3）"})
        self.assertEqual(cfg["mode"], "audio_mp3")

    def test_build_cmd_per_mode(self):
        base = dict(core.default_config(), fmt="", merge="mp4", sub=False,
                    thumb=False, meta=False)
        for key in core.MODE_KEYS:
            cmd = core.build_download_cmd(dict(base, mode=key), ["https://a.com/v"])
            text = " ".join(cmd)
            want = core.MODE_FMT.get(key, "")
            if want:
                self.assertIn("-f " + want, text, key)
            self.assertEqual("-x" in cmd, key == "audio_mp3", key)
            self.assertEqual("--merge-output-format" in cmd, key == "best", key)
            self.assertNotIn("最佳画质", text)
        # 自定义模式用用户手填的表达式，不套模式默认值
        cmd = core.build_download_cmd(dict(base, mode="custom", fmt="bv*[height<=720]"),
                                      ["https://a.com/v"])
        self.assertIn("-f bv*[height<=720]", " ".join(cmd))


class TestTypeCoercion(unittest.TestCase):
    def test_int_fields(self):
        cfg, w = core.coerce_config({"retries": "十次", "conc_n": "0", "socket_timeout": "-5"})
        self.assertEqual(cfg["retries"], "")
        self.assertEqual(cfg["conc_n"], str(core.DEFAULT_CONFIG["conc_n"]))
        self.assertEqual(cfg["socket_timeout"], "")
        self.assertEqual(len([x for x in w if "重试次数" in x or "分片并发" in x
                              or "超时秒数" in x]), 3)

    def test_int_fields_valid_values_kept(self):
        cfg, w = core.coerce_config({"retries": "5", "conc_n": "16", "socket_timeout": "30"})
        self.assertEqual((cfg["retries"], cfg["conc_n"], cfg["socket_timeout"]), ("5", "16", "30"))
        self.assertEqual(w, [])

    def test_out_of_range_reports_warning(self):
        cfg, w = core.coerce_config({"conc_n": "9999"})
        self.assertEqual(cfg["conc_n"], core.DEFAULT_CONFIG["conc_n"])
        self.assertTrue(any("分片并发" in x for x in w))

    def test_bytes_fields(self):
        self.assertEqual(core.coerce_config({"rate": "2M"})[0]["rate"], "2M")
        self.assertEqual(core.coerce_config({"rate": "500K-2M"})[0]["rate"], "500K-2M")
        cfg, w = core.coerce_config({"rate": "2 MB"})
        self.assertEqual(cfg["rate"], "")
        self.assertTrue(any("限速" in x for x in w))

    def test_string_false_is_real_false(self):
        # 手改配置写成 "false"：非空字符串恒为真，勾选框就关不掉了
        cfg, _ = core.coerce_config({"sub": "false", "net_hard": "0", "aria": "否"})
        for k in ("sub", "net_hard", "aria"):
            self.assertIs(cfg[k], False)

    def test_true_words(self):
        cfg, _ = core.coerce_config({"sub": "TRUE", "net_hard": "1", "aria": "开"})
        for k in ("sub", "net_hard", "aria"):
            self.assertIs(cfg[k], True)

    def test_gibberish_bool_reverts_to_default(self):
        cfg, w = core.coerce_config({"net_hard": "maybe"})
        self.assertIs(cfg["net_hard"], core.DEFAULT_CONFIG["net_hard"])
        self.assertTrue(any("net_hard" in x for x in w))

    def test_silent_when_no_sink(self):
        merged = core.default_config()
        merged["retries"] = "十次"
        core.coerce_types(merged)          # 不传 warnings 也不能抛
        self.assertEqual(merged["retries"], "")

    def test_save_reports_what_it_changed(self):
        """保存时悄悄改掉的值必须说一声，否则用户只会觉得「填了没生效」。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            w = []
            self.assertTrue(core.save_config(
                p, {"mode": "仅音频（转 MP3）", "retries": "十次", "rate": "2 MB"}, warn=w.append))
            with open(p, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["mode"], "audio_mp3")
            self.assertEqual(on_disk["retries"], "")
            self.assertEqual(on_disk["rate"], "")
            self.assertEqual(len([x for x in w if "重试次数" in x or "限速" in x]), 2)


class TestPathInside(unittest.TestCase):
    def test_inside_and_equal(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(core.path_inside(os.path.join(d, "a", "b.mp4"), d))
            self.assertTrue(core.path_inside(d, d))

    def test_sibling_prefix_is_not_inside(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(core.path_inside(d + "s", d))

    def test_traversal_is_not_inside(self):
        with tempfile.TemporaryDirectory() as d:
            outer = os.path.join(os.path.dirname(d), "definitely-outside", "a.txt")
            self.assertFalse(core.path_inside(outer, d))
            self.assertFalse(core.path_inside(os.path.join(d, "..", "x.txt"), d))

    def test_mixed_separators(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(core.path_inside(d.replace("\\", "/") + "/a.mp4", d))

    def test_garbage_is_refused(self):
        self.assertFalse(core.path_inside(None, None))


class TestAtomicWrite(unittest.TestCase):
    def test_backup_and_no_leftover_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            core.atomic_write_text(p, '{"v": 1}')
            core.atomic_write_text(p, '{"v": 2}')
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"v": 2}')
            with open(p + ".bak", encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"v": 1}')
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])

    def test_backup_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            core.atomic_write_text(p, "1")
            core.atomic_write_text(p, "2", backup=False)
            self.assertFalse(os.path.exists(p + ".bak"))

    def test_creates_missing_parent(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "deep", "a.txt")
            core.atomic_write_text(p, "x")
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "x")


class TestCookieHeaderArg(unittest.TestCase):
    def test_long_cookie_is_not_truncated(self):
        cfg = core.default_config()
        cfg["cookie_header"] = "sid=" + ("a" * 5000) + "; t=1"
        cmd = []
        core.add_access_args(cmd, cfg)
        vals = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-header"]
        cookies = [v for v in vals if v.lower().startswith("cookie:")]
        self.assertEqual(len(cookies), 1)
        self.assertIn("a" * 5000, cookies[0])

    def test_redundant_prefix_is_not_doubled(self):
        cfg = core.default_config()
        cfg["cookie_header"] = "Cookie: sid=1"
        cmd = []
        core.add_access_args(cmd, cfg)
        self.assertIn("Cookie: sid=1", cmd)

    def test_control_chars_cannot_forge_headers(self):
        cfg = core.default_config()
        cfg["cookie_header"] = "sid=1\r\nX-evil: 1"
        cmd = []
        core.add_access_args(cmd, cfg)
        vals = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-header"]
        self.assertEqual(len(vals), 1)
        self.assertNotIn("\r", vals[0])
        self.assertNotIn("\n", vals[0])

    def test_scrub_header_value_limit(self):
        self.assertEqual(core.scrub_header_value("a\tb\r\n"), "a b")
        self.assertEqual(len(core.scrub_header_value("x" * 300, limit=100)), 100)


class TestWatchdog(unittest.TestCase):
    class _Proc:
        def __init__(self, chunks):
            self.stdout = iter(chunks)

    def test_all_lines_and_no_kill(self):
        with mock.patch.object(core, "kill_tree") as kt:
            why = {}
            out = list(core.iter_lines_watch(self._Proc([b"a\n", b"b\n"]), why=why))
            self.assertEqual(out, ["a", "b"])
            self.assertEqual(why["reason"], "normal")
            kt.assert_not_called()

    def test_abort_stops_before_yield_and_kills(self):
        calls = []

        def gone():
            calls.append(1)
            return True

        with mock.patch.object(core, "kill_tree") as kt:
            why = {}
            out = list(core.iter_lines_watch(self._Proc([b"a\n", b"b\n"]), abort=gone, why=why))
            self.assertEqual(out, [])
            self.assertEqual(why["reason"], "client")
            kt.assert_called_once()

    def test_keepalive_only_fires_when_idle(self):
        """每条输出都发心跳会把日志刷满：keepalive 只在队列空的时候调用。"""
        beats = []

        class FastProc:
            stdout = iter([b"a\n", b"b\n", b"c\n"])

        out = list(core.iter_lines_watch(FastProc(),
                                         keepalive=lambda: beats.append(1) or True,
                                         idle_seconds=5))
        self.assertEqual(out, ["a", "b", "c"])
        self.assertEqual(beats, [])

    def test_heartbeat_failure_terminates_task(self):
        class LaggingProc:
            @staticmethod
            def _gen():
                yield b"a\n"
                time.sleep(0.5)

            def __init__(self):
                self.stdout = self._gen()

        why = {}
        with mock.patch.object(core, "kill_tree") as kt:
            out = list(core.iter_lines_watch(LaggingProc(), keepalive=lambda: False,
                                             idle_seconds=0.05, why=why))
        self.assertEqual(out, ["a"])
        self.assertEqual(why["reason"], "client")
        kt.assert_called_once()

    def test_stall_kills_the_process(self):
        class SlowProc:
            @staticmethod
            def _gen():
                yield b"first\n"
                time.sleep(1.0)
                yield b"never\n"

            def __init__(self):
                self.stdout = self._gen()

        with mock.patch.object(core, "kill_tree") as kt:
            why = {}
            out = list(core.iter_lines_watch(SlowProc(), idle_seconds=0.05,
                                             stall_seconds=0.2, why=why))
        self.assertEqual(out, ["first"])
        self.assertEqual(why["reason"], "stall")
        kt.assert_called_once()

    def test_reader_exception_ends_cleanly(self):
        class BoomProc:
            @property
            def stdout(self):
                raise OSError("pipe broken")

        with mock.patch.object(core, "kill_tree"):
            out = list(core.iter_lines_watch(BoomProc()))
        self.assertEqual(out, [])


class TestCookieJar(unittest.TestCase):
    def test_valid_entries(self):
        server = load_web_server()
        jar = [
            {"name": "persist", "value": "1", "domain": ".example.com",
             "path": "/", "secure": True, "expirationDate": 1800000000},
            {"name": "session", "value": "2", "domain": "example.com"},
        ]
        path = server.write_cookie_jar(jar, "https://example.com/v/1")
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().rstrip("\n").split("\n")
            self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
            self.assertEqual(len(lines), 3)
            first, second = lines[1].split("\t"), lines[2].split("\t")
            self.assertEqual(len(first), 7)
            self.assertEqual(first[0], ".example.com")
            self.assertEqual(first[1], "TRUE")       # 以点开头才允许子域
            self.assertEqual(first[3], "TRUE")       # secure
            self.assertEqual(first[4], "1800000000")
            self.assertEqual(second[1], "FALSE")
            self.assertGreater(int(second[4]), int(time.time()))   # 会话 cookie 续一年
        finally:
            if path:
                os.remove(path)

    def test_host_from_url_when_domain_missing(self):
        server = load_web_server()
        path = server.write_cookie_jar([{"name": "a", "value": "b"}], "https://cdn.example.org/v")
        try:
            with open(path, encoding="utf-8") as f:
                self.assertIn("cdn.example.org", f.read())
        finally:
            os.remove(path)

    def test_junk_entries_are_dropped(self):
        server = load_web_server()
        jar = ["not-a-dict", {"value": "no name"}, {"name": "x", "domain": "nodot"},
               {"name": "y", "domain": ".ok.com", "expirationDate": "abc"}]
        path = server.write_cookie_jar(jar, "https://ok.com/v")
        try:
            with open(path, encoding="utf-8") as f:
                rows = [ln for ln in f.read().split("\n") if ln and not ln.startswith("#")]
            self.assertEqual(len(rows), 1)
            self.assertGreater(int(rows[0].split("\t")[4]), int(time.time()))
        finally:
            os.remove(path)

    def test_empty_jar_returns_none(self):
        server = load_web_server()
        self.assertIsNone(server.write_cookie_jar([], "https://a.com"))
        self.assertIsNone(server.write_cookie_jar([{"value": "x"}], "https://a.com"))


class TestSSEFraming(unittest.TestCase):
    """SSE 是手写 chunked 帧，写坏了浏览器只会「卡在探测中」，所以按字节校验。"""

    @staticmethod
    def decode(raw):
        frames, i = [], 0
        while i < len(raw):
            nl = raw.index(b"\r\n", i)
            size = int(raw[i:nl], 16)
            if size == 0:
                return frames, raw[nl + 2:]
            end = nl + 2 + size
            assert raw[end:end + 2] == b"\r\n", "帧尾缺 CRLF"
            frames.append(raw[nl + 2:end])
            i = end + 2
        return frames, b""

    def make(self):
        server = load_web_server()

        class H:
            wfile = io.BytesIO()

        return server.SSE(H()), H

    def test_send_frame_shape(self):
        sse, h = self.make()
        sse.send({"type": "log", "text": "中文"})
        frames, rest = self.decode(h.wfile.getvalue())
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].startswith(b"data: "))
        self.assertTrue(frames[0].endswith(b"\n\n"))
        self.assertEqual(json.loads(frames[0][6:].decode("utf-8"))["text"], "中文")
        self.assertEqual(rest, b"")

    def test_heartbeat_is_a_comment_frame(self):
        sse, h = self.make()
        self.assertTrue(sse.heartbeat())
        frames, _ = self.decode(h.wfile.getvalue())
        self.assertEqual(frames, [b": ping\n\n"])

    def test_close_writes_zero_length_terminator(self):
        sse, h = self.make()
        sse.send({"a": 1})
        sse.close()
        frames, rest = self.decode(h.wfile.getvalue())
        self.assertEqual(len(frames), 1)
        self.assertEqual(rest, b"\r\n")
        self.assertTrue(sse.closed)

    def test_write_failure_marks_closed(self):
        server = load_web_server()

        class Dead:
            wfile = None

        sse = server.SSE(Dead())
        sse.send({"a": 1})
        self.assertTrue(sse.closed)
        self.assertFalse(sse.heartbeat())

    def test_send_after_close_is_noop(self):
        sse, h = self.make()
        sse.close()
        h.wfile.truncate(0)
        h.wfile.seek(0)
        sse.send({"a": 1})
        sse.heartbeat()
        self.assertEqual(h.wfile.getvalue(), b"")


class TestLocalhostGuards(unittest.TestCase):
    def test_extension_origin_is_pinned_to_the_manifest_key(self):
        server = load_web_server()
        self.assertTrue(server.extension_origin_ok(
            "chrome-extension://omdbcoobeaimllaomejhegpfppadapee"))
        self.assertFalse(server.extension_origin_ok(
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
        self.assertFalse(server.extension_origin_ok("https://evil.com"))
        self.assertFalse(server.extension_origin_ok(""))

    def test_manifest_key_matches_server_allowlist(self):
        server = load_web_server()
        with open(os.path.join(ROOT, "sniffer_extension", "manifest.json"),
                  encoding="utf-8") as f:
            man = json.load(f)
        # 服务端白名单里的 ID 必须真是 manifest 的 key 算出来的那个：
        # 改了 key（或换机器重新打包）不同步这里，嗅探就会整条链路 403。
        der = base64.b64decode(man["key"])
        digest = hashlib.sha256(der).hexdigest()[:32]
        ext_id = "".join(chr(ord("a") + int(c, 16)) for c in digest)
        self.assertEqual(list(server.ALLOWED_EXTENSION_IDS), [ext_id])

    def test_open_denies_executables(self):
        server = load_web_server()
        self.assertIn(".exe", server.OPEN_DENY_EXT)
        self.assertIn(".bat", server.OPEN_DENY_EXT)


class TestThumbSafety(unittest.TestCase):
    def test_public_https_ok(self):
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            self.assertTrue(core.is_public_http_url("https://i.ytimg.com/vi/x/hqdefault.jpg"))

    def test_non_http_schemes_rejected(self):
        for u in ("file:///C:/a.jpg", "ftp://x/a.jpg", "blob:https://a/b", "data:image/png;base64,AA", ""):
            self.assertFalse(core.is_public_http_url(u), u)

    def test_literal_private_addresses_rejected(self):
        for u in ("http://127.0.0.1/a.jpg", "http://10.1.2.3/a.jpg", "http://192.168.1.1/a.jpg",
                  "http://172.16.0.1/a.jpg", "http://169.254.1.1/a.jpg", "http://[::1]/a.jpg",
                  "http://localhost/a.jpg"):
            self.assertFalse(core.is_public_http_url(u), u)

    def test_public_hostname_resolving_to_private_rejected(self):
        # 只看字面 IP 挡不住这一招：域名是公网的，解析结果指向本机
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            self.assertFalse(core.is_public_http_url("https://evil.example/a.jpg"))

    def test_resolution_failure_rejected(self):
        with mock.patch("socket.getaddrinfo", side_effect=OSError("no such host")):
            self.assertFalse(core.is_public_http_url("https://nope.invalid/a.jpg"))

    def test_cache_path_is_stable_and_extension_whitelisted(self):
        u = "https://i.ytimg.com/vi/x/hqdefault.jpg"
        a = core.thumb_cache_path(u, "C:/tmp/x")
        b = core.thumb_cache_path(u, "C:/tmp/x")
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(".jpg"))
        self.assertEqual(core.thumb_cache_path("https://a/b?x=1", "C:/tmp/x")[-4:], ".img")

    def test_headers_carry_access_credentials(self):
        cfg = {"ua": "UA/1", "referer": "https://a.com/", "headers": "X-Token: abc",
               "cookie_header": "Cookie: sid=1"}
        h = core.thumb_headers(cfg)
        self.assertEqual(h["User-Agent"], "UA/1")
        self.assertEqual(h["Referer"], "https://a.com/")
        self.assertEqual(h["X-Token"], "abc")
        self.assertEqual(h["Cookie"], "sid=1")

    def test_fetch_rejects_private_before_any_request(self):
        with mock.patch("urllib.request.urlopen") as op:
            path, err = core.fetch_thumb("http://127.0.0.1/a.jpg", {})
        op.assert_not_called()
        self.assertEqual(path, "")
        self.assertTrue(err)

    def test_fetch_cache_hit_skips_network(self):
        with tempfile.TemporaryDirectory() as d:
            u = "https://cdn.example/a.jpg"
            p = core.thumb_cache_path(u, d)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\xff\xd8\xff")
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("urllib.request.urlopen") as op:
                path, err = core.fetch_thumb(u, {}, cache_dir=d)
        op.assert_not_called()
        self.assertEqual(path, p)
        self.assertIsNone(err)

    def test_fetch_size_cap(self):
        big = b"x" * (core.THUMB_MAX_BYTES + 1)
        resp = mock.MagicMock()
        resp.read.return_value = big
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("urllib.request.urlopen", return_value=resp):
                path, err = core.fetch_thumb("https://cdn.example/a.jpg", {}, cache_dir=d)
        self.assertEqual(path, "")
        self.assertIn("2 MB", err)

    def test_fetch_network_error_falls_back_to_ytdlp(self):
        # 兜底会真起 yt-dlp 进程，测试里必须替身掉，否则单测会联网
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("urllib.request.urlopen", side_effect=OSError("reset")), \
                 mock.patch("ytdlp_core.fetch_thumb_via_ytdlp",
                            return_value=("", "兜底也失败")) as fb:
                path, err = core.fetch_thumb("https://cdn.example/a.jpg", {}, cache_dir=d)
        fb.assert_called_once()
        self.assertEqual(path, "")
        self.assertEqual(err, "兜底也失败")

    def test_ytdlp_fallback_reports_reason_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.spawn", side_effect=OSError("no exe")):
                path, err = core.fetch_thumb_via_ytdlp("https://cdn.example/a.jpg", {}, cache_dir=d)
        self.assertEqual(path, "")
        self.assertIn("no exe", err)


class TestPreviewFields(unittest.TestCase):
    def test_thumb_from_thumbnail(self):
        info = core.info_from_json({"title": "T", "thumbnail": "https://a/x.jpg"})
        self.assertEqual(info["thumb"], "https://a/x.jpg")

    def test_thumb_falls_back_to_thumbnails_last(self):
        d = {"title": "T", "thumbnails": [{"url": "https://a/small.jpg"},
                                          {"url": "https://a/big.jpg"}]}
        self.assertEqual(core.info_from_json(d)["thumb"], "https://a/big.jpg")

    def test_thumb_empty_when_absent(self):
        self.assertEqual(core.info_from_json({"title": "T"})["thumb"], "")

    def test_existing_six_keys_unchanged(self):
        d = {"title": "T", "duration": 61, "height": 1080, "width": 1920,
             "filesize": 2048, "uploader": "U", "webpage_url": "https://a/v",
             "thumbnail": "https://a/x.jpg"}
        info = core.info_from_json(d)
        self.assertEqual(info["title"], "T")
        self.assertEqual(info["dur"], "01:01")
        self.assertEqual(info["res"], "1920x1080")
        self.assertEqual(info["size"], "2.0 KB")
        self.assertEqual(info["up"], "U")
        self.assertEqual(info["url"], "https://a/v")
        raw = core.info_from_json_raw(d)
        self.assertEqual(raw["dur"], 61)
        self.assertEqual(raw["size"], 2048)
        self.assertEqual(raw["thumb"], "https://a/x.jpg")

    def test_raw_path_keeps_full_path_and_format_marker(self):
        st = core.new_progress_state()
        line = "PROG|downloading|500|NA|1000|NA|NA|C:/d/Title.f137.mp4"
        core.progress_event(st, line)
        self.assertEqual(st["raw_path"], "C:/d/Title.f137.mp4")
        # 显示名照旧：去目录、去格式标记
        self.assertEqual(st["file"], "Title.mp4")

    def test_raw_path_from_destination_line(self):
        st = core.new_progress_state()
        core.progress_event(st, "[download] Destination: C:/d/Title.f140.m4a")
        self.assertEqual(st["raw_path"], "C:/d/Title.f140.m4a")
        self.assertEqual(st["file"], "Title.m4a")

    def test_parse_progress_line_exposes_raw(self):
        p = core.parse_progress_line("PROG|downloading|500|NA|1000|NA|NA|C:/d/a.f137.mp4")
        self.assertEqual(p["raw"], "C:/d/a.f137.mp4")
        self.assertEqual(p["file"], "a.mp4")

    def test_legacy_progress_line_has_empty_raw(self):
        p = core.parse_progress_line("[download]  12.5% of  10.00MiB at 1.00MiB/s ETA 00:10")
        self.assertEqual(p["raw"], "")


FFMPEG = os.path.join(ROOT, "ffmpeg.exe")
FFPROBE = os.path.join(ROOT, "ffprobe.exe")
HAS_FFMPEG = os.path.isfile(FFMPEG) and os.path.isfile(FFPROBE)


class TestPlayHelpers(unittest.TestCase):
    def test_separated_fmt(self):
        self.assertTrue(core.fmt_is_separated({"mode": "best"}))
        self.assertTrue(core.fmt_is_separated({"mode": "single", "fmt": "137+140"}))
        self.assertFalse(core.fmt_is_separated({"mode": "single"}))
        self.assertFalse(core.fmt_is_separated({"mode": "best", "fmt": "b"}))
        self.assertFalse(core.fmt_is_separated({"mode": "audio_mp3"}))

    def test_real_path_prefers_part_file(self):
        with tempfile.TemporaryDirectory() as d:
            final = os.path.join(d, "a.mp4")
            self.assertEqual(core.real_download_path(final), "")
            with open(final + ".part", "wb") as f:
                f.write(b"x")
            self.assertEqual(core.real_download_path(final), final + ".part")
            os.remove(final + ".part")
            with open(final, "wb") as f:
                f.write(b"x")
            self.assertEqual(core.real_download_path(final), final)

    def test_real_path_empty_input(self):
        self.assertEqual(core.real_download_path(""), "")
        self.assertEqual(core.real_download_path(None), "")

    def test_probe_missing_file(self):
        v = core.probe_playable("Z:/definitely/not/here.mkv")
        self.assertFalse(v["ok"])
        self.assertTrue(v["reason"])

    def test_link_keeps_format_marker_and_clears_safely(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "Title.f137.mp4.part")
            with open(src, "wb") as f:
                f.write(b"abcdef")
            link, err = core.make_preview_link(src, d)
            self.assertIsNone(err)
            self.assertEqual(os.path.basename(link), "Title.f137.mp4")
            self.assertEqual(os.path.dirname(link), os.path.join(d, ".preview"))
            with open(link, "rb") as f:
                self.assertEqual(f.read(), b"abcdef")
            # 硬链接：写源文件，链接看得到增长
            with open(src, "ab") as f:
                f.write(b"growing")
            with open(link, "rb") as f:
                self.assertEqual(f.read(), b"abcdefgrowing")
            core.clear_preview_links(d)
            self.assertFalse(os.path.exists(link))
            self.assertTrue(os.path.isfile(src))
            with open(src, "rb") as f:
                self.assertEqual(f.read(), b"abcdefgrowing")

    def test_link_replaces_stale_one(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.mp4.part")
            with open(src, "wb") as f:
                f.write(b"new")
            os.makedirs(os.path.join(d, ".preview"))
            with open(os.path.join(d, ".preview", "a.mp4"), "wb") as f:
                f.write(b"stale")
            link, err = core.make_preview_link(src, d)
            self.assertIsNone(err)
            with open(link, "rb") as f:
                self.assertEqual(f.read(), b"new")

    def test_link_missing_source_reports_reason(self):
        with tempfile.TemporaryDirectory() as d:
            link, err = core.make_preview_link(os.path.join(d, "nope.part"), d)
            self.assertEqual(link, "")
            self.assertTrue(err)

    def test_clear_on_missing_dir_is_quiet(self):
        core.clear_preview_links("Z:/definitely/not/here")


@unittest.skipUnless(HAS_FFMPEG, "需要随包 ffmpeg/ffprobe")
class TestProbePlayableReal(unittest.TestCase):
    """把设计阶段的探针固化成测试：截断的 mkv/webm/ts 可读，mp4 不可读。"""

    def _make(self, d, name, extra):
        out = os.path.join(d, name)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
               "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6"] + extra + ["-y", out]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=core.CREATE_NO_WINDOW, timeout=120)
        return out

    def _truncate(self, src, pct):
        dst = src + ".partial"
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data[:len(data) * pct // 100])
        return dst

    def test_mkv_partial_is_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.mkv", ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac"])
            self.assertTrue(core.probe_playable(self._truncate(src, 40))["ok"])

    def test_webm_partial_is_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.webm", ["-c:v", "libvpx", "-b:v", "200k", "-c:a", "libopus"])
            self.assertTrue(core.probe_playable(self._truncate(src, 40))["ok"])

    def test_mpegts_partial_is_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.ts", ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-f", "mpegts"])
            self.assertTrue(core.probe_playable(self._truncate(src, 40))["ok"])

    def test_mp4_partial_is_not_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.mp4", ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac"])
            v = core.probe_playable(self._truncate(src, 40))
            self.assertFalse(v["ok"])
            self.assertTrue(v["reason"])


class TestPlayWhileCmd(unittest.TestCase):
    URLS = ["https://example.com/v"]

    def test_off_by_default(self):
        self.assertFalse(core.DEFAULT_CONFIG["play_while"])

    def test_command_byte_identical_when_off(self):
        cfg = core.default_config()
        base = core.build_download_cmd(cfg, self.URLS)
        cfg["play_while"] = False
        self.assertEqual(core.build_download_cmd(cfg, self.URLS), base)
        self.assertNotIn("--hls-use-mpegts", base)
        self.assertNotIn("--remux-video", base)

    def test_never_adds_no_part(self):
        for pw in (False, True):
            cfg = dict(core.default_config(), play_while=pw)
            self.assertNotIn("--no-part", core.build_download_cmd(cfg, self.URLS))

    def test_on_adds_mpegts(self):
        cfg = dict(core.default_config(), play_while=True, mode="single")
        cmd = core.build_download_cmd(cfg, self.URLS)
        self.assertIn("--hls-use-mpegts", cmd)

    def test_on_remuxes_to_merge_target(self):
        cfg = dict(core.default_config(), play_while=True, mode="single", merge="mkv")
        cmd = core.build_download_cmd(cfg, self.URLS)
        i = cmd.index("--remux-video")
        self.assertEqual(cmd[i + 1], "mkv")

    def test_no_remux_when_merge_disabled(self):
        for merge in ("", "不转换"):
            cfg = dict(core.default_config(), play_while=True, mode="single", merge=merge)
            self.assertNotIn("--remux-video", core.build_download_cmd(cfg, self.URLS))

    def test_no_remux_for_audio_modes(self):
        for mode in ("audio_mp3", "audio_raw"):
            cfg = dict(core.default_config(), play_while=True, mode=mode)
            self.assertNotIn("--remux-video", core.build_download_cmd(cfg, self.URLS))

    def test_flags_come_before_url_separator(self):
        cfg = dict(core.default_config(), play_while=True, mode="single")
        cmd = core.build_download_cmd(cfg, self.URLS)
        self.assertLess(cmd.index("--hls-use-mpegts"), cmd.index("--"))

    def test_type_coercion(self):
        cfg, warns = core.coerce_config({"play_while": "false"})
        self.assertIs(cfg["play_while"], False)
        cfg, _ = core.coerce_config({"play_while": "开"})
        self.assertIs(cfg["play_while"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
