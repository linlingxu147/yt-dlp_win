# -*- coding: utf-8 -*-
"""嗅探判定的 Python 侧实现。

规则不在这里写：从 ``sniffer_extension/sniff_rules.js`` 的 RULE-SOURCES 块读取
同一份 JSON 表，编译成等价的正则。扩展与桌面内嵌浏览器因此不会各写一套。

URL 清洗沿用 ``ytdlp_core.clean_media_url``（服务端登记时也是它），
本模块只负责「响应要不要登记」与「归到哪个分类」。
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
JS_PATH = os.path.join(ROOT, "sniffer_extension", "sniff_rules.js")
_BLOCK_RE = re.compile(r"//\s*RULE-SOURCES-BEGIN(.*?)//\s*RULE-SOURCES-END", re.S)


def _load_sources():
    with open(JS_PATH, encoding="utf-8") as f:
        text = f.read()
    m = _BLOCK_RE.search(text)
    if not m:
        raise RuntimeError("sniff_rules.js 缺少 RULE-SOURCES 标记")
    block = m.group(1)
    start, end = block.find("{"), block.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("sniff_rules.js 的 RULE-SOURCES 不是对象")
    return json.loads(block[start:end + 1])


SOURCES = _load_sources()
MIME_RE = re.compile(SOURCES["mime"], re.I)
NON_MEDIA_RE = re.compile(SOURCES["nonMedia"], re.I)
MANIFEST_MIME_RE = re.compile(SOURCES["manifestMime"], re.I)
MEDIA_EXT_RE = re.compile(SOURCES["mediaExt"], re.I)
MANIFEST_RE = re.compile(SOURCES["manifest"], re.I)
SEGMENT_RE = re.compile(SOURCES["segment"], re.I)
AUDIO_EXT_RE = re.compile(SOURCES["audioExt"], re.I)
BILI_AUDIO_RE = re.compile(SOURCES["biliAudio"], re.I)
IMAGE_EXT_RE = re.compile(SOURCES["imageExt"], re.I)
OPAQUE_MIME_RE = re.compile(SOURCES["opaqueMime"], re.I)
STATIC_EXT_RE = re.compile(SOURCES["staticExt"], re.I)
MIN_MIME_BYTES = int(SOURCES.get("minMimeBytes", 204800))
FALLBACK_BYTES = int(SOURCES.get("fallbackBytes", 1048576))

try:
    from ytdlp_core import clean_media_url as _clean_media_url
except Exception:                      # 单独 import 本模块做测试时的兜底
    _clean_media_url = None


def _nohash(url):
    return str(url or "").split("#", 1)[0]


def _noquery(url):
    return _nohash(url).split("?", 1)[0]


def is_manifest(url):
    return bool(MANIFEST_RE.search(_nohash(url)))


def is_segment(url):
    return bool(SEGMENT_RE.search(_nohash(url)))


def is_audio(url):
    path = _noquery(url)
    return bool(BILI_AUDIO_RE.search(path) or AUDIO_EXT_RE.search(path))


def guess_kind(url):
    u = _nohash(url)
    if MANIFEST_RE.search(u):
        return "mpd" if re.search(r"\.mpd(\?|#|$)", u, re.I) else "m3u8"
    if is_audio(url):
        return "audio"
    return "video"


def classify(url, mime):
    m = str(mime or "").lower()
    if is_audio(url) or m.startswith("audio/"):
        return "audio"
    if is_manifest(url) or is_segment(url) or MANIFEST_MIME_RE.search(m):
        return "stream"
    if m.startswith("video/"):
        return "video"
    if m.startswith("image/") or IMAGE_EXT_RE.search(_nohash(url)):
        return "image"
    if MEDIA_EXT_RE.search(_nohash(url)):
        return "video"
    return "other"


def accept(url, mime, size, opts=None):
    """这条响应要不要登记。

    opts 是 dict（内嵌浏览器 / 扩展传入），目前只认 ``range``：响应带 Content-Range
    时说明是分片续传，基本可判定为大媒体文件。不传 opts 时行为与旧版完全一致。
    """
    raw = str(url or "")
    if not re.match(r"^https?://", raw, re.I):
        return False
    u = _nohash(raw)
    path = _noquery(u)
    m = str(mime or "")
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        n = 0
    ranged = bool(isinstance(opts, dict) and (opts.get("range") or opts.get("ranged")))
    mime_hit = bool(MIME_RE.search(m))
    ext_hit = bool(MEDIA_EXT_RE.search(u))
    path_ext_hit = bool(MEDIA_EXT_RE.search(path))
    if mime_hit or ext_hit:
        # 非媒体 MIME 只有路径本身带媒体后缀才收：日志/埋点常把页面或媒体
        # 地址塞进查询串，不能因为 ?url=...mp4 就当成视频。
        if (NON_MEDIA_RE.search(m) and not MANIFEST_MIME_RE.search(m)
                and not path_ext_hit):
            return False
        if mime_hit and not ext_hit and 0 < n < MIN_MIME_BYTES:
            return False
        return True
    # 兜底：签名直链（无后缀 + application/octet-stream / 空 MIME）。
    # 见 sniff_rules.js 同一段注释：只靠体积与分片信号判断，宁可多收几条。
    if not isinstance(opts, dict) or not OPAQUE_MIME_RE.match(m.strip()):
        return False
    # JS/CSS/字体等静态资源即便被 CDN 标成 octet-stream、体积过 1MB，也不是媒体
    if STATIC_EXT_RE.search(path):
        return False
    if ranged:
        return True
    return n >= FALLBACK_BYTES


def normalize_url(url):
    if _clean_media_url is not None:
        return _clean_media_url(url)
    s = str(url or "").strip()
    s = re.sub(r"[\s\u200b-\u200f\u2028\u2029\ufeff]+", "", s)
    s = s.replace("&amp;", "&").replace("&#38;", "&")
    return s.strip().strip('"').strip("'")