# -*- coding: utf-8 -*-
"""便携性回归：程序目录内的路径落盘成 @{APP}/ 标记，换盘换机器后能还原。

覆盖三件事：
  1. 编码/解码本身幂等、只吃程序目录内的绝对路径；
  2. save_config 落盘的是标记，load_config 读回的是本机绝对路径；
  3. 换机器后必然踩到的两个坑——yt-dlp 路径失效、下载目录所在磁盘不存在——有明确提示，
     且不会悄悄改掉用户自己填的路径。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ytdlp_core as core  # noqa: E402


def _missing_drive():
    """找一个本机不存在的盘符，用来模拟「换了台没有 D 盘的机器」。"""
    for letter in "ZDEFGHIJKLMNOPQRSTUVWXY":
        if not os.path.exists(letter + ":\\"):
            return letter + ":"
    return ""


class TestPortablePath(unittest.TestCase):
    def test_encode_inside_app_dir(self):
        inner = os.path.join(core.RUNTIME_DIR, "downloads")
        self.assertEqual(core.portable_encode(inner), "@{APP}/runtime/downloads")

    def test_encode_is_idempotent(self):
        token = "@{APP}/runtime/downloads"
        self.assertEqual(core.portable_encode(token), token)
        self.assertEqual(core.portable_encode(core.portable_encode(token)), token)

    def test_encode_leaves_outside_paths_alone(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(core.portable_encode(d), d)
        # 相对路径的用户输入（如 runtime\downloads）不猜，原样保留
        self.assertEqual(core.portable_encode("runtime\\downloads"), "runtime\\downloads")
        self.assertEqual(core.portable_encode(""), "")

    def test_decode_roundtrip(self):
        inner = os.path.join(core.RUNTIME_DIR, "downloads")
        back = core.portable_decode(core.portable_encode(inner))
        self.assertEqual(os.path.normcase(back), os.path.normcase(inner))

    def test_decode_untouched_for_plain_paths(self):
        self.assertEqual(core.portable_decode(r"D:\视频\下载"), r"D:\视频\下载")
        self.assertEqual(core.portable_decode("yt-dlp.exe"), "yt-dlp.exe")

    def test_token_survives_space_and_chinese(self):
        # 纯字符串拼接：路径里有空格/中文也不该出错
        token = "@{APP}/我的 工具/runtime"
        got = core.portable_decode(token)
        self.assertEqual(got, os.path.normpath(os.path.join(core.BASE_DIR, "我的 工具", "runtime")))

    def test_unc_root_roundtrip(self):
        # 放在网络共享上（\\nas\share\yt-dlp_win）也该正常：这是便携包常见的用法
        unc = r"\\nas\share\yt-dlp_win"
        with mock.patch.object(core, "BASE_DIR", unc):
            token = core.portable_encode(os.path.join(unc, "runtime", "downloads"))
            self.assertEqual(token, "@{APP}/runtime/downloads")
            self.assertEqual(core.portable_decode(token),
                             os.path.normpath(os.path.join(unc, "runtime", "downloads")))


class TestConfigPortableStorage(unittest.TestCase):
    def test_saved_config_uses_token(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            cfg = core.default_config()
            cfg["outdir"] = core.DOWNLOAD_DIR
            cfg["yt_dlp_path"] = core.find_tool("yt-dlp.exe")
            self.assertTrue(core.save_config(path, cfg))
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw["outdir"], "@{APP}/runtime/downloads")
            self.assertTrue(raw["yt_dlp_path"].startswith("@{APP}/"))

    def test_load_expands_token_to_current_base(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"outdir": "@{APP}/runtime/downloads"}, f)
            cfg = core.load_config(path)
            self.assertEqual(os.path.normcase(cfg["outdir"]),
                             os.path.normcase(os.path.join(core.RUNTIME_DIR, "downloads")))

    def test_outside_path_stays_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            out = os.path.join(d, "下载")
            self.assertTrue(core.save_config(path, dict(core.default_config(), outdir=out)))
            cfg = core.load_config(path)
            self.assertEqual(cfg["outdir"], os.path.abspath(out))


class TestStalePathWarnings(unittest.TestCase):
    def test_missing_ytdlp_falls_back_to_bundled_or_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            ghost = os.path.join(d, "old-machine", "yt-dlp.exe")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"yt_dlp_path": ghost}, f)
            warns = []
            cfg = core.load_config(path, warn=warns.append)
            self.assertEqual(cfg["yt_dlp_path"], core.find_tool("yt-dlp.exe"))
            self.assertTrue(any("yt-dlp 路径不存在" in w for w in warns))

    def test_missing_drive_warns_but_keeps_user_choice(self):
        drive = _missing_drive()
        if not drive:
            self.skipTest("本机没有空闲盘符可用")
        out = drive + "\\旧机器\\下载"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"outdir": out}, f)
            warns = []
            cfg = core.load_config(path, warn=warns.append)
            self.assertEqual(cfg["outdir"], out)          # 不擅自改用户的选择
            self.assertTrue(any("磁盘不存在" in w for w in warns))
    def test_unc_outdir_does_not_trigger_drive_warning(self):
        # 网络共享离线时 exists 也是 False，但这不是「磁盘不存在」，不该报
        says = []
        core._heal_config_paths({"outdir": r"\\nas\share\下载"}, says.append)
        self.assertEqual(says, [])


if __name__ == "__main__":
    unittest.main()
