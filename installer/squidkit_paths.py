# -*- coding: utf-8 -*-
"""安装版的路径解析与 Windows 应用身份。

便携版把所有状态写在程序目录；安装版把配置与历史放到 %APPDATA%\\SquidKit，
默认下载目录放到「下载」文件夹，二进制 runtime 仍留在安装目录（用户可写、
自动更新可替换）。本模块不依赖 frozen 环境，可以直接单测。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys

APP_USER_MODEL_ID = "SquidKit.Desktop"
DATA_DIR_ENV = "SQUIDKIT_DATA_DIR"
CONFIG_FILE_ENV = "SQUIDKIT_CONFIG_FILE"
DOWNLOAD_DIR_ENV = "SQUIDKIT_DOWNLOAD_DIR"
DATA_DIR_NAME = "SquidKit"


def installed_root(executable=None):
    """onedir 布局：{app}\\squidkit.exe，源码与 runtime 在 {app}\\src。

    冻结后 sys.executable 就是安装目录里的 exe（不是 _MEIPASS 临时目录），
    这一点是整套路径解析的基石。
    """
    exe = executable or getattr(sys, "executable", "")
    return os.path.dirname(os.path.abspath(exe))


def src_dir(root=None):
    return os.path.join(root if root is not None else installed_root(), "src")


def data_dir(environ=None, home=None):
    env = environ if environ is not None else os.environ
    override = env.get(DATA_DIR_ENV)
    if override:
        return os.path.abspath(override)
    base = env.get("APPDATA") or (home or os.path.expanduser("~"))
    return os.path.join(base, DATA_DIR_NAME)


def default_download_dir(home=None, environ=None):
    env = environ if environ is not None else os.environ
    override = env.get(DOWNLOAD_DIR_ENV)
    if override:
        return os.path.abspath(override)
    home = home or os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    base = downloads if os.path.isdir(downloads) else home
    return os.path.join(base, DATA_DIR_NAME)


def config_file(environ=None, home=None):
    env = environ if environ is not None else os.environ
    override = env.get(CONFIG_FILE_ENV)
    if override:
        return os.path.abspath(override)
    return os.path.join(data_dir(env, home), "config.json")


def set_app_user_model_id(app_id=APP_USER_MODEL_ID):
    """让任务栏分组、跳转列表、通知把窗口归到本应用，而不是 pythonw.exe。"""
    if os.name != "nt":
        return False
    try:
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_long
        return fn(app_id) == 0
    except Exception:
        return False


def migrate_legacy_state(src_root, dest_cfg):
    """把便携版放在 src\\YtDlpGUI 下的 config/history 一次性搬到数据目录。

    只搬一次：目标已存在就跳过，绝不覆盖用户在安装版里已经改过的配置。
    用复制而不是移动，避免破坏仍可能继续使用的便携版目录。
    返回被搬走的文件名列表。
    """
    legacy = os.path.join(src_root, "YtDlpGUI")
    dest_dir = os.path.dirname(dest_cfg)
    moved = []
    for name in ("config.json", "history.json"):
        old = os.path.join(legacy, name)
        new = os.path.join(dest_dir, name)
        if not os.path.isfile(old) or os.path.exists(new):
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(old, new)
            moved.append(name)
        except OSError:
            pass
    return moved
