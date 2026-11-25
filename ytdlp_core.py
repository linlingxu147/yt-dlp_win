# -*- coding: utf-8 -*-
"""yt-dlp 下载器的共用核心。

桌面版（YtDlpGUI\\main.py）与网页版（YtDlpWeb\\server.py）原先各自复制了一份
1500 行代码，改一处要改两处、还容易漂移。这里把「与界面无关」的部分集中起来：

  * 模式 / 格式表达式 / 预设 UA / 浏览器列表 / 诊断规则
  * 命令行构建（下载、探测、格式列表、排障探测）
  * 进度解析（--progress-template 的机器可读行，旧正则作兜底）
  * 进程启动封装、连接重置的自愈阶梯、配置读写（含 headers 清洗与原子写）

约定：本模块只依赖标准库，绝不 import tkinter / http.server。
"""

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
from urllib.parse import urlsplit

# 本文件就在项目根目录：yt-dlp.exe / ffmpeg.exe / downloads 都相对它定位
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
     ["确认 ffmpeg.exe 与本程序在同一目录",
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


def find_tool(name):
    """优先使用项目根目录里的 exe，找不到就交给 PATH。"""
    p = os.path.join(BASE_DIR, name)
    return p if os.path.isfile(p) else name


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


def path_inside(path, root):
    """判断 path 是否落在 root 目录内（Windows 下大小写不敏感）。"""
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        return p == r or p.startswith(r + os.sep)
    except Exception:
        return False


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


def sniff_head(text, limit=120):
    s = str(text or "")
    return s[:limit] + ("…" if len(s) > limit else "")


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
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_BARE_DOMAIN_RE = re.compile(r"^(?:[\w\-]+\.)+[A-Za-z]{2,}(?:[:/?#]\S*)?$")


def is_http_url(url):
    return bool(re.match(r"^https?://\S+$", str(url or "").strip(), re.I))


# ---------------- 封面预浏览 ----------------

THUMB_CACHE_DIR = os.path.join(tempfile.gettempdir(), "ytdlp_thumbs")
THUMB_MAX_BYTES = 2 * 1024 * 1024
THUMB_TIMEOUT = 8
_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10"))


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
        if any(ip in net for net in _PRIVATE_NETS):
            return False
    return True


def thumb_cache_path(url, cache_dir=None):
    """同一 URL 永远落到同一个缓存文件；扩展名只认图片白名单。"""
    d = cache_dir or THUMB_CACHE_DIR
    ext = os.path.splitext(urlsplit(str(url or "")).path)[1].lower()
    if ext not in _THUMB_EXTS:
        ext = ".img"
    return os.path.join(d, hashlib.sha1(str(url).encode("utf-8")).hexdigest() + ext)


def thumb_headers(cfg):
    """取图用与下载同一套凭据，否则防盗链站点会 403。"""
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
    cmd = [_exe(cfg), "--skip-download", "--write-thumbnail", "--no-write-info-json",
           "-P", d, "-o", "thumb_%(id)s.%(ext)s"]
    add_access_args(cmd, cfg)
    cmd += ["--", str(url)]
    try:
        proc = spawn(cmd, d)
        out, _ = proc.communicate(timeout=60)
    except Exception as e:
        return "", f"封面兜底取图失败：{e}"
    if proc.returncode != 0:
        tail = decode_bytes(out or b"").strip().splitlines()
        return "", "封面兜底取图失败：%s" % (tail[-1] if tail else "yt-dlp 返回非零")
    best, best_mtime = "", -1.0
    try:
        for name in os.listdir(d):
            if not name.startswith("thumb_"):
                continue
            p = os.path.join(d, name)
            if os.path.splitext(name)[1].lower() not in _THUMB_EXTS:
                continue
            m = os.path.getmtime(p)
            if m > best_mtime:
                best, best_mtime = p, m
    except OSError:
        pass
    if not best:
        return "", "站点没有提供封面"
    return best, None


def fetch_thumb(url, cfg, cache_dir=None, timeout=THUMB_TIMEOUT, max_bytes=THUMB_MAX_BYTES):
    """取封面到本地缓存，返回 (本地路径, 失败原因)。永不向调用方抛异常。"""
    if not is_public_http_url(url):
        return "", "封面地址不是公网 http(s)，已拒绝"
    path = thumb_cache_path(url, cache_dir)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path, None
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        return "", f"无法创建封面缓存目录：{e}"
    req = urllib.request.Request(str(url), headers=thumb_headers(cfg))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except Exception:
        return fetch_thumb_via_ytdlp(url, cfg, cache_dir)
    if len(data) > max_bytes:
        return "", "封面超过 2 MB，已跳过"
    if not data:
        return "", "封面是空响应"
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        return "", f"封面写盘失败：{e}"
    return path, None


# ---------------- 边下边播 ----------------

PREVIEW_SUBDIR = ".preview"


def fmt_is_separated(cfg):
    """最终格式表达式含 "+" 即音视频分离（下出来是两个文件，任何一个都不是完整片子）。"""
    fmt = str(cfg.get("fmt") or "").strip()
    if not fmt:
        fmt = MODE_FMT.get(mode_key(cfg.get("mode")), "")
    return "+" in fmt


def real_download_path(raw_path):
    """progress.filename 给的是目标名；磁盘上先找 .part，没有才用目标名本身。"""
    p = str(raw_path or "").strip()
    if not p:
        return ""
    part = p + ".part"
    if os.path.isfile(part):
        return part
    return p if os.path.isfile(p) else ""


def probe_playable(path, timeout=6):
    """让 ffprobe 读正在增长的文件，读得到流才算能播。只读不转码。"""
    if not os.path.isfile(path):
        return {"ok": False, "reason": "文件还没写出来"}
    cmd = [find_tool("ffprobe.exe"), "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", path]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        return {"ok": False, "reason": f"ffprobe 不可用：{e}"}
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return {"ok": False, "reason": "ffprobe 超时"}
    text = decode_bytes(out or b"").strip()
    if proc.returncode != 0 or not text:
        lines = decode_bytes(err or b"").strip().splitlines()
        return {"ok": False, "reason": lines[0] if lines else "读不到媒体信息"}
    return {"ok": True, "reason": ""}


def make_preview_link(real_path, outdir):
    """硬链接出一个扩展名正确的名字给系统播放器。

    播放器按内容识别、不看扩展名，但 .part 没有文件关联，os.startfile 会弹
    「选择打开方式」。硬链接零字节拷贝，且放在下载目录内的子目录保证同卷。
    """
    if not os.path.isfile(real_path):
        return "", "文件还没写出来"
    base = os.path.basename(real_path)
    if base.lower().endswith(".part"):
        base = base[:-5]
    d = os.path.join(outdir or "", PREVIEW_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        return "", f"无法创建预览目录：{e}"
    link = os.path.join(d, base)
    try:
        if os.path.exists(link):
            os.remove(link)
        os.link(real_path, link)
    except OSError as e:
        return "", f"此磁盘不支持硬链接（常见于 exFAT/FAT32 U 盘）：{e}"
    return link, None


def clear_preview_links(outdir):
    """只删链接，不动被链接的数据——删硬链接不影响源文件。"""
    d = os.path.join(outdir or "", PREVIEW_SUBDIR)
    try:
        for name in os.listdir(d):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
        os.rmdir(d)
    except OSError:
        pass


def sanitize_urls(urls, allow_other=False):
    """过滤出可交给 yt-dlp 的地址，返回 (可用列表, 被丢弃列表)。

    - 丢掉空行与以 # 开头的注释行
    - 裸域名（www.example.com/xxx）自动补 https://
    - 其余非法项（含以 - 开头的伪链接）一律返回给调用方提示
    """
    good, bad = [], []
    for raw in (urls or []):
        u = clean_media_url(raw)
        if not u or u.startswith("#"):
            continue
        if _BARE_DOMAIN_RE.match(u):
            u = "https://" + u
        if is_http_url(u) or (allow_other and _URL_SCHEME_RE.match(u)):
            good.append(u)
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
    r"--pa($|=)|--paths($|=)"
)
UNSAFE_ARG_RE = re.compile("^(" + _UNSAFE_LONG + ")", re.I)
# 短选项大小写敏感：-o/--output 与 -P/--paths 的落盘路径完全由用户指定，
# 但 -p、-o 之外的短选项（-4 / -N / -i）不该被误伤。
UNSAFE_SHORT_RE = re.compile(r"^(-o|-P)")


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
CONFIG_SCHEMA_VERSION = 2

DEFAULT_CONFIG = {
    "outdir": os.path.join(BASE_DIR, "downloads"),
    "mode": "best",
    "merge": "mp4",
    "fmt": "",
    "tpl": TEMPLATES[0],
    "sub": False, "sublang": "zh-Hans,zh,en", "embedsub": False,
    "thumb": True, "meta": True,
    "pl": True, "items": "", "ignore": True,
    "open_after": True,
    "rate": "", "proxy": "", "cookie": "", "cookie_header": "",
    "conc": False, "conc_n": "8", "aria": False, "aria_args": "aria2c:-x 16 -s 16 -k 1M",
    "flat": False, "extra": "",
    "browser": "（不使用）", "browser_profile": "",
    "referer": "", "ua": "", "headers": "",
    "generic": False, "extractor_args": "", "retries": "",
    # 网络自愈用的键（桌面版界面不暴露，但加载/保存时不能丢）
    "impersonate": "", "ipv4": False, "chunk": "", "proxy_off": False,
    "socket_timeout": "", "net_hard": True, "play_while": False,
    "yt_dlp_path": find_tool("yt-dlp.exe"),
    "schema_version": CONFIG_SCHEMA_VERSION,
}
# 旧键名 → 新键名（桌面版曾用 open，网页版用 open_after）
LEGACY_KEYS = {"open": "open_after"}


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
               "socket_timeout": ("超时秒数", 1, 600)}
_BOOL_FIELDS = ("sub", "embedsub", "thumb", "meta", "pl", "open_after", "ignore",
                "conc", "aria", "flat", "generic", "ipv4", "proxy_off", "net_hard",
                "play_while")
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
        if s.isdigit() and lo <= int(s) <= hi:
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


def load_config(path, defaults=None, warn=None):
    """读配置：缺失/损坏时退回默认值，并把问题通过 warn 回调报出来。"""
    base = dict(defaults or DEFAULT_CONFIG)
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("配置根节点不是对象")
        except Exception as e:
            msg = f"配置文件无法读取（{e}），已改用默认设置：{path}"
            if warn:
                warn(msg)
            else:
                print("[warn] " + msg, file=sys.stderr)
            data = {}
    cfg, warnings = coerce_config(data, base)
    for w in warnings:
        if warn:
            warn(w)
    return cfg


def atomic_write_text(path, text, backup=True):
    """先写临时文件再替换，避免断电/强杀留下半截配置。

    临时名用 mkstemp 而不是固定的 path+".tmp"：桌面版和网页版共用一个 config.json，
    两个进程同时保存时会互相覆盖临时文件的内容。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
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
    data, warnings = coerce_config(cfg, cfg)
    for w in warnings:
        if warn:
            warn(w)
    data["headers"] = normalize_headers(data.get("headers"))
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return True
    except Exception as e:
        msg = f"配置保存失败：{e}（{path}）"
        if warn:
            warn(msg)
        else:
            print("[err] " + msg, file=sys.stderr)
        return False


# ---------------- 命令行构建 ----------------

def add_access_args(cmd, cfg):
    """把「访问与身份」相关的参数追加到命令里。

    探测、下载、看格式用的是同一套凭据——否则会出现「探测不到但能下载」的矛盾。
    """
    browser = str(cfg.get("browser") or "（不使用）").strip()
    cookie_file = str(cfg.get("cookie") or "").strip()
    if browser and browser != "（不使用）":
        # 直接从浏览器读取登录态，省去手工导出 cookies.txt
        arg = browser
        if str(cfg.get("browser_profile") or "").strip():
            arg += ":" + str(cfg["browser_profile"]).strip()
        cmd += ["--cookies-from-browser", arg]
    elif cookie_file:
        cmd += ["--cookies", cookie_file]
    if str(cfg.get("ua") or "").strip():
        cmd += ["--user-agent", str(cfg["ua"]).strip()]
    if str(cfg.get("referer") or "").strip():
        cmd += ["--referer", str(cfg["referer"]).strip()]
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


def build_download_cmd(cfg, urls, no_playlist=False):
    cmd = [_exe(cfg), "--newline", "--progress-template", PROGRESS_TEMPLATE]
    if os.path.isfile(os.path.join(BASE_DIR, "ffmpeg.exe")):
        cmd += ["--ffmpeg-location", BASE_DIR]
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
    if cfg.get("play_while"):
        # 可流式容器：HLS 分片写成 mpegts，下载途中就能被播放器读。
        # 这个开关只影响 HLS 源，对别的源无副作用，所以不必先判断协议。
        cmd.append("--hls-use-mpegts")
        merge = str(cfg.get("merge") or "").strip()
        if merge and merge != "不转换" and not mode.startswith("audio"):
            # 下完 remux 回目标容器，最终产物与不开这个开关时一致；
            # remux 发生在下载结束后，不占下载带宽。
            cmd += ["--remux-video", merge]
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
    add_access_args(cmd, cfg)
    if cfg.get("conc"):
        try:
            n = max(1, min(32, int(str(cfg.get("conc_n") or 8).strip())))
        except ValueError:
            n = 8
        cmd += ["-N", str(n)]
    if cfg.get("aria"):
        cmd += ["--external-downloader", "aria2c"]
        if cfg.get("aria_args") and not unsafe_arg_in(cfg["aria_args"]):
            cmd += ["--external-downloader-args", str(cfg["aria_args"])]
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
    add_access_args(cmd, cfg)
    if cfg.get("items"):
        cmd += ["--playlist-items", str(cfg["items"])]
    cmd += ["--yes-playlist" if cfg.get("pl") else "--no-playlist"]
    # 多个链接一起探测时，单个失败不应中断其余链接
    good, _bad = sanitize_urls(urls)
    cmd += ["-i", "--", *good]
    return cmd


def build_formats_cmd(cfg, url):
    """看可用格式：以前只带 proxy/cookie，导致加了防盗链的站点看不到格式表。

    限速不进这条命令：yt-dlp 的 -F 只解析清单，--limit-rate 对它没有意义。
    """
    cmd = [_exe(cfg), "-F"]
    if str(cfg.get("proxy") or "").strip():
        cmd += ["--proxy", str(cfg["proxy"]).strip()]
    add_access_args(cmd, cfg)
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
        f = s.split(PROG_MARK, 1)[1].split("|")
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
      * 连续 stall_seconds 没有新行 → 终止子进程，reason="stall"
    结束原因写进 why 字典（why={"reason": ...}），由调用方决定日志文案。
    """
    box = queue.Queue()

    def pump():
        try:
            for line in iter_lines(proc, keep_blank=keep_blank):
                box.put(line)
        except Exception:
            pass
        box.put(_EOF)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    last_line = time.time()
    reason = "normal"

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
            if time.time() - last_line >= stall_seconds:
                reason = "stall"
                break
            continue
        if item is _EOF:
            break
        last_line = time.time()
        if abort and abort():
            reason = "client"
            break
        yield item
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
    cmd = [_exe(cfg), "--skip-download", "-j", "--no-warnings",
           "--no-playlist", "--playlist-items", "1"]
    if str(cfg.get("proxy") or "").strip():
        cmd += ["--proxy", str(cfg["proxy"]).strip()]
    if variant == "ua":
        cmd += ["--user-agent", UA_WINDOWS]
        r = referer_of(url)
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
        if str(cfg.get("cookie") or "").strip():
            cmd += ["--cookies", str(cfg["cookie"]).strip()]
    cmd += ["--", url]
    return cmd


def apply_probe_fix(cfg, url, fix):
    """把某一套排障方案落进配置，返回被改动的项目名列表。"""
    touched = []
    if fix.get("ua"):
        cfg["ua"] = UA_WINDOWS
        touched.append("User-Agent")
    if fix.get("referer"):
        r = referer_of(url)
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
        touched.append("代理")
    if fix.get("rate"):
        touched.append("限速")
    if fix.get("fmt"):
        touched.append("格式")
    return touched


def now():
    return time.time()
