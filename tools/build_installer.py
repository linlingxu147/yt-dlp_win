# -*- coding: utf-8 -*-
"""构建「鱿鱼浏览器工具包」Windows 安装包（完整版）。

流程：生成图标 / 版本资源 → PyInstaller 冻结出 squidkit.exe（自带 Python 运行时）
→ 组装安装载荷（源码 + runtime）→ 冻结产物无界面冒烟 → Inno Setup 编译 setup.exe。

与 tools/build_release.py 的 zip 便携包并存，互不影响。

用法：
    py -3 tools/build_installer.py [--version 1.12.0] [--skip-freeze] [--skip-inno]

构建工具默认放在 build/tools/（不入库）：
    py -3 -m pip install --target build/tools/pyinstaller "pyinstaller==6.22.3"
    Inno Setup 7 静默安装到 build/tools/inno7，或其 ISCC.exe 在 PATH / 默认安装位置。
WebView2 官方引导器（MicrosoftEdgeWebview2Setup.exe，约 1.8 MB）首次构建时自动下载到
build/tools/webview2/；下载失败只会让安装包退化为「检测到缺失就提示」，不影响出包。

仅使用标准库 + 上述两个构建工具，不改动业务模块行为。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "installer"))

import build_release  # noqa: E402
import make_icon  # noqa: E402
import squidkit_paths  # noqa: E402

INSTALLER_DIR = os.path.join(ROOT, "installer")
BUILD_DIR = os.path.join(ROOT, "build", "installer")
TOOLS_DIR = os.path.join(ROOT, "build", "tools")
DIST_DIR = os.path.join(ROOT, "dist")
APP_NAME = "鱿鱼浏览器工具包"
PUBLISHER = "SquidKit"
APP_ID_GUID = "{{6D9A2F41-3C7B-4E85-9A21-7F0C4B8E1D53}}"
ICON_SRC = os.path.join(ROOT, "sniffer_extension", "icon128.png")

# WebView2 Evergreen 引导器（微软官方、有签名，约 1.8 MB）。只在构建时下载，不进仓库
WEBVIEW2_DIR = os.path.join(TOOLS_DIR, "webview2")
WEBVIEW2_EXE = os.path.join(WEBVIEW2_DIR, "MicrosoftEdgeWebview2Setup.exe")
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# 安装载荷里需要的顶层项：不含 tools/（开发脚本）、tests/、docs/、legacy/
INSTALL_TOP = (
    "README.md",
    "ytdlp_core.py",
    "ytdlp_queue.py",
    "sniff_rules.py",
    "YtDlpGUI",
    "YtDlpWeb",
    "sniffer_extension",
    "runtime",
)
# 安装版用 squidkit.exe 启动，不再需要 bat
SKIP_REL = {"YtDlpGUI/启动.bat", "YtDlpWeb/启动.bat"}
REQUIRED_PAYLOAD = (
    "src/ytdlp_core.py",
    "src/ytdlp_queue.py",
    "src/sniff_rules.py",
    "src/YtDlpGUI/main.py",
    "src/YtDlpGUI/host.py",
    "src/YtDlpGUI/api.py",
    "src/YtDlpGUI/embedded.py",
    "src/YtDlpGUI/ui/index.html",
    "src/YtDlpWeb/server.py",
    "src/runtime/yt-dlp.exe",
    "src/runtime/ffmpeg.exe",
    "squidkit.exe",
    "squidkit.ico",
)


def version_4(version):
    parts = []
    for piece in str(version).split(".")[:4]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def read_version(root=ROOT):
    return build_release.read_extension_version(root)


def find_pyinstaller():
    """返回可执行 PyInstaller 的环境（None 表示不可用）。"""
    env = os.environ.copy()
    bundled = os.path.join(TOOLS_DIR, "pyinstaller")
    if os.path.isdir(os.path.join(bundled, "PyInstaller")):
        env["PYTHONPATH"] = bundled + os.pathsep + env.get("PYTHONPATH", "")
    try:
        r = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                           env=env, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    return env if r.returncode == 0 else None


def find_iscc():
    # 显式指定优先：非标准安装路径（自己解压的 Inno、CI 缓存目录）用 SQUIDKIT_ISCC 指过来
    explicit = os.environ.get("SQUIDKIT_ISCC")
    if explicit and os.path.isfile(explicit):
        return explicit
    local = os.environ.get("LOCALAPPDATA") or ""
    candidates = [
        os.path.join(TOOLS_DIR, "inno7", "ISCC.exe"),
        r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
        r"C:\Program Files\Inno Setup 7\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        # Inno Setup 允许「仅为我安装」，落在用户目录而不是 Program Files
        os.path.join(local, "Programs", "Inno Setup 6", "ISCC.exe") if local else "",
        os.path.join(local, "Programs", "Inno Setup 7", "ISCC.exe") if local else "",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which("ISCC") or shutil.which("iscc") or None


def ensure_webview2(path=WEBVIEW2_EXE, url=WEBVIEW2_URL, timeout=120):
    """确保有官方的 WebView2 引导器；下载失败不阻断构建。

    安装器会在目标机器上查 WebView2 是否已安装，缺了才静默跑这个引导器（需要联网）。
    没有它时安装包退化成「检测到缺失也不补装」，用户仍能在启动失败的弹窗里拿到下载地址，
    所以这里只警告、不让整个构建失败。
    """
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
            return path
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "SquidKit-Builder"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
        if os.path.getsize(path) > 1024 * 1024:
            return path
        raise RuntimeError("下载到的文件过小")
    except Exception as e:
        print("警告：WebView2 引导器下载失败（%s）；安装包将不含静默补装。" % e, file=sys.stderr)
        try:
            os.remove(path)      # 半截文件别留着，下次构建会重下
        except OSError:
            pass
        return None


def version_info_text(version):
    v4 = version_4(version)
    return """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%s, prodvers=%s,
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', '%s'),
        StringStruct('FileDescription', '%s'),
        StringStruct('FileVersion', '%s'),
        StringStruct('InternalName', 'squidkit'),
        StringStruct('OriginalFilename', 'squidkit.exe'),
        StringStruct('ProductName', '%s'),
        StringStruct('ProductVersion', '%s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ])
""" % (v4, v4, PUBLISHER, APP_NAME, version, APP_NAME, version)


def render_iss(template_path, dest_path, ctx):
    with open(template_path, "r", encoding="utf-8") as f:
        text = f.read()
    for key, value in ctx.items():
        text = text.replace("@@%s@@" % key, str(value))
    if "@@" in text:
        raise ValueError("模板里还有未替换的占位符：%s" % template_path)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(text)
    return dest_path


def collect_payload_files(root=ROOT):
    files = []
    for top in INSTALL_TOP:
        files.extend(build_release._walk_files(root, top))
    return sorted(rel for rel in files if rel not in SKIP_REL)


def assemble_payload(root, files, freeze_dir, payload_dir, icon_path):
    """幂等组装：只覆盖同名文件，不整目录删除。

    载荷是纯构建产物，覆盖比清空更安全（也避免触发批量删除保护）。
    需要彻底重建时手动删除 build/installer/payload 即可。
    """
    os.makedirs(payload_dir, exist_ok=True)
    shutil.copytree(freeze_dir, payload_dir, dirs_exist_ok=True)
    for rel in files:
        src = os.path.join(root, rel.replace("/", os.sep))
        dst = os.path.join(payload_dir, "src", rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(icon_path, os.path.join(payload_dir, "squidkit.ico"))
    return payload_dir


def validate_payload(payload_dir):
    return [rel for rel in REQUIRED_PAYLOAD
            if not os.path.isfile(os.path.join(payload_dir, rel.replace("/", os.sep)))]


def freeze(env, icon_path, version_info_path):
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", "squidkit",
        "--icon", icon_path,
        "--version-file", version_info_path,
        "--collect-all", "webview",
        "--collect-all", "pythonnet",
        "--hidden-import", "clr",
        "--paths", INSTALLER_DIR,
        "--distpath", BUILD_DIR,
        "--workpath", os.path.join(BUILD_DIR, "work"),
        "--specpath", BUILD_DIR,
        os.path.join(INSTALLER_DIR, "squidkit_launcher.py"),
    ]
    print("[1/5] 冻结 squidkit.exe …")
    r = subprocess.run(cmd, env=env, cwd=ROOT, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("PyInstaller 失败（退出码 %d）" % r.returncode)
    out = os.path.join(BUILD_DIR, "squidkit")
    if not os.path.isfile(os.path.join(out, "squidkit.exe")):
        raise RuntimeError("PyInstaller 没有产出 squidkit.exe：%s" % out)
    return out


def run_selftest(exe_path, log_path):
    print("[3/5] 冻结产物冒烟检查 …")
    env = os.environ.copy()
    env["SQUIDKIT_SELFTEST_LOG"] = log_path
    env["SQUIDKIT_DATA_DIR"] = os.path.join(BUILD_DIR, "selftest_data")
    if os.path.isfile(log_path):
        os.remove(log_path)
    try:
        r = subprocess.run([exe_path, "--selftest"], env=env, timeout=120)
    except Exception as e:
        raise RuntimeError("冒烟检查无法启动 exe：%s" % e)
    if not os.path.isfile(log_path):
        raise RuntimeError("冒烟检查没有写出日志（退出码 %d）" % r.returncode)
    with open(log_path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    print(text)
    if "RESULT=OK" not in text:
        raise RuntimeError("冒烟检查未通过，详见 %s" % log_path)


def compile_installer(iscc, iss_path, version, out_dir):
    print("[4/5] 编译 Inno Setup 安装包 …")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [iscc, "/Q", "/O" + out_dir,
           "/F" + "squidkit-%s-setup" % version, iss_path]
    r = subprocess.run(cmd, cwd=ROOT, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("ISCC 失败（退出码 %d）" % r.returncode)
    exe = os.path.join(out_dir, "squidkit-%s-setup.exe" % version)
    if not os.path.isfile(exe):
        raise RuntimeError("ISCC 没有产出安装包：%s" % exe)
    return exe


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建鱿鱼浏览器工具包 Windows 安装包")
    parser.add_argument("--version", default=None, help="版本号（默认读扩展 manifest）")
    parser.add_argument("--out", default=None, help="输出目录（默认 dist）")
    parser.add_argument("--skip-freeze", action="store_true", help="复用已有冻结产物")
    parser.add_argument("--skip-selftest", action="store_true", help="跳过冻结产物冒烟")
    parser.add_argument("--skip-inno", action="store_true", help="只组装载荷，不编译安装包")
    args = parser.parse_args(argv)

    build_release._setup_console()
    version = args.version or read_version()
    out_dir = os.path.abspath(args.out) if args.out else DIST_DIR
    os.makedirs(BUILD_DIR, exist_ok=True)

    icon_path = os.path.join(BUILD_DIR, "squidkit.ico")
    version_info_path = os.path.join(BUILD_DIR, "version_info.txt")
    freeze_dir = os.path.join(BUILD_DIR, "squidkit")
    payload_dir = os.path.join(BUILD_DIR, "payload", "squidkit")
    iss_path = os.path.join(BUILD_DIR, "squidkit.iss")

    print("[0/5] 生成图标与版本资源（版本 %s）" % version)
    make_icon.build_ico(ICON_SRC, icon_path)
    with open(version_info_path, "w", encoding="utf-8") as f:
        f.write(version_info_text(version))

    if not args.skip_freeze:
        env = find_pyinstaller()
        if env is None:
            print("缺少 PyInstaller。请先执行：", file=sys.stderr)
            print('  py -3 -m pip install --target build/tools/pyinstaller "pyinstaller==6.22.3"',
                  file=sys.stderr)
            return 1
        freeze(env, icon_path, version_info_path)
    elif not os.path.isfile(os.path.join(freeze_dir, "squidkit.exe")):
        print("没有可复用的冻结产物：%s" % freeze_dir, file=sys.stderr)
        return 1

    print("[2/5] 组装安装载荷 …")
    webview2 = ensure_webview2()
    if webview2:
        print("      WebView2 引导器：已内置（缺失时静默补装）")
    else:
        print("      WebView2 引导器：未内置（安装时只检测并提示）")
    files = collect_payload_files()
    assemble_payload(ROOT, files, freeze_dir, payload_dir, icon_path)
    missing = validate_payload(payload_dir)
    if missing:
        print("载荷缺少文件：", file=sys.stderr)
        for rel in missing:
            print("  - " + rel, file=sys.stderr)
        return 1

    exe_path = os.path.join(payload_dir, "squidkit.exe")
    if not args.skip_selftest:
        try:
            run_selftest(exe_path, os.path.join(BUILD_DIR, "selftest.txt"))
        except RuntimeError as e:
            print("警告：%s" % e, file=sys.stderr)
            print("继续产出安装包，但请人工验证启动。", file=sys.stderr)

    render_iss(os.path.join(INSTALLER_DIR, "squidkit.iss.tpl"), iss_path, {
        "VERSION": version,
        "VERSION4": ".".join(str(n) for n in version_4(version)),
        "APPNAME": APP_NAME,
        "PUBLISHER": PUBLISHER,
        "APPID": APP_ID_GUID,
        "AUMID": squidkit_paths.APP_USER_MODEL_ID,
        "PAYLOAD": payload_dir,
        "ICON": icon_path,
        # 引导器只在安装时用一下：放 {tmp}，装完即删，不留在 {app} 里占空间
        "WEBVIEW2FILE": ('Source: "%s"; DestDir: "{tmp}"; Flags: deleteafterinstall'
                         % webview2.replace("\\", "/")) if webview2 else "",
        "WEBVIEW2RUN": ('Filename: "{tmp}\\MicrosoftEdgeWebview2Setup.exe"; '
                        'Parameters: "/silent /install"; '
                        'StatusMsg: "正在补装 WebView2 运行时（需要联网）…"; '
                        'Flags: runhidden waituntilterminated; Check: NeedWebView2'
                        ) if webview2 else "",
    })

    if args.skip_inno:
        print("[5/5] 已跳过安装包编译。载荷：%s" % payload_dir)
        print("      脚本：%s" % iss_path)
        return 0

    iscc = find_iscc()
    if not iscc:
        print("缺少 Inno Setup 7（ISCC.exe）。安装后重试，或用 --skip-inno 只产出载荷。",
              file=sys.stderr)
        return 1
    setup_exe = compile_installer(iscc, iss_path, version, out_dir)
    print("[5/5] 完成：%s（%s）" % (setup_exe, build_release.size_text(os.path.getsize(setup_exe))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
