# -*- coding: utf-8 -*-
"""鱿鱼浏览器工具包的共用核心。

桌面版（YtDlpGUI\\main.py）与扩展服务（YtDlpWeb\\server.py，原网页版）原先各自复制了一份
1500 行代码，改一处要改两处、还容易漂移。这里把「与界面无关」的部分集中起来：

  * 模式 / 格式表达式 / 预设 UA / 浏览器列表 / 诊断规则
  * 命令行构建（下载、探测、格式列表、排障探测）
  * 进度解析（--progress-template 的机器可读行，旧正则作兜底）
  * 进程启动封装、连接重置的自愈阶梯、配置读写（含 headers 清洗与原子写）

约定：本模块只依赖标准库，绝不 import tkinter / http.server。
"""

import contextlib
import hashlib
import ipaddress
import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

# 本文件在项目根目录；便携运行文件统一放在 runtime/（exe / _internal / ffmpeg / downloads），
# 源码目录保持干净。旧布局的根目录 exe 仍然兼容。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
DOWNLOAD_DIR = os.path.join(RUNTIME_DIR, "downloads")
# 本地服务的 agent 发现文件（端口 + 临时 token）；只在启动服务时写入
AGENT_DISCOVERY_FILE = os.path.join(RUNTIME_DIR, "agent_service.json")
CREATE_NO_WINDOW = 0x08000000
PORT_RANGE = range(8765, 8776)

# ---------------- 模式与预设 ----------------

# 模式存的是稳定的 ASCII 键，界面显示中文标签：
# 以前直接把中文当配置值，改一次文案就让所有老配置的下载模式悄悄退回默认。
MODE_LABELS = [
    ("best",       "最佳画质（视频+音频自动合并）"),
    ("single",     "最佳单文件（不合并，画质较低）"),
    ("audio_mp3",  "仅音频（转 MP3）"),
    ("audio_raw",  "仅音频（保持原格式）"),
    ("video_only", "仅视频（无音轨）"),
    ("custom",     "自定义格式代码"),
]
MODE_KEYS = [k for k, _ in MODE_LABELS]
MODE_CHOICES = [label for _, label in MODE_LABELS]   # 下拉框直接显示中文标签
MODE_NAME = dict(MODE_LABELS)                    # 键 → 界面标签
_LABEL_TO_MODE = {v: k for k, v in MODE_LABELS}  # 兼容旧配置里存的中文标签
# 各模式默认的格式选择表达式；custom 没有默认值，由用户手填
MODE_FMT = {"best": "bv*+ba/b", "single": "b", "audio_mp3": "ba",
            "audio_raw": "ba", "video_only": "bv"}


def mode_key(value):
    """把配置里的 mode（新键或旧中文标签）归一成 ASCII 键。"""
    v = str(value or "").strip()
    return v if v in MODE_KEYS else _LABEL_TO_MODE.get(v, "best")

TEMPLATES = [
    "%(title)s.%(ext)s",
    "%(title)s [%(id)s].%(ext)s",
    "%(uploader)s - %(title)s.%(ext)s",
    "%(playlist_title)s/%(playlist_index)s - %(title)s.%(ext)s",
    "%(upload_date>%Y-%m-%d)s %(title)s.%(ext)s",
]

UA_PRESETS = {
    "Chrome / Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Edge / Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Firefox / Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
                         "Gecko/20100101 Firefox/133.0",
    "iPhone / Safari": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Android / Chrome": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
}
# 排障/一键修复统一用桌面版那一条 Chrome UA，避免两端修出来的结果不一样
UA_WINDOWS = UA_PRESETS["Chrome / Windows"]

BROWSERS = ["（不使用）", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "whale", "chromium"]

# ---------------- 失败诊断 ----------------
# 常见失败原因 → 可执行建议。fix 供界面「一键应用」直接改设置，省去手填。
# 说明：DRM 一类属于版权保护机制，只做识别与解释，不提供绕过手段。

URL_RE = re.compile(r"https?://[^\s'\"<>()]+", re.I)


def scrub_urls(text):
    """把 URL 换成占位符。

    诊断看的是 yt-dlp 的报错措辞，不是链接本身：地址里带 ?region=1、&timeout=30、
    ?referer=... 时，不抹掉就会把「正常下载中」判成地区限制 / 超时 / 防盗链。
    """
    return URL_RE.sub(" <url> ", str(text or ""))


DIAG_RULES = [
    (r"(?i)unsupported url|unsupported scheme|no supported schemes|could not detect",
     "unsupported", "站点或链接未被 yt-dlp 识别",
     ["先点「更新 yt-dlp」——站点改版后通常要靠新版提取器",
      "勾选「通用提取器兜底」再试一次",
      "确认填的是具体视频页，而不是首页、频道页或搜索页"],
     {"generic": True}),
    (r"(?i)could not copy \w*(?:\s+\w+)?\s*cookie database|"
     r"failed to (?:copy|read|decrypt) .{0,24}cookie database|"
     r"cookie database is locked",
     "cookies", "浏览器 Cookies 读不出来（数据库被浏览器独占）",
     ["这个浏览器的 Cookies 数据库正被它自己锁着：完全退出浏览器后重试最直接",
      "更省事：用浏览器扩展面板里的「下载」——扩展直接把本页 cookie 交给本程序，不碰数据库",
      "或者导出 cookies.txt，填到「Cookies 文件」"],
     {}),
    (r"(?i)http error 403|forbidden|hotlink|防盗链|referer",
     "403", "被防盗链拦截（403）",
     ["填「Referer」为该站点域名，例如 https://www.example.com/",
      "把「User-Agent」改成浏览器 UA",
      "改用「浏览器 Cookies」——登录态通常能过防盗链"],
     {"ua": True, "referer": True}),
    (r"(?i)http error 401|sign in|log in|登录|private video|私享|members[- ]only|需要登录",
     "login", "需要登录才能访问",
     ["在「浏览器 Cookies」里选你正在用的浏览器，直接从浏览器读登录态",
      "或导出 cookies.txt 后填到「Cookies 文件」"],
     {"browser": True}),
    (r"(?i)sign in to confirm|not a bot|bot check|captcha|recaptcha|hcaptcha|"
     r"are you a robot|confirm you(?:'re| are) (?:a )?bot|人机验证|请完成验证",
     "bot", "站点要求人机验证",
     ["用「浏览器 Cookies」（选你已登录的那个浏览器）",
      "换个代理或稍后再试"],
     {"browser": True}),
    (r"(?i)not available in your (?:country|region)|country[- ]?restrict|"
     r"geo[- ]?(?:restrict|check|block)|region[- ]?block|blocked in your|"
     r"uploader has not made|地区限制|仅限.{0,6}地区|不在你所在",
     "geo", "存在地区限制",
     ["在「代理」里填可用代理地址，如 http://127.0.0.1:7890",
      "注意：本机系统代理目前连不通，必须在界面里显式填写"],
     {"proxy": True}),
    (r"(?i)widevine|playready|fairplay|\bdrm\b|encrypted media|contentprotection|has drm",
     "drm", "内容受 DRM 加密保护",
     ["这是版权保护机制：地址能抓到，但明文不对外暴露，合规工具无法也不应获取",
      "请改用该平台官方提供的离线下载或正版渠道"],
     {}),
    (r"(?i)age[- ]gated|age verification|年龄",
     "age", "年龄限制内容",
     ["用「浏览器 Cookies」登录后重试",
      "取消「快速探测」让它解析完整信息"],
     {"browser": True}),
    (r"(?i)http error 429|too many requests|rate limit",
     "429", "被限流（429）",
     ["降低「分片并发 -N」，或填「限速」如 2M",
      "稍后再试，或换个代理"],
     {"rate": True}),
    (r"(?i)http error 400|bad request|invalid request",
     "400", "请求被判非法（400）",
     ["多半是把播放器内部接口当成页面链接了：直接点「下载本页」，或选嗅探列表里的 M3U8/VIDEO 条目",
      "清掉「Referer / User-Agent / 附加参数」再试一次",
      "嗅探到的地址带时效签名，隔太久要回页面重新播放生成新链接"],
     {}),
    (r"(?i)timed out|timeout|urlopen error|ssl|proxyerror|bad gateway|\b502\b|"
     r"connection (?:reset|refused|aborted)|name or service not known|getaddrinfo",
     "net", "网络不通或代理不可用",
     ["检查「代理」地址是否真的可用（本机系统代理目前连不通，需在界面里显式填写）",
      "确认浏览器能正常打开该站点，必要时换个网络"],
     {"proxy": True}),
    (r"(?i)requested format is not available|no video formats|format .{0,20}not available",
     "fmt", "所选格式不可用",
     ["点「查看可用格式」，挑一个列表里真实存在的 ID",
      "或把下载模式改回「最佳画质」，让它自动挑"],
     {"fmt": True}),
    (r"(?i)ffmpeg|merger|post[- ]process|转换失败|合并失败",
     "ffmpeg", "后处理（合并 / 转码）失败",
     ["确认 ffmpeg.exe 在 runtime 目录（或程序目录 / PATH）",
      "或把「合并容器」改成「不转换」"],
     {}),
    (r"(?i)video unavailable|视频不存在|已删除|\bremoved\b|deleted|does not exist|http error 404",
     "gone", "视频本身不可用（已删除 / 私有 / 链接失效）",
     ["换一个链接试试——这不是配置问题，也不是工具问题"],
     {}),
]


def diagnose(text, max_hits=3):
    """从 yt-dlp 输出中归类失败原因，返回 [{code,title,advice,fix}, ...]。"""
    if not text or not text.strip():
        return []
    text = scrub_urls(text)
    hits, seen = [], set()
    for pat, code, title, advices, fix in DIAG_RULES:
        if code in seen:
            continue
        if re.search(pat, text):
            seen.add(code)
            hits.append({"code": code, "title": title, "advice": advices, "fix": fix})
        if len(hits) >= max_hits:
            break
    if not hits and re.search(r"(?i)error|错误", text):
        hits.append({
            "code": "unknown", "title": "未能自动归类，请展开运行日志看原文",
            "advice": ["先试「更新 yt-dlp」", "把日志里的 ERROR 原文贴出来排查"],
            "fix": {}})
    return hits


def diagnose_pairs(text, max_hits=3):
    """桌面版日志用的简版：[(标题, [建议...]), ...]。"""
    return [(h["title"], h["advice"]) for h in diagnose(text, max_hits)]


# ---------------- 输出解析 ----------------

PROG_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+~?\s*(?P<size>\S+)"
    r"(?:\s+at\s+(?P<speed>[\d.]+\s*\S+/s))?(?:\s+ETA\s+(?P<eta>\S+))?",
)
FILE_RE = re.compile(r"^\[download\]\s+Destination:\s*(?P<path>.+)$")
# aria2c 的控制台读数：外部下载器不走 --progress-template，只有这一种进度源。
#   [#d4498d 8.4MiB/40MiB(21%) CN:1 DL:4.0MiB ETA:7s]
# 刚开始时可能没有百分号/ETA，总量也可能还是 0B。
ARIA_RE = re.compile(
    r"\[#(?P<id>[0-9a-zA-Z]+)\s+(?P<dl>[^\s/]+)/(?P<total>[^\s(]+)"
    r"(?:\((?P<pct>\d+(?:\.\d+)?)%)?(?P<rest>[^\]]*)\]"
)
_ARIA_FACTORS = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}
ITEM_RE = re.compile(r"Downloading\s+(?:item|video|playlist\s+entry)?\s*(\d+)\s+of\s+(\d+)", re.I)
# 去掉中间格式标记，例如 xxx.f137.mp4 -> xxx.mp4
FRAG_RE = re.compile(r"\.[fF]\d+\.(?=[^.]+$)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 机器可读的进度行：不依赖中英文文案与列宽，--newline 下每次刷新一行。
# 注意：不要用 --print 取最终文件名——--print 会隐含 --quiet/--simulate，
# 会把进度与下载本身一起关掉；文件名从 progress.filename 字段取即可。
PROGRESS_TEMPLATE = (
    "download:PROG|%(progress.status)s|%(progress.downloaded_bytes)s|"
    "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
    "%(progress.speed)s|%(progress.eta)s|%(progress.filename)s"
)
PROG_MARK = "PROG|"
_NA_VALUES = ("", "NA", "None", "none", "null", "N/A")


def _tool_dirs():
    """运行文件查找顺序：runtime/ 优先，其次兼容旧布局的项目根。"""
    return (RUNTIME_DIR, BASE_DIR)


def find_tool(name):
    """优先使用 runtime/ 里的 exe，其次旧布局的根目录，找不到就交给 PATH。"""
    for d in _tool_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return name


def which_tool(name):
    """真的能把 name 跑起来吗？返回可执行文件路径，找不到返回空串。

    find_tool 找不到时会原样返回名字（让 yt-dlp 自己报错），适合「交给 PATH」；
    但 aria2c 这种可选外部程序必须先确认存在，否则命令行一定失败。
    """
    for d in _tool_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    stem = os.path.splitext(name)[0]
    return shutil.which(stem) or shutil.which(name) or ""


def decode_bytes(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gbk", errors="replace")


def fmt_dur(sec):
    try:
        sec = int(float(sec or 0))
    except (TypeError, ValueError):
        return "-"
    if sec <= 0:
        return "-"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(b):
    try:
        b = float(b or 0)
    except (TypeError, ValueError):
        return "-"
    if b <= 0:
        return "-"
    for unit in ("KB", "MB", "GB"):
        b /= 1024.0
        if b < 1024:
            return f"{b:.1f} {unit}"
    return f"{b:.1f} TB"


def _aria_bytes(text):
    """aria2c 的 '8.4MiB' / '125KiB' / '0B' → 字节数；认不出返回 0。"""
    m = re.match(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)?",
                 str(text or "").strip(), re.I)
    if not m:
        return 0
    try:
        num = float(m.group("num"))
    except (TypeError, ValueError):
        return 0
    unit = (m.group("unit") or "B").upper().replace("I", "")
    return int(num * _ARIA_FACTORS.get(unit, 1))


def info_from_json(d):
    """把 yt-dlp -j 的一条 JSON 变成表格/卡片需要的一行。"""
    title = d.get("title") or d.get("fulltitle") or "(无标题)"
    if d.get("playlist_index"):
        title = f"[{d['playlist_index']}] {title}"
    h, w = d.get("height"), d.get("width")
    for f in (d.get("formats") or []):
        if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) > (h or 0):
            h, w = f.get("height"), f.get("width") or w
    res = f"{w}x{h}" if (w and h) else (f"{h}p" if h else "-")
    size = d.get("filesize") or d.get("filesize_approx")
    if not size:
        size = max([(f.get("filesize") or f.get("filesize_approx") or 0)
                    for f in (d.get("formats") or [])], default=0)
    thumb = str(d.get("thumbnail") or "")
    if not thumb:
        for t in reversed(d.get("thumbnails") or []):
            if isinstance(t, dict) and t.get("url"):
                thumb = str(t["url"])
                break
    return {
        "title": title, "dur": fmt_dur(d.get("duration")), "res": res,
        "size": fmt_size(size), "up": d.get("uploader") or d.get("channel") or "",
        "url": d.get("webpage_url") or d.get("original_url") or d.get("url") or "",
        "thumb": thumb,
    }


def info_from_json_raw(d):
    """桌面版表格保持「原始数值、显示时再格式化」的老行为。"""
    info = info_from_json(d)
    info["dur"] = d.get("duration")
    size = d.get("filesize") or d.get("filesize_approx")
    if not size:
        size = max([(f.get("filesize") or f.get("filesize_approx") or 0)
                    for f in (d.get("formats") or [])], default=0)
    info["size"] = size
    return info


def referer_of(url):
    m = re.match(r"(https?://[^/]+)", str(url or "").strip())
    return (m.group(1) + "/") if m else ""


def origin_of(url):
    """取 URL 的协议+主机部分，用于补 Origin 头。"""
    m = re.match(r"(https?://[^/]+)", str(url or "").strip(), re.I)
    return m.group(1) if m else ""


# 自动 Referer 只对这些站点补首页；未知站点不猜，避免把 A 站 Referer 发给 B 站。
SITE_HOMES = {
    "douyin.com": "https://www.douyin.com/",
    "bilibili.com": "https://www.bilibili.com/",
    "tiktok.com": "https://www.tiktok.com/",
    "youtube.com": "https://www.youtube.com/",
}
SITE_ALIASES = {
    "v.douyin.com": "douyin.com",
    "iesdouyin.com": "douyin.com",
    "b23.tv": "bilibili.com",
    "youtu.be": "youtube.com",
    "vm.tiktok.com": "tiktok.com",
}


def _site_key(host):
    h = str(host or "").lower().strip(".")
    if h in SITE_ALIASES:
        return SITE_ALIASES[h]
    for domain in SITE_HOMES:
        if h == domain or h.endswith("." + domain):
            return domain
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def _site_home(url):
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    return SITE_HOMES.get(_site_key(host), "")


def effective_referer(cfg, urls=None):
    """按目标站点选 Referer，避免「A 站 Referer 发给 B 站」。

    优先级：条目显式 referer > 与目标同站的全局设置 > 目标站点首页 > 空。
    多个不同站点的链接共用一个全局 Referer 没有意义，直接不带。
    """
    cfg_ref = str(cfg.get("referer") or "").strip()
    if cfg.get("_referer_explicit"):
        return cfg_ref
    good = [str(u or "").strip() for u in (urls or []) if str(u or "").strip()]
    if not good:
        return cfg_ref
    keys = set()
    for u in good:
        try:
            keys.add(_site_key(urlsplit(u).hostname))
        except ValueError:
            keys.add("")
    if cfg_ref:
        try:
            ref_key = _site_key(urlsplit(cfg_ref).hostname)
        except ValueError:
            ref_key = ""
        if len(keys) == 1 and ref_key in keys:
            return cfg_ref
    if len(keys) == 1:
        return _site_home(good[0])
    return ""


def path_inside(path, root):
    """判断 path 是否落在 root 目录内（Windows 下大小写不敏感）。"""
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        # 目录根（"C:\\"）本身就以分隔符结尾，再拼一次会变成 "c:\\\\"，
        # 于是整个盘里的路径都被判成「不在里面」。
        if not r.endswith(os.sep):
            r += os.sep
        if not p.endswith(os.sep):
            p += os.sep
        return p.startswith(r)
    except Exception:
        return False


# ---------------- 路径可移植性 ----------------
# 便携包会被人换个盘、换台机器、换个用户名继续用。配置里只要写着
# E:\smallUsefulTools\... 这种本机绝对路径，拷过去就指向不存在的目录。
# 所以落在程序目录内的路径统一存成 @{APP}/runtime/downloads 这种标记，
# 读的时候再按「当前程序目录」还原。目录外的路径（用户的 D:\视频 之类）原样保留：
# 那是用户选的，跨机器本来就没有对应位置。
APP_PATH_TOKEN = "@{APP}/"
# 需要做标记化的配置键：只列路径语义的键，不碰 extra / aria_args 这类命令行文本
PATH_KEYS = ("outdir", "yt_dlp_path", "cookie")


def portable_encode(path):
    """程序目录内的绝对路径 → @{APP}/ 相对标记；其余原样返回（幂等）。"""
    s = str(path or "").strip()
    if not s or s.startswith(APP_PATH_TOKEN) or not os.path.isabs(s):
        return s
    try:
        if not path_inside(s, BASE_DIR):
            return s
        rel = os.path.relpath(os.path.abspath(s), BASE_DIR)
    except Exception:
        return s
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return s
    return APP_PATH_TOKEN + rel.replace(os.sep, "/")


def portable_decode(path):
    """@{APP}/ 标记 → 当前程序目录下的绝对路径；其余原样返回。"""
    s = str(path or "")
    if not s.startswith(APP_PATH_TOKEN):
        return s
    rel = s[len(APP_PATH_TOKEN):].replace("/", os.sep)
    return os.path.normpath(os.path.join(BASE_DIR, rel)) if rel else BASE_DIR


def _heal_config_paths(cfg, say):
    """解码标记路径，并给「磁盘不存在」的老绝对路径一个明确提示。

    只提示不擅自改：用户手填的 D:\\视频 可能只是暂时没插盘，悄悄换掉更气人。
    唯一自动回退的是 yt_dlp_path——它不属于用户偏好，程序目录/PATH 里有一份就该用那份。
    """
    for key, value in list(cfg.items()):
        if isinstance(value, str) and value.startswith(APP_PATH_TOKEN):
            cfg[key] = portable_decode(value)
    yt = str(cfg.get("yt_dlp_path") or "").strip()
    if yt and os.path.isabs(yt) and not os.path.isfile(yt):
        found = find_tool("yt-dlp.exe")
        say(f"配置里的 yt-dlp 路径不存在（{yt}），已改用 {found}")
        cfg["yt_dlp_path"] = found
    out = str(cfg.get("outdir") or "").strip()
    if out and os.path.isabs(out):
        drive = os.path.splitdrive(out)[0]
        # 只对 X:\ 这种本地盘符做检查：UNC（\\nas\share）离线与否不能靠 exists 判断，
        # 误报「磁盘不存在」比不报更烦人。
        if os.name == "nt" and len(drive) == 2 and drive[1] == ":" and not os.path.exists(drive + os.sep):
            say(f"下载目录所在磁盘不存在（{out}），请在设置里改成可用的目录")


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def clean_media_url(url):
    """扩展抓到的地址常带 &amp; 实体、零宽字符与换行，先洗干净再交给 yt-dlp。"""
    s = str(url or "").strip()
    if not s:
        return ""
    s = s.translate(_ZERO_WIDTH)
    s = s.replace("&amp;", "&").replace("&#38;", "&")
    # 地址里不该出现任何空白；整段删掉比留下空格更安全（残留空格一样会被判 400）
    s = re.sub(r"[\s\u200b-\u200f\u2028\u2029\ufeff]+", "", s)
    return s.strip().strip('"').strip("'")


def scrub_header_value(text, limit=0):
    """去掉能伪造请求头的控制字符（CR/LF/NUL/制表符），用于整串 Cookie 这类原样透传的值。

    limit 传 0 表示不截断：Cookie 被截一半只会让站点判定未登录，比不传更糟。
    """
    s = re.sub(r"[\r\n\t\x00]+", " ", str(text or "")).strip()
    if limit and len(s) > limit:
        s = s[:limit]
    return s


# ---------------- 链接校验 ----------------
# 以 "-" 开头的「链接」会被 yt-dlp 当成参数；非 http(s) 的字符串同样没意义。
_BARE_DOMAIN_RE = re.compile(r"^(?:[\w\-]+\.)+[A-Za-z]{2,}(?:[:/?#]\S*)?$")


def is_http_url(url):
    return bool(re.match(r"^https?://\S+$", str(url or "").strip(), re.I))


# ---------------- 封面预浏览 ----------------

THUMB_CACHE_DIR = os.path.join(tempfile.gettempdir(), "ytdlp_thumbs")
THUMB_MAX_BYTES = 2 * 1024 * 1024
THUMB_TIMEOUT = 8
THUMB_CACHE_TTL = 6 * 3600          # 缓存超过 6 小时就重取（CDN 链接会失效）
_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_THUMB_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")

# 非公网网段黑名单。除了 RFC1918，还要覆盖这些「看着像公网、其实是本机/内网」的段：
#   ::ffff:0:0/96  IPv4-mapped IPv6（::ffff:127.0.0.1 是最常见的绕过写法）
#   100.64/10      运营商级 NAT；198.18/15 基准测试；240/4 保留；64:ff9b::/96 NAT64
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128", "::1/128", "64:ff9b::/96", "::ffff:0:0/96", "100::/64",
    "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8"))


def _ip_is_blocked(ip):
    """一个已解析的地址是否指向本机 / 内网 / 保留段。"""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:          # ::ffff:127.0.0.1 拆回 IPv4 再判，别被写法绕开
        return _ip_is_blocked(mapped)
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return True
    return any(ip in net for net in _PRIVATE_NETS if net.version == ip.version)


def is_public_http_url(url):
    """封面地址必须是公网 http(s)。

    站点可以在 -j 的 thumbnail 里塞一个指向 127.0.0.1 的地址，让本地服务替它探内网。
    只看字面 IP 挡不住「公网域名解析到私网」，所以解析之后再判。
    """
    s = str(url or "").strip()
    if not is_http_url(s):
        return False
    host = urlsplit(s).hostname
    if not host or host.lower() in ("localhost", "localhost.localdomain"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return False
        if _ip_is_blocked(ip):
            return False
    return True


class _ThumbRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随跳转，但每一跳都重新校验——只看首个地址等于给 302 开了后门。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_http_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "重定向到非公网地址，已拒绝", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_THUMB_OPENER = urllib.request.build_opener(_ThumbRedirectHandler())


def _open_thumb(req, timeout):
    """单点出口：测试可以替身掉这一层，不必真联网。"""
    return _THUMB_OPENER.open(req, timeout=timeout)


def thumb_cache_path(url, cache_dir=None):
    """同一 URL 永远落到同一个缓存文件；扩展名只认图片白名单。"""
    d = cache_dir or THUMB_CACHE_DIR
    ext = os.path.splitext(urlsplit(str(url or "")).path)[1].lower()
    if ext not in _THUMB_EXTS:
        ext = ".img"
    return os.path.join(d, hashlib.sha1(str(url).encode("utf-8")).hexdigest() + ext)


def thumb_headers(cfg):
    """取图用与下载同一套凭据，否则防盗链站点会 403。

    注意：这里会带上 Cookie / 自定义头，**只给 yt-dlp 兜底通路用**。
    直接用 urllib 取图请用 thumb_direct_headers()——封面主机来自站点元数据，
    不该拿到我们的登录态。
    """
    h = {}
    ua = str(cfg.get("ua") or "").strip()
    if ua:
        h["User-Agent"] = ua
    ref = str(cfg.get("referer") or "").strip()
    if ref:
        h["Referer"] = ref
    for line in normalize_headers(cfg.get("headers")).splitlines():
        name, _, value = line.partition(":")
        if name.strip() and value.strip():
            h[name.strip()] = value.strip()
    cookie = scrub_header_value(cfg.get("cookie_header"))
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    if cookie:
        h["Cookie"] = cookie
    return h


def thumb_direct_headers(cfg):
    """urllib 直连取图时只带 UA + Referer：不带 Cookie，也不带自定义头。

    UA/Referer 是防盗链站点的最低要求（没有就 403），泄露风险可接受；
    Cookie 和自定义头（常放 token）一旦发给站点元数据里指定的任意主机，
    就等于把登录态交出去。真需要凭据时由 yt-dlp 兜底通路去取。
    """
    h = {}
    ua = str(cfg.get("ua") or "").strip()
    if ua:
        h["User-Agent"] = ua
    ref = str(cfg.get("referer") or "").strip()
    if ref:
        h["Referer"] = scrub_header_value(ref)
    return h


def fetch_thumb_via_ytdlp(url, cfg, cache_dir=None):
    """兜底：让 yt-dlp 自己取封面。

    urllib 拿不到浏览器 cookie 文件、也没有浏览器指纹；这两样正是本项目
    对付 hostile 站点的看家本领，所以失败时复用现成通路再试一次。
    """
    d = cache_dir or THUMB_CACHE_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        return "", f"无法创建封面缓存目录：{e}"

    def snapshot():
        """记下调用前目录里已有的封面，事后只认「这次新出现的」。"""
        seen = {}
        try:
            for name in os.listdir(d):
                if name.startswith("thumb_") and \
                        os.path.splitext(name)[1].lower() in _THUMB_EXTS:
                    try:
                        seen[name] = os.path.getmtime(os.path.join(d, name))
                    except OSError:
                        seen[name] = 0.0
        except OSError:
            pass
        return seen

    before = snapshot()
    cmd = [_exe(cfg), "--skip-download", "--write-thumbnail", "--no-write-info-json",
           "-P", d, "-o", "thumb_%(id)s.%(ext)s"]
    add_access_args(cmd, cfg)
    cmd += ["--", str(url)]
    proc = None
    try:
        proc = spawn(cmd, d)
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        # 超时也要把子进程收掉，否则句柄和进程都留在后台
        if proc is not None:
            kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return "", "封面兜底取图超时"
    except Exception as e:
        if proc is not None:
            kill_tree(proc)
        return "", f"封面兜底取图失败：{e}"
    if proc.returncode != 0:
        tail = decode_bytes(out or b"").strip().splitlines()
        return "", "封面兜底取图失败：%s" % (tail[-1] if tail else "yt-dlp 返回非零")
    best, best_mtime = "", -1.0
    for name, mtime in snapshot().items():
        # 只认本次新出现、或被本次改写的文件：缓存目录是所有站点共用的，
        # 取「mtime 最新的一个」会把别的站点 6 小时内的封面认成自己的。
        if name in before and before[name] >= mtime:
            continue
        if mtime > best_mtime:
            best, best_mtime = os.path.join(d, name), mtime
    if not best:
        return "", "站点没有提供封面"
    return best, None


def _looks_like_image(data):
    """200 的 HTML / JSON 错误页不该被当成封面长期缓存。"""
    head = bytes(data[:16])
    if head.startswith(_THUMB_MAGIC):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:4] == b"\x00\x00\x01\x00":            # .ico
        return True
    # 认不出格式时不硬拦：ffmpeg 才是最终裁判，只挡明显是文本的响应
    return head.lstrip()[:1] not in (b"<", b"{", b"[")


def fetch_thumb(url, cfg, cache_dir=None, timeout=THUMB_TIMEOUT, max_bytes=THUMB_MAX_BYTES,
                ttl=THUMB_CACHE_TTL):
    """取封面到本地缓存，返回 (本地路径, 失败原因)。永不向调用方抛异常。"""
    if not is_public_http_url(url):
        return "", "封面地址不是公网 http(s)，已拒绝"
    path = thumb_cache_path(url, cache_dir)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            if ttl <= 0 or time.time() - os.path.getmtime(path) < ttl:
                return path, None
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        return "", f"无法创建封面缓存目录：{e}"
    req = urllib.request.Request(str(url), headers=thumb_direct_headers(cfg))
    try:
        with _open_thumb(req, timeout) as resp:
            data = resp.read(max_bytes + 1)
    except Exception:
        # 直连失败（含「重定向去了内网」）才动用带凭据的兜底通路
        return fetch_thumb_via_ytdlp(url, cfg, cache_dir)
    if len(data) > max_bytes:
        return "", "封面超过 2 MB，已跳过"
    if not data:
        return "", "封面是空响应"
    if not _looks_like_image(data):
        return "", "封面地址返回的不是图片"
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        return "", f"封面写盘失败：{e}"
    return path, None


# ---------------- 直链预览：截帧 / 短片段 / 时长 ----------------
# 扩展嗅探到的多是 .mp4 / .m3u8 直链，yt-dlp 的 generic 提取器不会给它缩略图，
# 所以封面只能自己截；而 m3u8 浏览器原生播不了，只能先抓一小段再交给 <video>。
SNIFF_CLIP_SECONDS = 6
# HLS/DASH 把整片切成几秒一个的分片，播放器逐个请求：这些 URL 单独下载没有意义
# （多数还要 key 解密），嗅探结果里要能认出来、别和「完整视频」混在一起。
SEGMENT_EXT = (".ts", ".m4s", ".f4v", ".m2ts")
MANIFEST_EXT = (".m3u8", ".mpd")
# B 站 DASH 的音频轨是独立 m4s，流 ID 落在 302xx（30216/30232/30280…）；
# 视频轨是 100026/30080 这类。嗅探列表默认要把音频轨藏起来，避免只下到音轨。
BILI_AUDIO_RE = re.compile(r"[-_]302\d{2}\.m4s$", re.I)
AUDIO_EXT = (".m4a", ".mp3", ".aac", ".flac", ".wav", ".opus")
SNIFF_FRAME_TIMEOUT = 40
SNIFF_CLIP_TIMEOUT = 90

# 这些容器浏览器能直接播，原地址就能塞进 <video>；其余（m3u8/mpd/ts）得先转一份
BROWSER_NATIVE_EXT = (".mp4", ".m4v", ".webm", ".m4a", ".mp3", ".ogg", ".oga", ".wav")


def stream_http_headers(cfg):
    """把 UA / Referer / Cookie 拼成 ffmpeg 的 -headers 值。

    直链站点多半靠 Referer 防盗链：不带它，服务器回 403 或一段错误页，
    ffmpeg 就会「读到了但不是视频」。
    """
    cfg = cfg or {}
    lines = []
    for name, key in (("Referer", "referer"), ("User-Agent", "ua"),
                      ("Cookie", "cookie_header")):
        val = scrub_header_value(cfg.get(key) or "", limit=0)
        if val:
            lines.append("%s: %s" % (name, val))
    if not lines:
        return ""
    # ffmpeg 要求多行头用 CRLF 分隔，末尾也要有一个
    return "\r\n".join(lines) + "\r\n"


def _run_stream_tool(cmd, timeout):
    """跑一次 ffmpeg，返回 (是否成功, 合并输出)。

    ffmpeg -i 的 Duration 打在 stderr 上、stdout 基本为空，两者都要收；
    读头时故意不带输出文件，退出码必为 1，成功与否由调用方看输出判断。
    超时必须 kill_tree：ffmpeg 会挂住等切片的子请求，terminate 常常杀不干净。
    """
    try:
        proc = spawn(cmd)
    except FileNotFoundError:
        return False, "找不到 ffmpeg"
    except Exception as e:
        return False, str(e)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode == 0, decode_bytes((out or b"") + (err or b""))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return False, "超时"


def looks_like_segment(url):
    """这是不是一个分片（而不是一门完整的视频/清单）。"""
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        return False
    return os.path.splitext(path)[1] in SEGMENT_EXT


def looks_like_audio(url):
    """这是不是音频单轨：B 站 DASH 音频流（302xx m4s）或明确的音频后缀。"""
    try:
        path = urlsplit(url).path
    except Exception:
        return False
    return bool(BILI_AUDIO_RE.search(path)) or os.path.splitext(path.lower())[1] in AUDIO_EXT


def looks_like_manifest(url):
    """这是不是 HLS/DASH 的播放清单（拿到它就能下整片）。"""
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        return False
    return os.path.splitext(path)[1] in MANIFEST_EXT


def browser_can_play_url(url):
    """这个地址能不能直接塞进 <video>（不转码、不额外等待）。"""
    try:
        path = urlsplit(url).path.lower()
    except Exception:
        return False
    return os.path.splitext(path)[1] in BROWSER_NATIVE_EXT


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def _parse_duration_text(text):
    """从 ffmpeg 日志里抠出时长（秒）；直播流的 Duration: N/A 自然落空。"""
    m = _DURATION_RE.search(text or "")
    if not m:
        return None
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def stream_duration(url, cfg=None, timeout=20):
    """问一下流有多长（秒）；问不出来返回 None（m3u8 直播流就会是 None）。"""
    h = stream_http_headers(cfg)
    cmd = [find_tool("ffmpeg.exe"), "-hide_banner"]
    if h:
        cmd += ["-headers", h]
    cmd += ["-i", url]
    _ok, out = _run_stream_tool(cmd, timeout)
    return _parse_duration_text(out)


def grab_stream_frame(url, cfg, out_path, at=1.0, timeout=SNIFF_FRAME_TIMEOUT):
    """截一张缩略图。直链没有元数据，这是「像预浏览那样有封面」的唯一办法。"""
    h = stream_http_headers(cfg)
    cmd = [find_tool("ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y"]
    if h:
        cmd += ["-headers", h]
    cmd += ["-ss", "%.2f" % at, "-i", url, "-frames:v", "1",
            "-vf", "scale=320:-1", "-f", "image2", out_path]
    ok, _ = _run_stream_tool(cmd, timeout)
    return ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 0


def grab_stream_clip(url, cfg, out_path, seconds=SNIFF_CLIP_SECONDS,
                     timeout=SNIFF_CLIP_TIMEOUT):
    """抓前几秒存成 mp4，让浏览器能播 m3u8 这种它自己解不了的流。

    用 -c copy 而不是重编码：转码 6 秒 1080p 要几十秒，copy 只花下载时间。
    copy 失败（音轨是 ac3/eac3 这类塞不进 mp4 的）就退一步只留视频轨。
    """
    h = stream_http_headers(cfg)
    base = [find_tool("ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y"]
    if h:
        base += ["-headers", h]
    base += ["-i", url, "-t", "%.2f" % seconds, "-movflags", "+faststart"]

    for codec_args in (["-c", "copy"], ["-c:v", "copy", "-an"]):
        if os.path.isfile(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        ok, _ = _run_stream_tool(base + codec_args + [out_path], timeout)
        if ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return True
    return False


def normalize_site_url(url):
    """把站点里「带视频 ID 的页面形态」归一成 yt-dlp 认识的地址。

    抖音从精选/推荐页点开视频时地址栏常是 `...?modal_id=<id>`，
    yt-dlp 的 Douyin 提取器只认 `/video/<id>`；这里直接换掉，不碰页面内容。
    """
    s = clean_media_url(url)
    try:
        parts = urlsplit(s)
    except ValueError:
        return s
    host = (parts.hostname or "").lower()
    if not (host == "douyin.com" or host.endswith(".douyin.com") or
            host == "iesdouyin.com" or host.endswith(".iesdouyin.com")):
        return s
    try:
        qs = parse_qs(parts.query)
    except ValueError:
        return s
    aid = str((qs.get("modal_id") or [""])[0] or "").strip()
    if aid.isdigit():
        return "https://www.douyin.com/video/%s" % aid
    return s


def sanitize_urls(urls):
    """过滤出可交给 yt-dlp 的地址，返回 (可用列表, 被丢弃列表)。

    - 丢掉空行与以 # 开头的注释行
    - 裸域名（www.example.com/xxx）自动补 https://
    - 抖音 `?modal_id=` 页面自动换成 `/video/<id>`
    - 其余非法项（含以 - 开头的伪链接）一律返回给调用方提示
    """
    good, bad = [], []
    for raw in (urls or []):
        u = clean_media_url(raw)
        if not u or u.startswith("#"):
            continue
        if _BARE_DOMAIN_RE.match(u):
            u = "https://" + u
        if is_http_url(u):
            good.append(normalize_site_url(u))
        else:
            bad.append(str(raw or "").strip())
    return good, bad


# ---------------- 附加参数过滤 ----------------
# 这些 yt-dlp 参数本身就等于「执行命令 / 写任意路径 / 读本机文件 / 加载外部配置」，
# 通过「附加参数」传进来就能绕过下载这件事，一律拦掉。
# 注意：这是纵深防御而不是硬边界——argparse 允许前缀缩写（`--out` 即 `--output`），
# 真正的边界是「只有本机界面与固定 ID 的扩展能改配置」（见 server._guard）。
_UNSAFE_LONG = (
    r"--exec($|=|-)|--actions($|=)|--config-location|--downloader($|=)|"
    r"--external-downloader|--cookies($|=)|--cookies-from-browser|--load-info-json|"
    r"--plugin-dirs|--netrc-cmd|--on-download($|=)|--use-postprocessor($|=)|"
    r"--batch-file($|=)|--print-to-file($|=)|--ffmpeg-location($|=)|"
    r"--remote-components($|=)|--js-runtimes($|=)|--out($|=)|--output($|=)|"
    r"--pa($|=)|--paths($|=)|--update($|=)|--postprocessor-args($|=)|"
    r"--parse-metadata($|=)|--replace-in-metadata($|=)"
)
UNSAFE_ARG_RE = re.compile("^(" + _UNSAFE_LONG + ")", re.I)
# 短选项大小写敏感：-o/--output 与 -P/--paths 的落盘路径完全由用户指定，
# 但 -p、-o 之外的短选项（-4 / -N / -i）不该被误伤。
UNSAFE_SHORT_RE = re.compile(r"^(-o|-P|-U)")


def is_unsafe_arg(token):
    """单个参数是否属于危险项。"""
    return bool(UNSAFE_ARG_RE.match(token) or UNSAFE_SHORT_RE.match(token))


def split_extra(text):
    try:
        return shlex.split(str(text or ""))
    except ValueError:
        return str(text or "").split()


def unsafe_arg_in(text):
    """返回附加参数里第一个危险项，没有则返回空串。"""
    for a in split_extra(text):
        if is_unsafe_arg(a):
            return a
    return ""


def safe_extra_args(text):
    """拼命令行前再过滤一次，旧配置文件里残留的危险参数也不会生效。"""
    parts, out, skip = split_extra(text), [], False
    for a in parts:
        if skip:
            skip = False
            continue
        if is_unsafe_arg(a):
            if "=" not in a:      # 形如 --exec "cmd"：值也要一起丢掉
                skip = True
            continue
        out.append(a)
    return out


def command_text(cmd):
    """把参数列表拼成可复制、可读的一行（含空格的参数加引号）。"""
    return " ".join(('"%s"' % a) if (" " in a or not a) else a for a in (cmd or []))


# ---------------- 配置读写 ----------------
CONFIG_SCHEMA_VERSION = 3

DEFAULT_CONFIG = {
    "outdir": DOWNLOAD_DIR,
    "mode": "best",
    "merge": "mp4",
    "fmt": "",
    "tpl": TEMPLATES[0],
    "sub": False, "sublang": "zh-Hans,zh,en", "embedsub": False,
    "thumb": True, "meta": True,
    "pl": True, "items": "", "ignore": True,
    "open_after": True,
    "rate": "", "proxy": "", "update_mirror": "", "cookie": "", "cookie_header": "",
    "conc": True, "conc_n": "4", "aria": False, "aria_args": "aria2c:-x 16 -s 16 -k 1M",
    "queue_n": "2",
    "flat": False, "extra": "",
    "browser": "（不使用）", "browser_profile": "",
    "referer": "", "ua": "", "headers": "",
    "generic": False, "extractor_args": "", "retries": "",
    # 网络自愈用的键（桌面版界面不暴露，但加载/保存时不能丢）
    "impersonate": "", "ipv4": False, "chunk": "", "proxy_off": False,
    "socket_timeout": "", "net_hard": True,
    # 桌面版内嵌的浏览器扩展服务（扩展靠它下载/预览）。关掉就只有桌面版能用。
    "ext_service": True,
    "yt_dlp_path": find_tool("yt-dlp.exe"),
    "schema_version": CONFIG_SCHEMA_VERSION,
}
# 旧键名 → 新键名（桌面版曾用 open，网页版用 open_after）
LEGACY_KEYS = {"open": "open_after"}
# 功能已删除的配置键：旧配置里可能还在，加载时静默丢弃
REMOVED_KEYS = ("play_while",)


def default_config():
    return dict(DEFAULT_CONFIG)


def normalize_headers(text):
    """把「请求头」文本框的内容清洗成一行一个合法头。

    现实里最常见的事故是粘贴时丢了换行：``Origin: https://a.com\\nX-Token: abc``
    被粘成 ``Origin: https://a.comX-Token: abc``，或者整段重复。这类畸形头会让
    某些 CDN 直接判请求非法（HTTP 400），所以要在这里拆开、去重、剔除非法行。
    """
    raw = str(text or "").translate(_ZERO_WIDTH)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    ordered = {}          # 小写头名 → [原始头名, 值]；保持首次出现的位置
    for chunk in raw.split("\n"):
        # 粘连段拆分：只在「前一字符不是空白」时把 "Xxx:" 当作新头的起点，
        # 这样 "Referer: https://a.com" 这种值里的 "https:" 不会被误拆。
        pieces, last = [], 0
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9\-]{0,40}\s*:", chunk):
            if m.start() > last and not chunk[m.start() - 1].isspace():
                pieces.append(chunk[last:m.start()])
                last = m.start()
        pieces.append(chunk[last:])
        for piece in pieces:
            line = piece.strip()
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            name, value = name.strip(), value.strip().strip('"')
            if not re.match(r"^[A-Za-z][A-Za-z0-9\-]{0,40}$", name) or not value:
                continue
            key = name.lower()
            if key in ordered:      # 同名头只留最后一条，但位置保持第一次出现处
                ordered[key][1] = value
            else:
                ordered[key] = [name, value]
    return "\n".join(f"{name}: {value}" for name, value in ordered.values())


def coerce_config(cfg, defaults=None):
    """补齐缺键、迁移旧键名、清洗 headers，返回 (新配置, 警告列表)。"""
    warnings = []
    src = dict(cfg or {})
    merged = dict(defaults or DEFAULT_CONFIG)
    present = set(src)
    for k, v in src.items():
        if k in LEGACY_KEYS:
            new_k = LEGACY_KEYS[k]
            if new_k not in present:      # 新旧键都在时以新键为准
                merged[new_k] = v
                warnings.append(f"配置项「{k}」已改名为「{new_k}」")
            continue
        merged[k] = v
    for key in REMOVED_KEYS:
        merged.pop(key, None)
    # v3：分片并发改成默认开（-N 4 是实测甜点）。只升级「从没动过」的旧配置：
    # 用户自己调过 conc_n 的，说明他有意为之，不覆盖。
    if int(str(src.get("schema_version") or 0) or 0) < 3:
        if src.get("conc") in (False, 0, "false", "0", None) and \
                str(src.get("conc_n") or "8").strip() in ("", "8"):
            merged["conc"] = True
            merged["conc_n"] = "4"
    merged["schema_version"] = CONFIG_SCHEMA_VERSION
    if not str(merged.get("browser") or "").strip():
        merged["browser"] = "（不使用）"
    fixed = normalize_headers(merged.get("headers"))
    if fixed != str(merged.get("headers") or "").strip():
        warnings.append("「请求头」有粘连或重复行，已自动整理")
    merged["headers"] = fixed
    # 模式统一成 ASCII 键：旧配置里存的中文标签也能对上（改文案不再丢设置）
    merged["mode"] = mode_key(merged.get("mode"))
    coerce_types(merged, warnings)
    return merged, warnings


# 手改配置最常见的是类型写错：数字写成中文、布尔写成 "false" 字符串
# （非空字符串在 if 里恒为真，勾选框就会「关不掉」）。
_INT_FIELDS = {"retries": ("重试次数", 0, 999), "conc_n": ("分片并发", 1, 64),
               "socket_timeout": ("超时秒数", 1, 600),
               "queue_n": ("同时下载数", 1, 5)}
_BOOL_FIELDS = ("sub", "embedsub", "thumb", "meta", "pl", "open_after", "ignore",
                "conc", "aria", "flat", "generic", "ipv4", "proxy_off", "net_hard")
_BYTES_RE = re.compile(r"^\d+(?:\.\d+)?[KMG]?(?:-\d+(?:\.\d+)?[KMG]?)?$", re.I)
_TRUE_WORDS = {"1", "true", "yes", "on", "y", "是", "开"}
_FALSE_WORDS = {"", "0", "false", "no", "off", "n", "否", "关"}


def coerce_types(merged, warnings=None):
    """就地校正配置里写歪的类型；warnings 为 None 时保持安静。"""
    warn = warnings if warnings is not None else []

    def say(msg):
        if msg not in warn:
            warn.append(msg)

    for key, (label, lo, hi) in _INT_FIELDS.items():
        s = str(merged.get(key) if merged.get(key) is not None else "").strip()
        # 必须 isascii + isdigit：'²'.isdigit() 是 True，但 int('²') 会抛 ValueError，
        # 手改配置写成上标/带圈数字就能让整个配置读写崩掉。
        if s.isascii() and s.isdigit() and lo <= int(s) <= hi:
            merged[key] = s
            continue
        fallback = str(DEFAULT_CONFIG.get(key) or "")
        if s:
            say(f"「{label}」需要填 {lo}-{hi} 的整数（当前是「{s}」），已改回"
                f"「{fallback or '不填，用 yt-dlp 默认值'}」")
        merged[key] = fallback

    for key, label in (("rate", "限速"), ("chunk", "分片大小")):
        s = str(merged.get(key) or "").strip()
        if not s:
            merged[key] = ""
        elif not _BYTES_RE.match(s):
            say(f"「{label}」要写成 500K、2M 这样（当前是「{s}」），已清空")
            merged[key] = ""
        else:
            merged[key] = s

    for key in _BOOL_FIELDS:
        v = merged.get(key)
        if isinstance(v, bool):
            continue
        word = v.strip().lower() if isinstance(v, str) else None
        if word is not None:
            merged[key] = word in _TRUE_WORDS
            if word not in _TRUE_WORDS and word not in _FALSE_WORDS:
                merged[key] = bool(DEFAULT_CONFIG.get(key))
                say(f"「{key}」只能是开或关（当前是「{v}」），已改回默认")
        else:
            merged[key] = bool(v) if v is not None else bool(DEFAULT_CONFIG.get(key))


def _stderr_note(prefix, msg):
    """往 stderr 打一行提示。pythonw（无控制台）下 stderr/stdout 都可能是 None，
    这时静默跳过——提示没了可以接受，为一行日志把调用方炸掉不行。"""
    try:
        stream = getattr(sys, "stderr", None) or getattr(sys, "stdout", None)
        if stream is None:
            return
        stream.write("%s%s\n" % (prefix, msg))
        stream.flush()
    except Exception:
        pass


def load_config(path, defaults=None, warn=None):
    """读配置：缺失/损坏时退回默认值，并把问题通过 warn 回调报出来。"""
    base = dict(defaults or DEFAULT_CONFIG)
    data = {}
    if os.path.isfile(path):
        # utf-8-sig：记事本/PowerShell 存出来的 BOM 会让 json.load 直接失败，
        # 用户看到的现象是「所有设置突然回默认」，所以宁可吞掉 BOM。
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("配置根节点不是对象")
        except Exception as e:
            msg = f"配置文件无法读取（{e}），已改用默认设置：{path}"
            if warn:
                warn(msg)
            else:
                _stderr_note("[warn] ", msg)
            data = {}
    try:
        cfg, warnings = coerce_config(data, base)
    except Exception as e:      # 校正逻辑自身的意外不能连累启动
        cfg, warnings = dict(base), [f"配置校正失败（{e}），已改用默认设置"]
    # 便携标记还原 + 失效路径提示（换机器/换盘后最常见的那两个坑）
    healed = []
    _heal_config_paths(cfg, healed.append)
    for w in list(warnings) + healed:
        if warn:
            warn(w)
    return cfg


@contextlib.contextmanager
def _write_lock(path, timeout=2.0):
    """按目标文件加一把跨进程的建议锁，让「读-改-写」不至于交错。

    O_EXCL 建锁文件在 Windows 上是原子的；拿不到就等一会儿，超时后照样往下走
    （宁可慢一次，也不能因为别人留了个死锁文件就永久保存不了配置）。
    这只防交错写入，防不了「后写覆盖先写的字段」——那需要锁住整个读改写周期。
    """
    lock = path + ".lock"
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:                     # 上一个进程被强杀留下的死锁文件
                if time.time() - os.path.getmtime(lock) > 30:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                break
            time.sleep(0.02)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(lock)
            except OSError:
                pass


def atomic_write_text(path, text, backup=True):
    """先写临时文件再替换，避免断电/强杀留下半截配置。

    临时名用 mkstemp 而不是固定的 path+".tmp"：桌面版和内嵌/独立服务共用一个 config.json，
    两个进程同时保存时会互相覆盖临时文件的内容。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with _write_lock(os.path.abspath(path)):
        _write_text_locked(path, d, text, backup)


def _write_text_locked(path, d, text, backup):
    base = os.path.basename(path)
    # 强杀留下的临时文件不会自己消失：超过 5 分钟的顺手清掉
    try:
        now = time.time()
        for name in os.listdir(d):
            if name.startswith(base + ".") and name.endswith(".tmp"):
                full = os.path.join(d, name)
                if now - os.path.getmtime(full) > 300:
                    os.remove(full)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix=base + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if backup and os.path.isfile(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_config(path, cfg, warn=None):
    """保存配置：原子写 + 失败不再静默。返回是否成功。

    校正出来的提醒要回给调用方，否则界面「保存成功」但值被悄悄改掉了，
    用户只会觉得「填了没生效」。
    """
    try:
        data, warnings = coerce_config(cfg, cfg)
    except Exception as e:
        msg = f"配置校正失败：{e}（{path}）"
        if warn:
            warn(msg)
        else:
            _stderr_note("[err] ", msg)
        return False
    for w in warnings:
        if warn:
            warn(w)
    data["headers"] = normalize_headers(data.get("headers"))
    # 落盘前把程序目录内的路径压成 @{APP}/ 标记：配置文件跟着便携包走，换盘换机器都能用
    for key in PATH_KEYS:
        if isinstance(data.get(key), str):
            data[key] = portable_encode(data[key])
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return True
    except Exception as e:
        msg = f"配置保存失败：{e}（{path}）"
        if warn:
            warn(msg)
        else:
            _stderr_note("[err] ", msg)
        return False


# ---------------- 命令行构建 ----------------

def add_access_args(cmd, cfg, urls=None):
    """把「访问与身份」相关的参数追加到命令里。

    探测、下载、看格式用的是同一套凭据——否则会出现「探测不到但能下载」的矛盾。
    urls 用于按站点自动选 Referer；不传时沿用旧行为（直接用配置里的 Referer）。
    """
    browser = str(cfg.get("browser") or "（不使用）").strip()
    cookie_file = str(cfg.get("cookie") or "").strip()
    sniff_jar = str(cfg.get("_sniff_cookie_file") or "").strip()
    if sniff_jar and os.path.exists(sniff_jar):
        # 扩展刚把本页的 cookie 直接交过来：既是最新的，也不用去碰浏览器那个
        # 被独占锁住的 Cookies 数据库（--cookies-from-browser 会因此直接失败）。
        cmd += ["--cookies", sniff_jar]
    elif browser and browser != "（不使用）":
        # 直接从浏览器读取登录态，省去手工导出 cookies.txt
        arg = browser
        if str(cfg.get("browser_profile") or "").strip():
            arg += ":" + str(cfg["browser_profile"]).strip()
        cmd += ["--cookies-from-browser", arg]
    elif cookie_file:
        cmd += ["--cookies", cookie_file]
    if str(cfg.get("ua") or "").strip():
        cmd += ["--user-agent", str(cfg["ua"]).strip()]
    ref = effective_referer(cfg, urls)
    if ref:
        cmd += ["--referer", ref]
    for line in normalize_headers(cfg.get("headers")).splitlines():
        cmd += ["--add-header", line]
    cookie_hdr = scrub_header_value(cfg.get("cookie_header"))
    if cookie_hdr.lower().startswith("cookie:"):
        cookie_hdr = cookie_hdr[7:].strip()
    if cookie_hdr:
        # 嗅探兜底：整串 Cookie 头直接透传，不进「请求头」文本框，
        # 免得被 normalize_headers 的粘连拆分规则拆坏（Cookie 值里常有冒号）
        cmd += ["--add-header", "Cookie: " + cookie_hdr]
    if cfg.get("generic"):
        cmd += ["--force-generic-extractor"]
    if str(cfg.get("extractor_args") or "").strip():
        cmd += ["--extractor-args", str(cfg["extractor_args"]).strip()]
    if str(cfg.get("retries") or "").strip():
        cmd += ["--retries", str(cfg["retries"]).strip()]
    # 网络抗重置：由自动重试计划注入，手工配置时也生效
    if str(cfg.get("impersonate") or "").strip():
        cmd += ["--impersonate", str(cfg["impersonate"]).strip()]
    if cfg.get("ipv4"):
        cmd += ["-4"]
    if str(cfg.get("chunk") or "").strip():
        cmd += ["--http-chunk-size", str(cfg["chunk"]).strip()]
    if cfg.get("proxy_off"):
        # 显式直连：否则环境变量里的 HTTP_PROXY 仍会生效
        cmd += ["--proxy", ""]
    if str(cfg.get("socket_timeout") or "").strip():
        cmd += ["--socket-timeout", str(cfg["socket_timeout"]).strip()]
    return cmd


def _exe(cfg):
    return cfg.get("yt_dlp_path") or find_tool("yt-dlp.exe")


# 直链多连接的经验门槛：小于这个体积时，aria2c 建连/分段的固定开销
# 比多连接省下的时间还多（一个 3MB 的 mp4 用 16 连接通常反而更慢）。
ARIA_MIN_BYTES = 8 * 1024 * 1024


def aria_worth_it(cfg):
    """这个任务值不值得交给 aria2c。

    cfg 带嗅探元数据（``_sniff_cat`` / ``_sniff_seg`` / ``_sniff_manifest`` /
    ``_sniff_size``）时按源类型判断：只有「直链音视频、不是清单/分片、体积过门槛」
    才开多连接。手工粘贴、历史重下、AI 任务没有元数据，沿用 aria 开关本身。
    """
    if not cfg.get("aria"):
        return False
    cat = str(cfg.get("_sniff_cat") or "").strip().lower()
    if not cat:
        return True
    if cat not in ("video", "audio"):
        return False
    if cfg.get("_sniff_seg") or cfg.get("_sniff_manifest"):
        return False
    try:
        size = int(cfg.get("_sniff_size") or 0)
    except (TypeError, ValueError):
        size = 0
    # 体积未知（0）也放行：HEAD 常被 CDN 拒，不能因此把可能的大文件一律降级
    return size == 0 or size >= ARIA_MIN_BYTES


def build_download_cmd(cfg, urls, no_playlist=False, warn=None):
    cmd = [_exe(cfg), "--newline", "--progress-template", PROGRESS_TEMPLATE]
    ffmpeg = find_tool("ffmpeg.exe")
    if os.path.isfile(ffmpeg):
        cmd += ["--ffmpeg-location", os.path.dirname(os.path.abspath(ffmpeg))]
    if cfg.get("outdir"):
        cmd += ["-P", str(cfg["outdir"])]
    if cfg.get("tpl"):
        cmd += ["-o", str(cfg["tpl"])]
    mode = mode_key(cfg.get("mode"))
    fmt = str(cfg.get("fmt") or "").strip()
    if not fmt:
        # 未手填时按模式套用默认表达式（自定义模式无默认值）
        fmt = MODE_FMT.get(mode, "")
    if fmt:
        cmd += ["-f", fmt]
    if mode == "best":
        if cfg.get("merge") and cfg["merge"] != "不转换":
            cmd += ["--merge-output-format", str(cfg["merge"])]
    elif mode == "audio_mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if cfg.get("sub"):
        cmd += ["--write-subs", "--write-auto-subs",
                "--sub-langs", str(cfg.get("sublang") or "zh-Hans,zh,en")]
    if cfg.get("embedsub"):
        cmd += ["--embed-subs"]
    if cfg.get("thumb"):
        cmd += ["--embed-thumbnail"]
    if cfg.get("meta"):
        cmd += ["--embed-metadata"]
    cmd += ["--no-playlist" if (no_playlist or not cfg.get("pl")) else "--yes-playlist"]
    if cfg.get("items"):
        cmd += ["--playlist-items", str(cfg["items"])]
    if cfg.get("ignore"):
        cmd += ["-i"]
    if cfg.get("rate"):
        cmd += ["--limit-rate", str(cfg["rate"])]
    if cfg.get("proxy"):
        cmd += ["--proxy", str(cfg["proxy"])]
    add_access_args(cmd, cfg, urls)
    if cfg.get("conc"):
        try:
            n = max(1, min(32, int(str(cfg.get("conc_n") or 8).strip())))
        except ValueError:
            n = 8
        cmd += ["-N", str(n)]
    if aria_worth_it(cfg):
        # 没装 aria2c 还硬加这个参数，yt-dlp 会直接退出：确认存在再加。
        # 协议前缀让 yt-dlp 只在 http/https 直链上用 aria2c：m3u8/dash 这类
        # 分片流仍走内置下载器（那里提速靠 -N，不是多连接）。
        if which_tool("aria2c.exe"):
            cmd += ["--external-downloader", "http,https:aria2c"]
            if cfg.get("aria_args") and not unsafe_arg_in(cfg["aria_args"]):
                cmd += ["--external-downloader-args", str(cfg["aria_args"])]
        elif isinstance(warn, list):
            warn.append("没有找到 aria2c.exe（放到程序目录或加入 PATH），本次改用 yt-dlp 自带下载器")
    if cfg.get("extra"):
        cmd += safe_extra_args(cfg["extra"])
    # 调用方（界面/HTTP 路由）负责把非法项提示给用户，这里只保证命令行里
    # 不会出现以 "-" 开头、会被 yt-dlp 当成参数的伪链接
    good, _bad = sanitize_urls(urls)
    cmd += ["--", *good]
    return cmd


def build_detect_cmd(cfg, urls):
    cmd = [_exe(cfg), "--skip-download", "-j"]
    if cfg.get("flat"):
        cmd.append("--flat-playlist")
    if str(cfg.get("proxy") or "").strip():
        cmd += ["--proxy", str(cfg["proxy"]).strip()]
    # 多个链接一起探测时，单个失败不应中断其余链接
    good, _bad = sanitize_urls(urls)
    add_access_args(cmd, cfg, good)
    if cfg.get("items"):
        cmd += ["--playlist-items", str(cfg["items"])]
    cmd += ["--yes-playlist" if cfg.get("pl") else "--no-playlist"]
    cmd += ["-i", "--", *good]
    return cmd


def build_formats_cmd(cfg, url):
    """看可用格式：以前只带 proxy/cookie，导致加了防盗链的站点看不到格式表。

    限速不进这条命令：yt-dlp 的 -F 只解析清单，--limit-rate 对它没有意义。
    """
    cmd = [_exe(cfg), "-F"]
    if str(cfg.get("proxy") or "").strip():
        cmd += ["--proxy", str(cfg["proxy"]).strip()]
    add_access_args(cmd, cfg, [url])
    cmd += ["--", url]
    return cmd


# ---------------- 进度解析 ----------------

def new_progress_state():
    return {"pct": 0.0, "size": "", "speed": "", "eta": "",
            "file": "", "raw_path": "", "item_i": 0, "item_total": 0, "done": set()}


def _clean_num(v):
    s = str(v or "").strip()
    return "" if s in _NA_VALUES else s


def parse_progress_line(line):
    """解析一行进度输出；不是进度行时返回 None。

    优先用 --progress-template 的 ``PROG|`` 行（机器可读），
    旧版/被改写的输出退回 PROG_RE 文案正则。
    """
    s = str(line or "")
    if PROG_MARK in s:
        # 文件名是最后一列，里面本身可能带 "|"，所以只切前 6 个分隔符
        f = s.split(PROG_MARK, 1)[1].split("|", 6)
        f += [""] * (7 - len(f))
        status, dl, total, est, speed, eta, fname = [x.strip() for x in f[:7]]
        total_bytes = None
        for candidate in (total, est):
            try:
                total_bytes = int(float(candidate))
                break
            except (TypeError, ValueError):
                continue
        if total_bytes:
            try:
                pct = round(int(float(dl or 0)) * 100.0 / total_bytes, 1)
            except (TypeError, ValueError):
                pct = 0.0
        else:
            pct = 100.0 if status in ("finished", "finished_download") else 0.0
        size = fmt_size(total_bytes) if total_bytes else ""
        spd = ""
        try:
            spd = fmt_size(float(speed)) + "/s" if _clean_num(speed) else ""
        except (TypeError, ValueError):
            spd = ""
        name = ""
        if fname:
            name = FRAG_RE.sub(".", os.path.basename(fname.strip()))
        return {"status": status, "pct": max(0.0, min(100.0, pct)), "size": size,
                "speed": spd, "eta": _clean_num(eta), "file": name, "raw": fname.strip()}
    m = ARIA_RE.search(s)
    if m:
        total = _aria_bytes(m.group("total"))
        rest = m.group("rest") or ""
        raw_pct = m.group("pct")
        if raw_pct is None:
            # 没有百分号时按「已下/总量」估；总量还没出来就保持 0
            try:
                raw_pct = min(100.0, _aria_bytes(m.group("dl")) * 100.0 / total) if total else 0
            except (TypeError, ValueError):
                raw_pct = 0
        spd = 0
        sm = re.search(r"DL:(?P<v>\S+)", rest)
        if sm:
            spd = _aria_bytes(sm.group("v"))
        em = re.search(r"ETA:(?P<v>\S+)", rest)
        try:
            pct = float(raw_pct)
        except (TypeError, ValueError):
            pct = 0.0
        return {"status": "downloading", "pct": max(0.0, min(100.0, pct)),
                "size": fmt_size(total) if total else "",
                "speed": (fmt_size(spd) + "/s") if spd else "",
                "eta": _clean_num(em.group("v")) if em else "",
                "file": "", "raw": ""}
    m = PROG_RE.search(s)
    if m:
        return {"status": "downloading", "pct": float(m.group("pct")),
                "size": m.group("size") or "", "speed": m.group("speed") or "",
                "eta": m.group("eta") or "", "file": "", "raw": ""}
    return None


def parse_file_line(line):
    """从 ``[download] Destination:`` 行里取出文件名（老版本回退路径）。"""
    f = FILE_RE.match(str(line or ""))
    if not f:
        return ""
    return FRAG_RE.sub(".", os.path.basename(f.group("path").strip()))


def progress_event(state, line):
    """把一行输出并入 state，并告诉调用方这是什么事件。

    返回 {"kind": "progress"|"file"|"item"} 或 None。
    """
    p = parse_progress_line(line)
    if p:
        prev_file = state.get("file") or ""
        for k in ("pct", "size", "speed", "eta"):
            if p.get(k) not in ("", None):
                state[k] = p[k]
        state["pct"] = p["pct"]
        if p.get("file"):
            state["file"] = p["file"]
        if p.get("raw"):
            state["raw_path"] = p["raw"]
        if p.get("status") in ("finished", "finished_download"):
            state["done"].add(os.path.splitext(state.get("file") or "")[0])
        if state.get("file") != prev_file:
            return {"kind": "file", "file": state.get("file") or ""}
        return {"kind": "progress"}
    base = parse_file_line(line)
    if base:
        state["done"].add(os.path.splitext(base)[0])
        state["file"] = base
        m = FILE_RE.match(str(line or ""))
        if m:
            state["raw_path"] = m.group("path").strip()
        return {"kind": "file", "file": base}
    m = ITEM_RE.search(str(line or ""))
    if m:
        state["item_i"], state["item_total"] = int(m.group(1)), int(m.group(2))
        return {"kind": "item"}
    return None


def classify_line(line):
    """日志分级：err / warn / ok / None（两端一致）。"""
    s = str(line or "")
    low = s.lower()
    if "error" in low or "traceback" in low or "[error]" in low:
        return "err"
    if "warning" in low:
        return "warn"
    if s.startswith(("[Merger]", "[ExtractAudio]", "[Fixup", "[Metadata]", "[Thumb", "[EmbedSubtitle]")):
        return "ok"
    return None


def progress_text(state):
    """把状态拼成一行人类可读的进度（日志里显示这个，而不是 PROG| 原始行）。"""
    parts = [f"[download] {state.get('pct', 0):.1f}%"]
    if state.get("size"):
        parts.append("of " + str(state["size"]))
    if state.get("speed"):
        parts.append("at " + str(state["speed"]))
    if state.get("eta"):
        parts.append("ETA " + str(state["eta"]))
    return " ".join(parts)


# ---------------- 进程启动 ----------------

def spawn(cmd, cwd=None):
    """统一的 yt-dlp 启动方式：UTF-8、无控制台窗口、stderr 并入 stdout。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    kwargs = {}
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env, cwd=cwd or BASE_DIR, **kwargs,
    )


def iter_lines(proc, keep_blank=False):
    """逐行读子进程输出（解码后）。

    默认跳过空行（下载日志 / 探测输出里空行没有意义）；
    格式表、更新日志这类「原样呈现」的场景传 keep_blank=True，保证行数一致。
    """
    try:
        for raw in proc.stdout:
            line = decode_bytes(raw).rstrip("\r\n")
            if line or keep_blank:
                yield line
    except Exception:
        return


def kill_tree(proc):
    """结束子进程及其后代（yt-dlp 会拉起 ffmpeg / aria2c）。"""
    if not proc or proc.poll() is not None:
        return False
    try:
        kwargs = {"creationflags": CREATE_NO_WINDOW} if hasattr(subprocess, "STARTUPINFO") else {}
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    return True


_EOF = object()
IDLE_PING_SECONDS = 5.0
STALL_SECONDS = 180


def iter_lines_watch(proc, abort=None, keepalive=None, idle_seconds=IDLE_PING_SECONDS,
                     stall_seconds=STALL_SECONDS, keep_blank=False, why=None):
    """逐行读子进程输出，空闲时也有机会发心跳、判断对端是否已经走掉。

    `for line in proc.stdout` 只在新行到达时才有机会检查客户端状态：子进程一旦卡住
    不输出，任务就永远占着那唯一的执行槽。这里把读输出放进线程，主循环每
    idle_seconds 醒一次：
      * 调 keepalive()，返回 False 说明对端断开 → 终止子进程，reason="client"
      * 连续 stall_seconds 没有任何字节输出 → 终止子进程，reason="stall"
    结束原因写进 why 字典（why={"reason": ...}），由调用方决定日志文案。

    「活动」按**收到的字节**算，不按行算：ffmpeg 合并/转码时只往 stderr 刷
    `\\r` 进度、几十秒不产出一个 `\\n`，按行判活会把这种合法阶段当成卡死杀掉。
    """
    box = queue.Queue()
    activity = [time.time()]          # pump 线程更新，主循环只读

    def pump():
        buf = b""

        def emit(raw):
            line = decode_bytes(raw).rstrip("\r")
            if line or keep_blank:
                box.put(line)

        def feed(chunk):
            nonlocal buf
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", "replace")
            buf += chunk
            # 只有 \n 才产出「行」，\r 仅用于判活：否则 ffmpeg 的进度条会刷出成百上千行
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                emit(raw)

        try:
            stdout = proc.stdout
            # 真实管道是 BufferedReader；测试替身与文本流只有迭代器，两条路都支持
            reader = getattr(stdout, "read1", None) or getattr(stdout, "read", None)
            if reader is not None:
                while True:
                    chunk = reader(4096)
                    if not chunk:
                        break
                    activity[0] = time.time()
                    feed(chunk)
            else:
                for chunk in stdout:
                    activity[0] = time.time()
                    feed(chunk)
            if buf:
                emit(buf)
        except Exception:
            pass
        box.put(_EOF)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    reason = "normal"

    try:
        while True:
            try:
                item = box.get(timeout=idle_seconds)
            except queue.Empty:
                # 只有真的闲下来才探活：keepalive 通常带心跳写入，每条输出都发一次就太吵了
                if abort and abort():
                    reason = "client"
                    break
                if keepalive and not keepalive():
                    reason = "client"
                    break
                if time.time() - activity[0] >= stall_seconds:
                    reason = "stall"
                    break
                continue
            if item is _EOF:
                break
            activity[0] = time.time()
            if abort and abort():
                reason = "client"
                break
            yield item
    except GeneratorExit:
        # 消费侧提前退出（break / 异常）同样要收掉进程树，别把 ffmpeg 后代留在后台
        reason = "closed"
        raise
    finally:
        if reason != "normal":
            kill_tree(proc)
        if why is not None:
            why["reason"] = reason


def yt_dlp_version(exe=None):
    """读取 yt-dlp 版本——版本过旧是「站点嗅探不到」的常见原因。"""
    try:
        kwargs = {"creationflags": CREATE_NO_WINDOW} if hasattr(subprocess, "STARTUPINFO") else {}
        r = subprocess.run([exe or find_tool("yt-dlp.exe"), "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=20, **kwargs)
        return decode_bytes(r.stdout).strip()
    except Exception:
        return ""


# ---------- 连接被重置（10054）的自愈阶梯 ----------
# 对端掐连接通常不是地址失效，而是它不喜欢请求的「长相」：
#   TLS 指纹（JA3）、HTTP/2 行为、IPv6 路由、分块方式。
# 逐级换一种长相重试，比让用户去理解 10054 有用。
NET_HARD_PLANS = {
    "no-referer": {"referer": "", "ipv4": True, "retries": "3"},
    "origin": {"ipv4": True, "retries": "3"},
    "direct": {"proxy": "", "proxy_off": True, "ipv4": True, "retries": "3",
               "socket_timeout": "20"},
    "impersonate": {"impersonate": "Chrome", "ipv4": True, "retries": "5",
                    "socket_timeout": "20"},
    "chunk": {"impersonate": "Edge", "ipv4": True, "retries": "10",
              "socket_timeout": "30", "chunk": "10M"},
}
NET_PLAN_LABEL = {"no-referer": "去掉 Referer（部分 CDN 见到外来来源直接判非法）",
                  "origin": "补上 Origin/Accept 等浏览器头",
                  "direct": "直连（绕开代理）",
                  "impersonate": "浏览器 TLS 指纹（Chrome）+ IPv4",
                  "chunk": "Edge 指纹 + 10M 分块传输"}

# 这些字样出现在输出里，说明是连接层被打断，值得换个姿势重来
NET_FAILURE_HINTS = (
    "10054", "connection aborted", "connection reset", "connectionreseterror",
    "transporterror", "eof occurred", "remote end closed connection",
    "connection closed", "broken pipe", "sslerror", "ssl:", "handshake",
    "timed out", "timeout", "temporary failure", "getaddrinfo",
    "proxy", "tunnel", "no route to host", "network is unreachable",
)

# 浏览器 Cookies 数据库被独占锁住：与网络无关，关掉浏览器能解决，但用户未必愿意。
# 先去掉 --cookies-from-browser 重试一遍——公开内容不需要登录态，多半照常能下。
COOKIE_COPY_RE = re.compile(
    r"could not copy \w*(?:\s+\w+)?\s*cookie database|"
    r"failed to (?:copy|read|decrypt) .{0,24}cookie database|"
    r"cookie database is locked", re.I)
COOKIE_PLAN_LABEL = "临时不用浏览器 Cookies（公开内容照常，需要登录态的内容得关掉浏览器或用扩展）"


def cookie_copy_failed(text):
    """失败原因是不是「读不出浏览器 Cookies 数据库」。"""
    return bool(COOKIE_COPY_RE.search(str(text or "")))


def next_cookie_plan(tail, tried, cfg=None):
    """数据库被锁时给出一招「去掉浏览器读取」的重试计划；否则返回 None。"""
    cfg = cfg or {}
    if "no-cookie" in (tried or ()) or not cookie_copy_failed(tail):
        return None
    browser = str(cfg.get("browser") or "").strip()
    if not browser or browser == "（不使用）":
        return None
    return "no-cookie", {"browser": "（不使用）", "browser_profile": ""}


# HTTP 400 允许的重试次数：这类错误多半是请求头的问题，试两招就够，
# 不设上限会让用户白等好几套 25 秒的方案。
NET_400_MAX_TRIES = 2


def is_net_failure(text):
    """判断失败是否属于「换个姿势可能就好了」，而非地址/权限类硬错误。"""
    low = scrub_urls(text).lower()
    if "unsupported url" in low or "http error 403" in low or "http error 404" in low:
        return False
    if "http error 400" in low:
        return True          # 请求被判非法：多为来源头或请求形态问题
    return any(h in low for h in NET_FAILURE_HINTS)


def next_net_plan(tail, tried, cfg=None, url=""):
    """根据失败原因决定下一招；没有可用的招就返回 None。

    先分清失败类型：
    - 代理类 → 直接绕开代理最省事
    - HTTP 400（请求被判非法）→ 先摘掉外来 Referer，再补浏览器头，最后才伪装指纹
    - 其余连接层错误 → 伪装浏览器指纹

    url 是本次下载/探测的地址：没填 Referer 时，Origin 只能从它自己的站点推。
    """
    low = scrub_urls(tail).lower()
    cfg = cfg or {}
    if not is_net_failure(tail):
        return None          # 403/404/站点不支持：换姿势也没用，别浪费用户时间
    can_imp = impersonate_available(cfg.get("yt_dlp_path"))
    if "http error 400" in low:
        n400 = len([t for t in tried if t in ("no-referer", "origin")])
        if n400 < NET_400_MAX_TRIES:
            if "no-referer" not in tried and str(cfg.get("referer") or "").strip():
                return "no-referer", NET_HARD_PLANS["no-referer"]
            if "origin" not in tried:
                org = origin_of(cfg.get("referer") or "") or origin_of(url)
                base = str(cfg.get("headers") or "").strip()
                plan = dict(NET_HARD_PLANS["origin"])
                if org:
                    extra = "\n".join(["Origin: " + org, "Accept: */*",
                                       "Accept-Language: zh-CN,zh;q=0.9"])
                    plan["headers"] = (base + "\n" + extra) if base else extra
                    return "origin", plan
    order = ["direct", "impersonate", "chunk"] if (
        "proxy" in low or "tunnel" in low) else ["impersonate", "chunk"]
    for name in order:
        if name in tried:
            continue
        plan = dict(NET_HARD_PLANS[name])
        if not can_imp:
            # 旧版 yt-dlp 没有 --impersonate（缺 curl_cffi），只保留传输层手段
            if name == "impersonate":
                continue
            plan.pop("impersonate", None)
        return name, plan
    return None


_IMP_CACHE = {}


def impersonate_available(exe=None):
    """yt-dlp 是否带 curl_cffi（决定 --impersonate 能否用），按 exe 各探测一次。"""
    key = os.path.abspath(exe) if exe else "default"
    if key in _IMP_CACHE:
        return _IMP_CACHE[key]
    ok = False
    try:
        kwargs = {"creationflags": CREATE_NO_WINDOW} if hasattr(subprocess, "STARTUPINFO") else {}
        r = subprocess.run([exe or find_tool("yt-dlp.exe"), "--list-impersonate-targets"],
                           capture_output=True, text=True, timeout=25, **kwargs)
        ok = "curl_cffi" in ((r.stdout or "") + (r.stderr or ""))
    except Exception:
        ok = False
    _IMP_CACHE[key] = ok
    return ok


# ---------------- 格式列表 ----------------

def parse_format_line(line):
    """宽松解析 yt-dlp -F 的一行（列宽随版本变化，失败时整行塞进 note）。"""
    line = ANSI_RE.sub("", str(line or "")).rstrip()
    if not line.strip() or line.lstrip().startswith("ID ") or set(line.strip()) <= set("-| "):
        return None
    parts = [p.strip() for p in line.split("|")]
    left = parts[0].split()
    if not left or not re.match(r"^[\w\-+]+$", left[0]):
        return None  # 说明性文字行，交给日志
    ext = left[1] if len(left) > 1 else ""
    rest = left[2:]
    if rest and rest[0].lower() == "audio":      # "audio only" 占两列，是一个整体
        tail = rest[1:]
        if tail and tail[0].lower() == "only":
            tail = tail[1:]
        res, note = "audio only", " ".join(tail)
    else:
        res, note = (rest[0] if rest else ""), " ".join(rest[1:])
    if re.match(r"^sb\d", left[0], re.I):
        return None          # 故事板（storyboard）不是可下载的媒体格式
    codec = re.sub(r"\s{2,}", " ", parts[2]).strip() if len(parts) > 2 else ""
    return {
        "id": left[0], "ext": ext, "res": res,
        "size": parts[1].split()[0] if len(parts) > 1 and parts[1] else "",
        "codec": codec, "note": note,
    }


def json_ok(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


# ---------------- 自动排障 ----------------
# 尝试顺序：从最省事到最强手段，命中即停。
# fix 字段告诉界面该把哪些设置项改掉，用户点一下就能应用。
PROBE_VARIANTS = [
    ("plain",   "直连（当前设置）",      {}),
    ("ua",      "加浏览器 UA + Referer", {"ua": True, "referer": True}),
    ("cookies", "用浏览器 Cookies",      {"browser": True}),
    ("generic", "通用提取器兜底",         {"generic": True}),
]


def build_probe_cmd(cfg, url, variant):
    """自动排障用的极简探测命令：只求拿到一条元数据 JSON，越快越好。"""
    cmd = [_exe(cfg), "--skip-download", "-j", "--no-warnings", "--no-playlist"]
    if str(cfg.get("proxy") or "").strip():
        cmd += ["--proxy", str(cfg["proxy"]).strip()]
    if variant == "ua":
        cmd += ["--user-agent", UA_WINDOWS]
        r = _site_home(url) or referer_of(url)
        if r:
            cmd += ["--referer", r]
    elif variant == "cookies":
        b = str(cfg.get("browser") or "").strip()
        if not b or b == "（不使用）":
            b = "chrome"          # 排障时默认试 Chrome，用户可在设置里改
        prof = str(cfg.get("browser_profile") or "").strip()
        cmd += ["--cookies-from-browser", b + (":" + prof if prof else "")]
    elif variant == "generic":
        cmd += ["--force-generic-extractor"]
    elif variant == "plain":
        # 「直连（当前设置）」= 不加指纹/分块/代理这些花活，但 UA/Referer/自定义头
        # 本来就是用户当前配置的一部分，漏掉它们探测出来的结果没有参考价值。
        ua = str(cfg.get("ua") or "").strip()
        if ua:
            cmd += ["--user-agent", ua]
        ref = effective_referer(cfg, [url]) or referer_of(url)
        if ref:
            cmd += ["--referer", ref]
        for name, value in normalize_headers(cfg.get("headers")):
            cmd += ["--add-header", "%s: %s" % (name, value)]
        if str(cfg.get("cookie") or "").strip():
            cmd += ["--cookies", str(cfg["cookie"]).strip()]
    cmd += ["--", url]
    return cmd


def apply_probe_fix(cfg, url, fix):
    """把某一套排障方案落进配置。

    返回 (touched, manual)：
      touched —— 真的写进 cfg 的项目名
      manual  —— 无法自动填、必须用户自己动手的项目名
    代理地址、限速值、具体格式 ID 都不存在「正确默认值」，硬塞一个只会让
    用户以为已经修好了，所以这里只如实报告，不假装应用。
    """
    touched, manual = [], []
    if fix.get("ua"):
        cfg["ua"] = UA_WINDOWS
        touched.append("User-Agent")
    if fix.get("referer"):
        r = _site_home(url) or referer_of(url)
        if r:
            cfg["referer"] = r
            touched.append("Referer")
    if fix.get("browser"):
        if str(cfg.get("browser") or "").strip() in ("", "（不使用）"):
            cfg["browser"] = "chrome"
        touched.append("浏览器 Cookies")
    if fix.get("generic"):
        cfg["generic"] = True
        touched.append("通用提取器")
    if fix.get("proxy"):
        manual.append("代理地址")
    if fix.get("rate"):
        manual.append("限速")
    if fix.get("fmt"):
        manual.append("格式")
    return touched, manual


def now():
    return time.time()
