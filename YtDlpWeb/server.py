# -*- coding: utf-8 -*-
"""
yt-dlp 下载器 · Web 界面服务端

纯 Python 标准库实现（无第三方依赖）：
  GET  /                 界面
  GET  /api/config       读取配置      POST /api/config  保存配置
  GET  /api/status       当前任务状态
  POST /api/detect       探测资源（SSE 流式返回条目）
  POST /api/download     开始下载（SSE 流式返回进度）
  POST /api/formats      列出可用格式（SSE）
  POST /api/stop         停止当前任务
  POST /api/open         打开输出目录
  POST /api/sniff        接收浏览器扩展嗅探到的地址，立即交给 yt-dlp 下载
  GET  /api/sniff/log    嗅探下载任务的进度与日志（扩展轮询用）

默认假设（业务信息未指定时的合理兜底，界面设置区同样标注）：
  1. 单任务队列：同一时间只运行一个探测/下载任务，避免日志与进度混淆。
  2. 仅本机使用：只监听 127.0.0.1，并校验 Host / Origin / Content-Type，
     阻断网页跨站提交与 DNS rebinding。
  3. 输出目录默认为上级目录的 downloads，首次运行时自动创建。
  4. 默认「最佳画质 + 自动合并为 mp4」，并嵌入封面与元数据。
"""

import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# 与桌面版共用的核心（模式表、命令构建、进度解析、自愈阶梯、配置读写）都在这里。
# 放在项目根目录，网页版只是它的一个前端。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytdlp_core as core  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = core.BASE_DIR
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
INDEX_FILE = os.path.join(APP_DIR, "index.html")

PORT_RANGE = core.PORT_RANGE
APP_ID = "yt-dlp-web"          # 扩展靠它确认「端口上跑的是本项目」，而不是别的服务
APP_VERSION = "1.7.1"

# 唯一被放行的扩展 ID——由 sniffer_extension\manifest.json 里的 "key" 决定（打包后固定不变）。
# 以前这里是「任何 chrome-extension:// 都放行」，等于让本机的其它扩展也能改配置、
# 发起下载、打开下载目录里的东西。
ALLOWED_EXTENSION_IDS = ("omdbcoobeaimllaomejhegpfppadapee",)
_EXTENSION_SCHEMES = ("chrome-extension://", "moz-extension://")

# ---------------- 本机访问约束 ----------------
LISTEN_HOST = "127.0.0.1"          # 只对本机开放，别改成 0.0.0.0
LOCAL_HOSTNAMES = ("127.0.0.1", "localhost", "::1")
MAX_BODY_BYTES = 1 << 20           # 单次请求体上限 1MB，防内存被撑爆

# /api/open 会调用 os.startfile，等于「按系统的默认方式打开」——可执行文件会被直接运行。
OPEN_DENY_EXT = (".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".psm1",
                 ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".jar", ".lnk",
                 ".hta", ".cpl", ".scf", ".msc", ".reg", ".url", ".pif", ".cer")


_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2})")


def validate_config_value(key, value):
    """校验 /api/config 里的结构化字段，返回错误文案或 None。

    为什么「附加参数」要黑名单、而这里可以放心收：extra/aria_args 是自由文本，
    可以拼出任意 yt-dlp 参数；outdir/tpl/cookie 是结构化字段，含义固定，
    只要把「盘符/绝对路径/..」这几个越界口子堵住，就只是「指定下载到哪」。
    """
    if key in ("outdir", "cookie") and isinstance(value, str):
        v = value.strip().strip('"')
        if v.startswith(("\\\\", "//")):
            return "不支持网络路径（UNC）"
        if key == "outdir" and v and _ABS_PATH_RE.match(v) and not os.path.isabs(v):
            return "输出目录写法不认识，请用完整的本地路径"
        return None
    if key == "tpl" and isinstance(value, str):
        v = value.strip()
        if not v:
            return "文件名模板不能为空"
        if v.startswith(("/", "\\\\")) or _ABS_PATH_RE.match(v):
            return "文件名模板不能带盘符或绝对路径"
        # 模板只决定「下载目录内的名字」，出现 .. 就等于允许写到目录外
        if ".." in v.replace("\\", "/").split("/"):
            return "文件名模板里不能出现 .."
        return None
    return None


def extension_origin_ok(origin):
    """Origin 是否属于本项目那份被固定的嗅探扩展。"""
    for scheme in _EXTENSION_SCHEMES:
        if origin.startswith(scheme):
            return origin[len(scheme):].split("/")[0] in ALLOWED_EXTENSION_IDS
    return False

# 与桌面版共用的实现统一放在 ytdlp_core：这里只导入本文件真正用到的名字。
from ytdlp_core import (  # noqa: E402
    NET_PLAN_LABEL, PROBE_VARIANTS, build_probe_cmd, classify_line, clean_media_url,
    clear_preview_links, command_text, diagnose, find_tool, fmt_is_separated,
    info_from_json, is_net_failure, iter_lines_watch, json_ok as _json_ok, kill_tree,
    make_preview_link, new_progress_state, next_net_plan, parse_format_line, path_inside,
    probe_playable, progress_event, progress_text, real_download_path, sanitize_urls,
    scrub_header_value, spawn, unsafe_arg_in,
)




# 诊断规则、模式表、格式表达式都在 ytdlp_core；网页版只保留自己的默认配置。
DEFAULT_CONFIG = core.default_config()


def set_config_file(path):
    """让宿主（比如桌面版）指定用哪一份 config.json。

    默认是本目录的；桌面版内嵌服务时指向桌面版那份 config.json，
    这样扩展触发的下载和界面上看到的设置才是同一份。
    """
    global CONFIG_FILE
    p = os.path.abspath(str(path))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    CONFIG_FILE = p
    return CONFIG_FILE


def load_config(warn=None):
    return core.load_config(CONFIG_FILE, warn=warn)


def save_config(cfg, warn=None):
    return core.save_config(CONFIG_FILE, cfg, warn=warn)


def build_download_cmd(cfg, urls, no_playlist=False, warn=None):
    return core.build_download_cmd(cfg, urls, no_playlist=no_playlist, warn=warn)


def build_detect_cmd(cfg, urls):
    return core.build_detect_cmd(cfg, urls)


def bad_urls_msg(bad, fallback):
    if not bad:
        return fallback
    return "无法识别这些链接：%s" % "、".join(bad[:3])


def warn_bad_urls(emit, bad):
    if not bad:
        return
    emit({"type": "log", "level": "warn",
          "text": "已忽略 %d 个无法识别的链接：%s" % (len(bad), "、".join(bad[:3]))})


# ---------------- 预浏览：封面 token 表 ----------------
# 客户端只拿 token，不能传 URL 让服务端去 fetch——否则这就是个能探内网的 SSRF 洞。
THUMB_LOCK = threading.Lock()
THUMB_TOKENS = {}
THUMB_TOKEN_MAX = 512
THUMB_TOKEN_TTL = 6 * 3600          # token 只活一轮浏览：过期就重新探测，别长期有效

_THUMB_CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
                ".img": "application/octet-stream"}


def register_thumb(url):
    if not url:
        return ""
    token = secrets.token_urlsafe(12)
    now = time.time()
    with THUMB_LOCK:
        # 顺手清过期项：表里只有 URL 没有秘密，但让过期地址一直可被取图没有意义
        for k, (_, ts) in list(THUMB_TOKENS.items()):
            if now - ts > THUMB_TOKEN_TTL:
                THUMB_TOKENS.pop(k, None)
        THUMB_TOKENS[token] = (str(url), now)
        while len(THUMB_TOKENS) > THUMB_TOKEN_MAX:
            THUMB_TOKENS.pop(next(iter(THUMB_TOKENS)))
    return token


def thumb_url_for(token):
    item = None
    with THUMB_LOCK:
        item = THUMB_TOKENS.get(str(token or ""))
    if not item:
        return None
    url, ts = item
    if time.time() - ts > THUMB_TOKEN_TTL:
        with THUMB_LOCK:
            THUMB_TOKENS.pop(str(token), None)
        return None
    return url


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


class Runner:
    """单任务执行器：同一时间只允许一个任务，保证状态清晰。

    占位（reserve）与启动（launch）分开，是为了堵住「先 busy() 再起线程」的竞态：
    两个请求可以在检查通过之后同时启动，最后两个任务都写同一份进度状态。
    reserve 在同一把锁里把槽位占住，后来的请求直接拿到 409。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.kind = None
        self.task_id = 0
        self.started_at = None

    def busy(self):
        return self.kind is not None

    def reserve(self, kind):
        """原子占位：成功返回 True，已被占用返回 False。"""
        with self.lock:
            if self.kind is not None:
                return False
            self.kind = kind
            self.task_id += 1
            self.started_at = time.time()
            return True

    def launch(self, cmd, cwd, kind, emit, adopt=False):
        with self.lock:
            # adopt=True：本次启动就是来兑现 reserve() 占下的槽位
            if self.kind is not None and not (adopt and self.proc is None and self.kind == kind):
                emit({"type": "error", "text": "已有任务正在运行，请先停止或等待结束。"})
                return False
            try:
                proc = spawn(cmd, cwd)
            except FileNotFoundError:
                self.kind = None
                self.started_at = None
                emit({"type": "log", "level": "err", "text": f"找不到可执行文件：{cmd[0]}"})
                emit({"type": "done", "code": -1, "elapsed": 0})
                return False
            except Exception as e:
                self.kind = None
                self.started_at = None
                emit({"type": "log", "level": "err", "text": f"启动失败：{e}"})
                emit({"type": "done", "code": -1, "elapsed": 0})
                return False
            self.proc = proc
            self.kind = kind
            return True

    def finish(self):
        with self.lock:
            self.proc = None
            self.kind = None
            self.started_at = None

    def stop(self):
        with self.lock:
            proc, kind = self.proc, self.kind
        if not proc or proc.poll() is not None:
            return False
        kill_tree(proc)
        return kind is not None


RUNNER = Runner()


# 读子进程输出：统一走 ytdlp_core.iter_lines_watch（带心跳与停滞超时）
def watch_note(why):
    """把 iter_lines_watch 的结束原因翻译成日志文案。"""
    return {"client": "[客户端已断开，任务终止]",
            "stall": "[长时间没有输出，已终止本次任务]"}.get((why or {}).get("reason"), "")


def run_detect(cfg, urls, emit, abort=None, keepalive=None):
    cmd = build_detect_cmd(cfg, urls)
    emit({"type": "cmd", "text": command_text(cmd)})
    # 探测不写文件，工作目录固定在 BASE_DIR（输出目录可能尚未创建）
    if not RUNNER.launch(cmd, BASE_DIR, "detect", emit, adopt=True):
        return
    proc = RUNNER.proc
    t0 = time.time()
    buf = []
    why = {}
    try:
        for line in iter_lines_watch(proc, abort, keepalive, why=why):
            s = line.strip()
            if s.startswith("{"):
                try:
                    info = info_from_json(json.loads(s))
                    info["thumb_id"] = register_thumb(info.get("thumb"))
                    emit({"type": "row", "data": info})
                    continue
                except Exception:
                    pass
            level = None
            low = s.lower()
            if "error" in low:
                level = "err"
            elif "warning" in low:
                level = "warn"
            buf.append(s)
            emit({"type": "log", "level": level, "text": s})
    finally:
        note = watch_note(why)
        if note:
            emit({"type": "log", "level": "warn", "text": note})
        code = proc.wait() if proc else -1
        RUNNER.finish()
        emit({"type": "done", "code": code, "elapsed": round(time.time() - t0, 1),
              "diag": diagnose("\n".join(buf[-300:]))})


def run_download(cfg, urls, emit, no_playlist=False, abort=None, keepalive=None):
    outdir = cfg.get("outdir") or os.path.join(BASE_DIR, "downloads")
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception as e:
        emit({"type": "log", "level": "err", "text": f"无法创建输出目录：{e}"})
        emit({"type": "done", "code": -1, "elapsed": 0})
        return
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


def _download_attempt(cfg, urls, emit, no_playlist, abort, outdir, keepalive=None):
    """跑一次 yt-dlp，返回 (退出码, 末尾输出, 是否被客户端断开中断, 已完成文件数)。"""
    warns = []
    cmd = build_download_cmd(cfg, urls, no_playlist=no_playlist, warn=warns)
    for w in warns:
        emit({"type": "log", "level": "warn", "text": w})
    emit({"type": "cmd", "text": command_text(cmd)})
    if not RUNNER.launch(cmd, outdir, "download", emit, adopt=True):
        return None, "", False, 0
    proc = RUNNER.proc
    state = new_progress_state()
    buf = []
    why = {}
    try:
        for line in iter_lines_watch(proc, abort, keepalive, why=why):
            ev = progress_event(state, line)
            if ev:
                if ev["kind"] == "file":
                    emit({"type": "log", "level": "ok", "text": line})
                emit({"type": "progress",
                      "pct": state["pct"], "speed": state["speed"], "eta": state["eta"],
                      "size": state["size"], "file": state["file"],
                      "item_i": state["item_i"], "item_total": state["item_total"],
                      "done_count": len(state["done"]),
                      "text": line if ev["kind"] == "file" else progress_text(state)})
                raw = state.get("raw_path") or ""
                if raw and cfg.get("play_while"):
                    with PLAY_LOCK:
                        known = PLAY_STATE["raw_path"]
                    if raw != known:
                        _start_play_probe(raw, cfg, outdir, emit)
                continue
            buf.append(line)
            emit({"type": "log", "level": classify_line(line), "text": line})
    finally:
        stopped = why.get("reason") == "client"
        note = watch_note(why)
        if note:
            emit({"type": "log", "level": "warn", "text": note})
        # 看门狗已经杀过进程树，这里只负责收尸，避免僵尸进程占用句柄
        code = proc.wait() if proc else -1
        RUNNER.finish()
    return code, "\n".join(buf[-300:]), stopped, len(state["done"])


# ---------- 浏览器扩展嗅探提交（借鉴 FileCentipede：扩展嗅探 → 本机服务 → 下载核心） ----------

SNIFF_STATE = {
    "lines": [],        # 最近日志行（环形截断）
    "prog": None,       # 最近一条进度
    "last_text": "",    # 最近一条日志/进度原文
    "done": True, "code": None,
    "url": "", "title": "", "kind": "",
    "token": 0,          # 当前这轮的任务号（与 RUNNER.task_id 对齐）
}
SNIFF_LOCK = threading.Lock()
SNIFF_MAX_LINES = 200


def sniff_emit(obj, token=None):
    """把 /api/sniff 任务的输出收集到内存，供扩展轮询进度。

    token 用来隔离任务：上一轮的线程如果收尾得晚，它的输出不该写进新一轮的
    进度里（以前没有 token，扩展看到的就是两个任务的日志混在一起）。
    """
    with SNIFF_LOCK:
        if token is not None and SNIFF_STATE.get("token") != token:
            return
        t = obj.get("type")
        if t == "progress":
            SNIFF_STATE["prog"] = {k: obj.get(k) for k in
                                   ("pct", "speed", "eta", "size", "file", "item_i", "item_total")}
        elif t == "log":
            SNIFF_STATE["lines"].append(str(obj.get("text") or ""))
            del SNIFF_STATE["lines"][:-SNIFF_MAX_LINES]
        elif t == "done":
            SNIFF_STATE["done"] = True
            SNIFF_STATE["code"] = obj.get("code")
        if obj.get("text"):
            SNIFF_STATE["last_text"] = obj.get("text")


# 嗅探失败提示按核心诊断的 code 走：判据只有一处，界面、排障、扩展提示不会各说各话
SNIFF_HINT = {
    "drm": "该内容受 DRM 版权保护，无法下载。",
    "login": "站点要求登录。请在 Web 界面设置里配置该站 Cookie 后重试。",
    "bot": "站点要求人机验证。请在设置里改用「浏览器 Cookies」，或换个代理稍后再试。",
    "403": "被防盗链拒绝（403）。刷新页面重新播放生成新链接，再从列表点「下载」（会自动带 Cookie/Referer）。",
    "gone": "地址已失效（视频不存在 / 404）。刷新页面重新播放后重试。",
    "geo": "地区限制。请在 Web 界面配置代理后重试。",
    "400": ("服务器判定这个请求不合法（400），已自动尝试去掉 Referer、补浏览器请求头重试。"
            "仍失败多半是链接被页面脚本改写或带时效参数：回页面重新播放，"
            "等新条目出现后立刻点「下载」；若那条地址异常短（缺参数），改用「下载本页」。"),
    "fmt": "所选格式不存在。改回「最佳画质」模式，或点「查看可用格式」挑一个真实存在的 ID。",
}


def sniff_hint(code, last_text, lines, kind):
    """把 yt-dlp 常见失败翻译成下一步该做什么的提示；无法识别返回空串。"""
    if code == 0 or code is None:
        return ""
    # 只取「最后一次尝试」的输出：否则自动重试前的旧错误会被当成最终结论，
    # 出现「下载已成功却提示连接被掐断」这种自相矛盾的提示。
    all_lines = [str(x) for x in (lines or [])]
    cut = 0
    for i, ln in enumerate(all_lines):
        if ln.startswith("[自动重试]"):
            cut = i + 1
    recent = all_lines[cut:] or all_lines
    blob = str(last_text or "") + "\n" + "\n".join(recent[-30:])
    low = blob.lower()
    for hit in diagnose(blob, max_hits=4):
        c = hit["code"]
        if c == "unsupported":
            if kind == "page":
                return ("这个网站不在 yt-dlp 的支持站点列表里，「下载本页」无效。"
                        "请在页面上播放视频，再从嗅探列表里选 M3U8/VIDEO 条目点「下载」。")
            return "该地址不是 yt-dlp 能直接识别的媒体链接。请播放视频后改用嗅探列表里的 M3U8/VIDEO 条目。"
        if c == "net":
            # 同样是网络问题，「连接被掐断」和「单纯超时」的下一步并不一样
            if any(x in low for x in ("10054", "reset", "aborted", "broken pipe", "eof occurred")):
                return ("对方服务器把连接掐断了（10054 连接重置），已自动换浏览器指纹、IPv4 和分块传输重试。"
                        "这类链接通常带时效签名，或只认刚播放时那个会话：请在页面上重新播放，"
                        "等新条目出现后立刻点「下载」；仍不行就在设置里配代理再试。")
            return "连接超时。检查网络/代理后重试。"
        text = SNIFF_HINT.get(c)
        if text:
            return text
    return ""


def sniff_log_json():
    """扩展轮询 /api/sniff/log 的统一响应。"""
    with SNIFF_LOCK:
        d = {
            "busy": RUNNER.busy(), "running_kind": RUNNER.kind,
            "done": SNIFF_STATE["done"], "code": SNIFF_STATE["code"],
            "prog": SNIFF_STATE["prog"], "last_text": SNIFF_STATE["last_text"],
            "url": SNIFF_STATE["url"], "title": SNIFF_STATE["title"], "kind": SNIFF_STATE["kind"],
            "lines": SNIFF_STATE["lines"][-60:],
        }
        d["hint"] = sniff_hint(d["code"], d["last_text"], d["lines"], d["kind"]) if d["done"] else ""
        # 失败时给出一条真正的 ERROR 行，避免把普通进度行当成结论展示
        err = ""
        for ln in reversed(d["lines"]):
            if "ERROR" in ln:
                err = ln.strip()
                break
        d["last_error"] = err
    # 扩展面板据此显示「边下边播」按钮：探测是本机在后台做的，不占下载带宽
    with PLAY_LOCK:
        d["playable"] = {"ok": bool(PLAY_STATE["ok"]), "reason": PLAY_STATE["reason"],
                         "active": bool(PLAY_STATE["raw_path"])}
    return d


# ---------------- 嗅探条目登记与直链预览 ----------------
# 扩展把嗅探到的地址报上来，本机才有资格「替它去看一眼」：预览接口只认登记过的
# 地址，客户端不能凭空给一个 URL 让服务端去连（否则就是 SSRF 入口）。
SNIFF_ITEMS = {}
SNIFF_ITEMS_LOCK = threading.Lock()
SNIFF_MAX_ITEMS = 300

PREVIEW_DIR = os.path.join(tempfile.gettempdir(), "ytdlp_preview")
PREVIEW_TOKENS = {}
PREVIEW_LOCK = threading.Lock()
PREVIEW_GEN_LOCK = threading.Lock()     # 抓流是带宽活，一次只做一个
PREVIEW_TOKEN_TTL = 6 * 3600
PREVIEW_TOKEN_MAX = 256


def register_sniff_items(items, page_url=""):
    """登记扩展上报的嗅探条目（同地址只更新，不重复占位）。"""
    now = time.time()
    kept = 0
    with SNIFF_ITEMS_LOCK:
        for it in items:
            if not isinstance(it, dict):
                continue
            url = clean_media_url(it.get("url"))
            if not core.is_http_url(url):
                continue
            rec = SNIFF_ITEMS.get(url) or {"url": url, "first_ts": now}
            rec.update({
                # 分片/清单的判定只有一份定义（ytdlp_core），扩展也照同一套扩展名走；
                # 网页版据此默认把「有清单时的分片」折叠起来。
                "seg": core.looks_like_segment(url),
                "manifest": core.looks_like_manifest(url),
                "kind": str(it.get("kind") or rec.get("kind") or "")[:16],
                "size": int(it.get("size") or 0),
                "source": str(it.get("source") or "")[:80],
                "page_url": str(it.get("page_url") or page_url or rec.get("page_url") or "")[:500],
                "referer": str(it.get("referer") or rec.get("referer") or "")[:500],
                "ua": str(it.get("ua") or rec.get("ua") or "")[:400],
                "title": str(it.get("title") or rec.get("title") or "")[:200],
                "ts": now,
            })
            SNIFF_ITEMS[url] = rec
            kept += 1
        while len(SNIFF_ITEMS) > SNIFF_MAX_ITEMS:
            SNIFF_ITEMS.pop(next(iter(SNIFF_ITEMS)))
    return kept


def sniff_items_json():
    with SNIFF_ITEMS_LOCK:
        items = list(SNIFF_ITEMS.values())
    items.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"ok": True, "items": items[:SNIFF_MAX_ITEMS], "busy": RUNNER.busy()}


def _preview_path(url, kind):
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    digest = hashlib.sha1((kind + "|" + url).encode("utf-8")).hexdigest()[:20]
    return os.path.join(PREVIEW_DIR, digest + (".png" if kind == "thumb" else ".mp4"))


def _preview_token(path, kind):
    token = secrets.token_urlsafe(12)
    now = time.time()
    with PREVIEW_LOCK:
        for k, (_, _, ts) in list(PREVIEW_TOKENS.items()):
            if now - ts > PREVIEW_TOKEN_TTL:
                PREVIEW_TOKENS.pop(k, None)
        PREVIEW_TOKENS[token] = (path, kind, now)
        while len(PREVIEW_TOKENS) > PREVIEW_TOKEN_MAX:
            PREVIEW_TOKENS.pop(next(iter(PREVIEW_TOKENS)))
    return token


def preview_token_lookup(token, want_kind=None):
    item = None
    with PREVIEW_LOCK:
        item = PREVIEW_TOKENS.get(str(token or ""))
    if not item:
        return None, None
    path, kind, ts = item
    if want_kind and kind != want_kind:
        return None, None
    if time.time() - ts > PREVIEW_TOKEN_TTL:
        with PREVIEW_LOCK:
            PREVIEW_TOKENS.pop(str(token), None)
        return None, None
    return path, kind


def build_sniff_preview(url):
    """给一条直链生成缩略图 / 短片段 / 时长。

    顺序有讲究：抓片段和问时长并发起来，片段落盘后再从**本地**截帧——网络只走
    一遍。纯 mp4 直链没有片段可抓，就直接对原地址截帧（这种情况也快）。
    """
    with SNIFF_ITEMS_LOCK:
        item = dict(SNIFF_ITEMS.get(url) or {})
    if not item:
        return {"ok": False, "error": "这条地址不在本机嗅探列表里，不能预览"}
    cfg = {"referer": item.get("referer") or item.get("page_url") or "",
           "ua": item.get("ua") or ""}
    native = core.browser_can_play_url(url)
    out = {"ok": True, "url": url, "native": native, "duration": None,
           "thumb": "", "clip": "", "kind": item.get("kind") or ""}
    # 抓流是带宽活：一次只做一个，别让用户连点把上行撑满
    with PREVIEW_GEN_LOCK:
        thumb = _preview_path(url, "thumb")
        clip = _preview_path(url, "clip")
        frame_job = clip_job = dur_job = None
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        try:
            dur_job = pool.submit(core.stream_duration, url, cfg)
            if not (os.path.isfile(thumb) and os.path.getsize(thumb) > 0):
                at = 1.0
                st = dur_job.result(timeout=25)
                out["duration"] = st
                if st:
                    at = max(0.5, min(5.0, st * 0.25))
                if not native:
                    if not (os.path.isfile(clip) and os.path.getsize(clip) > 0):
                        clip_job = pool.submit(core.grab_stream_clip, url, cfg, clip)
                        clip_job.result()
                    if os.path.isfile(clip) and os.path.getsize(clip) > 0:
                        # 片段已在本地：截帧不走网络，几乎瞬时
                        frame_job = pool.submit(core.grab_stream_frame, clip, {}, thumb, at)
                    else:
                        frame_job = pool.submit(core.grab_stream_frame, url, cfg, thumb, at)
                else:
                    frame_job = pool.submit(core.grab_stream_frame, url, cfg, thumb, at)
                frame_job.result()
            else:
                out["duration"] = dur_job.result(timeout=25)
                if not native and not (os.path.isfile(clip) and os.path.getsize(clip) > 0):
                    pool.submit(core.grab_stream_clip, url, cfg, clip).result()
        except concurrent.futures.TimeoutError:
            pass
        finally:
            pool.shutdown(wait=False)
        if os.path.isfile(thumb) and os.path.getsize(thumb) > 0:
            out["thumb"] = _preview_token(thumb, "thumb")
        if os.path.isfile(clip) and os.path.getsize(clip) > 0:
            out["clip"] = _preview_token(clip, "clip")
    if not out["thumb"] and not out["clip"]:
        out["ok"] = False
        out["error"] = "抓不到画面：这个源可能需要登录，或是加密流（DRM）"
    return out


def purge_stale_previews(hours=6):
    """清掉过期的预览素材：临时目录不该无限长大。"""
    cutoff = time.time() - hours * 3600
    try:
        names = os.listdir(PREVIEW_DIR)
    except OSError:
        return
    for name in names:
        p = os.path.join(PREVIEW_DIR, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def write_cookie_jar(jar, url):
    """把扩展送来的 cookie 列表写成 Netscape 格式临时文件，返回路径。
    比塞进请求头更规范：yt-dlp 会按域名作用域使用，避免安全告警。
    """
    lines = ["# Netscape HTTP Cookie File"]
    host = ""
    try:
        host = re.match(r"https?://([^/]+)", url).group(1)
    except Exception:
        pass
    for c in jar:
        try:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            domain = str(c.get("domain") or host)
            if not domain or "." not in domain:
                continue
            # 标准库要求：第二列 TRUE 当且仅当域名以 "." 开头
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = str(c.get("path") or "/")
            secure = "TRUE" if c.get("secure") else "FALSE"
            try:
                expires = int(c.get("expirationDate") or 0)
            except (TypeError, ValueError):
                expires = 0
            if expires < 10 ** 8:            # 会话 cookie：给一年
                expires = int(time.time()) + 365 * 86400
            lines.append("\t".join([domain, flag, path, secure,
                                    str(expires), str(c["name"]), str(c.get("value") or "")]))
        except Exception:
            continue
    if len(lines) <= 1:
        return None
    fd, path = tempfile.mkstemp(prefix="ytdlp_sniff_cookies_", suffix=".txt",
                                text=True, dir=tempfile.gettempdir())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def run_sniff_task(cfg, url, kind, token=None):
    """嗅探下载线程：直接媒体地址走通用提取器 + 网页嗅探带的访问凭据。"""
    cookie_file = None
    emit = (lambda obj: sniff_emit(obj, token)) if token is not None else sniff_emit
    if kind == "media":
        # 直链媒体（m3u8/mp4）本身没有封面和元数据，强行 embed 只会让 ffmpeg 报错、
        # 把一次成功的下载判成失败。这里只做「下回来」这一件事。
        cfg.update({"thumb": False, "meta": False, "sub": False, "embedsub": False})
    # 直链只有一路格式，这里写死 b：否则全局的 bv*+ba/b 会被判成「音视频分离」，
    # 可播性判断直接返回「不适用」。play_while 只用于让服务端探测，不会自动弹播放器。
    cfg.update({"fmt": "b", "play_while": True})
    try:
        run_download(cfg, [url], emit, no_playlist=True)
    except Exception as e:
        emit({"type": "log", "level": "err", "text": "内部错误：%s" % e})
        emit({"type": "done", "code": -1, "elapsed": 0, "diag": []})
    finally:
        RUNNER.finish()          # 没走到 launch（如建目录失败）也要放行槽位
        with SNIFF_LOCK:
            if token is None or SNIFF_STATE.get("token") == token:
                SNIFF_STATE["done"] = True
        cookie_file = cfg.get("_sniff_cookie_file")
        if cookie_file and os.path.isfile(cookie_file):
            try:
                os.remove(cookie_file)
            except Exception:
                pass


def run_formats(cfg, url, emit, abort=None, keepalive=None):
    # 以前这里只带 proxy/cookie，需要 UA/Referer/浏览器 Cookies 的站点
    # 会「下载得了却看不到格式表」，现在与下载/探测共用同一套凭据
    cmd = core.build_formats_cmd(cfg, url)
    emit({"type": "cmd", "text": command_text(cmd)})
    if not RUNNER.launch(cmd, BASE_DIR, "formats", emit, adopt=True):
        return
    proc = RUNNER.proc
    why = {}
    try:
        for line in iter_lines_watch(proc, abort, keepalive, why=why):
            row = parse_format_line(line)
            if row:
                emit({"type": "fmt", "data": row})
            else:
                emit({"type": "log", "level": None, "text": line})
    finally:
        note = watch_note(why)
        if note:
            emit({"type": "log", "level": "warn", "text": note})
        code = proc.wait() if proc else -1
        RUNNER.finish()
        emit({"type": "done", "code": code, "elapsed": 0})


def _probe_once(cmd, emit, abort=None, timeout=25):
    """跑一条探测命令，拿到 JSON 即算成功。

    超时交给 iter_lines_watch 兜底，不再另起读线程：这样既能保证子进程一定被回收，
    也避免了「线程卡在管道上、RUNNER 槽位被占住」的老问题。
    """
    if not RUNNER.launch(cmd, BASE_DIR, "troubleshoot", emit, adopt=True):
        return False, "无法启动 yt-dlp", 0.0, []
    proc = RUNNER.proc
    t0 = time.time()
    lines = []
    why = {}
    got_json = False
    timed_out = []

    def expired():
        if abort and abort():
            return True
        if time.time() - t0 >= timeout:
            timed_out.append(True)
            return True
        return False

    try:
        for line in iter_lines_watch(proc, expired, why=why):
            s = line.strip()
            if not s:
                continue
            lines.append(s)
            if s.startswith("{") and _json_ok(s):
                got_json = True
                break
    finally:
        if proc and proc.poll() is None:   # 提前拿到结果也得把进程收干净
            kill_tree(proc)
        if proc:
            proc.wait()
        RUNNER.finish()
    sec = round(time.time() - t0, 1)
    if got_json:
        return True, "成功解析出条目", sec, lines
    if timed_out:
        return False, "超时 %ss（网络不通，或站点响应极慢）" % timeout, sec, lines[-25:]
    if why.get("reason") == "client":
        return False, "已取消（页面已关闭）", sec, lines[-25:]
    return False, (lines[-1][:120] if lines else "无输出"), sec, lines[-25:]


def run_troubleshoot(cfg, url, emit, abort=None):
    """依次尝试多套访问配置，找出第一个能解析成功的方案。"""
    for key, label, fix in PROBE_VARIANTS:
        if abort and abort():
            break
        cmd = build_probe_cmd(cfg, url, key)
        emit({"type": "probe_start", "key": key, "label": label,
              "cmd": " ".join(('"%s"' % a) if " " in a else a for a in cmd)})
        ok, msg, sec, tail = _probe_once(cmd, emit, abort)
        if not ok:
            for l in tail:
                emit({"type": "log", "level": "warn", "text": l})
        emit({"type": "probe", "key": key, "label": label,
              "ok": ok, "msg": msg, "sec": sec, "fix": fix})
        if ok:
            break
    emit({"type": "done", "code": 0, "elapsed": 0})


def run_update(cfg, emit, abort=None, keepalive=None):
    """更新 yt-dlp 本体——站点改版后，多数「嗅探不到」靠更新就能解决。"""
    yt = cfg.get("yt_dlp_path") or find_tool("yt-dlp.exe")
    cmd = [yt, "-U"]
    if (cfg.get("proxy") or "").strip():
        cmd += ["--proxy", str(cfg["proxy"]).strip()]
    emit({"type": "cmd", "text": " ".join(cmd)})
    if not RUNNER.launch(cmd, BASE_DIR, "update", emit, adopt=True):
        return
    proc = RUNNER.proc
    why = {}
    try:
        for line in iter_lines_watch(proc, abort, keepalive, why=why):
            emit({"type": "log", "level": None, "text": line})
    finally:
        note = watch_note(why)
        if note:
            emit({"type": "log", "level": "warn", "text": note})
        code = proc.wait() if proc else -1
        RUNNER.finish()
        emit({"type": "done", "code": code, "elapsed": 0})


class SSE:
    """手写 chunked 编码的 Server-Sent Events 输出。"""

    def __init__(self, handler):
        self.h = handler
        self.closed = False

    def is_closed(self):
        """供任务循环调用：客户端断开时及时终止子进程，避免空跑。"""
        return self.closed

    def heartbeat(self):
        """空闲时发一条 SSE 注释帧。

        写失败就说明对端已经不在了；顺带也防止浏览器/代理把静默连接判死。
        返回 False 表示连接不可用，调用方据此终止子进程。
        """
        if self.closed:
            return False
        try:
            data = b": ping\n\n"
            self.h.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.h.wfile.flush()
            return True
        except Exception:
            self.closed = True
            return False

    def send(self, obj):
        if self.closed:
            return
        try:
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
            self.h.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.h.wfile.flush()
        except Exception:
            self.closed = True

    def close(self):
        if self.closed:
            return
        try:
            self.h.wfile.write(b"0\r\n\r\n")
            self.h.wfile.flush()
        except Exception:
            pass
        self.closed = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "yt-dlp-web/%s" % (APP_VERSION,)

    def log_message(self, fmt, *args):
        # pythonw 启动时 sys.stderr 是 None（没有控制台），写入会抛 AttributeError。
        # 这行日志只用于独立模式看访问记录，绝不能因为它让响应变成「空回复」。
        if "/api/" not in (self.path or ""):
            return
        stream = getattr(sys, "stderr", None)
        if stream is None:
            return
        try:
            stream.write("%s %s\n" % (self.command, self.path))
        except Exception:
            pass

    def handle_one_request(self):
        """浏览器/SSE 客户端随时可能掐断连接：这类错误不值得打一整段 traceback。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, msg, code):
        # 请求体可能还没读，继续复用这条连接会让残留字节被当成下一个请求
        self.close_connection = True
        self._json({"error": msg}, code)

    def _guard(self, need_json=False):
        """只服务本机页面：校验 Host 防 DNS rebinding，校验 Origin/Content-Type 防跨站提交。"""
        try:
            host = urlsplit("//" + (self.headers.get("Host") or "")).hostname or ""
        except Exception:
            host = ""
        if host not in LOCAL_HOSTNAMES:
            self._deny("仅允许本机访问（127.0.0.1 / localhost）", 403)
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            # 浏览器读不到跨站响应体（没发 CORS 头），但带 Origin 的请求一律挡掉更省心，
            # 也免得以后新增会改状态的 GET 接口时忘记补上防护。直接地址栏访问没有 Origin，不受影响。
            port = self.server.server_port
            allowed = {
                "http://127.0.0.1:%d" % port, "http://localhost:%d" % port,
                "http://127.0.0.1", "http://localhost",
            }
            if origin not in allowed and not extension_origin_ok(origin):
                self._deny("已拒绝跨站请求", 403)
                return False
        if not need_json:
            return True

        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct != "application/json":
            # 网页用 text/plain 发起的「简单请求」不带预检，这一条把它们挡在门外
            self._deny("请求格式不正确，需要 application/json", 415)
            return False

        return True

    def _html(self):
        try:
            with open(INDEX_FILE, "rb") as f:
                body = f.read()
        except Exception:
            body = b"<h1>index.html missing</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                         "form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _drain_chunked(self, limit=MAX_BODY_BYTES):
        """消费掉分块请求体。

        不消费的话，HTTP/1.1 长连接上残留的字节会被当成「下一个请求」解析，
        等于把一个请求体变成了后续请求的走私通道。
        """
        total = 0
        try:
            while True:
                line = self.rfile.readline(1024).strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    self.close_connection = True
                    return False
                if size <= 0:
                    self.rfile.readline(1024)     # 末尾空行
                    return True
                total += size
                if total > limit:
                    self._body_too_large = True
                    self.close_connection = True
                    return False
                if len(self.rfile.read(size)) != size:
                    self.close_connection = True
                    return False
                self.rfile.readline(2)            # 块尾 CRLF
        except Exception:
            self.close_connection = True
            return False

    def _body(self):
        self._body_too_large = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self._drain_chunked()
            return {}
        if n > MAX_BODY_BYTES:
            self._body_too_large = True
            self.close_connection = True
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _send_file(self, path, ctype):
        """发送本地文件，支持 Range：<video> 拖动进度条会发 Range 请求。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"error": "预览文件不见了"}, 404)
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and size > 0:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _busy_json(self):
        """忙碌时统一回 409：前端 stream() 会读 error 字段并 toast，不会卡在「探测中」。"""
        return self._json({"ok": False, "busy": True, "running_kind": RUNNER.kind,
                           "error": "已有任务正在运行（%s），请先停止或等待结束"
                                    % (RUNNER.kind or "未知")}, 409)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    # ---- routes ----
    def do_GET(self):
        if not self._guard():
            return
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._html()
        if p == "/api/config":
            cfg = load_config()
            cfg["env"] = {
                "yt_dlp_path": find_tool("yt-dlp.exe"),
                "yt_dlp_version": yt_dlp_version(),
                "ffmpeg": os.path.isfile(os.path.join(BASE_DIR, "ffmpeg.exe")),
                "aria2c": bool(core.which_tool("aria2c.exe")),
                # 下载模式、UA 预设只有一份定义（ytdlp_core）：页面按这里的键渲染，
                # 不再自带一张中文表，改文案不会让两端判据分叉。
                "modes": [{"key": k, "label": lb} for k, lb in core.MODE_LABELS],
                "mode_fmt": core.MODE_FMT,
                "ua_presets": core.UA_PRESETS,
            }
            return self._json(cfg)
        if p == "/api/status":
            return self._json({
                "app": APP_ID, "version": APP_VERSION,
                "busy": RUNNER.busy(), "kind": RUNNER.kind,
                "elapsed": round(time.time() - RUNNER.started_at, 1) if RUNNER.started_at else 0,
            })
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
            # 站点给的封面字节一律别让浏览器猜类型：猜成 HTML 就是一个同源脚本执行点
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/sniff/log":
            return self._json(sniff_log_json())
        if p == "/api/sniff/items":
            return self._json(sniff_items_json())
        if p == "/api/sniff/media":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            path, kind = preview_token_lookup((qs.get("id") or [""])[0])
            if not path:
                return self._json({"error": "预览不存在或已过期"}, 404)
            return self._send_file(path, "video/mp4" if kind == "clip" else "image/png")
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard(need_json=True):
            return
        p = self.path.split("?")[0]
        body = self._body()
        if getattr(self, "_body_too_large", False):
            return self._json({"error": "请求体过大"}, 413)

        if p == "/api/config":
            cfg = load_config()
            for k, v in body.items():
                if k not in DEFAULT_CONFIG:
                    continue
                bad = validate_config_value(k, v)
                if bad:
                    return self._json({"error": bad}, 400)
                # 附加参数能干的事远超「下载」，命中危险项直接拒绝
                if k in ("extra", "aria_args"):
                    bad = unsafe_arg_in(v if isinstance(v, str) else "")
                    if bad:
                        return self._json({"error": "「附加参数」里不允许使用 %s" % bad}, 400)
                if k == "outdir" and isinstance(v, str):
                    v = v.strip().strip('"')
                cfg[k] = v
            cfg["yt_dlp_path"] = find_tool("yt-dlp.exe")
            notes = []
            ok = save_config(cfg, warn=notes.append)
            # 回读一次：headers 等会被规范化，界面拿到的就是真正生效的内容；
            # 整理过程中的提醒一并回给前端，别让「填了没生效」变成静默失败。
            fresh = load_config(warn=notes.append)
            return self._json({"ok": ok, "config": fresh, "warnings": notes})

        if p == "/api/detect":
            cfg = load_config()
            urls, bad = sanitize_urls(body.get("urls") or [])
            if not urls:
                return self._json({"error": bad_urls_msg(bad, "请先填写链接")}, 400)
            if not RUNNER.reserve("detect"):
                return self._busy_json()
            self._sse_headers()
            sse = SSE(self)
            try:
                warn_bad_urls(sse.send, bad)
                run_detect(cfg, urls, sse.send, sse.is_closed, sse.heartbeat)
            finally:
                RUNNER.finish()          # 占位异常退出也要放行，别把任务槽锁死
                sse.close()
            return

        if p == "/api/download":
            cfg = load_config()
            urls, bad = sanitize_urls(body.get("urls") or [])
            if not urls:
                return self._json({"error": bad_urls_msg(bad, "请先选择要下载的内容")}, 400)
            if not RUNNER.reserve("download"):
                return self._busy_json()
            self._sse_headers()
            sse = SSE(self)
            try:
                warn_bad_urls(sse.send, bad)
                run_download(cfg, urls, sse.send, no_playlist=bool(body.get("no_playlist")),
                             abort=sse.is_closed, keepalive=sse.heartbeat)
            finally:
                RUNNER.finish()
                sse.close()
            return

        if p == "/api/formats":
            cfg = load_config()
            url = clean_media_url(body.get("url"))
            if not core.is_http_url(url):
                return self._json({"error": "请填写以 http:// 或 https:// 开头的链接"}, 400)
            if not RUNNER.reserve("formats"):
                return self._busy_json()
            self._sse_headers()
            sse = SSE(self)
            try:
                run_formats(cfg, url, sse.send, sse.is_closed, sse.heartbeat)
            finally:
                RUNNER.finish()
                sse.close()
            return

        if p == "/api/command":
            # 只构建不执行：供界面「复制命令」使用
            cfg = load_config()
            urls, bad = sanitize_urls(body.get("urls") or [])
            warns = []
            cmd = build_download_cmd(cfg, urls, no_playlist=bool(body.get("no_playlist")),
                                     warn=warns)
            return self._json({"cmd": cmd, "text": command_text(cmd), "ignored": bad,
                               "notes": warns})

        if p == "/api/reset":
            cfg = dict(DEFAULT_CONFIG)
            cfg["yt_dlp_path"] = find_tool("yt-dlp.exe")
            save_config(cfg)
            return self._json({"ok": True, "config": cfg})

        if p == "/api/troubleshoot":
            cfg = load_config()
            url = clean_media_url(body.get("url"))
            if not core.is_http_url(url):
                return self._json({"error": "请填写以 http:// 或 https:// 开头的链接"}, 400)
            if not RUNNER.reserve("troubleshoot"):
                return self._busy_json()
            self._sse_headers()
            sse = SSE(self)
            try:
                run_troubleshoot(cfg, url, sse.send, sse.is_closed)
            finally:
                RUNNER.finish()
                sse.close()
            return

        if p == "/api/update":
            cfg = load_config()
            if not RUNNER.reserve("update"):
                return self._busy_json()
            self._sse_headers()
            sse = SSE(self)
            try:
                run_update(cfg, sse.send, sse.is_closed, sse.heartbeat)
            finally:
                RUNNER.finish()
                sse.close()
            return

        if p == "/api/sniff/items":
            items = body.get("items")
            if not isinstance(items, list):
                return self._json({"error": "items 必须是数组"}, 400)
            n = register_sniff_items(items[:200], str(body.get("page_url") or ""))
            return self._json({"ok": True, "count": n})

        if p == "/api/sniff/preview":
            url = clean_media_url(body.get("url"))
            if not core.is_http_url(url):
                return self._json({"error": "请填写以 http:// 或 https:// 开头的链接"}, 400)
            return self._json(build_sniff_preview(url))

        if p == "/api/sniff":
            # 浏览器扩展提交嗅探到的地址，立即排队交给 yt-dlp
            cfg = load_config()
            url = clean_media_url(body.get("url"))
            if not url.lower().startswith(("http://", "https://")):
                return self._json({"error": "仅支持 http/https 链接（blob:/data: 无法交给 yt-dlp）"}, 400)
            kind = body.get("kind") or "media"
            # 直接媒体地址：带上页面嗅探到的访问凭据，绕过防盗链
            if kind == "media":
                referer = clean_media_url(body.get("referer"))
                ua = str(body.get("ua") or "").strip().replace("\r", " ").replace("\n", " ")
                jar = body.get("cookie_jar")
                if referer:
                    cfg["referer"] = referer
                if ua:
                    cfg["ua"] = ua
                if isinstance(jar, list) and jar:
                    # 首选：完整 cookie 字段 → Netscape 临时文件
                    path = write_cookie_jar(jar, url)
                    if path:
                        cfg["cookie"] = path
                        cfg["_sniff_cookie_file"] = path
                if not cfg.get("cookie"):
                    # 兜底：只有 Cookie 头字符串（或临时文件没写成）时原样透传。
                    # 注意别截断：Cookie 少一段，站点照样判你没登录。
                    cookie = scrub_header_value(body.get("cookie"))
                    if cookie:
                        cfg["cookie_header"] = cookie
            if not RUNNER.reserve("download"):
                return self._json({"ok": False, "busy": True, "running_kind": RUNNER.kind,
                                   "error": "已有任务正在运行（%s），请先停止或等待结束" % (RUNNER.kind or "")}, 409)
            token = RUNNER.task_id
            with SNIFF_LOCK:
                SNIFF_STATE.update({"lines": [], "prog": None, "last_text": "已接收，正在启动 yt-dlp…",
                                    "done": False, "code": None, "token": token,
                                    "url": url, "title": str(body.get("title") or "")[:200], "kind": kind})
            threading.Thread(target=run_sniff_task, args=(cfg, url, kind, token),
                             daemon=True).start()
            return self._json({"ok": True, "busy": False, "url": url})

        if p == "/api/sniff/log":
            return self._json(sniff_log_json())

        if p == "/api/stop":
            ok = RUNNER.stop()
            return self._json({"ok": ok})

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

        if p == "/api/open":
            cfg = load_config()
            root = cfg.get("outdir") or os.path.join(BASE_DIR, "downloads")
            path = str(body.get("path") or root).strip().strip('"')
            if not path:
                return self._json({"error": "路径为空"}, 400)
            # UNC 路径会让 Windows 主动向远端 SMB 认证（泄露 NTLM 哈希），一律拒绝
            if path.startswith(("\\\\", "//")):
                return self._json({"error": "不支持网络路径"}, 400)
            path = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
            if not path_inside(path, root):
                return self._json({"error": "只允许打开下载目录内的路径"}, 403)
            # os.startfile 对可执行文件等于「直接运行」：下载目录里的东西不能被这样点开
            if path.lower().endswith(OPEN_DENY_EXT):
                return self._json({"error": "出于安全考虑，不直接打开可执行文件"}, 400)
            try:
                os.makedirs(path, exist_ok=True)
                # 校验用 realpath，打开也必须用 realpath：两次解析之间被换成快捷方式
                # 就是典型的检查/使用时间差（TOCTOU）。
                target = os.path.realpath(path)
                if not path_inside(target, root) or target.lower().endswith(OPEN_DENY_EXT):
                    return self._json({"error": "只允许打开下载目录内的普通文件"}, 403)
                os.startfile(target)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": "打开失败：%s" % e}, 500)

        self._json({"error": "not found"}, 404)


def yt_dlp_version():
    """读取 yt-dlp 版本——版本过旧是「站点嗅探不到」的常见原因，界面会显示出来。"""
    return core.yt_dlp_version(find_tool("yt-dlp.exe"))


def purge_stale_cookie_files():
    """异常退出（强杀/断电）会残留明文 cookie 文件，启动时清掉过期的。"""
    tmp = tempfile.gettempdir()
    cutoff = time.time() - 6 * 3600
    try:
        for name in os.listdir(tmp):
            if not name.startswith("ytdlp_sniff_cookies_"):
                continue
            path = os.path.join(tmp, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


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


class Service:
    """一个跑在后台线程里的 HTTP 服务。

    ``start_service()`` 只负责占端口、拉起 serve_forever；打印横幅、开浏览器
    这些「独立进程」才需要的事留在 ``main()`` 里，宿主（桌面版）调用时不会有
    任何标准输出副作用。
    """

    def __init__(self, httpd, port, thread):
        self.httpd = httpd
        self.port = port
        self.thread = thread
        self._closed = False
        self.url = "http://%s:%d" % (LISTEN_HOST, port)

    def shutdown(self, timeout=5.0):
        """停掉服务并释放端口。可重复调用（宿主退出与 finally 都会调）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        try:
            self.thread.join(timeout)
        except Exception:
            pass


def pick_port(port=None):
    """占一个端口：优先用指定的，否则在 PORT_RANGE 里避让。返回 (port, 被占用的列表)。"""
    ports = [port] if port else list(PORT_RANGE)
    skipped = []
    for p in ports:
        try:
            httpd = ThreadingHTTPServer((LISTEN_HOST, p), Handler)
            return p, httpd, skipped
        except OSError:
            skipped.append(p)
    return None, None, skipped


def start_service(port=None):
    """启动服务并立刻返回 Service；失败抛 OSError（端口全被占）。

    宿主可以完全不管标准输出：这里不 print、不开浏览器。
    """
    purge_stale_cookie_files()
    purge_stale_thumbs()
    purge_stale_previews()
    p, httpd, skipped = pick_port(port)
    if p is None:
        raise OSError("端口 %s 都被占用，无法启动" %
                      (port if port else "8765-8775"))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="ytdlp-web", daemon=True)
    t.start()
    svc = Service(httpd, p, t)
    svc.skipped = skipped
    return svc


def main():
    # 允许 --port 指定端口，否则在 8765-8775 内自动避让
    arg_port = None
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            try:
                arg_port = int(sys.argv[i + 1])
            except ValueError:
                pass
    try:
        svc = start_service(arg_port)
    except OSError as e:
        print(e)
        return 1

    print("=" * 56)
    print("  yt-dlp 下载器 · Web 界面已启动")
    print(f"  访问地址：  {svc.url}")
    print("  访问范围：  仅本机（不监听局域网，手机无法访问）")
    print("  停止服务：  关闭本窗口，或按 Ctrl+C")
    print("=" * 56)
    if svc.skipped:
        print("  注意：端口 %s 已被占用（可能是另一个实例还在运行），已改用 %s。"
              % (", ".join(str(x) for x in svc.skipped), svc.port))
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(svc.url)
        except Exception:
            pass
    try:
        while svc.thread.is_alive():
            svc.thread.join(0.5)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        svc.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
