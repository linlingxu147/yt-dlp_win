# -*- coding: utf-8 -*-
"""镜像更新的纯逻辑用例：URL 拼接、SHA256、解压替换/回滚、下载。

不启动 GUI、不联网（下载只连本机临时 HTTP）。
"""
import hashlib
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
_PATH = os.path.join(_ROOT, "YtDlpGUI", "updater.py")
_spec = importlib.util.spec_from_file_location("ytdlp_gui_updater", _PATH)
updater = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = updater
_spec.loader.exec_module(updater)


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


class TestMirrorUrl(unittest.TestCase):
    def test_prefix_and_template(self):
        official = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_win.zip"
        self.assertEqual(updater.mirror_url("https://ghproxy.net", official),
                         "https://ghproxy.net/" + official)
        self.assertEqual(updater.mirror_url("https://ghproxy.net/", official),
                         "https://ghproxy.net/" + official)
        self.assertEqual(updater.mirror_url("https://x/{url}", official),
                         "https://x/" + official)

    def test_invalid_prefix(self):
        self.assertEqual(updater.mirror_url("", "https://a"), "")
        self.assertEqual(updater.mirror_url("ghproxy.net", "https://a"), "")


class TestChecksumAndHash(unittest.TestCase):
    def test_parse_checksum(self):
        text = ("622ed48023cac1daa52a3bf3a6ac38f03514de214b5f0fa069389fd0f6d17e5c  yt-dlp.exe\n"
                "824860984b96ca01de09f98f7bfd74ed7bbdce72a3340b51af8b9dc5ee037a5f *yt-dlp_win.zip\n")
        self.assertEqual(
            updater.parse_checksum(text, "yt-dlp_win.zip"),
            "824860984b96ca01de09f98f7bfd74ed7bbdce72a3340b51af8b9dc5ee037a5f")
        self.assertEqual(updater.parse_checksum(text, "missing.zip"), "")

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.bin")
            with open(p, "wb") as f:
                f.write(b"abc")
            self.assertEqual(updater.sha256_file(p),
                             hashlib.sha256(b"abc").hexdigest())


class TestInstallZip(unittest.TestCase):
    def _runtime(self, d):
        rt = os.path.join(d, "runtime")
        os.makedirs(os.path.join(rt, "_internal"))
        with open(os.path.join(rt, "yt-dlp.exe"), "wb") as f:
            f.write(b"old-exe")
        with open(os.path.join(rt, "_internal", "old.txt"), "wb") as f:
            f.write(b"old-internal")
        return rt

    def test_install_replaces_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as d:
            rt = self._runtime(d)
            zpath = os.path.join(d, "new.zip")
            make_zip(zpath, {"yt-dlp.exe": b"new-exe",
                             "_internal/new.txt": b"new-internal"})
            ok, version, backup, err = updater.install_zip(
                zpath, rt, verify=lambda exe: (True, "2026.11.23", ""))
            self.assertTrue(ok, err)
            self.assertEqual(version, "2026.11.23")
            with open(os.path.join(rt, "yt-dlp.exe"), "rb") as f:
                self.assertEqual(f.read(), b"new-exe")
            self.assertTrue(os.path.isfile(os.path.join(rt, "_internal", "new.txt")))
            self.assertFalse(os.path.exists(os.path.join(rt, "_internal", "old.txt")))
            self.assertTrue(os.path.isdir(backup))
            with open(os.path.join(backup, "yt-dlp.exe"), "rb") as f:
                self.assertEqual(f.read(), b"old-exe")
            self.assertTrue(os.path.isfile(os.path.join(backup, "_internal", "old.txt")))

    def test_verify_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            rt = self._runtime(d)
            zpath = os.path.join(d, "bad.zip")
            make_zip(zpath, {"yt-dlp.exe": b"bad-exe",
                             "_internal/bad.txt": b"bad-internal"})
            ok, version, backup, err = updater.install_zip(
                zpath, rt, verify=lambda exe: (False, "", "无法启动"))
            self.assertFalse(ok)
            self.assertIn("无法启动", err)
            with open(os.path.join(rt, "yt-dlp.exe"), "rb") as f:
                self.assertEqual(f.read(), b"old-exe")
            self.assertTrue(os.path.isfile(os.path.join(rt, "_internal", "old.txt")))
            self.assertFalse(os.path.exists(os.path.join(rt, "_internal", "bad.txt")))

    def test_zip_slip_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            rt = self._runtime(d)
            zpath = os.path.join(d, "evil.zip")
            make_zip(zpath, {"../evil.txt": b"x", "yt-dlp.exe": b"new-exe"})
            ok, _version, _backup, err = updater.install_zip(zpath, rt)
            self.assertFalse(ok)
            self.assertIn("路径异常", err)
            self.assertFalse(os.path.exists(os.path.join(d, "evil.txt")))
            with open(os.path.join(rt, "yt-dlp.exe"), "rb") as f:
                self.assertEqual(f.read(), b"old-exe")


class TestDownload(unittest.TestCase):
    def test_downloads_without_proxy(self):
        payload = b"mirror-bytes"
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 8897), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                dest = os.path.join(d, "x.bin")
                n = updater.download("http://127.0.0.1:8897/x", dest)
                self.assertEqual(n, len(payload))
                with open(dest, "rb") as f:
                    self.assertEqual(f.read(), payload)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_abort_stops_download(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "10")
                self.end_headers()
                self.wfile.write(b"0123456789")
            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 8897), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(updater.UpdateCancelled):
                    updater.download("http://127.0.0.1:8897/x",
                                     os.path.join(d, "x.bin"), abort=lambda: True)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()