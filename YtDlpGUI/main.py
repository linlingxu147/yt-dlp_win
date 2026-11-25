# -*- coding: utf-8 -*-
"""yt-dlp 桌面版入口（pywebview + Web 前端）。"""
import ctypes
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(APP_DIR)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, APP_DIR)

import ytdlp_core as core  # noqa: E402
from host import Host  # noqa: E402
from api import Api  # noqa: E402

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def msgbox(text, title="yt-dlp 下载器"):
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)


def selftest():
    host = Host(CONFIG_FILE)
    cmd = core.build_download_cmd(host.cfg, ["https://www.youtube.com/watch?v=TEST"])
    print(json.dumps(cmd, ensure_ascii=False, indent=2))
    sys.stdout.flush()


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    try:
        import webview
    except ImportError:
        msgbox("缺少 pywebview。请在命令行执行：\npy -3 -m pip install pywebview")
        return
    host = Host(CONFIG_FILE)
    api = Api(host)
    try:
        window = webview.create_window(
            "yt-dlp 下载器   ·   可视化界面",
            url=os.path.join(APP_DIR, "ui", "index.html"),
            js_api=api, width=1100, height=900, min_size=(920, 700))
    except Exception as e:
        msgbox("创建窗口失败（可能缺少 WebView2 运行时）：\n%s\n\n请安装 WebView2：\n%s"
               % (e, WEBVIEW2_URL))
        return
    api.window = window

    def on_loaded():
        host.mark_ready()
        if host.cfg.get("ext_service", True):
            host.start_ext_service()
        else:
            host._put({"type": "svc", "text": "扩展服务：未启用", "ok": False})
        host.start_pump()

    def on_closing():
        if host.task_kind:
            host._put({"type": "exit_confirm"})
            return False
        host.shutdown()
        return True

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    try:
        webview.start(http_server=True, debug="--debug" in sys.argv)
    except Exception as e:
        msgbox("WebView2 启动失败：%s\n\n请安装 WebView2 运行时：\n%s" % (e, WEBVIEW2_URL))


if __name__ == "__main__":
    main()