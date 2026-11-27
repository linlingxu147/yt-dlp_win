# -*- coding: utf-8 -*-
"""安装版构建链的单测（标准库 unittest，不触发 PyInstaller / ISCC）。"""
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_installer as bi  # noqa: E402
import make_icon  # noqa: E402
import squidkit_paths as paths  # noqa: E402

ICON_PNG = os.path.join(ROOT, "sniffer_extension", "icon128.png")
ISS_TPL = os.path.join(ROOT, "installer", "squidkit.iss.tpl")


class TestPaths(unittest.TestCase):
    def test_installed_root_is_exe_directory(self):
        self.assertEqual(
            paths.installed_root(r"C:\Program Files\SquidKit\squidkit.exe"),
            r"C:\Program Files\SquidKit")

    def test_src_dir(self):
        self.assertEqual(paths.src_dir(r"C:\app"), os.path.join(r"C:\app", "src"))

    def test_data_dir_honors_override(self):
        self.assertEqual(paths.data_dir({paths.DATA_DIR_ENV: r"D:\mydata"}), r"D:\mydata")

    def test_data_dir_defaults_to_appdata(self):
        env = {"APPDATA": r"C:\Users\u\AppData\Roaming"}
        self.assertEqual(paths.data_dir(env, home=r"C:\Users\u"),
                         r"C:\Users\u\AppData\Roaming\SquidKit")

    def test_config_file_honors_override(self):
        self.assertEqual(paths.config_file({paths.CONFIG_FILE_ENV: r"D:\x\c.json"}),
                         r"D:\x\c.json")

    def test_config_file_default_is_in_data_dir(self):
        env = {"APPDATA": r"C:\Users\u\AppData\Roaming"}
        self.assertEqual(paths.config_file(env, home=r"C:\Users\u"),
                         r"C:\Users\u\AppData\Roaming\SquidKit\config.json")

    def test_default_download_dir_prefers_downloads(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "Downloads"))
            self.assertEqual(paths.default_download_dir(home=home, environ={}),
                             os.path.join(home, "Downloads", "SquidKit"))

    def test_default_download_dir_falls_back_to_home(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(paths.default_download_dir(home=home, environ={}),
                             os.path.join(home, "SquidKit"))

    def test_default_download_dir_honors_override(self):
        self.assertEqual(
            paths.default_download_dir(environ={paths.DOWNLOAD_DIR_ENV: r"E:\dl"}),
            r"E:\dl")

    def test_migrate_copies_once_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "src", "YtDlpGUI")
            os.makedirs(legacy)
            for name, text in (("config.json", '{"a":1}'), ("history.json", "[]")):
                with open(os.path.join(legacy, name), "w", encoding="utf-8") as f:
                    f.write(text)
            dest = os.path.join(tmp, "data", "config.json")
            self.assertEqual(sorted(paths.migrate_legacy_state(
                os.path.join(tmp, "src"), dest)), ["config.json", "history.json"])
            with open(os.path.join(tmp, "data", "history.json"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "[]")
            with open(dest, "w", encoding="utf-8") as f:
                f.write('{"a":2}')
            self.assertEqual(paths.migrate_legacy_state(
                os.path.join(tmp, "src"), dest), [])
            with open(dest, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"a":2}')


class TestIcon(unittest.TestCase):
    def test_single_png_ico_header(self):
        with open(ICON_PNG, "rb") as f:
            raw = f.read()
        data = make_icon.build_single_png_ico(raw)
        self.assertEqual(struct.unpack_from("<HHH", data, 0), (0, 1, 1))
        self.assertEqual(make_icon.ico_entry_count(data), 1)
        self.assertTrue(data.endswith(raw))

    def test_build_ico_multisize(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "nested", "squidkit.ico")
            make_icon.build_ico(ICON_PNG, dest)
            with open(dest, "rb") as f:
                data = f.read()
            self.assertGreaterEqual(make_icon.ico_entry_count(data), 2)
            self.assertGreater(len(data), 1000)

    def test_reject_non_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.png")
            with open(bad, "wb") as f:
                f.write(b"not a png")
            with self.assertRaises(ValueError):
                make_icon.build_ico(bad, os.path.join(tmp, "out.ico"))


class TestBuildInstaller(unittest.TestCase):
    def test_version_4(self):
        self.assertEqual(bi.version_4("1.12.0"), (1, 12, 0, 0))
        self.assertEqual(bi.version_4("2.3"), (2, 3, 0, 0))
        self.assertEqual(bi.version_4("1.2.3.4"), (1, 2, 3, 4))

    def test_version_info_text(self):
        text = bi.version_info_text("1.12.0")
        self.assertIn("filevers=(1, 12, 0, 0)", text)
        self.assertIn("StringStruct('FileVersion', '1.12.0')", text)
        self.assertIn("VarStruct('Translation', [2052, 1200])", text)

    def test_render_iss(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.iss")
            bi.render_iss(ISS_TPL, dest, {
                "VERSION": "1.12.0", "VERSION4": "1.12.0.0",
                "APPNAME": "鱿鱼浏览器工具包", "PUBLISHER": "SquidKit",
                "APPID": "{{GUID}}", "AUMID": "SquidKit.Desktop",
                "PAYLOAD": r"E:\build\payload",
                "ICON": r"E:\build\squidkit.ico",
                "WEBVIEW2FILE": "", "WEBVIEW2RUN": "",
            })
            with open(dest, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("@@", text)
            self.assertIn("AppId={{GUID}}", text)
            self.assertIn('#define MyAppVersion "1.12.0"', text)
            self.assertIn("VersionInfoVersion=1.12.0.0", text)
            self.assertIn("PrivilegesRequired=lowest", text)
            self.assertIn("compiler:Languages\\ChineseSimplified.isl", text)
            self.assertIn('AppUserModelID: "SquidKit.Desktop"', text)
            self.assertIn("SuppressibleMsgBox", text, "卸载询问必须可抑制，否则静默卸载会卡住")
            self.assertIn("MB_DEFBUTTON2, IDNO)", text, "静默卸载应默认保留用户数据")
            self.assertNotIn(" MsgBox(", text)
            self.assertIn(r'Source: "E:\build\payload\*"', text)
            self.assertIn("UninstallDisplayName", text)
            # WebView2 检测必须是真的查注册表，而不是永远返回 True/False
            self.assertIn("function NeedWebView2(): Boolean;", text)
            self.assertIn("SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients", text)
            self.assertNotIn("MicrosoftEdgeWebview2Setup.exe", text, "没给引导器时不该有安装行")

    def test_render_iss_with_webview2_bootstrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.iss")
            bi.render_iss(ISS_TPL, dest, {
                "VERSION": "1.12.0", "VERSION4": "1.12.0.0",
                "APPNAME": "鱿鱼浏览器工具包", "PUBLISHER": "SquidKit",
                "APPID": "{{GUID}}", "AUMID": "SquidKit.Desktop",
                "PAYLOAD": r"E:\build\payload",
                "ICON": r"E:\build\squidkit.ico",
                "WEBVIEW2FILE": 'Source: "E:/build/webview2/MicrosoftEdgeWebview2Setup.exe"; '
                                'DestDir: "{tmp}"; Flags: deleteafterinstall',
                "WEBVIEW2RUN": 'Filename: "{tmp}\\MicrosoftEdgeWebview2Setup.exe"; '
                               'Parameters: "/silent /install"; '
                               'StatusMsg: "正在补装 WebView2 运行时（需要联网）…"; '
                               'Flags: runhidden waituntilterminated; Check: NeedWebView2',
            })
            with open(dest, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("@@", text)
            self.assertIn('Flags: deleteafterinstall', text, "引导器应装在临时目录、装完即删")
            self.assertIn('Check: NeedWebView2', text, "已装 WebView2 的机器不该重复安装")
            self.assertIn('Parameters: "/silent /install"', text)

    def test_ensure_webview2_reuses_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "MicrosoftEdgeWebview2Setup.exe")
            with open(p, "wb") as f:
                f.write(b"x" * (1024 * 1024 + 1))
            self.assertEqual(bi.ensure_webview2(p), p)

    def test_ensure_webview2_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "boot.exe")
            # file:// 指向不存在的路径，urlopen 立刻失败；构建脚本只应返回 None、不留半截文件
            self.assertIsNone(bi.ensure_webview2(p, url="file:///no/such/webview2.exe", timeout=5))
            self.assertFalse(os.path.exists(p))

    def test_render_iss_detects_unreplaced_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tpl = os.path.join(tmp, "tpl.iss")
            with open(tpl, "w", encoding="utf-8") as f:
                f.write("AppName=@@APPNAME@@\nLeft=@@UNKNOWN@@\n")
            with self.assertRaises(ValueError):
                bi.render_iss(tpl, os.path.join(tmp, "out.iss"), {"APPNAME": "x"})

    def test_collect_payload_files(self):
        files = bi.collect_payload_files()
        self.assertIn("ytdlp_core.py", files)
        self.assertIn("YtDlpGUI/main.py", files)
        self.assertIn("YtDlpWeb/server.py", files)
        self.assertIn("runtime/yt-dlp.exe", files)
        self.assertNotIn("YtDlpGUI/启动.bat", files)
        self.assertNotIn("YtDlpWeb/启动.bat", files)
        self.assertNotIn("YtDlpGUI/config.json", files)
        self.assertNotIn("YtDlpGUI/history.json", files)
        self.assertFalse(any(f.endswith(".bak") or "__pycache__" in f for f in files))
        self.assertFalse(any(f.startswith("tools/") or f.startswith("tests/") for f in files))

    def test_validate_payload_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = bi.validate_payload(tmp)
            self.assertIn("src/ytdlp_core.py", missing)
            self.assertIn("squidkit.exe", missing)

    def test_assemble_payload_overwrites_without_wiping(self):
        with tempfile.TemporaryDirectory() as tmp:
            freeze = os.path.join(tmp, "freeze")
            os.makedirs(freeze)
            with open(os.path.join(freeze, "app.exe"), "wb") as f:
                f.write(b"new-exe")
            root = os.path.join(tmp, "root")
            os.makedirs(os.path.join(root, "YtDlpGUI"))
            with open(os.path.join(root, "YtDlpGUI", "main.py"), "w", encoding="utf-8") as f:
                f.write("new-main")
            payload = os.path.join(tmp, "payload")
            os.makedirs(payload)
            with open(os.path.join(payload, "app.exe"), "wb") as f:
                f.write(b"old-exe")
            extra = os.path.join(payload, "extra.txt")
            with open(extra, "w", encoding="utf-8") as f:
                f.write("keep me")

            bi.assemble_payload(root, ["YtDlpGUI/main.py"], freeze, payload, ICON_PNG)

            with open(os.path.join(payload, "app.exe"), "rb") as f:
                self.assertEqual(f.read(), b"new-exe")
            with open(os.path.join(payload, "src", "YtDlpGUI", "main.py"),
                      encoding="utf-8") as f:
                self.assertEqual(f.read(), "new-main")
            self.assertTrue(os.path.isfile(os.path.join(payload, "squidkit.ico")))
            self.assertTrue(os.path.isfile(extra), "组装不应清空整个载荷目录")


if __name__ == "__main__":
    unittest.main()
