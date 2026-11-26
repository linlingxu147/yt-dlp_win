# -*- coding: utf-8 -*-
"""yt-dlp 更新镜像回退：官方 -U 失败时，从镜像下载 onedir 包并安全替换。

只做三件事：拼镜像 URL、下载并 SHA256 校验、解压替换 runtime（失败回滚）。
不在这里决定什么时候用镜像——由 Host 在官方更新失败后调用。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from urllib.request import ProxyHandler, Request, build_opener

LATEST_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
ZIP_ASSET = "yt-dlp_win.zip"
SUMS_ASSET = "SHA2-256SUMS"
USER_AGENT = "yt-dlp-win-updater/1.0"
MAX_BYTES = 200 * 1024 * 1024


class UpdateCancelled(Exception):
    pass


def mirror_url(prefix, official_url):
    """镜像前缀 + 官方地址；支持 `{url}` 占位符，也支持 ghproxy 的路径前缀。"""
    p = str(prefix or "").strip()
    if not p.lower().startswith(("http://", "https://")):
        return ""
    if "{url}" in p:
        return p.replace("{url}", official_url)
    return p.rstrip("/") + "/" + official_url


def parse_checksum(text, name):
    """从 SHA2-256SUMS 里取出指定文件名的 64 位十六进制摘要。"""
    for line in str(text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, fname = parts
        fname = fname.lstrip("*").strip()
        if os.path.basename(fname) == name and len(digest) == 64:
            return digest.lower()
    return ""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _opener(proxy=""):
    # 镜像默认直连；需要代理时由调用方显式传入。ProxyHandler({}) 同时屏蔽系统代理，
    # 免得更新时又被那个已经挂掉的下载代理拖住。
    if proxy:
        return build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return build_opener(ProxyHandler({}))


def download(url, dest, proxy="", timeout=90, abort=None, progress=None):
    """流式下载到 dest；abort() 为真或超限时中止。返回字节数。"""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    opener = _opener(proxy)
    total = 0
    length = 0
    with opener.open(req, timeout=timeout) as r, open(dest, "wb") as f:
        try:
            length = int(r.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        while True:
            if abort and abort():
                raise UpdateCancelled()
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise RuntimeError("更新包超过 %d MB，已中止" % (MAX_BYTES // 1024 // 1024))
            if progress:
                progress(total, length)
    if length and total != length:
        raise RuntimeError("下载不完整（%d/%d 字节）" % (total, length))
    return total


def _safe_extract(zf, dest):
    base = os.path.abspath(dest)
    for info in zf.infolist():
        target = os.path.abspath(os.path.join(base, info.filename))
        if target != base and not target.startswith(base + os.sep):
            raise RuntimeError("压缩包内路径异常：%s" % info.filename)
    zf.extractall(base)


def _payload_root(tmp):
    for root, _dirs, files in os.walk(tmp):
        if "yt-dlp.exe" in files:
            return root
    return ""


def verify_version(exe):
    """运行新 exe 的 --version；返回 (ok, version, error)。"""
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        p = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=30, creationflags=flags)
        out = (p.stdout or "").strip().splitlines()
        if p.returncode == 0 and out:
            return True, out[0].strip(), ""
        err = (p.stderr or "").strip() or ("退出码 %s" % p.returncode)
        return False, "", err[:300]
    except Exception as e:
        return False, "", str(e)


def _prune_backups(runtime_dir, keep):
    try:
        names = sorted(n for n in os.listdir(runtime_dir) if n.startswith(".update_backup_"))
    except OSError:
        return
    for name in names[:-max(1, int(keep))]:
        shutil.rmtree(os.path.join(runtime_dir, name), ignore_errors=True)


def install_zip(zip_path, runtime_dir, verify=None, keep_backups=1):
    """解压 zip 并替换 runtime 的 yt-dlp.exe/_internal；失败回滚。

    返回 (ok, version, backup_dir, error)。
    """
    runtime_dir = os.path.abspath(runtime_dir)
    exe = os.path.join(runtime_dir, "yt-dlp.exe")
    internal = os.path.join(runtime_dir, "_internal")
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_%d" % os.getpid()
    backup = os.path.join(runtime_dir, ".update_backup_" + stamp)
    tmp = tempfile.mkdtemp(prefix="ytdlp_update_")
    backup_ready = False
    backed = []          # 已移进备份的旧文件
    installed = []       # 已装进 runtime 的新文件
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError("压缩包损坏：%s" % bad)
            _safe_extract(zf, tmp)
        root = _payload_root(tmp)
        new_exe = os.path.join(root, "yt-dlp.exe")
        if not root or not os.path.isfile(new_exe):
            raise RuntimeError("更新包里没有 yt-dlp.exe")
        new_internal = os.path.join(root, "_internal")

        os.makedirs(backup, exist_ok=True)
        backup_ready = True
        if os.path.isfile(exe):
            shutil.move(exe, os.path.join(backup, "yt-dlp.exe"))
            backed.append("yt-dlp.exe")
        if os.path.isdir(internal):
            shutil.move(internal, os.path.join(backup, "_internal"))
            backed.append("_internal")

        shutil.move(new_exe, exe)
        installed.append("yt-dlp.exe")
        if os.path.isdir(new_internal):
            shutil.move(new_internal, internal)
            installed.append("_internal")

        version = ""
        if verify is not None:
            ok, version, err = verify(exe)
            if not ok:
                raise RuntimeError("新 yt-dlp 无法启动：%s" % (err or "未知原因"))
        _prune_backups(runtime_dir, keep_backups)
        return True, version, backup, ""
    except Exception as e:
        # 只有「已经把旧文件移进备份」后才需要回滚；解压/校验阶段失败时
        # 当前 runtime 还没被碰过，乱删会直接毁掉原来的 yt-dlp。
        if backup_ready:
            try:
                for name in installed:
                    p = os.path.join(runtime_dir, name)
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    elif os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                for name in backed:
                    b = os.path.join(backup, name)
                    if os.path.exists(b):
                        shutil.move(b, os.path.join(runtime_dir, name))
            except Exception:
                pass
        return False, "", backup if backup_ready else "", str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)