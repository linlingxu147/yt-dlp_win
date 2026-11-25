# 预浏览 + 边下边播放 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 下载前能看到封面图与元数据防止下错视频；可选开关让下载途中用系统播放器打开正在增长的文件，且不牺牲下载速度。

**Architecture:** 全部与界面无关的逻辑放进共享核心 `ytdlp_core.py`（封面取图与缓存、ffprobe 可播性探测、硬链接预览、命令构建分支），网页版与桌面版各做一层薄界面。网页版新增 `GET /api/thumb?id=<token>` 与 `POST /api/play` 两个端点，封面地址与播放路径都由服务端掌握，客户端只拿 token、不传路径。

**Tech Stack:** Python 3.9+ 标准库（urllib / socket / ipaddress / hashlib / subprocess / http.server）、Tkinter、原生 JS 单文件前端、随包 `yt-dlp.exe` 2026.10.26 与 `ffmpeg.exe` / `ffprobe.exe`（LGPL 构建，无 libx264）。

**Spec:** `docs/superpowers/specs/2026-11-25-preview-and-play-while-downloading-design.md`

## Global Constraints

- `ytdlp_core.py` 只依赖标准库，**绝不 import tkinter / http.server**。
- 解释器：`E:/python/python.exe`（3.13.5）。测试命令 `E:/python/python.exe -m unittest discover -s tests`，现有 **102 例必须全绿**。
- 配置新键只有 `play_while`，bool，默认 `False`，加入 `_BOOL_FIELDS`。**不得复用已有的 `thumb` 键**（那是 `--embed-thumbnail`）。
- `play_while` 为假时，`build_download_cmd` 输出与改动前**逐字节相同**。
- **任何情况下都不加 `--no-part`**（保留续传与「已下完」判定）。
- `info_from_json` 现有 6 键（`title` `dur` `res` `size` `up` `url`）语义与取值不变，只新增 `thumb`。
- `progress_event` 的显示名 `state["file"]` 逻辑一个字不改，只新增 `state["raw_path"]`。
- 封面 URL 必须 `http(s)` 且**解析后**所有地址都是公网；客户端不得向 `/api/thumb` 传 `url`、不得向 `/api/play` 传 `path`/`url`。
- 界面文案全部中文；失败一律「降级不阻塞」，下载与探测结果不因预览/播放失败而受影响。
- `sniffer_extension/` 一行不改。
- 起服务做手工验证时用 `--port 8799`（8765–8775 之外），避免抢占正在使用的实例。
- 提交信息用中文，说明「为什么」；每个任务结束提交一次。

---

### Task 1: 核心 — 封面地址安全校验、缓存路径与取图

**Files:**
- Modify: `ytdlp_core.py`（ imports 区 15-25 行；`is_http_url` 之后即 357 行后插入新代码）
- Test: `tests/test_core.py`（追加新测试类）

**Interfaces:**
- Consumes: 现有 `is_http_url(url)`（`ytdlp_core.py:355`）、`normalize_headers(text)`、`scrub_header_value(text, limit=0)`、`find_tool(name)`、`spawn(cmd, cwd=None)`、`add_access_args(cmd, cfg)`、`decode_bytes(b)`。
- Produces:
  - `THUMB_CACHE_DIR: str`、`THUMB_MAX_BYTES = 2*1024*1024`、`THUMB_TIMEOUT = 8`
  - `is_public_http_url(url) -> bool`
  - `thumb_cache_path(url, cache_dir=None) -> str`
  - `thumb_headers(cfg) -> dict[str, str]`
  - `fetch_thumb(url, cfg, cache_dir=None, timeout=8, max_bytes=2097152) -> (path: str, err: str|None)`
  - `fetch_thumb_via_ytdlp(url, cfg, cache_dir=None) -> (path: str, err: str|None)`

- [ ] **Step 1: 写失败测试**

在 `tests/test_core.py` 末尾追加：

```python
class TestThumbSafety(unittest.TestCase):
    def test_public_https_ok(self):
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            self.assertTrue(core.is_public_http_url("https://i.ytimg.com/vi/x/hqdefault.jpg"))

    def test_non_http_schemes_rejected(self):
        for u in ("file:///C:/a.jpg", "ftp://x/a.jpg", "blob:https://a/b", "data:image/png;base64,AA", ""):
            self.assertFalse(core.is_public_http_url(u), u)

    def test_literal_private_addresses_rejected(self):
        for u in ("http://127.0.0.1/a.jpg", "http://10.1.2.3/a.jpg", "http://192.168.1.1/a.jpg",
                  "http://172.16.0.1/a.jpg", "http://169.254.1.1/a.jpg", "http://[::1]/a.jpg",
                  "http://localhost/a.jpg"):
            self.assertFalse(core.is_public_http_url(u), u)

    def test_public_hostname_resolving_to_private_rejected(self):
        # 只看字面 IP 挡不住这一招：域名是公网的，解析结果指向本机
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            self.assertFalse(core.is_public_http_url("https://evil.example/a.jpg"))

    def test_resolution_failure_rejected(self):
        with mock.patch("socket.getaddrinfo", side_effect=OSError("no such host")):
            self.assertFalse(core.is_public_http_url("https://nope.invalid/a.jpg"))

    def test_cache_path_is_stable_and_extension_whitelisted(self):
        u = "https://i.ytimg.com/vi/x/hqdefault.jpg"
        a = core.thumb_cache_path(u, "C:/tmp/x")
        b = core.thumb_cache_path(u, "C:/tmp/x")
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(".jpg"))
        self.assertEqual(core.thumb_cache_path("https://a/b?x=1", "C:/tmp/x")[-4:], ".img")

    def test_headers_carry_access_credentials(self):
        cfg = {"ua": "UA/1", "referer": "https://a.com/", "headers": "X-Token: abc",
               "cookie_header": "Cookie: sid=1"}
        h = core.thumb_headers(cfg)
        self.assertEqual(h["User-Agent"], "UA/1")
        self.assertEqual(h["Referer"], "https://a.com/")
        self.assertEqual(h["X-Token"], "abc")
        self.assertEqual(h["Cookie"], "sid=1")

    def test_fetch_rejects_private_before_any_request(self):
        with mock.patch("urllib.request.urlopen") as op:
            path, err = core.fetch_thumb("http://127.0.0.1/a.jpg", {})
        op.assert_not_called()
        self.assertEqual(path, "")
        self.assertTrue(err)

    def test_fetch_cache_hit_skips_network(self):
        with tempfile.TemporaryDirectory() as d:
            u = "https://cdn.example/a.jpg"
            p = core.thumb_cache_path(u, d)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\xff\xd8\xff")
            with mock.patch("urllib.request.urlopen") as op:
                path, err = core.fetch_thumb(u, {}, cache_dir=d)
        op.assert_not_called()
        self.assertEqual(path, p)
        self.assertIsNone(err)

    def test_fetch_size_cap(self):
        big = b"x" * (core.THUMB_MAX_BYTES + 1)
        resp = mock.MagicMock()
        resp.read.return_value = big
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("urllib.request.urlopen", return_value=resp):
                path, err = core.fetch_thumb("https://cdn.example/a.jpg", {}, cache_dir=d)
        self.assertEqual(path, "")
        self.assertIn("2 MB", err)

    def test_fetch_network_error_falls_back_to_ytdlp(self):
        # 兜底会真起 yt-dlp 进程，测试里必须替身掉，否则单测会联网
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.is_public_http_url", return_value=True), \
                 mock.patch("urllib.request.urlopen", side_effect=OSError("reset")), \
                 mock.patch("ytdlp_core.fetch_thumb_via_ytdlp",
                            return_value=("", "兜底也失败")) as fb:
                path, err = core.fetch_thumb("https://cdn.example/a.jpg", {}, cache_dir=d)
        fb.assert_called_once()
        self.assertEqual(path, "")
        self.assertEqual(err, "兜底也失败")

    def test_ytdlp_fallback_reports_reason_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("ytdlp_core.spawn", side_effect=OSError("no exe")):
                path, err = core.fetch_thumb_via_ytdlp("https://cdn.example/a.jpg", {}, cache_dir=d)
        self.assertEqual(path, "")
        self.assertIn("no exe", err)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestThumbSafety -v`
Expected: FAIL / ERROR，`AttributeError: module 'ytdlp_core' has no attribute 'is_public_http_url'`

- [ ] **Step 3: 加 imports**

把 `ytdlp_core.py` 第 15-25 行的 import 块改成（新增 `hashlib`、`ipaddress`、`socket`、`urllib.error`、`urllib.request`、`urlsplit`，其余保持原样）：

```python
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
```

- [ ] **Step 4: 实现安全校验与缓存路径**

在 `ytdlp_core.py` 的 `is_http_url`（355-356 行）之后、`sanitize_urls` 之前插入：

```python
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
```

- [ ] **Step 5: 实现取图（含 yt-dlp 兜底）**

紧接上一步插入的代码之后追加：

```python
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
```

- [ ] **Step 6: 跑测试确认通过**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestThumbSafety -v`
Expected: PASS（12 例）

- [ ] **Step 7: 全量回归**

Run: `E:/python/python.exe -m unittest discover -s tests`
Expected: OK，114 例（102 + 12）

- [ ] **Step 8: 提交**

```bash
git add ytdlp_core.py tests/test_core.py
git commit -m "核心：封面取图与公网地址校验

站点返回的 thumbnail 可能指向 127.0.0.1，让本地服务替它探内网，
所以解析后再判私网；urllib 取不到时复用 yt-dlp 的指纹与 cookie 通路兜底。"
```

---

### Task 2: 核心 — 探测结果带封面、进度带真实路径

**Files:**
- Modify: `ytdlp_core.py:264-294`（`info_from_json` / `info_from_json_raw`）、`807-809`（`new_progress_state`）、`817-858`（`parse_progress_line`）、`869-897`（`progress_event`）
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: Task 1 的 `thumb_cache_path` 等（本任务不直接用，仅同模块）。
- Produces:
  - `info_from_json(d)` 与 `info_from_json_raw(d)` 的返回 dict 新增 `"thumb": str`
  - `new_progress_state()` 新增 `"raw_path": ""`
  - `parse_progress_line(line)` 返回 dict 新增 `"raw": str`（完整路径，未做 basename / FRAG_RE 处理）
  - `progress_event(state, line)` 维护 `state["raw_path"]`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_core.py`：

```python
class TestPreviewFields(unittest.TestCase):
    def test_thumb_from_thumbnail(self):
        info = core.info_from_json({"title": "T", "thumbnail": "https://a/x.jpg"})
        self.assertEqual(info["thumb"], "https://a/x.jpg")

    def test_thumb_falls_back_to_thumbnails_last(self):
        d = {"title": "T", "thumbnails": [{"url": "https://a/small.jpg"},
                                          {"url": "https://a/big.jpg"}]}
        self.assertEqual(core.info_from_json(d)["thumb"], "https://a/big.jpg")

    def test_thumb_empty_when_absent(self):
        self.assertEqual(core.info_from_json({"title": "T"})["thumb"], "")

    def test_existing_six_keys_unchanged(self):
        d = {"title": "T", "duration": 61, "height": 1080, "width": 1920,
             "filesize": 2048, "uploader": "U", "webpage_url": "https://a/v",
             "thumbnail": "https://a/x.jpg"}
        info = core.info_from_json(d)
        self.assertEqual(info["title"], "T")
        self.assertEqual(info["dur"], "01:01")
        self.assertEqual(info["res"], "1920x1080")
        self.assertEqual(info["size"], "2.0 KB")
        self.assertEqual(info["up"], "U")
        self.assertEqual(info["url"], "https://a/v")
        raw = core.info_from_json_raw(d)
        self.assertEqual(raw["dur"], 61)
        self.assertEqual(raw["size"], 2048)
        self.assertEqual(raw["thumb"], "https://a/x.jpg")

    def test_raw_path_keeps_full_path_and_format_marker(self):
        st = core.new_progress_state()
        line = "PROG|downloading|500|NA|1000|NA|NA|C:/d/Title.f137.mp4"
        core.progress_event(st, line)
        self.assertEqual(st["raw_path"], "C:/d/Title.f137.mp4")
        # 显示名照旧：去目录、去格式标记
        self.assertEqual(st["file"], "Title.mp4")

    def test_raw_path_from_destination_line(self):
        st = core.new_progress_state()
        core.progress_event(st, "[download] Destination: C:/d/Title.f140.m4a")
        self.assertEqual(st["raw_path"], "C:/d/Title.f140.m4a")
        self.assertEqual(st["file"], "Title.m4a")

    def test_parse_progress_line_exposes_raw(self):
        p = core.parse_progress_line("PROG|downloading|500|NA|1000|NA|NA|C:/d/a.f137.mp4")
        self.assertEqual(p["raw"], "C:/d/a.f137.mp4")
        self.assertEqual(p["file"], "a.mp4")

    def test_legacy_progress_line_has_empty_raw(self):
        p = core.parse_progress_line("[download]  12.5% of  10.00MiB at 1.00MiB/s ETA 00:10")
        self.assertEqual(p["raw"], "")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPreviewFields -v`
Expected: FAIL，`KeyError: 'thumb'` / `KeyError: 'raw_path'`

- [ ] **Step 3: info_from_json 加 thumb**

把 `ytdlp_core.py:278-282` 的 return 块替换为：

```python
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
```

`info_from_json_raw` 不用改——它调 `info_from_json` 后只覆盖 `dur` / `size`，`thumb` 自动带上。

- [ ] **Step 4: new_progress_state 加 raw_path**

把 `ytdlp_core.py:807-809` 替换为：

```python
def new_progress_state():
    return {"pct": 0.0, "size": "", "speed": "", "eta": "",
            "file": "", "raw_path": "", "item_i": 0, "item_total": 0, "done": set()}
```

- [ ] **Step 5: parse_progress_line 暴露原始路径**

在 `ytdlp_core.py:851-852` 的 return 里加 `raw`（`fname` 是机器行第 7 段，原样保留）：

```python
        return {"status": status, "pct": max(0.0, min(100.0, pct)), "size": size,
                "speed": spd, "eta": _clean_num(eta), "file": name, "raw": fname.strip()}
```

把 `853-857` 的旧正则兜底 return 改成：

```python
    m = PROG_RE.search(s)
    if m:
        return {"status": "downloading", "pct": float(m.group("pct")),
                "size": m.group("size") or "", "speed": m.group("speed") or "",
                "eta": m.group("eta") or "", "file": "", "raw": ""}
```

- [ ] **Step 6: progress_event 维护 raw_path**

在 `ytdlp_core.py:881-882`（`if p.get("file"): state["file"] = p["file"]`）之后插入：

```python
        if p.get("raw"):
            state["raw_path"] = p["raw"]
```

把 `888-892` 的 Destination 兜底分支替换为：

```python
    base = parse_file_line(line)
    if base:
        state["done"].add(os.path.splitext(base)[0])
        state["file"] = base
        m = FILE_RE.match(str(line or ""))
        if m:
            state["raw_path"] = m.group("path").strip()
        return {"kind": "file", "file": base}
```

- [ ] **Step 7: 跑测试确认通过 + 全量回归**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPreviewFields -v`
Expected: PASS（8 例）

Run: `E:/python/python.exe -m unittest discover -s tests`
Expected: OK，122 例

- [ ] **Step 8: 提交**

```bash
git add ytdlp_core.py tests/test_core.py
git commit -m "核心：探测结果带封面地址，进度带磁盘真实路径

显示名会去掉目录和 .f137 标记，那是给人看的；边下边播要定位真实文件，
所以另存一份原始路径，显示名逻辑不动。"
```

---

### Task 3: 核心 — 可播性探测、硬链接预览、分离源判定

**Files:**
- Modify: `ytdlp_core.py`（Task 1 新增的封面区块之后追加）
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `find_tool(name)`、`decode_bytes(b)`、`kill_tree(proc)`、`CREATE_NO_WINDOW`、`mode_key(value)`、`MODE_FMT`。
- Produces:
  - `PREVIEW_SUBDIR = ".preview"`
  - `fmt_is_separated(cfg) -> bool`
  - `real_download_path(raw_path) -> str`（空串表示文件还没出现）
  - `probe_playable(path, timeout=6) -> {"ok": bool, "reason": str}`
  - `make_preview_link(real_path, outdir) -> (link_path: str, err: str|None)`
  - `clear_preview_links(outdir) -> None`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_core.py`：

```python
FFMPEG = os.path.join(ROOT, "ffmpeg.exe")
FFPROBE = os.path.join(ROOT, "ffprobe.exe")
HAS_FFMPEG = os.path.isfile(FFMPEG) and os.path.isfile(FFPROBE)


class TestPlayHelpers(unittest.TestCase):
    def test_separated_fmt(self):
        self.assertTrue(core.fmt_is_separated({"mode": "best"}))
        self.assertTrue(core.fmt_is_separated({"mode": "single", "fmt": "137+140"}))
        self.assertFalse(core.fmt_is_separated({"mode": "single"}))
        self.assertFalse(core.fmt_is_separated({"mode": "best", "fmt": "b"}))
        self.assertFalse(core.fmt_is_separated({"mode": "audio_mp3"}))

    def test_real_path_prefers_part_file(self):
        with tempfile.TemporaryDirectory() as d:
            final = os.path.join(d, "a.mp4")
            self.assertEqual(core.real_download_path(final), "")
            with open(final + ".part", "wb") as f:
                f.write(b"x")
            self.assertEqual(core.real_download_path(final), final + ".part")
            os.remove(final + ".part")
            with open(final, "wb") as f:
                f.write(b"x")
            self.assertEqual(core.real_download_path(final), final)

    def test_real_path_empty_input(self):
        self.assertEqual(core.real_download_path(""), "")
        self.assertEqual(core.real_download_path(None), "")

    def test_probe_missing_file(self):
        v = core.probe_playable("Z:/definitely/not/here.mkv")
        self.assertFalse(v["ok"])
        self.assertTrue(v["reason"])

    def test_link_keeps_format_marker_and_clears_safely(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "Title.f137.mp4.part")
            with open(src, "wb") as f:
                f.write(b"abcdef")
            link, err = core.make_preview_link(src, d)
            self.assertIsNone(err)
            self.assertEqual(os.path.basename(link), "Title.f137.mp4")
            self.assertEqual(os.path.dirname(link), os.path.join(d, ".preview"))
            with open(link, "rb") as f:
                self.assertEqual(f.read(), b"abcdef")
            # 硬链接：写源文件，链接看得到增长
            with open(src, "ab") as f:
                f.write(b"growing")
            with open(link, "rb") as f:
                self.assertEqual(f.read(), b"abcdefgrowing")
            core.clear_preview_links(d)
            self.assertFalse(os.path.exists(link))
            self.assertTrue(os.path.isfile(src))
            with open(src, "rb") as f:
                self.assertEqual(f.read(), b"abcdefgrowing")

    def test_link_replaces_stale_one(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.mp4.part")
            with open(src, "wb") as f:
                f.write(b"new")
            os.makedirs(os.path.join(d, ".preview"))
            with open(os.path.join(d, ".preview", "a.mp4"), "wb") as f:
                f.write(b"stale")
            link, err = core.make_preview_link(src, d)
            self.assertIsNone(err)
            with open(link, "rb") as f:
                self.assertEqual(f.read(), b"new")

    def test_link_missing_source_reports_reason(self):
        with tempfile.TemporaryDirectory() as d:
            link, err = core.make_preview_link(os.path.join(d, "nope.part"), d)
            self.assertEqual(link, "")
            self.assertTrue(err)

    def test_clear_on_missing_dir_is_quiet(self):
        core.clear_preview_links("Z:/definitely/not/here")


@unittest.skipUnless(HAS_FFMPEG, "需要随包 ffmpeg/ffprobe")
class TestProbePlayableReal(unittest.TestCase):
    """把设计阶段的探针固化成测试：截断的 mkv/webm/ts 可读，mp4 不可读。"""

    def _make(self, d, name, extra):
        out = os.path.join(d, name)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
               "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6"] + extra + ["-y", out]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=core.CREATE_NO_WINDOW, timeout=120)
        return out

    def _truncate(self, src, pct):
        dst = src + ".partial"
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data[:len(data) * pct // 100])
        return dst

    def test_mkv_partial_is_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.mkv", ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac"])
            self.assertTrue(core.probe_playable(self._truncate(src, 40))["ok"])

    def test_webm_partial_is_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.webm", ["-c:v", "libvpx", "-b:v", "200k", "-c:a", "libopus"])
            self.assertTrue(core.probe_playable(self._truncate(src, 40))["ok"])

    def test_mpegts_partial_is_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.ts", ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-f", "mpegts"])
            self.assertTrue(core.probe_playable(self._truncate(src, 40))["ok"])

    def test_mp4_partial_is_not_playable(self):
        with tempfile.TemporaryDirectory() as d:
            src = self._make(d, "a.mp4", ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac"])
            v = core.probe_playable(self._truncate(src, 40))
            self.assertFalse(v["ok"])
            self.assertTrue(v["reason"])
```

在 `tests/test_core.py` 的 import 区补 `import subprocess`（现有 import 里没有）。

- [ ] **Step 2: 跑测试确认失败**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPlayHelpers -v`
Expected: FAIL，`AttributeError: module 'ytdlp_core' has no attribute 'fmt_is_separated'`

- [ ] **Step 3: 实现**

在 `ytdlp_core.py` 的 `fetch_thumb` 之后追加：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPlayHelpers -v`
Expected: PASS（8 例）

Run: `E:/python/python.exe -m unittest discover -s tests -k TestProbePlayableReal -v`
Expected: PASS（4 例；无 ffmpeg 的机器上是 skipped）

- [ ] **Step 5: 全量回归**

Run: `E:/python/python.exe -m unittest discover -s tests`
Expected: OK，134 例

- [ ] **Step 6: 提交**

```bash
git add ytdlp_core.py tests/test_core.py
git commit -m "核心：可播性探测与硬链接预览

能不能播交给 ffprobe 实测，不靠猜格式；硬链接零字节拷贝，
清理只删链接不动数据。截断容器的探针结论固化成回归测试。"
```

---

### Task 4: 核心 — play_while 配置键与命令构建分支

**Files:**
- Modify: `ytdlp_core.py:442-463`（`DEFAULT_CONFIG`）、`538-539`（`_BOOL_FIELDS`）、`718-773`（`build_download_cmd`）
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: Task 3 的 `fmt_is_separated(cfg)`。
- Produces: 配置键 `play_while`（bool，默认 False）；`build_download_cmd(cfg, urls, no_playlist=False)` 在 `play_while` 为真时追加 `--hls-use-mpegts`，并在 merge 有效且非音频模式时追加 `--remux-video <merge>`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_core.py`：

```python
class TestPlayWhileCmd(unittest.TestCase):
    URLS = ["https://example.com/v"]

    def test_off_by_default(self):
        self.assertFalse(core.DEFAULT_CONFIG["play_while"])

    def test_command_byte_identical_when_off(self):
        cfg = core.default_config()
        base = core.build_download_cmd(cfg, self.URLS)
        cfg["play_while"] = False
        self.assertEqual(core.build_download_cmd(cfg, self.URLS), base)
        self.assertNotIn("--hls-use-mpegts", base)
        self.assertNotIn("--remux-video", base)

    def test_never_adds_no_part(self):
        for pw in (False, True):
            cfg = dict(core.default_config(), play_while=pw)
            self.assertNotIn("--no-part", core.build_download_cmd(cfg, self.URLS))

    def test_on_adds_mpegts(self):
        cfg = dict(core.default_config(), play_while=True, mode="single")
        cmd = core.build_download_cmd(cfg, self.URLS)
        self.assertIn("--hls-use-mpegts", cmd)

    def test_on_remuxes_to_merge_target(self):
        cfg = dict(core.default_config(), play_while=True, mode="single", merge="mkv")
        cmd = core.build_download_cmd(cfg, self.URLS)
        i = cmd.index("--remux-video")
        self.assertEqual(cmd[i + 1], "mkv")

    def test_no_remux_when_merge_disabled(self):
        for merge in ("", "不转换"):
            cfg = dict(core.default_config(), play_while=True, mode="single", merge=merge)
            self.assertNotIn("--remux-video", core.build_download_cmd(cfg, self.URLS))

    def test_no_remux_for_audio_modes(self):
        for mode in ("audio_mp3", "audio_raw"):
            cfg = dict(core.default_config(), play_while=True, mode=mode)
            self.assertNotIn("--remux-video", core.build_download_cmd(cfg, self.URLS))

    def test_flags_come_before_url_separator(self):
        cfg = dict(core.default_config(), play_while=True, mode="single")
        cmd = core.build_download_cmd(cfg, self.URLS)
        self.assertLess(cmd.index("--hls-use-mpegts"), cmd.index("--"))

    def test_type_coercion(self):
        cfg, warns = core.coerce_config({"play_while": "false"})
        self.assertIs(cfg["play_while"], False)
        cfg, _ = core.coerce_config({"play_while": "开"})
        self.assertIs(cfg["play_while"], True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPlayWhileCmd -v`
Expected: FAIL，`KeyError: 'play_while'`

- [ ] **Step 3: 加配置键**

把 `ytdlp_core.py:460` 那一行改成：

```python
    "socket_timeout": "", "net_hard": True, "play_while": False,
```

把 `538-539` 的 `_BOOL_FIELDS` 改成：

```python
_BOOL_FIELDS = ("sub", "embedsub", "thumb", "meta", "pl", "open_after", "ignore",
                "conc", "aria", "flat", "generic", "ipv4", "proxy_off", "net_hard",
                "play_while")
```

- [ ] **Step 4: 加命令分支**

在 `ytdlp_core.py` 的 `build_download_cmd` 里，`elif mode == "audio_mp3":` 那两行（736-737）之后、`if cfg.get("sub"):`（738）之前插入：

```python
    if cfg.get("play_while"):
        # 可流式容器：HLS 分片写成 mpegts，下载途中就能被播放器读。
        # 这个开关只影响 HLS 源，对别的源无副作用，所以不必先判断协议。
        cmd.append("--hls-use-mpegts")
        merge = str(cfg.get("merge") or "").strip()
        if merge and merge != "不转换" and not mode.startswith("audio"):
            # 下完 remux 回目标容器，最终产物与不开这个开关时一致；
            # remux 发生在下载结束后，不占下载带宽。
            cmd += ["--remux-video", merge]
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPlayWhileCmd -v`
Expected: PASS（9 例）

Run: `E:/python/python.exe -m unittest discover -s tests`
Expected: OK，143 例

- [ ] **Step 6: 桌面版命令自检**

Run: `E:/python/python.exe YtDlpGUI/main.py --selftest`
Expected: 退出码 0，打印的命令里**不含** `--hls-use-mpegts`（默认关闭）

- [ ] **Step 7: 提交**

```bash
git add ytdlp_core.py tests/test_core.py
git commit -m "核心：边下边播开关与命令分支

开关关闭时命令逐字节不变；开启时只加 --hls-use-mpegts 与下载后的 remux，
不加 --no-part，续传与「已下完」判定完全不受影响。"
```

---

### Task 5: 网页版服务端 — token 表、两个端点、可播性事件与清理

**Files:**
- Modify: `YtDlpWeb/server.py`（imports 25-35；`from ytdlp_core import` 75-81；`run_detect` 193；`_download_attempt` 252-285；`run_download` 215-249；`do_GET` 726-755；`do_POST` 905-931；`purge_stale_cookie_files` 939-954；`main` 957-958）
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: Task 1-4 的全部核心函数。
- Produces:
  - `register_thumb(url) -> str`（token，空 URL 返回空串）、`thumb_url_for(token) -> str|None`
  - `THUMB_TOKENS: dict`、`THUMB_TOKEN_MAX = 512`、`THUMB_LOCK`
  - `PLAY_STATE: dict`（键 `raw_path` `outdir` `cfg` `ok` `reason`）、`PLAY_LOCK`
  - `GET /api/thumb?id=<token>`：200 图片字节 / 400 传了 url / 404 未知 token / 502 取图失败
  - `POST /api/play`：200 `{"ok":true}` / 400 传了 path 或不可播 / 409 无下载任务 / 500 播放器打开失败
  - SSE 事件 `{"type":"playable","ok":bool,"reason":str}`；`row` 事件的 `data` 里多一个 `thumb_id`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_core.py`：

```python
class TestThumbTokens(unittest.TestCase):
    def setUp(self):
        self.srv = load_web_server()
        with self.srv.THUMB_LOCK:
            self.srv.THUMB_TOKENS.clear()

    def test_register_and_lookup(self):
        t = self.srv.register_thumb("https://a/x.jpg")
        self.assertTrue(t)
        self.assertEqual(self.srv.thumb_url_for(t), "https://a/x.jpg")

    def test_empty_url_gives_empty_token(self):
        self.assertEqual(self.srv.register_thumb(""), "")

    def test_unknown_token(self):
        self.assertIsNone(self.srv.thumb_url_for("nope"))
        self.assertIsNone(self.srv.thumb_url_for(""))

    def test_fifo_cap(self):
        for i in range(self.srv.THUMB_TOKEN_MAX + 5):
            self.srv.register_thumb("https://a/%d.jpg" % i)
        with self.srv.THUMB_LOCK:
            self.assertLessEqual(len(self.srv.THUMB_TOKENS), self.srv.THUMB_TOKEN_MAX)


class TestPlayState(unittest.TestCase):
    def setUp(self):
        self.srv = load_web_server()

    def test_separated_source_verdict(self):
        out = []
        self.srv._play_probe_loop("C:/d/a.f137.mp4", {"mode": "best"}, "C:/d", out.append)
        self.assertFalse(out[-1]["ok"])
        self.assertIn("分离", out[-1]["reason"])

    def test_aria_source_verdict(self):
        out = []
        self.srv._play_probe_loop("C:/d/a.mp4", {"mode": "single", "aria": True}, "C:/d", out.append)
        self.assertFalse(out[-1]["ok"])
        self.assertIn("aria2c", out[-1]["reason"])

    def test_play_state_defaults_not_playable(self):
        with self.srv.PLAY_LOCK:
            self.assertFalse(self.srv.PLAY_STATE["ok"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestThumbTokens -v`
Expected: FAIL，`AttributeError: module ... has no attribute 'THUMB_LOCK'`

- [ ] **Step 3: 补 imports**

`YtDlpWeb/server.py` 第 25-35 行的 import 块改为：

```python
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
```

把 75-81 行的 `from ytdlp_core import (...)` 名单补上新函数（保持字母序插入即可）：

```python
from ytdlp_core import (  # noqa: E402
    NET_PLAN_LABEL, PROBE_VARIANTS, build_probe_cmd, classify_line, clean_media_url,
    clear_preview_links, command_text, diagnose, find_tool, fmt_is_separated,
    info_from_json, is_net_failure, iter_lines_watch, json_ok as _json_ok, kill_tree,
    make_preview_link, new_progress_state, next_net_plan, parse_format_line, path_inside,
    probe_playable, progress_event, progress_text, real_download_path, sanitize_urls,
    scrub_header_value, spawn, unsafe_arg_in,
)
```

- [ ] **Step 4: token 表与播放状态**

在 `YtDlpWeb/server.py` 的 `class Runner:`（119 行）之前插入：

```python
# ---------------- 预浏览：封面 token 表 ----------------
# 客户端只拿 token，不能传 URL 让服务端去 fetch——否则这就是个能探内网的 SSRF 洞。
THUMB_LOCK = threading.Lock()
THUMB_TOKENS = {}
THUMB_TOKEN_MAX = 512

_THUMB_CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
                ".img": "application/octet-stream"}


def register_thumb(url):
    if not url:
        return ""
    token = secrets.token_urlsafe(12)
    with THUMB_LOCK:
        THUMB_TOKENS[token] = str(url)
        while len(THUMB_TOKENS) > THUMB_TOKEN_MAX:
            THUMB_TOKENS.pop(next(iter(THUMB_TOKENS)))
    return token


def thumb_url_for(token):
    with THUMB_LOCK:
        return THUMB_TOKENS.get(str(token or ""))


# ---------------- 边下边播：当前任务的可播性 ----------------
PLAY_LOCK = threading.Lock()
PLAY_STATE = {"raw_path": "", "outdir": "", "cfg": {}, "ok": False,
              "reason": "还没有开始下载"}


def _play_verdict(ok, reason, emit):
    with PLAY_LOCK:
        PLAY_STATE["ok"] = bool(ok)
        PLAY_STATE["reason"] = reason
    emit({"type": "playable", "ok": bool(ok), "reason": reason})


def _play_probe_loop(raw, cfg, outdir, emit):
    """后台判断能否边下边播。只读不转码，不占下载带宽。"""
    if fmt_is_separated(cfg):
        return _play_verdict(False, "此源音视频分离，边下边播不适用（改用单文件模式或关掉此开关）", emit)
    if cfg.get("aria"):
        return _play_verdict(False, "aria2c 外部下载器的文件布局不可控，边下边播不适用", emit)
    reason = "文件还没写出来"
    deadline = time.time() + 3
    for _ in range(3):
        # 「≥256KB」与「≥3 秒」任一满足即探：慢源不干等体积，快源不白等 3 秒
        while time.time() < deadline:
            with PLAY_LOCK:
                if PLAY_STATE["raw_path"] != raw:
                    return
            p = real_download_path(raw)
            try:
                if p and os.path.getsize(p) >= 256 * 1024:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        with PLAY_LOCK:
            if PLAY_STATE["raw_path"] != raw:
                return
        p = real_download_path(raw)
        if p:
            v = probe_playable(p)
            if v["ok"]:
                return _play_verdict(True, "", emit)
            reason = v["reason"]
        deadline = time.time() + 5
    return _play_verdict(False, "此源要下完才能播：%s" % reason, emit)


def _start_play_probe(raw, cfg, outdir, emit):
    with PLAY_LOCK:
        PLAY_STATE.update({"raw_path": raw, "outdir": outdir, "cfg": dict(cfg),
                           "ok": False, "reason": "正在判断能否边下边播…"})
    emit({"type": "playable", "ok": False, "reason": "正在判断能否边下边播…"})
    threading.Thread(target=_play_probe_loop, args=(raw, dict(cfg), outdir, emit),
                     daemon=True).start()
```

- [ ] **Step 5: 探测结果登记封面 token**

把 `YtDlpWeb/server.py:193` 那一行替换为：

```python
                    info = info_from_json(json.loads(s))
                    info["thumb_id"] = register_thumb(info.get("thumb"))
                    emit({"type": "row", "data": info})
```

- [ ] **Step 6: 下载时触发探测、结束时清理**

在 `_download_attempt` 的 `if ev:` 块里，`emit({"type": "progress", ...})` 之后、`continue`（274 行）之前插入：

```python
                raw = state.get("raw_path") or ""
                if raw and cfg.get("play_while"):
                    with PLAY_LOCK:
                        known = PLAY_STATE["raw_path"]
                    if raw != known:
                        _start_play_probe(raw, cfg, outdir, emit)
```

把 `run_download` 的 223-249 行（从 `t0 = time.time()` 到循环结束）整段替换为下面这版——循环体逐字保留，只是外面包了 try/finally，保证任何退出路径都清理预览链接：

```python
    t0 = time.time()
    # 失败后按「直连 → 换浏览器指纹 → 换传输方式」阶梯自愈，专治 ConnectionReset(10054)
    tried = set()
    name, extra = "normal", {}
    try:
        while True:
            c = cfg if not extra else dict(cfg, **extra)
            if name != "normal":
                emit({"type": "log", "level": "warn",
                      "text": "[自动重试] 换用%s重试（已下载部分会续传）" % NET_PLAN_LABEL[name]})
            code, tail, stopped, done_count = _download_attempt(
                c, urls, emit, no_playlist, abort, outdir, keepalive)
            if code is None:          # 启动失败，launch 已自行上报
                return
            if code == 0 or stopped:
                emit({"type": "done", "code": code, "elapsed": round(time.time() - t0, 1),
                      "done_count": done_count,
                      "diag": diagnose(tail) if code != 0 else []})
                return
            tried.add(name)
            # 用原始 cfg 推导 Origin：no-referer 那招已经把 referer 清空了
            nxt = next_net_plan(tail, tried, cfg, url=urls[0]) if (
                cfg.get("net_hard", True) and is_net_failure(tail)) else None
            if not nxt:
                emit({"type": "done", "code": code, "elapsed": round(time.time() - t0, 1),
                      "done_count": done_count, "diag": diagnose(tail)})
                return
            name, extra = nxt
    finally:
        clear_preview_links(outdir)
        with PLAY_LOCK:
            PLAY_STATE.update({"raw_path": "", "ok": False, "reason": "还没有开始下载"})
```

- [ ] **Step 7: GET /api/thumb**

在 `do_GET` 的 `if p == "/api/sniff/log":`（753 行）之前插入：

```python
        if p == "/api/thumb":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if "url" in qs:
                return self._json({"error": "不接受客户端指定封面地址"}, 400)
            url = thumb_url_for((qs.get("id") or [""])[0])
            if not url:
                return self._json({"error": "封面不存在或已过期"}, 404)
            path, err = core.fetch_thumb(url, load_config())
            if err:
                return self._json({"error": err}, 502)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                return self._json({"error": "封面读取失败：%s" % e}, 502)
            ctype = _THUMB_CTYPE.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
```

- [ ] **Step 8: POST /api/play**

在 `do_POST` 的 `if p == "/api/open":`（909 行）之前插入：

```python
        if p == "/api/play":
            # 路径由服务端自己掌握：客户端传路径等于让它指定打开本机任意文件
            if body.get("path") or body.get("url"):
                return self._json({"error": "不接受客户端指定路径"}, 400)
            if not RUNNER.busy() or RUNNER.kind != "download":
                return self._json({"error": "当前没有正在下载的任务"}, 409)
            with PLAY_LOCK:
                st = dict(PLAY_STATE)
            if not st.get("ok"):
                return self._json({"error": st.get("reason") or "此源不支持边下边播"}, 400)
            real = real_download_path(st.get("raw_path"))
            if not real:
                return self._json({"error": "文件还没写出来"}, 400)
            link, err = make_preview_link(real, st.get("outdir"))
            if err:
                return self._json({"error": err}, 400)
            try:
                os.startfile(link)
            except Exception as e:
                return self._json({"error": "打开播放器失败：%s（建议安装 VLC 或 PotPlayer）" % e}, 500)
            return self._json({"ok": True})
```

- [ ] **Step 9: 启动时清理过期封面缓存**

在 `purge_stale_cookie_files`（939-954 行）之后追加：

```python
def purge_stale_thumbs():
    """异常退出会残留封面缓存，启动时清掉过期的（与 cookie 临时文件同一套 6 小时口径）。"""
    cutoff = time.time() - 6 * 3600
    try:
        for name in os.listdir(core.THUMB_CACHE_DIR):
            path = os.path.join(core.THUMB_CACHE_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass
```

把 `main()` 的第一行 `purge_stale_cookie_files()`（958）改成：

```python
    purge_stale_cookie_files()
    purge_stale_thumbs()
```

- [ ] **Step 10: 跑测试 + 全量回归 + 语法检查**

Run: `E:/python/python.exe -m unittest discover -s tests -k TestThumbTokens -v`
Expected: PASS（4 例）

Run: `E:/python/python.exe -m unittest discover -s tests -k TestPlayState -v`
Expected: PASS（3 例）

Run: `E:/python/python.exe -m unittest discover -s tests`
Expected: OK，150 例

Run: `E:/python/python.exe -m py_compile YtDlpWeb/server.py ytdlp_core.py`
Expected: 无输出，退出码 0

- [ ] **Step 11: 提交**

```bash
git add YtDlpWeb/server.py tests/test_core.py
git commit -m "网页版：封面 token 端点与边下边播端点

客户端只拿 token、不传路径，避免本地服务变成 SSRF 跳板或任意文件打开器；
可播性由后台 ffprobe 判定，分离源与 aria2c 直接置灰。"
```

---

### Task 6: 网页版前端 — 预览卡、开关与播放按钮

**Files:**
- Modify: `YtDlpWeb/index.html`（结果区 210-217；进度区 221-228；容错分组 302-306；`els` 表 418-423；`FIELDS` 424-426；`setBusy` 447-461；`rowHtml` 556-567；`renderResults` 568-589；`doDownload` 的事件分支 662 起）

**Interfaces:**
- Consumes: Task 5 的 `GET /api/thumb?id=`、`POST /api/play`、SSE `playable` 事件、`row.data.thumb_id`。
- Produces:  DOM id `preview`（预览卡）、`pvImg`、`pvMeta`、`pvOpen`、`pvClose`、`btnPlay`、`play_while`；JS 函数 `showPreview(i)`、`hidePreview()`、`setPlayable(ok, reason)`。

- [ ] **Step 1: 加 HTML**

在 `index.html:210` 的 `<div class="results" id="results">…</div>` 之后插入：

```html
    <div id="preview" style="display:none;margin-top:10px;padding:10px;border:1px solid var(--bd,#d0d7de);border-radius:8px">
      <div style="display:flex;gap:12px;align-items:flex-start">
        <img id="pvImg" alt="封面" style="width:320px;max-width:45%;border-radius:6px;background:#f6f8fa">
        <div style="flex:1;min-width:0">
          <div id="pvMeta" class="muted" style="line-height:1.7"></div>
          <div style="margin-top:8px;display:flex;gap:8px">
            <button class="btn sm" id="pvOpen">打开原网页</button>
            <button class="btn sm" id="pvClose">收起</button>
          </div>
        </div>
      </div>
    </div>
```

在进度区卡片头（`index.html:223` 的「打开文件夹」按钮）之前插入：

```html
      <button class="btn sm" id="btnPlay" disabled title="还没有开始下载">边下边播</button>
```

在容错分组的 `chk-list`（`index.html:302-305`）里，`net_hard` 那行之后插入：

```html
            <label class="chk"><input type="checkbox" id="play_while"> 边下边播（不影响下载速度）</label>
```

并把 306 行的 `.muted` 提示改为：

```html
          <div class="muted" style="margin-top:6px">连接被掐断（10054）时会自动换一套请求方式重来（浏览器指纹 / 分块传输）；关掉后只按当前设置试一次。
            边下边播只对能流式播放的源有效（HLS / mkv / webm）；音视频分离的源与 aria2c 会置灰并说明原因。</div>
```

- [ ] **Step 2: 加 els 与 FIELDS**

把 `index.html:422` 的 `fmtClose:$('fmtClose'), fmtUseBest:$('fmtUseBest')` 改成：

```javascript
  fmtClose:$('fmtClose'), fmtUseBest:$('fmtUseBest'),
  preview:$('preview'), pvImg:$('pvImg'), pvMeta:$('pvMeta'), pvOpen:$('pvOpen'), pvClose:$('pvClose'),
  btnPlay:$('btnPlay')
```

把 `FIELDS`（424-426）里的 `'open_after','net_hard'` 改成 `'open_after','net_hard','play_while'`。

- [ ] **Step 3: 行内加「预览」按钮并绑定**

把 `rowHtml`（556-567）的 `.metas` 块之后、`</div>` 之前加一个按钮，整段替换为：

```javascript
function rowHtml(i){
  const r = results[i];
  return `
      <div class="res-row ${checked.has(i)?'on':''}" data-i="${i}">
        <input type="checkbox" ${checked.has(i)?'checked':''} data-i="${i}">
        <div class="t" title="${esc(r.title)}">${esc(r.title)}</div>
        <div class="metas">
          <span>${esc(r.dur)}</span><span>${esc(r.res)}</span><span>${esc(r.size)}</span>
          <span class="m-up" title="${esc(r.up)}">${esc(r.up||'-')}</span>
        </div>
        <button class="btn sm pv-btn" data-i="${i}" title="看封面与详情">预览</button>
      </div>`;
}
```

在 `renderResults` 里，`els.results.querySelectorAll('input[type=checkbox]').forEach(...)` 块（584-586）之后插入：

```javascript
    els.results.querySelectorAll('.pv-btn').forEach(btn=>{
      btn.addEventListener('click', e=>{
        e.stopPropagation();   // 行点击是勾选，预览按钮不能顺带切换勾选
        showPreview(+btn.dataset.i);
      });
    });
```

- [ ] **Step 4: 实现预览卡**

在 `esc` 函数（605 行）之后插入：

```javascript
/* ---------- 预浏览 ---------- */
let pvUrl = '';
function hidePreview(){ els.preview.style.display='none'; pvUrl=''; }
async function showPreview(i){
  const r = results[i]; if(!r) return;
  pvUrl = r.url || '';
  els.preview.style.display='';
  els.pvMeta.innerHTML = `<b>${esc(r.title)}</b><br>时长 ${esc(r.dur)} · 清晰度 ${esc(r.res)} · 大小 ${esc(r.size)}<br>上传者 ${esc(r.up||'-')}`;
  els.pvImg.removeAttribute('src');
  els.pvImg.alt = '封面加载中…';
  if(!r.thumb_id){ els.pvImg.alt = '这个条目没有封面地址'; return; }
  try{
    const res = await fetch('/api/thumb?id=' + encodeURIComponent(r.thumb_id));
    if(!res.ok){
      const j = await res.json().catch(()=>({}));
      els.pvImg.alt = '封面取不到：' + (j.error || res.status);
      return;
    }
    els.pvImg.src = URL.createObjectURL(await res.blob());
    els.pvImg.alt = '封面';
  }catch(e){ els.pvImg.alt = '封面取不到：' + String(e.message||e); }
}
```

在事件绑定区（`btnOpen` / `btnStop` 的绑定附近）追加：

```javascript
els.pvClose.addEventListener('click', hidePreview);
els.pvOpen.addEventListener('click', ()=>{ if(pvUrl) window.open(pvUrl, '_blank', 'noopener'); });
els.btnPlay.addEventListener('click', async ()=>{
  try{
    const res = await fetch('/api/play', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const j = await res.json().catch(()=>({}));
    if(!res.ok){ toast(j.error || '无法播放'); return; }
    toast('已交给系统播放器');
  }catch(e){ toast(String(e.message||e)); }
});
```

- [ ] **Step 5: 可播性状态与 SSE 分支**

在 `setPlayable` 处新增函数（放在 `setBusy` 之后）：

```javascript
function setPlayable(ok, reason){
  els.btnPlay.disabled = !ok;
  els.btnPlay.title = ok ? '用系统播放器打开正在下载的文件' : (reason || '此源不支持边下边播');
}
```

在 `setBusy`（447-461）里 `$('btnStop').disabled = !v;` 之后插入：

```javascript
  if(!v) setPlayable(false, '还没有开始下载');
```

在 `doDownload` 的事件分支里（`if(ev.type==='progress'){…}` 之后）插入：

```javascript
      else if(ev.type==='playable'){ setPlayable(ev.ok, ev.reason); }
```

在 `doDetect` 开头 `results = []; checked = new Set(); renderResults();`（633 行）之后插入 `hidePreview();`。

- [ ] **Step 6: 语法检查**

把 `index.html` 里 `<script>` 与 `</script>` 之间的内容抽到临时文件再检查：

```bash
E:/python/python.exe -c "import re,io;s=open('YtDlpWeb/index.html',encoding='utf-8').read();m=re.findall(r'<script>(.*?)</script>',s,re.S);open('/tmp/inline.js','w',encoding='utf-8').write('\n'.join(m))"
node --check /tmp/inline.js
```

Expected: 无输出，退出码 0

- [ ] **Step 7: 起服务手工验证**

```bash
E:/python/python.exe YtDlpWeb/server.py --port 8799 --no-browser
```

浏览器开 `http://127.0.0.1:8799`，验证：
1. 探测一个单视频链接 → 行尾出现「预览」按钮 → 点它 → 封面与元数据出现，**勾选状态不被改变**。
2. 点「打开原网页」→ 新标签打开原地址。
3. 设置里出现「边下边播」勾选框，勾选后刷新页面仍保持（配置已存盘）。
4. 「边下边播」按钮默认置灰，鼠标悬停显示原因。
5. 直接访问 `/api/thumb?url=https://example.com/a.jpg` → 400；`/api/thumb?id=不存在` → 404。

验证完关掉服务窗口。

- [ ] **Step 8: 提交**

```bash
git add YtDlpWeb/index.html
git commit -m "网页版：预览卡与边下边播按钮

预览走行内独立按钮，行点击仍是勾选，批量下载不受影响；
播放按钮由服务端下发的可播性事件启用，置灰时悬停显示原因。"
```

---

### Task 7: 桌面版 — 预览面板、开关与播放按钮

**Files:**
- Modify: `YtDlpGUI/main.py`（容错分组 291-299；`_build_results` 405-435；`_build_progress` 391-403；`_collect_cfg` 470-496；`_apply_config` 505-527；`_pump` 777-797；`_handle_line` 799-828；`_add_result` 1151-1160）
- Test: 桌面版 `--selftest` + 手工验证（Tkinter 无头单测不现实，逻辑都在核心已测）

**Interfaces:**
- Consumes: Task 1-4 的核心函数（`fetch_thumb`、`probe_playable`、`make_preview_link`、`clear_preview_links`、`real_download_path`、`fmt_is_separated`）。
- Produces: `self.var_playwhile`、`self.btn_play`、`self.lbl_play`、预览面板控件 `self.pv_img` / `self.pv_meta` / `self.btn_pv_open`；队列消息 `("preview", idx, path, err)` 与 `("playable", ok, reason)`；方法 `_show_preview(i)`、`_on_preview(payload)`、`_set_playable(ok, reason)`、`_play_now()`、`_start_play_probe()`。

- [ ] **Step 1: 加开关**

在 `YtDlpGUI/main.py:299`（那条 `.muted` 提示的 grid 结束）之后插入：

```python
        self.var_playwhile = tk.BooleanVar()
        ttk.Checkbutton(lf, text="边下边播（不影响下载速度）", variable=self.var_playwhile).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 2))
        ttk.Label(lf, text="只对能流式播放的源有效（HLS / mkv / webm）；音视频分离的源与 aria2c 会置灰并说明原因",
                  foreground="#8c959f", font=FONT_S, wraplength=470, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))
```

- [ ] **Step 2: 配置读写**

在 `_collect_cfg` 的 `"net_hard": self.var_nethard.get(),`（488 行）之后插入：

```python
            "play_while": self.var_playwhile.get(),
```

在 `_apply_config` 的 `self.var_nethard.set(self.cfg.get("net_hard", True))`（526 行）之后插入：

```python
        self.var_playwhile.set(self.cfg.get("play_while", False))
```

- [ ] **Step 3: 加预览面板**

在 `_build_results` 的 `self.lbl_count.pack(side="right")`（435 行）之后插入：

```python
        pf = ttk.Frame(lf)
        pf.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self.pv_img = ttk.Label(pf, text="选中一行看封面", font=FONT_S, foreground="#8c959f")
        self.pv_img.pack(side="left", padx=(0, 10))
        right = ttk.Frame(pf)
        right.pack(side="left", fill="both", expand=True)
        self.pv_meta = ttk.Label(right, text="", font=FONT_S, justify="left", wraplength=420)
        self.pv_meta.pack(anchor="w")
        self.btn_pv_open = ttk.Button(right, text="打开原网页", state="disabled",
                                      command=self._open_preview_url)
        self.btn_pv_open.pack(anchor="w", pady=(4, 0))
        self._preview_url = ""
        self._photo = None
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
```

- [ ] **Step 4: 预览取图与显示（后台线程 + 队列）**

在 `_add_result`（1151 行）之前插入：

```python
    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            i = int(sel[0])
        except ValueError:
            return
        if 0 <= i < len(self.results):
            self._show_preview(i)

    def _show_preview(self, i):
        info = self.results[i]
        self._preview_url = info.get("url") or ""
        self.btn_pv_open.config(state="normal" if self._preview_url else "disabled")
        self.pv_meta.config(text="%s\n时长 %s · 清晰度 %s · 大小 %s\n上传者 %s" % (
            info.get("title") or "", fmt_dur(info.get("dur")), info.get("res") or "-",
            fmt_size(info.get("size")), info.get("up") or "-"))
        url = info.get("thumb") or ""
        if not url:
            self._set_preview_image(None, "这个条目没有封面地址")
            return
        self._set_preview_image(None, "封面加载中…")
        cfg = self._collect_cfg()

        def work():
            path, err = core.fetch_thumb(url, cfg)
            self.q.put(("preview", path, err))

        threading.Thread(target=work, daemon=True).start()

    def _set_preview_image(self, photo, text):
        self._photo = photo          # 持有引用，否则 Tkinter 会把图回收掉
        if photo is not None:
            self.pv_img.config(image=photo, text="")
        else:
            self.pv_img.config(image="", text=text or "")

    def _on_preview(self, path, err):
        if err or not path:
            self._set_preview_image(None, "封面取不到：%s" % (err or "未知原因"))
            return
        png = os.path.join(tempfile.gettempdir(), "ytdlp_preview_%d.png" % os.getpid())
        cmd = [find_tool("ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y",
               "-i", path, "-vf", "scale=320:-1", "-frames:v", "1", png]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=core.CREATE_NO_WINDOW, timeout=30)
            photo = tk.PhotoImage(file=png)
        except Exception as e:
            self._set_preview_image(None, "封面格式无法显示：%s" % e)
            return
        self._set_preview_image(photo, "")

    def _open_preview_url(self):
        if self._preview_url:
            webbrowser.open(self._preview_url)
```

在 `_pump` 的 `elif kind == "trodone":` 分支（793-794 行）之后插入：

```python
                elif kind == "preview":
                    self._on_preview(rest[0], rest[1] if len(rest) > 1 else None)
                elif kind == "playable":
                    self._set_playable(rest[0], rest[1] if len(rest) > 1 else "")
```

`YtDlpGUI/main.py` 现有 import 是 `json / os / queue / re / shutil / sys / threading / time / tkinter`，**缺 `subprocess`、`tempfile`、`webbrowser`**。把 9-18 行改成：

```python
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox
```

- [ ] **Step 5: 加播放按钮与探测**

在 `_build_progress` 的 `self.lbl_detail.grid(...)`（403 行）之后插入：

```python
        pf = ttk.Frame(lf)
        pf.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
        self.btn_play = ttk.Button(pf, text="边下边播", state="disabled", command=self._play_now)
        self.btn_play.pack(side="left")
        self.lbl_play = ttk.Label(pf, text="还没有开始下载", font=FONT_S, foreground="#8c959f")
        self.lbl_play.pack(side="left", padx=8)
        self._play_raw = ""
        self._play_ok = False
```

在 `_handle_line` 的 `if ev["kind"] == "file":` 分支里，`self.log_line(...)`（815 行）之后插入：

```python
                self._maybe_start_play_probe()
```

在 `_open_preview_url` 之后插入：

```python
    def _maybe_start_play_probe(self):
        cfg = self._collect_cfg()
        raw = self.pstate.get("raw_path") or ""
        if not raw or not cfg.get("play_while") or raw == self._play_raw:
            return
        self._play_raw = raw
        self._set_playable(False, "正在判断能否边下边播…")
        outdir = cfg.get("outdir") or os.path.join(BASE_DIR, "downloads")

        def work():
            if core.fmt_is_separated(cfg):
                self.q.put(("playable", False, "此源音视频分离，边下边播不适用"))
                return
            if cfg.get("aria"):
                self.q.put(("playable", False, "aria2c 外部下载器不支持边下边播"))
                return
            reason = "文件还没写出来"
            deadline = time.time() + 3
            for _ in range(3):
                while time.time() < deadline:
                    p = core.real_download_path(raw)
                    try:
                        if p and os.path.getsize(p) >= 256 * 1024:
                            break
                    except OSError:
                        pass
                    time.sleep(0.5)
                p = core.real_download_path(raw)
                if p:
                    v = core.probe_playable(p)
                    if v["ok"]:
                        self.q.put(("playable", True, ""))
                        return
                    reason = v["reason"]
                deadline = time.time() + 5
            self.q.put(("playable", False, "此源要下完才能播：%s" % reason))

        threading.Thread(target=work, daemon=True).start()

    def _set_playable(self, ok, reason):
        self._play_ok = bool(ok)
        self.btn_play.config(state="normal" if ok else "disabled")
        self.lbl_play.config(text=reason or ("可以边下边播" if ok else "此源不支持边下边播"))

    def _play_now(self):
        if not self._play_ok:
            return
        cfg = self._collect_cfg()
        outdir = cfg.get("outdir") or os.path.join(BASE_DIR, "downloads")
        real = core.real_download_path(self._play_raw)
        if not real:
            messagebox.showwarning("提示", "文件还没写出来，稍等再试。")
            return
        link, err = core.make_preview_link(real, outdir)
        if err:
            messagebox.showwarning("无法边下边播", err)
            return
        try:
            os.startfile(link)
        except Exception as e:
            messagebox.showerror("打开播放器失败", "%s\n建议安装 VLC 或 PotPlayer。" % e)
```

- [ ] **Step 6: 任务结束清理**

在 `_on_done` 与 `_on_detect_done` 里各加一行清理。`_on_done` 开头插入：

```python
        core.clear_preview_links(self.cfg.get("outdir") or os.path.join(BASE_DIR, "downloads"))
        self._play_raw = ""
        self._set_playable(False, "还没有开始下载")
```

- [ ] **Step 7: 自检与手工验证**

Run: `E:/python/python.exe -m py_compile YtDlpGUI/main.py`
Expected: 无输出，退出码 0

Run: `E:/python/python.exe YtDlpGUI/main.py --selftest`
Expected: 退出码 0

Run: `E:/python/python.exe YtDlpGUI/main.py`
手工验证：
1. 探测一个单视频链接 → 选中表格行 → 预览面板出现封面（jpg/webp 也能显示，因为转了 PNG）+ 元数据。
2. 点「打开原网页」→ 默认浏览器打开。
3. 容错分组出现「边下边播」勾选框，勾选后保存、重开仍保持。
4. 勾选后下载一个 HLS 源 → 「边下边播」按钮在几秒后变亮 → 点它 → 系统播放器打开且画面随下载增长。
5. 下载 YouTube 高清（分离源）→ 按钮保持置灰，旁边写明原因。
6. 任务结束后 `downloads\.preview\` 目录消失。

验证完关掉窗口。

- [ ] **Step 8: 提交**

```bash
git add YtDlpGUI/main.py
git commit -m "桌面版：预览面板与边下边播按钮

Tkinter 的 PhotoImage 只认 PNG/GIF，所以封面经随包 ffmpeg 转一次 PNG；
取图与可播性探测都在后台线程，界面不卡。"
```

---

### Task 8: 文档、手工实测矩阵与全量回归

**Files:**
- Modify: `README.md`（目录结构 71-74；配置常用键 88-92；自愈与诊断 113-128；自测 131-148；已知限制 159-165）
- Test: 全量回归 + 扩展冒烟 + 手工实测

**Interfaces:**
- Consumes: Task 1-7 的全部产出。
- Produces: 更新后的 README；实测结论（速度对照、播放器锁文件行为）。

- [ ] **Step 1: 更新 README**

在「快速开始」之后新增一节：

```markdown
## 预浏览与边下边播

**预浏览**：探测出条目后，网页版点行尾「预览」、桌面版选中表格行，即可看到封面图 +
标题/时长/清晰度/大小/上传者，并可一键打开原网页。封面按需取（不批量拉图），
缓存在 `%TEMP%\ytdlp_thumbs\`，6 小时后自动清理。取不到封面不影响探测结果。

**边下边播**（设置里的「边下边播（不影响下载速度）」，默认关）：下载途中点「边下边播」，
程序把正在增长的 `.part` 文件硬链接成一个扩展名正确的名字，交给系统默认播放器。
零字节拷贝、不转码、不额外拉流。以下情况按钮会置灰并说明原因：

- 音视频分离的源（YouTube 高清等，`bv*+ba/b`）——两个 `.part` 都不是完整片子；
- 开了 aria2c 外部下载器——文件布局不可控；
- ffprobe 读不到流——例如渐进式 mp4 的索引在文件末尾，要下完才能播。

HLS 源会加 `--hls-use-mpegts`（下载中是 `.ts`，能播），下完按「合并容器」设置 remux 回去，
最终产物与不开这个开关时一致。**推荐 VLC 或 PotPlayer**；`.preview\` 子目录里的链接在任务结束时删除。
```

在「常用键」那行末尾补 `play_while`，并把测试数量从 102 改成实际数字（跑完全量后填）。

在「已知限制」里补三条：

```markdown
- 边下边播不支持音视频分离的源与 aria2c；渐进式 mp4 要下完才能播。
- 下载目录在 exFAT/FAT32 U 盘上时建不了硬链接，边下边播会置灰并说明原因。
- 个别播放器以独占方式打开文件时可能挡住续写，遇到就换 VLC / PotPlayer。
```

- [ ] **Step 2: 全量回归**

Run: `E:/python/python.exe -m unittest discover -s tests`
Expected: OK，150 例（若实际数字不同，以实际为准并同步 README）

Run: `E:/python/python.exe -m py_compile ytdlp_core.py YtDlpWeb/server.py YtDlpGUI/main.py`
Expected: 退出码 0

Run: `node --check sniffer_extension/background.js && node --check sniffer_extension/content.js && node --check sniffer_extension/injected.js`
Expected: 退出码 0

Run: `node tests/extension_smoke.js`
Expected: 5/5 通过

- [ ] **Step 3: 手工实测矩阵**

起服务 `E:/python/python.exe YtDlpWeb/server.py --port 8799 --no-browser`，按下表逐项验证并记录结果：

| 源类型 | 开关 | 预期 |
| --- | --- | --- |
| HLS / m3u8 | 开 | 几秒后按钮变亮，播放器画面随下载增长；下完产物是「合并容器」指定的格式 |
| 单文件渐进 mp4 | 开 | 按钮置灰，原因含「要下完才能播」 |
| YouTube 高清（分离源） | 开 | 按钮置灰，原因含「音视频分离」 |
| 任意源 | 关 | 命令与改动前一致，按钮始终置灰 |

播放器各试一次：VLC、PotPlayer、Windows「电影和电视」。**重点确认播放器打开期间 yt-dlp 仍在写**（进度百分比继续涨）。若某播放器导致写入失败，在 README 的已知限制里点名。

- [ ] **Step 4: 速度对照**

同一个 HLS 源，开关关/开各跑一次，记录两次的平均下载速度（进度行里的 `速度`）。
Expected: 两者在同一量级，无明显下降。把数字写进提交信息。

- [ ] **Step 5: 端点守卫复查**

```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/thumb?url=https://example.com/a.jpg"
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8799/api/thumb?id=nope"
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" -d "{\"path\":\"C:/Windows/win.ini\"}" http://127.0.0.1:8799/api/play
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" -d "{}" http://127.0.0.1:8799/api/play
```

Expected: 依次 400、404、400、409

- [ ] **Step 6: 清理与提交**

关掉服务窗口，确认 `downloads\.preview\` 已清空、`__pycache__` 已删。

```bash
git add README.md
git commit -m "文档：预浏览与边下边播的用法、置灰规则与已知限制

附实测结论：速度对照数字、各播放器的锁文件行为。"
```

---

## 自审记录

**Spec 覆盖**：spec 第 4.1 节七个核心函数 → Task 1（`is_public_http_url` / `thumb_cache_path` / `fetch_thumb`）、Task 2（`info_from_json` / `progress_event`）、Task 3（`probe_playable` / `make_preview_link` / `clear_preview_links` / `fmt_is_separated`）；4.2 配置键 → Task 4；4.3 两个端点与事件 → Task 5；4.4 两端界面 → Task 6/7；第 5 节预浏览行为 → Task 1/6/7；第 6 节边下边播 → Task 3/4/5/6/7；第 7 节错误处理 → 各任务的失败分支与 Task 8 守卫复查；第 8 节测试 → 各任务 Step 1-2 与 Task 8；第 9 节文档 → Task 8。

**额外补的核心函数**：`thumb_headers`、`fetch_thumb_via_ytdlp`、`real_download_path` 在 spec 里隐含于取图与定位描述中，计划里显式命名并给出签名，避免实现时各自发明。

**类型一致性**：`fetch_thumb` / `fetch_thumb_via_ytdlp` / `make_preview_link` 统一返回 `(str, str|None)`；`probe_playable` 返回 `{"ok": bool, "reason": str}`；`_play_verdict` 与 SSE `playable` 事件同形；两端播放按钮的启用条件都只认 `ok`。