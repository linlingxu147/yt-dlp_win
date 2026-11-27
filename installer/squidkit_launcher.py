# -*- coding: utf-8 -*-
"""安装版冻结入口（PyInstaller onedir）。

只做四件事：搭好 sys.path、准备用户数据目录、设置 Windows 应用身份，
然后动态加载安装目录里的 src\\YtDlpGUI\\main.py。

必须动态加载：静态 import 会让 PyInstaller 把业务模块收进 _MEIPASS，
ytdlp_core.__file__ 就变成临时目录，BASE_DIR / RUNTIME_DIR 与自动更新全乱。
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
import sys

import webview  # noqa: F401 —— 让 PyInstaller 把 WebView2 依赖收进 _internal

from squidkit_paths import (
    CONFIG_FILE_ENV, DATA_DIR_ENV, config_file, data_dir,
    default_download_dir, installed_root, migrate_legacy_state,
    set_app_user_model_id, src_dir,
)

SELFTEST_LOG_ENV = "SQUIDKIT_SELFTEST_LOG"


def _fatal(text):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "鱿鱼浏览器工具包", 0x10)
    except Exception:
        try:
            print(text, file=sys.stderr)
        except Exception:
            pass


def _selftest(log_path, src):
    """冻结产物的无界面冒烟：只验证模块能加载、路径指向安装目录。"""
    lines = []
    try:
        import ytdlp_core as core
        import ytdlp_queue  # noqa: F401
        import host  # noqa: F401
        import api  # noqa: F401
        import embedded  # noqa: F401
        import updater  # noqa: F401
        lines.append("frozen=%s" % bool(getattr(sys, "frozen", False)))
        lines.append("executable=%s" % sys.executable)
        lines.append("src=%s" % src)
        lines.append("BASE_DIR=%s" % core.BASE_DIR)
        lines.append("RUNTIME_DIR=%s" % core.RUNTIME_DIR)
        lines.append("yt-dlp=%s" % core.find_tool("yt-dlp.exe"))
        lines.append("ffmpeg=%s" % core.find_tool("ffmpeg.exe"))
        lines.append("webview=%s" % getattr(webview, "__version__", "?"))
        ok = os.path.normcase(os.path.abspath(core.BASE_DIR)) == os.path.normcase(src)
        lines.append("RESULT=%s" % ("OK" if ok else "BASE_DIR_MISMATCH"))
    except Exception as e:
        lines.append("RESULT=FAIL %r" % (e,))
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0 if lines and lines[-1].startswith("RESULT=OK") else 1


def main():
    root = installed_root()
    src = src_dir(root)
    gui = os.path.join(src, "YtDlpGUI", "main.py")
    if not os.path.isfile(gui):
        _fatal("缺少程序文件：src\\YtDlpGUI\\main.py\n请重新安装本程序。")
        return 1

    ddir = data_dir()
    os.makedirs(ddir, exist_ok=True)
    os.environ[DATA_DIR_ENV] = ddir
    cfg = config_file()
    os.environ[CONFIG_FILE_ENV] = cfg
    migrate_legacy_state(src, cfg)
    set_app_user_model_id()

    sys.path.insert(0, os.path.join(src, "YtDlpGUI"))
    sys.path.insert(0, src)

    import ytdlp_core as core
    core.DEFAULT_CONFIG["outdir"] = default_download_dir()

    log_path = os.environ.get(SELFTEST_LOG_ENV)
    if "--selftest" in sys.argv and log_path:
        return _selftest(log_path, src)

    spec = importlib.util.spec_from_file_location("squidkit_gui_main", gui)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
