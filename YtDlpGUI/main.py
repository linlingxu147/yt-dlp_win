# -*- coding: utf-8 -*-
"""鱿鱼浏览器工具包桌面版入口（pywebview + Web 前端）。"""
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

CONFIG_FILE = os.environ.get("SQUIDKIT_CONFIG_FILE") or os.path.join(APP_DIR, "config.json")
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def msgbox(text, title="鱿鱼浏览器工具包"):
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)


def webview_version_ok():
    """内嵌浏览器依赖 pywebview 6 的 response_received / 内部字段。"""
    try:
        from importlib.metadata import version
        return int(str(version("pywebview")).split(".")[0]) >= 6
    except Exception:
        return True


def start_hang_watchdog(window):
    """界面卡死时把 Python 线程栈写到 %TEMP%\\ytdlp_gui_hang.log。

    只在主窗口连续 8 秒不处理消息时写一次，正常使用不会产生日志。
    这是给「启动/切浏览器后未响应」这类难复现问题留证据的，不影响业务流程。
    """
    import faulthandler
    import tempfile
    import threading
    import time

    log_path = os.path.join(tempfile.gettempdir(), "ytdlp_gui_hang.log")

    def loop():
        hwnd = 0
        for _ in range(60):
            try:
                hwnd = int(window.native.Handle.ToInt64())
                if hwnd:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not hwnd:
            _dump(hwnd=None)
            return
        last = 0.0
        while True:
            time.sleep(2)
            try:
                hung = bool(ctypes.windll.user32.IsHungAppWindow(ctypes.c_void_p(hwnd)))
            except Exception:
                return
            if not hung:
                last = 0.0
                continue
            now = time.time()
            if now - last < 30:
                continue
            last = now
            _dump(hwnd=hwnd)

    def _dump(hwnd):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n=== %s 界面未响应（hwnd=%s）===\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), hwnd))
                faulthandler.dump_traceback(file=f, all_threads=True)
        except Exception:
            pass

    threading.Thread(target=loop, daemon=True).start()


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
        msgbox("缺少 pywebview。请在命令行执行：\npy -3 -m pip install \"pywebview>=6\"")
        return
    if not webview_version_ok():
        msgbox("内嵌浏览器需要 pywebview 6.0 或更新版本。\n"
               "请在命令行执行：\npy -3 -m pip install -U \"pywebview>=6\"")
        return
    host = Host(CONFIG_FILE)
    api = Api(host)
    try:
        window = webview.create_window(
            "鱿鱼浏览器工具包   ·   可视化界面",
            url=os.path.join(APP_DIR, "ui", "index.html"),
            js_api=api, width=1100, height=900, min_size=(920, 700))
    except Exception as e:
        msgbox("创建窗口失败（可能缺少 WebView2 运行时）：\n%s\n\n请安装 WebView2：\n%s"
               % (e, WEBVIEW2_URL))
        return
    api.bind_window(window)
    start_hang_watchdog(window)

    def on_loaded():
        host.mark_ready()
        if host.cfg.get("ext_service", True):
            host.start_ext_service()
        else:
            host._put({"type": "svc", "text": "扩展服务：未启用", "ok": False})
        host.start_pump()

    def on_closing():
        snap = host.queue.snapshot()
        # 暂停任务也算活跃（有 .part、退出要记 stopped），不能悄悄关掉
        if (host.aux_kind or host.updating or snap["running"] or snap["queued"]
                or snap["paused"]):
            host._put({"type": "exit_confirm",
                       "text": "%d 个下载中 / %d 个排队 / %d 个已暂停，退出将停止全部任务。"
                               % (snap["running"], snap["queued"], snap["paused"])})
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