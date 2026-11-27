# -*- coding: utf-8 -*-
"""发布打包脚本：版本读取、清单白名单、排除项与 zip 结构。

全部在临时目录构造假仓库，不读取真实 runtime、不联网。
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_TOOL = os.path.join(_ROOT, "tools", "build_release.py")


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_release", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


VERSION = "9.9.9"
MANIFEST = {
    "manifest_version": 3,
    "name": "测试扩展",
    "version": VERSION,
    "background": {"service_worker": "background.js"},
    "icons": {"128": "icon128.png"},
    "content_scripts": [
        {"js": ["sniff_rules.js", "audio_stream.js", "dom_scan.js", "content.js"]},
    ],
}


class BuildReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = _load_builder()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _write(self, rel, text="x"):
        path = os.path.join(self.root, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _make_valid_tree(self):
        for rel in (
            "README.md",
            "ytdlp_core.py",
            "ytdlp_queue.py",
            "sniff_rules.py",
            "YtDlpGUI/main.py",
            "YtDlpGUI/host.py",
            "YtDlpGUI/api.py",
            "YtDlpGUI/embedded.py",
            "YtDlpGUI/updater.py",
            "YtDlpGUI/启动.bat",
            "YtDlpGUI/ui/index.html",
            "YtDlpWeb/server.py",
            "YtDlpWeb/agent_client.py",
            "YtDlpWeb/启动.bat",
            "tools/ytdlp_agent.py",
            "tools/ytdlp_mcp.py",
            "runtime/yt-dlp.exe",
            "runtime/ffmpeg.exe",
            "runtime/ffprobe.exe",
            "runtime/_internal/a.dll",
            "runtime/README.md",
            "sniffer_extension/manifest.json",
            "sniffer_extension/sniff_rules.js",
            "sniffer_extension/background.js",
            "sniffer_extension/audio_stream.js",
            "sniffer_extension/dom_scan.js",
            "sniffer_extension/content.js",
            "sniffer_extension/injected.js",
            "sniffer_extension/icon128.png",
            "sniffer_extension/README.md",
        ):
            self._write(rel)
        self._write("YtDlpWeb/server.py", 'APP_ID = "yt-dlp-web"\nAPP_VERSION = "%s"\n' % VERSION)
        self._write("sniffer_extension/manifest.json", json.dumps(MANIFEST, ensure_ascii=False))
        for rel in (
            "YtDlpGUI/config.json",
            "YtDlpGUI/config.json.bak",
            "YtDlpGUI/history.json",
            "YtDlpGUI/legacy/main_tk.py",
            "YtDlpGUI/__pycache__/host.pyc",
            "YtDlpWeb/config.json",
            "runtime/downloads/半成品.part",
            "runtime/agent_service.json",
            "runtime/.update_backup_old/yt-dlp.exe",
            "tests/test_core.py",
            "docs/README.md",
        ):
            self._write(rel)

    def test_size_text_switches_units(self):
        self.assertEqual(self.builder.size_text(512), "512 B")
        self.assertEqual(self.builder.size_text(2048), "2.0 KB")
        self.assertEqual(self.builder.size_text(3 * 1024 * 1024), "3.0 MB")

    def test_reads_extension_and_server_versions(self):
        self._write("sniffer_extension/manifest.json", json.dumps({"version": "1.2.3"}))
        self._write("YtDlpWeb/server.py", 'APP_ID = "yt-dlp-web"\nAPP_VERSION = "1.2.2"\n')
        self.assertEqual(self.builder.read_extension_version(self.root), "1.2.3")
        self.assertEqual(self.builder.read_server_version(self.root), "1.2.2")

    def test_desktop_collection_keeps_runtime_and_drops_local_state(self):
        self._make_valid_tree()
        files = self.builder.collect_desktop_files(self.root)

        self.assertEqual(files, sorted(files))
        for rel in (
            "README.md",
            "YtDlpGUI/main.py",
            "YtDlpGUI/启动.bat",
            "YtDlpWeb/server.py",
            "tools/ytdlp_mcp.py",
            "sniffer_extension/sniff_rules.js",
            "sniffer_extension/dom_scan.js",
            "runtime/yt-dlp.exe",
            "runtime/_internal/a.dll",
            "runtime/README.md",
        ):
            self.assertIn(rel, files)
        for rel in (
            "YtDlpGUI/config.json",
            "YtDlpGUI/config.json.bak",
            "YtDlpGUI/history.json",
            "YtDlpGUI/legacy/main_tk.py",
            "YtDlpGUI/__pycache__/host.pyc",
            "YtDlpWeb/config.json",
            "runtime/downloads/半成品.part",
            "runtime/agent_service.json",
            "runtime/.update_backup_old/yt-dlp.exe",
            "tests/test_core.py",
            "docs/README.md",
        ):
            self.assertNotIn(rel, files)

    def test_extension_collection_includes_manifest_scripts_only(self):
        self._make_valid_tree()
        files = self.builder.collect_extension_files(self.root)

        self.assertEqual(
            files,
            sorted([
                "sniffer_extension/README.md",
                "sniffer_extension/audio_stream.js",
                "sniffer_extension/background.js",
                "sniffer_extension/content.js",
                "sniffer_extension/dom_scan.js",
                "sniffer_extension/icon128.png",
                "sniffer_extension/injected.js",
                "sniffer_extension/manifest.json",
                "sniffer_extension/sniff_rules.js",
            ]),
        )

    def test_manifest_validation_reports_missing_dom_scan(self):
        os.makedirs(os.path.join(self.root, "sniffer_extension"))
        self._write("sniffer_extension/manifest.json", json.dumps(MANIFEST))
        self._write("sniffer_extension/background.js")
        self._write("sniffer_extension/sniff_rules.js")
        self._write("sniffer_extension/audio_stream.js")
        self._write("sniffer_extension/content.js")
        self._write("sniffer_extension/icon128.png")

        problems = self.builder.validate_extension_manifest(
            os.path.join(self.root, "sniffer_extension"))

        self.assertTrue(any("dom_scan.js" in p for p in problems), problems)

    def test_validate_layout_reports_missing_runtime(self):
        problems = self.builder.validate_layout(self.root)

        self.assertTrue(any("YtDlpGUI/main.py" in p for p in problems), problems)
        self.assertTrue(any("runtime/yt-dlp.exe" in p for p in problems), problems)
        self.assertTrue(any("runtime/_internal" in p for p in problems), problems)

    def test_build_zip_prefixes_entries_and_adds_downloads_dir(self):
        self._write("README.md", "hello")
        out = os.path.join(self.root, "dist", "desktop.zip")

        self.builder.build_zip(
            self.root, ["README.md"], out, "squidkit-1.2.3",
            extra_dirs=("runtime/downloads",))

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            self.assertIn("squidkit-1.2.3/README.md", names)
            self.assertIn("squidkit-1.2.3/runtime/downloads/", names)
            self.assertEqual(zf.read("squidkit-1.2.3/README.md"), b"hello")

    def test_main_builds_both_zips_with_expected_contents(self):
        self._make_valid_tree()
        out_dir = os.path.join(self.root, "dist")

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = self.builder.main(["--out", out_dir], root=self.root)

        self.assertEqual(code, 0)
        desktop = os.path.join(out_dir, "squidkit-%s-desktop.zip" % VERSION)
        extension = os.path.join(out_dir, "squidkit-%s-extension.zip" % VERSION)
        self.assertTrue(os.path.isfile(desktop))
        self.assertTrue(os.path.isfile(extension))

        with zipfile.ZipFile(desktop) as zf:
            names = zf.namelist()
            prefix = "squidkit-%s/" % VERSION
            self.assertIn(prefix + "README.md", names)
            self.assertIn(prefix + "YtDlpGUI/main.py", names)
            self.assertIn(prefix + "YtDlpWeb/server.py", names)
            self.assertIn(prefix + "tools/ytdlp_mcp.py", names)
            self.assertIn(prefix + "sniffer_extension/dom_scan.js", names)
            self.assertIn(prefix + "runtime/downloads/", names)
            self.assertFalse(any("config.json" in n for n in names), names)
            self.assertFalse(any("history.json" in n for n in names), names)
            self.assertFalse(any("legacy/" in n for n in names), names)
            self.assertFalse(any(".part" in n for n in names), names)
            self.assertFalse(any(".update_backup_" in n for n in names), names)

        with zipfile.ZipFile(extension) as zf:
            names = zf.namelist()
            prefix = "squidkit-extension-%s/" % VERSION
            self.assertIn(prefix + "manifest.json", names)
            self.assertIn(prefix + "dom_scan.js", names)
            self.assertIn(prefix + "sniff_rules.js", names)
            self.assertFalse(any("YtDlpGUI/" in n for n in names), names)


if __name__ == "__main__":
    unittest.main()