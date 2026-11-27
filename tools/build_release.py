# -*- coding: utf-8 -*-
"""组装发布包：桌面端 zip + 浏览器扩展 zip。

用法：
    py -3 tools\\build_release.py [--out DIR]

桌面包是「源码 + runtime」的便携形态，解压后双击 YtDlpGUI\\启动.bat 即可；
扩展包解压后在 chrome://extensions 里按「加载已解压的扩展程序」加载。
仅使用标准库，不依赖测试或第三方包。
"""
import argparse
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DESKTOP_TOP = (
    "README.md",
    "ytdlp_core.py",
    "ytdlp_queue.py",
    "sniff_rules.py",
    "YtDlpGUI",
    "YtDlpWeb",
    "tools",
    "sniffer_extension",
    "runtime",
)

REQUIRED_FILES = (
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
    "sniffer_extension/manifest.json",
    "sniffer_extension/sniff_rules.js",
    "runtime/yt-dlp.exe",
    "runtime/ffmpeg.exe",
    "runtime/ffprobe.exe",
)

SKIP_DIR_NAMES = {"__pycache__", "node_modules"}
SKIP_FILE_NAMES = {"agent_service.json", ".mcp.json", "Thumbs.db", ".DS_Store"}
SKIP_REL_FILES = {
    "YtDlpGUI/config.json",
    "YtDlpGUI/history.json",
    "YtDlpWeb/config.json",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".bak", ".log", ".tmp", ".part", ".aria2", ".lock", ".pdb")


def _skip_dir(parts):
    name = parts[-1]
    if name in SKIP_DIR_NAMES:
        return True
    if name.startswith(".update_backup_"):
        return True
    if parts[:1] == ["YtDlpGUI"] and name == "legacy":
        return True
    if parts == ["runtime", "downloads"]:
        return True
    return False


def _skip_file(rel, name):
    if name in SKIP_FILE_NAMES or rel in SKIP_REL_FILES:
        return True
    if name.startswith(".update_backup_"):
        return True
    return name.lower().endswith(SKIP_SUFFIXES)


def _walk_files(root, top):
    base = os.path.join(root, top.replace("/", os.sep))
    if not os.path.exists(base):
        return
    if os.path.isfile(base):
        yield top
        return
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else [p for p in rel_dir.split(os.sep) if p not in ("", ".")]
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(parts + [d]))
        for name in sorted(filenames):
            rel = "/".join(parts + [name])
            if not _skip_file(rel, name):
                yield rel


def collect_desktop_files(root):
    files = []
    for top in DESKTOP_TOP:
        files.extend(_walk_files(root, top))
    return sorted(files)


def collect_extension_files(root):
    return sorted(_walk_files(root, "sniffer_extension"))


def _manifest_refs(manifest):
    refs = set()

    def add(value):
        if isinstance(value, str) and value and not any(c in value for c in "*?") \
                and "://" not in value:
            refs.add(value)

    background = manifest.get("background") or {}
    add(background.get("service_worker"))
    add(background.get("page"))
    for item in background.get("scripts") or []:
        add(item)

    for script in manifest.get("content_scripts") or []:
        for key in ("js", "css"):
            for item in script.get(key) or []:
                add(item)

    def add_icon(value):
        if isinstance(value, dict):
            for item in value.values():
                add(item)
        else:
            add(value)

    add_icon(manifest.get("icons"))
    action = manifest.get("action") or {}
    add(action.get("default_popup"))
    add_icon(action.get("default_icon"))
    add(manifest.get("options_page"))
    add((manifest.get("options_ui") or {}).get("page"))
    add(manifest.get("devtools_page"))
    add((manifest.get("side_panel") or {}).get("default_path"))
    for item in (manifest.get("chrome_url_overrides") or {}).values():
        add(item)
    return refs


def validate_extension_manifest(ext_dir):
    path = os.path.join(ext_dir, "manifest.json")
    if not os.path.isfile(path):
        return ["缺少扩展 manifest：%s" % path]
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        return ["扩展 manifest 无法读取（%s）：%s" % (e, path)]
    if not isinstance(manifest, dict):
        return ["扩展 manifest 根节点不是对象：%s" % path]
    problems = []
    for ref in sorted(_manifest_refs(manifest)):
        target = os.path.join(ext_dir, ref.replace("/", os.sep))
        if not os.path.isfile(target):
            problems.append("manifest 引用的文件不存在：sniffer_extension/%s" % ref)
    return problems


def validate_layout(root):
    problems = []
    for rel in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, rel.replace("/", os.sep))):
            problems.append("缺少必需文件：%s" % rel)

    internal = os.path.join(root, "runtime", "_internal")
    if not os.path.isdir(internal):
        problems.append("缺少目录：runtime/_internal")
    else:
        with os.scandir(internal) as entries:
            if next(entries, None) is None:
                problems.append("目录为空：runtime/_internal")

    ext_dir = os.path.join(root, "sniffer_extension")
    if not os.path.isdir(ext_dir):
        problems.append("缺少目录：sniffer_extension")
    elif os.path.isfile(os.path.join(ext_dir, "manifest.json")):
        problems.extend(validate_extension_manifest(ext_dir))
    return problems


def read_extension_version(root):
    path = os.path.join(root, "sniffer_extension", "manifest.json")
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    version = str((manifest or {}).get("version") or "").strip()
    if not version:
        raise ValueError("manifest.json 缺少 version 字段")
    return version


def read_server_version(root):
    path = os.path.join(root, "YtDlpWeb", "server.py")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return match.group(1) if match else None


def size_text(size):
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024.0)
    return "%.1f MB" % (size / (1024.0 * 1024.0))


def _setup_console():
    """Git Bash 的 Python 默认按 GBK 输出，会让中文变成乱码。"""
    if not os.environ.get("MSYSTEM"):
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_zip(root, files, out_path, prefix, extra_dirs=()):
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp_path = out_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as zf:
            for rel in extra_dirs:
                info = zipfile.ZipInfo(prefix + "/" + rel.strip("/") + "/")
                info.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(info, b"")
            for rel in sorted(files):
                source = os.path.join(root, rel.replace("/", os.sep))
                zf.write(source, prefix + "/" + rel)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return out_path


def main(argv=None, root=None):
    root = os.path.abspath(root or ROOT)
    parser = argparse.ArgumentParser(description="组装 yt-dlp 桌面端与浏览器扩展发布包")
    parser.add_argument("--out", default=None,
                        help="输出目录（默认：<仓库>/dist）")
    args = parser.parse_args(argv)

    problems = validate_layout(root)
    if problems:
        print("打包前检查未通过：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 1

    try:
        version = read_extension_version(root)
    except Exception as e:
        print("无法读取扩展版本：%s" % e, file=sys.stderr)
        return 1
    server_version = read_server_version(root)
    if server_version and server_version != version:
        print("警告：扩展版本 %s 与 YtDlpWeb/server.py 的 APP_VERSION %s 不一致"
              % (version, server_version), file=sys.stderr)

    out_dir = os.path.abspath(args.out) if args.out else os.path.join(root, "dist")
    desktop_files = collect_desktop_files(root)
    extension_files = collect_extension_files(root)
    extension_arcs = [rel.split("/", 1)[1] for rel in extension_files]

    desktop_zip = build_zip(
        root, desktop_files,
        os.path.join(out_dir, "yt-dlp_win-%s-desktop.zip" % version),
        "yt-dlp_win-%s" % version,
        extra_dirs=("runtime/downloads",))
    extension_zip = build_zip(
        os.path.join(root, "sniffer_extension"), extension_arcs,
        os.path.join(out_dir, "yt-dlp_win-%s-extension.zip" % version),
        "sniffer_extension-%s" % version)

    for label, path, count in (
            ("桌面端", desktop_zip, len(desktop_files)),
            ("扩展", extension_zip, len(extension_files))):
        print("%s：%s（%s，%d 个文件）"
              % (label, path, size_text(os.path.getsize(path)), count))
    return 0


if __name__ == "__main__":
    _setup_console()
    raise SystemExit(main())