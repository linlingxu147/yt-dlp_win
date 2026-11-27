# -*- coding: utf-8 -*-
"""
鱿鱼浏览器工具包 · 浏览器扩展服务（无网页界面）

纯 Python 标准库实现（无第三方依赖），日常由桌面版内嵌启动：
  GET  /api/status       当前任务状态（扩展靠它发现服务）
  POST /api/sniff/items  登记扩展嗅探到的媒体地址
  POST /api/sniff/preview 给直链生成缩略图 / 短片段 / 时长
  GET  /api/sniff/media  按 token 取预览字节
  POST /api/sniff        提交嗅探地址，下载交还桌面版流水线
  GET  /api/sniff/log    嗅探下载任务的进度与日志（扩展轮询用）
  POST /api/stop         停止当前任务

默认假设（业务信息未指定时的合理兜底，界面设置区同样标注）：
  1. 下载队列：独立模式自建 ytdlp_queue.QueueManager（并发数读 queue_n，默认 2），
     扩展连提多个任务按 FIFO 排队执行；SNIFF_STATE 只镜像最近一次扩展任务。
  2. 仅本机使用：只监听 127.0.0.1，并校验 Host / Origin / Content-Type，
     阻断网页跨站提交与 DNS rebinding。
  3. 输出目录默认为 runtime/downloads，首次运行时自动创建。
  4. 默认「最佳画质 + 自动合并为 mp4」，并嵌入封面与元数据。
"""

import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# 与桌面版共用的核心（模式表、命令构建、进度解析、自愈阶梯、配置读写）都在这里。
# 放在项目根目录，本服务与桌面版都是它的前端。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytdlp_core as core  # noqa: E402
import ytdlp_queue  # noqa: E402  共享下载队列内核（独立模式自建 QueueManager）

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = core.BASE_DIR
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

PORT_RANGE = core.PORT_RANGE
APP_ID = "yt-dlp-web"          # 扩展靠它确认「端口上跑的是本项目」，而不是别的服务
APP_VERSION = "1.13.0"

# ---------------- AI Agent API ----------------
# 独立于扩展路由：token 鉴权，只给本机 AI/脚本用；扩展的 Origin 守卫保持原样。
AGENT_API_PREFIX = "/api/agent/v1"
AGENT_API_VERSION = 1
AGENT_TOKEN = secrets.token_urlsafe(32)
AGENT_DISCOVERY_FILE = core.AGENT_DISCOVERY_FILE
AGENT_JOBS = {}
AGENT_LOCK = threading.Lock()
AGENT_MAX_JOBS = 100
AGENT_MAX_URLS = 20
AGENT_LOG_MAX = 200
ACTIVE_SERVICE = None
AGENT_PRESETS = {
    "best": {"mode": "best", "merge": "mp4", "fmt": "",
             "description": "最佳画质，自动合并为 mp4"},
    "audio": {"mode": "audio_mp3", "merge": "", "fmt": "",
              "description": "只取最佳音频，转 mp3"},
}

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
def extension_origin_ok(origin):
    """Origin 是否属于本项目那份被固定的嗅探扩展。"""
    for scheme in _EXTENSION_SCHEMES:
        if origin.startswith(scheme):
            return origin[len(scheme):].split("/")[0] in ALLOWED_EXTENSION_IDS
    return False

# 与桌面版共用的实现统一放在 ytdlp_core：这里只导入本文件真正用到的名字。
from ytdlp_core import (  # noqa: E402
    NET_PLAN_LABEL, PROBE_VARIANTS, build_probe_cmd, classify_line, clean_media_url,
    command_text, diagnose, find_tool,
    info_from_json, is_net_failure, json_ok as _json_ok,
    COOKIE_PLAN_LABEL, next_cookie_plan,
    next_net_plan, parse_format_line, path_inside,
    progress_event, progress_text, sanitize_urls,
    scrub_header_value, unsafe_arg_in,
)




# 诊断规则、模式表、格式表达式都在 ytdlp_core；网页版只保留自己的默认配置。

def set_config_file(path):
    """让宿主（比如桌面版）指定用哪一份 config.json。

    默认是本目录的；桌面版内嵌服务时指向桌面版那份 config.json，
    这样扩展触发的下载和界面上看到的设置才是同一份。
    """
    global CONFIG_FILE
    p = os.path.abspath(str(path))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    CONFIG_FILE = p
    try:
        QUEUE.set_max(_queue_n())
    except Exception:
        pass
    return CONFIG_FILE


def load_config(warn=None):
    return core.load_config(CONFIG_FILE, warn=warn)


def build_download_cmd(cfg, urls, no_playlist=False, warn=None):
    return core.build_download_cmd(cfg, urls, no_playlist=no_playlist, warn=warn)


# ---------------- 独立模式下载队列 ----------------
# 桌面版内嵌时下载走 TASK_SINK（host 自己的队列），这里的 QUEUE 只在独立模式用。
# SNIFF_STATE 仍只镜像「最近一次扩展任务」，token 与队列 task_id 对齐。
LAST_EXT_TASK = {"id": None}
QUEUE_STATUS = None      # 桌面模式由 host 注入快照回调（T9）；独立模式为 None


def _queue_emit(ev):
    """队列调度线程的事件回调：绝不能向外抛异常。

    执行器自己镜像进度/日志；这里兜底两件事——
    1. 最近扩展任务到达终态（含排队中被取消、执行器崩溃）时把镜像标记为 done；
    2. 任务结束时清掉扩展送来的临时 cookie 文件（排队中被取消不会进执行器）。
    """
    try:
        if ev.get("type") != "queue" or ev.get("action") != "state":
            return
        t = ev.get("task") or {}
        if t.get("state") not in ("done", "failed", "canceled"):
            return
        task = QUEUE.get(t.get("id"))
        if task is not None and task.ext_cookie_file:
            path, task.ext_cookie_file = task.ext_cookie_file, None
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        if t.get("id") != LAST_EXT_TASK.get("id"):
            return
        with SNIFF_LOCK:
            if SNIFF_STATE.get("token") == t.get("id"):
                SNIFF_STATE["done"] = True
                if SNIFF_STATE.get("code") is None:
                    code = t.get("exit_code")
                    SNIFF_STATE["code"] = code if code is not None else -1
    except Exception:
        pass


def _queue_n():
    try:
        return int(str(load_config().get("queue_n") or "2"))
    except Exception:
        return 2


QUEUE = ytdlp_queue.QueueManager(max_concurrent=_queue_n(), emit=_queue_emit)


# 读子进程输出：统一走 ytdlp_queue.run_process（内部用 iter_lines_watch，带停滞超时）
def watch_note(why):
    """把 iter_lines_watch 的结束原因翻译成日志文案。"""
    return {"client": "[客户端已断开，任务终止]",
            "stall": "[长时间没有输出，已终止本次任务]"}.get((why or {}).get("reason"), "")


def _queue_snapshot():
    """桌面模式优先用 host 注入的快照；回调异常时退回本地队列，接口不能因此挂掉。"""
    if QUEUE_STATUS is not None:
        try:
            s = QUEUE_STATUS()
            if isinstance(s, dict):
                return s
        except Exception:
            pass
    return QUEUE.snapshot()


def _queue_busy():
    if QUEUE_STATUS is not None:
        s = _queue_snapshot()
        return bool(s.get("running") or s.get("queued"))
    return QUEUE.any_active()


def _queue_position(tid):
    """tid 在排队任务中的位置（1 起）；运行中/已结束/快照不可查时返回 0。"""
    try:
        queued = [t.get("id") for t in _queue_snapshot().get("tasks") or []
                  if t.get("state") == "queued"]
        return queued.index(tid) + 1 if tid in queued else 0
    except Exception:
        return 0


def _mirror(task, obj):
    """把任务事件写进 SNIFF_STATE，并转发给任务自己的事件回调（agent 任务用）。"""
    try:
        cb = getattr(task, "ext_emit", None)
        if cb is not None:
            cb(obj)
    except Exception:
        pass
    try:
        if task.id == LAST_EXT_TASK.get("id"):
            sniff_emit(obj, task.id)
    except Exception:
        pass


def _server_line(task, line):
    """逐行回调：进度并入 task.pstate，日志/进度镜像给扩展轮询。"""
    task.log_ring.append(line)
    ev = progress_event(task.pstate, line)
    if ev:
        st = task.pstate
        if ev["kind"] == "file":
            _mirror(task, {"type": "log", "level": "ok", "text": line})
        _mirror(task, {"type": "progress",
                       "pct": st["pct"], "speed": st["speed"], "eta": st["eta"],
                       "size": st["size"], "file": st["file"],
                       "item_i": st["item_i"], "item_total": st["item_total"],
                       "done_count": len(st["done"]),
                       "text": line if ev["kind"] == "file" else progress_text(st)})
        return
    _mirror(task, {"type": "log", "level": classify_line(line), "text": line})


def _server_executor(task):
    """独立模式执行器：复用「直连 → 换浏览器指纹 → 换传输方式」自愈阶梯，按任务跑。

    暂停/取消由队列内核置位并杀进程树，这里只负责识别信号后收尾；
    续传交给 yt-dlp（.part/.aria2 保留），继续时内核会从 normal 重新起跑。
    """
    cfg = task.cfg
    t0 = time.time()

    def done(code, tail=""):
        task.exit_code = code
        _mirror(task, {"type": "done", "code": code,
                       "elapsed": round(time.time() - t0, 1),
                       "done_count": len(task.pstate["done"]),
                       "diag": diagnose(tail) if code not in (0, None) else []})

    try:
        os.makedirs(task.outdir, exist_ok=True)
    except Exception as e:
        _mirror(task, {"type": "log", "level": "err", "text": "无法创建输出目录：%s" % e})
        done(-1)
        return
    tried = set()
    name, extra = "normal", {}
    try:
        while True:
            c = cfg if not extra else dict(cfg, **extra)
            if name != "normal":
                _mirror(task, {"type": "log", "level": "warn",
                               "text": "[自动重试] 换用%s重试（已下载部分会续传）"
                                       % (NET_PLAN_LABEL.get(name) or COOKIE_PLAN_LABEL)})
            warns = []
            cmd = build_download_cmd(c, task.urls, no_playlist=task.no_playlist, warn=warns)
            for w in warns:
                _mirror(task, {"type": "log", "level": "warn", "text": w})
            _mirror(task, {"type": "cmd", "text": command_text(cmd)})
            code, tail, why = ytdlp_queue.run_process(
                task, cmd, task.outdir, lambda ln: _server_line(task, ln))
            note = watch_note(why)
            if note:
                _mirror(task, {"type": "log", "level": "warn", "text": note})
            if code is None:          # 启动失败（找不到可执行文件等）
                _mirror(task, {"type": "log", "level": "err",
                               "text": "启动失败：找不到 yt-dlp 或无法创建进程"})
                done(-1)
                return
            if task.pause_requested or task.cancel_requested:
                done(code, tail)
                return
            if code == 0:
                done(0)
                return
            tried.add(name)
            # 浏览器 Cookies 数据库被独占锁住：先脱掉登录态重试一次，
            # 公开内容不需要它，读不出来不该让整个下载直接失败。
            nxt = next_cookie_plan(tail, tried, cfg)
            if not nxt and cfg.get("net_hard", True) and is_net_failure(tail):
                # 用原始 cfg 推导 Origin：no-referer 那招已经把 referer 清空了
                nxt = next_net_plan(tail, tried, cfg, url=task.urls[0] if task.urls else "")
            if not nxt:
                done(code, tail)
                return
            name, extra = nxt
    except Exception as e:
        _mirror(task, {"type": "log", "level": "err", "text": "内部错误：%s" % e})
        done(-1)
    finally:
        _drop_cookie(cfg)


# ---------- 浏览器扩展嗅探提交（借鉴 FileCentipede：扩展嗅探 → 本机服务 → 下载核心） ----------

SNIFF_STATE = {
    "lines": [],        # 最近日志行（环形截断）
    "prog": None,       # 最近一条进度
    "last_text": "",    # 最近一条日志/进度原文
    "done": True, "code": None,
    "url": "", "title": "", "kind": "",
    "token": 0,          # 当前这轮的任务号（与队列 task_id 对齐）
}
SNIFF_LOCK = threading.Lock()
SNIFF_MAX_LINES = 200

# ---------------- 桌面版宿主：把下载交还桌面版执行 ----------------
# 桌面版内嵌本服务时在这里登记钩子：下载任务改由桌面版自己的流水线执行
# （进度、日志、「停止」按钮都在桌面版窗口里），本服务只负责接请求、
# 镜像状态、供扩展轮询。独立运行时钩子为空，保留自己的执行通路。
TASK_SINK = None
STOP_HOOK = None


def set_task_sink(sink, stop=None, queue_status=None):
    """桌面宿主登记钩子：sink 入 host 队列返回 task_id；stop 按任务作用域取消；
    queue_status 注入 host 队列快照（/api/status、busy、排队位置都改读它）。"""
    global TASK_SINK, STOP_HOOK, QUEUE_STATUS
    TASK_SINK, STOP_HOOK, QUEUE_STATUS = sink, stop, queue_status


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
    busy = _queue_busy()
    with SNIFF_LOCK:
        d = {
            "busy": busy, "running_kind": "download" if busy else None,
            "task_id": SNIFF_STATE.get("token"),
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
    return d


# ---------------- AI Agent jobs ----------------
def _agent_base_outdir():
    try:
        return load_config().get("outdir") or core.DOWNLOAD_DIR
    except Exception:
        return core.DOWNLOAD_DIR


def _agent_files(outdir):
    skip = (".part", ".ytdl", ".tmp", ".temp", ".aria2")
    files = []
    try:
        for root, _dirs, names in os.walk(outdir):
            for name in names:
                low = name.lower()
                if name.startswith(".") or low.endswith(skip):
                    continue
                path = os.path.join(root, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                files.append({"name": name, "path": path, "size": size})
    except OSError:
        pass
    files.sort(key=lambda x: x["path"].lower())
    return files


def _agent_trim_locked():
    if len(AGENT_JOBS) <= AGENT_MAX_JOBS:
        return
    finished = sorted((j for j in AGENT_JOBS.values() if j.get("done")),
                      key=lambda j: j.get("created_at", 0))
    for job in finished:
        if len(AGENT_JOBS) <= AGENT_MAX_JOBS:
            break
        AGENT_JOBS.pop(job["id"], None)


def _agent_apply_event(job, obj):
    t = (obj or {}).get("type")
    if t == "progress":
        for key in ("pct", "speed", "eta", "file"):
            if obj.get(key) not in (None, ""):
                job[key] = obj[key]
        if not job.get("done"):
            job["state"] = "running"
    elif t == "done":
        job["done"] = True
        job["code"] = obj.get("code")
        job["state"] = "done" if obj.get("code") == 0 else "failed"
        job["ended_at"] = time.time()
    else:
        text = str(obj.get("text") or "").strip()
        if text:
            job["logs"].append(text)
            del job["logs"][:-AGENT_LOG_MAX]
            job["last_text"] = text
            if "ERROR" in text:
                job["error"] = text


def _agent_task_row(tid):
    if tid is None:
        return None
    try:
        for row in _queue_snapshot().get("tasks") or []:
            if row.get("id") == tid:
                return row
    except Exception:
        pass
    return None


def _agent_job_json(job):
    with AGENT_LOCK:
        snap = dict(job)
        snap["logs"] = list(job["logs"])
    tid = snap.get("task_id")
    row = _agent_task_row(tid)
    state = row.get("state") if row else snap.get("state")
    if snap.get("done") and state in (None, "queued", "running"):
        state = "done" if snap.get("code") == 0 else "failed"
    pct = row.get("pct") if row else snap.get("pct")
    speed = row.get("speed") if row else snap.get("speed")
    eta = row.get("eta") if row else snap.get("eta")
    file_name = row.get("file") if row else snap.get("file")
    code = (row.get("exit_code") if row and row.get("exit_code") is not None
            else snap.get("code"))
    outdir = snap.get("outdir") or (row or {}).get("outdir") or ""
    files = _agent_files(outdir) if state == "done" else []
    hint = ""
    if state in ("failed", "canceled"):
        hint = sniff_hint(code if code is not None else -1,
                          snap.get("last_text") or "",
                          snap.get("logs") or [], "media")
    return {
        "ok": True,
        "api_version": AGENT_API_VERSION,
        "job_id": snap.get("id"),
        "task_id": tid,
        "state": state or "queued",
        "code": code,
        "pct": pct if pct is not None else 0.0,
        "speed": speed or "",
        "eta": eta or "",
        "file": file_name or "",
        "files": files,
        "outdir": outdir,
        "urls": snap.get("urls") or [],
        "title": snap.get("title") or "",
        "preset": snap.get("preset") or "",
        "created_at": snap.get("created_at"),
        "started_at": (row or {}).get("started_at") or snap.get("started_at"),
        "ended_at": (row or {}).get("ended_at") or snap.get("ended_at"),
        "queue_position": _queue_position(tid) if state == "queued" else 0,
        "logs": (snap.get("logs") or [])[-80:],
        "error": snap.get("error") or "",
        "hint": hint,
    }


def _agent_submit(body):
    raw = body.get("urls")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return 400, {"ok": False, "error": "urls 必须是非空数组", "code": "bad_request"}
    if len(raw) > AGENT_MAX_URLS:
        return 400, {"ok": False, "error": "一次最多 %d 个链接" % AGENT_MAX_URLS,
                     "code": "bad_request"}
    urls, bad = core.sanitize_urls([str(x) for x in raw])
    if bad:
        return 400, {"ok": False, "error": "有链接无法识别", "invalid": bad,
                     "code": "bad_url"}
    if not urls:
        return 400, {"ok": False, "error": "没有可下载的链接", "code": "bad_url"}
    preset_name = str(body.get("preset") or "best").strip().lower()
    preset = AGENT_PRESETS.get(preset_name)
    if preset is None:
        return 400, {"ok": False, "error": "preset 只支持 %s" % ", ".join(AGENT_PRESETS),
                     "code": "bad_preset"}
    fmt = str(body.get("format") or "").strip()
    if fmt and (fmt.startswith("-") or core.unsafe_arg_in(fmt)
                or any(c in fmt for c in "\r\n;\"'")):
        return 400, {"ok": False, "error": "format 不合法", "code": "bad_format"}
    playlist = bool(body.get("playlist", False))
    title = str(body.get("title") or "").strip()[:200]

    cfg = load_config()
    base = os.path.abspath(cfg.get("outdir") or core.DOWNLOAD_DIR)
    job_id = secrets.token_hex(6)
    while True:
        with AGENT_LOCK:
            taken = job_id in AGENT_JOBS
        if not taken:
            break
        job_id = secrets.token_hex(6)
    outdir = os.path.join(base, "agent", job_id)
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as e:
        return 500, {"ok": False, "error": "无法创建输出目录：%s" % e,
                     "code": "outdir_error"}

    cfg.update({
        "outdir": outdir,
        "open_after": False,
        "thumb": False, "meta": False, "sub": False, "embedsub": False,
        "pl": playlist,
        "mode": preset["mode"],
        "merge": preset["merge"],
        "fmt": fmt or preset["fmt"],
    })
    job = {
        "id": job_id, "task_id": None, "created_at": time.time(),
        "urls": urls, "title": title or (urls[0] if urls else ""),
        "preset": preset_name, "outdir": outdir, "state": "queued",
        "pct": 0.0, "speed": "", "eta": "", "file": "",
        "code": None, "done": False, "started_at": None, "ended_at": None,
        "logs": [], "last_text": "", "error": "",
    }
    with AGENT_LOCK:
        AGENT_JOBS[job_id] = job
        _agent_trim_locked()

    holder = {"task_id": None, "pending": []}
    hlock = threading.Lock()

    def emit(obj):
        with hlock:
            if holder["task_id"] is None:
                holder["pending"].append(obj)
                return
        with AGENT_LOCK:
            _agent_apply_event(job, obj)

    task = {"urls": urls, "source": "agent", "cfg": cfg, "outdir": outdir,
            "no_playlist": not playlist, "title": job["title"], "emit": emit}
    tid, err = None, ""
    try:
        if TASK_SINK is not None:
            tid = TASK_SINK(task)
        else:
            tid = QUEUE.submit({"urls": urls, "cfg": cfg, "outdir": outdir,
                                "no_playlist": not playlist, "source": "agent",
                                "title": job["title"], "emit": emit},
                               _server_executor)
    except ytdlp_queue.QueueFull as e:
        err = str(e)
    except Exception as e:
        err = str(e)
    if not tid:
        with AGENT_LOCK:
            AGENT_JOBS.pop(job_id, None)
        try:
            os.rmdir(outdir)
        except OSError:
            pass
        return 409, {"ok": False, "error": err or "队列繁忙或链接已在下载中，请稍后重试",
                     "code": "busy"}
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        tid = None
    with AGENT_LOCK:
        job["task_id"] = tid
    with hlock:
        holder["task_id"] = tid
        pending, holder["pending"] = holder["pending"], []
    for obj in pending:
        with AGENT_LOCK:
            _agent_apply_event(job, obj)
    return 200, {"ok": True, "job_id": job_id, "task_id": tid,
                 "state": "queued", "outdir": outdir, "preset": preset_name}


def _agent_cancel(job_id):
    with AGENT_LOCK:
        job = AGENT_JOBS.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": "任务不存在", "code": "not_found"}
        tid = job.get("task_id")
        if job.get("done"):
            return 409, {"ok": False, "error": "任务已经结束", "code": "finished"}
    if tid is None:
        return 409, {"ok": False, "error": "任务还没有进入队列", "code": "busy"}
    if TASK_SINK is not None and QUEUE_STATUS is not None and STOP_HOOK is not None:
        ok = bool(STOP_HOOK(tid))
    elif TASK_SINK is None:
        ok = bool(QUEUE.cancel(tid))
    else:
        ok = False
    return (200 if ok else 409), {"ok": ok, "job_id": job_id,
                                  "state": "canceling" if ok else "not_canceled"}


def _agent_jobs_list(limit=20):
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    with AGENT_LOCK:
        jobs = sorted(AGENT_JOBS.values(), key=lambda j: j.get("created_at", 0),
                      reverse=True)[:limit]
        out = [{"job_id": j["id"], "task_id": j.get("task_id"), "state": j.get("state"),
                "title": j.get("title") or "", "urls": j.get("urls") or [],
                "pct": j.get("pct", 0.0), "outdir": j.get("outdir") or "",
                "created_at": j.get("created_at"), "code": j.get("code")}
               for j in jobs]
    return {"ok": True, "jobs": out}


def _agent_status_json():
    snap = _queue_snapshot()
    return {
        "ok": True, "app": APP_ID, "version": APP_VERSION,
        "api_version": AGENT_API_VERSION,
        "queue": {"running": snap.get("running", 0), "queued": snap.get("queued", 0),
                  "paused": snap.get("paused", 0), "max": snap.get("max", 1),
                  "busy": bool(snap.get("running") or snap.get("queued"))},
        "presets": [{"name": k, "description": v["description"]}
                    for k, v in AGENT_PRESETS.items()],
        "outdir_base": _agent_base_outdir(),
        "discovery_file": AGENT_DISCOVERY_FILE,
    }


def _agent_route(path):
    if not path.startswith(AGENT_API_PREFIX + "/"):
        return None, None
    rest = path[len(AGENT_API_PREFIX) + 1:]
    if rest == "status":
        return "status", None
    if rest == "jobs":
        return "jobs", None
    if rest == "shutdown":
        return "shutdown", None
    m = re.match(r"^jobs/([A-Za-z0-9_-]+)$", rest)
    if m:
        return "job", m.group(1)
    m = re.match(r"^jobs/([A-Za-z0-9_-]+)/cancel$", rest)
    if m:
        return "cancel", m.group(1)
    return None, None


def _agent_shutdown():
    svc = ACTIVE_SERVICE
    if svc is None:
        return 404, {"ok": False, "error": "服务不存在", "code": "not_found"}
    if getattr(svc, "agent_embedded", False):
        return 409, {"ok": False, "error": "服务由桌面版管理，请关闭桌面版",
                     "code": "embedded"}
    threading.Thread(target=svc.shutdown).start()
    return 200, {"ok": True, "state": "stopping"}


# ---------------- 嗅探条目登记与直链预览 ----------------
# 扩展把嗅探到的地址报上来，本机才有资格「替它去看一眼」：预览接口只认登记过的
# 地址，客户端不能凭空给一个 URL 让服务端去连（否则就是 SSRF 入口）。
SNIFF_ITEMS = {}
SNIFF_ITEMS_LOCK = threading.Lock()
SNIFF_MAX_ITEMS = 300
SNIFF_CATS = ("video", "image", "audio", "stream", "other")


def sniff_cat(url, mime, seg=False, manifest=False, audio=False):
    """把条目归到 video/image/audio/stream/other（客户端没传 cat 时的兜底）。"""
    m = str(mime or "").lower()
    if audio or m.startswith("audio/"):
        return "audio"
    if manifest or seg or "mpegurl" in m or "dash" in m:
        return "stream"
    if m.startswith("video/"):
        return "video"
    if m.startswith("image/"):
        return "image"
    try:
        ext = os.path.splitext(urlsplit(str(url or "")).path.lower())[1]
    except Exception:
        ext = ""
    if ext in (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".mpg", ".mpeg"):
        return "video"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"):
        return "image"
    if ext in (".m4a", ".mp3", ".aac", ".flac", ".wav", ".opus", ".ogg"):
        return "audio"
    return "other"

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
            seg = core.looks_like_segment(url)
            manifest = core.looks_like_manifest(url)
            audio = core.looks_like_audio(url)
            mime = str(it.get("mime") or it.get("kind") or rec.get("mime") or "")[:200]
            # entry 记录「第一次是谁抓到的」：老扩展不传，默认 extension；
            # 同一 URL 被两边同时抓到时保留首个来源，列表里只有一条。
            raw_entry = str(it.get("entry") or "").strip().lower()
            if raw_entry not in ("extension", "embedded"):
                raw_entry = ""
            entry = rec.get("entry") or raw_entry or "extension"
            cat = str(it.get("cat") or "").strip().lower()[:16]
            if cat not in SNIFF_CATS:
                cat = sniff_cat(url, mime, seg, manifest, audio)
            try:
                size = int(it.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            rec.update({
                # 这里仍用 core 的扩展名集合给 UI 折叠/音频标记（兼容老扩展）；
                # 网络过滤本身以 sniff_rules.js 为唯一真源，内嵌/新扩展上报的 cat 优先。
                "seg": seg,
                "manifest": manifest,
                "audio": audio,
                "kind": str(it.get("kind") or mime or rec.get("kind") or "")[:16],
                "mime": mime,
                "cat": cat,
                "entry": entry,
                "size": size,
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
    return {"ok": True, "items": items[:SNIFF_MAX_ITEMS], "busy": _queue_busy()}


def clear_sniff_items():
    """清空嗅探列表：只清列表，不动正在跑的任务与预览缓存。"""
    with SNIFF_ITEMS_LOCK:
        SNIFF_ITEMS.clear()


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


def _drop_cookie(cfg):
    """删掉扩展送来的临时 cookie 文件；任务没启动/槽位被占时也要清。"""
    path = cfg.get("_sniff_cookie_file")
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


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

    def _agent_auth(self):
        raw = self.headers.get("Authorization") or ""
        token = (raw[7:].strip() if raw.lower().startswith("bearer ")
                 else (self.headers.get("X-Agent-Token") or "").strip())
        if not AGENT_TOKEN or not secrets.compare_digest(token, AGENT_TOKEN):
            self._deny("agent token 无效", 401)
            return False
        return True

    def _agent_get(self, path):
        name, arg = _agent_route(path)
        if name == "status":
            return self._json(_agent_status_json())
        if name == "jobs":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            return self._json(_agent_jobs_list((qs.get("limit") or ["20"])[0]))
        if name == "job":
            with AGENT_LOCK:
                job = AGENT_JOBS.get(arg)
            if job is None:
                return self._json({"ok": False, "error": "任务不存在",
                                   "code": "not_found"}, 404)
            return self._json(_agent_job_json(job))
        return self._json({"ok": False, "error": "not found"}, 404)

    def _agent_post(self, path, body):
        name, arg = _agent_route(path)
        if name == "jobs":
            code, obj = _agent_submit(body)
            return self._json(obj, code)
        if name == "cancel":
            code, obj = _agent_cancel(arg)
            return self._json(obj, code)
        if name == "shutdown":
            code, obj = _agent_shutdown()
            return self._json(obj, code)
        return self._json({"ok": False, "error": "not found"}, 404)

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

    def do_GET(self):
        if not self._guard():
            return
        p = self.path.split("?")[0]
        if p.startswith(AGENT_API_PREFIX):
            if not self._agent_auth():
                return
            return self._agent_get(p)
        if p == "/api/status":
            snap = _queue_snapshot()
            tasks = snap.get("tasks") or []
            first = next((t for t in tasks if t.get("state") == "running"), None)
            return self._json({
                "app": APP_ID, "version": APP_VERSION,
                # 旧字段保留：busy/kind/elapsed 取首个运行中任务（老扩展靠它判忙）
                "busy": bool(first), "kind": "download" if first else None,
                "elapsed": round(time.time() - first["started_at"], 1)
                           if first and first.get("started_at") else 0,
                "tasks": tasks,
                "running": snap.get("running", 0), "queued": snap.get("queued", 0),
            })
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
        if p.startswith(AGENT_API_PREFIX):
            if not self._agent_auth():
                return
            body = self._body()
            if getattr(self, "_body_too_large", False):
                return self._json({"error": "请求体过大"}, 413)
            return self._agent_post(p, body)
        body = self._body()
        if getattr(self, "_body_too_large", False):
            return self._json({"error": "请求体过大"}, 413)

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
            url = core.normalize_site_url(url)
            kind = body.get("kind") or "media"
            # 扩展提交的凭据：直链与「下载本页」都用；抖音这类站点 page 也需要 fresh cookies。
            referer = clean_media_url(body.get("referer"))
            ua = str(body.get("ua") or "").strip().replace("\r", " ").replace("\n", " ")
            jar = body.get("cookie_jar")
            if referer:
                cfg["referer"] = referer
                cfg["_referer_explicit"] = True
            if ua:
                cfg["ua"] = ua
            if isinstance(jar, list) and jar:
                # 首选：完整 cookie 字段 → Netscape 临时文件
                path = write_cookie_jar(jar, url)
                if path:
                    cfg["cookie"] = path
                    cfg["_sniff_cookie_file"] = path
                    # 扩展已经把本页 cookie 直接交过来了：别再让 yt-dlp 去读
                    # 浏览器那个大概率被独占锁住的 Cookies 数据库（读了必失败）。
                    cfg["browser"] = "（不使用）"
                    cfg["browser_profile"] = ""
            if not cfg.get("cookie"):
                # 兜底：只有 Cookie 头字符串（或临时文件没写成）时原样透传。
                # 注意别截断：Cookie 少一段，站点照样判你没登录。
                cookie = scrub_header_value(body.get("cookie"))
                if cookie:
                    cfg["cookie_header"] = cookie
            # 直链只有一路格式；page 走站点提取器，保留用户的画质/播放列表设置
            if kind == "media":
                cfg.update({"thumb": False, "meta": False, "sub": False, "embedsub": False,
                            "fmt": "b"})
            # 嗅探条目自带的源类型：build_download_cmd 据此决定直链要不要上
            # aria2c（清单/分片/小文件交给原生下载器）。page 提交没有 cat，
            # 仍按 aria 开关本身走，不拦。
            cat = str(body.get("cat") or "").strip().lower()[:16]
            if cat in SNIFF_CATS:
                cfg["_sniff_cat"] = cat
            cfg["_sniff_seg"] = bool(body.get("seg"))
            cfg["_sniff_manifest"] = bool(body.get("manifest"))
            try:
                cfg["_sniff_size"] = int(body.get("size") or 0)
            except (TypeError, ValueError):
                cfg["_sniff_size"] = 0
            if TASK_SINK is not None:
                # 桌面版是宿主：任务交给桌面版队列，本服务只镜像状态。
                # sink 返回 task_id（T5），token 直接对齐 task_id；入队到回填
                # token 之间的事件先缓冲，避免丢掉执行器抢跑的第一批日志。
                tok = {"id": None, "pending": [], "lock": threading.Lock()}

                def _emit(obj):
                    with tok["lock"]:
                        if tok["id"] is None:
                            tok["pending"].append(obj)
                        else:
                            sniff_emit(obj, tok["id"])

                task = {"url": url, "kind": kind, "cfg": cfg, "emit": _emit}
                tid = TASK_SINK(task)
                if not tid:
                    _drop_cookie(cfg)
                    return self._json({"ok": False, "busy": True, "running_kind": "download",
                                       "error": "桌面版暂时无法接收任务（队列已满或正在更新），请稍后再试"}, 409)
                tid = int(tid)
                LAST_EXT_TASK["id"] = tid
                with SNIFF_LOCK:
                    SNIFF_STATE.update({"lines": [], "prog": None,
                                        "last_text": "已接收，桌面版准备下载…",
                                        "done": False, "code": None, "token": tid,
                                        "url": url, "title": str(body.get("title") or "")[:200],
                                        "kind": kind})
                with tok["lock"]:
                    tok["id"] = tid
                    pending, tok["pending"] = tok["pending"], []
                for obj in pending:
                    sniff_emit(obj, tid)
                return self._json({"ok": True, "busy": False, "url": url,
                                   "task_id": tid, "queue_position": _queue_position(tid)})
            # 独立模式：入自建队列，FIFO 调度；队列满（≥200 未完成）才回 409
            try:
                tid = QUEUE.submit(
                    {"urls": [url], "cfg": cfg,
                     "outdir": cfg.get("outdir") or core.DOWNLOAD_DIR,
                     "no_playlist": True, "source": "ext",
                     "title": str(body.get("title") or "")[:200],
                     "ext_cookie_file": cfg.get("_sniff_cookie_file")},
                    _server_executor)
            except ytdlp_queue.QueueFull as e:
                _drop_cookie(cfg)
                return self._json({"ok": False, "busy": True, "running_kind": "download",
                                   "error": str(e)}, 409)
            LAST_EXT_TASK["id"] = tid
            with SNIFF_LOCK:
                SNIFF_STATE.update({"lines": [], "prog": None, "last_text": "已接收，正在排队…",
                                    "done": False, "code": None, "token": tid,
                                    "url": url, "title": str(body.get("title") or "")[:200],
                                    "kind": kind})
            # 任务可能在回填镜像前就已结束（如启动即失败）：补一次终态，避免永远「进行中」
            t = QUEUE.get(tid)
            if t is not None and t.state not in ytdlp_queue.ACTIVE:
                with SNIFF_LOCK:
                    if SNIFF_STATE.get("token") == tid:
                        SNIFF_STATE["done"] = True
                        SNIFF_STATE["code"] = t.exit_code if t.exit_code is not None else -1
            return self._json({"ok": True, "busy": False, "url": url,
                               "task_id": tid, "queue_position": _queue_position(tid)})

        if p == "/api/sniff/log":
            return self._json(sniff_log_json())

        if p == "/api/stop":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw = body.get("id")
            if raw in (None, ""):
                raw = (qs.get("id") or [None])[0]
            if raw in (None, ""):
                tid = None
            else:
                try:
                    tid = int(raw)
                except (TypeError, ValueError):
                    # 非法 id 直接拒绝：不能悄悄回退成「停最近一次任务」
                    return self._json({"ok": False})
            if QUEUE_STATUS is not None and STOP_HOOK is not None:
                # 桌面模式：按 id 精确停止；不传只停最近扩展任务。
                # 没有最近任务时给钩子 None，由宿主拒绝（不得 stop_all 误杀 GUI 任务）。
                if tid is None:
                    tid = LAST_EXT_TASK.get("id")
                ok = STOP_HOOK(tid)
            elif TASK_SINK is not None:
                # 旧桌面宿主（未注入 QUEUE_STATUS）：stop 钩子无参，沿用旧行为
                ok = STOP_HOOK() if (STOP_HOOK is not None and tid is None) else False
            elif tid is not None:
                ok = QUEUE.cancel(tid)
            else:
                # 不传 id：只停最近一次扩展任务，不误杀队列里的其它任务
                last = LAST_EXT_TASK.get("id")
                ok = QUEUE.cancel(last) if last else False
            return self._json({"ok": bool(ok)})

        self._json({"error": "not found"}, 404)


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


def _agent_discovery_write(port, embedded=False):
    data = {
        "app": APP_ID, "version": APP_VERSION, "api_version": AGENT_API_VERSION,
        "port": int(port), "url": "http://127.0.0.1:%d" % int(port),
        "token": AGENT_TOKEN, "pid": os.getpid(), "started_at": time.time(),
        "embedded": bool(embedded),
    }
    path = AGENT_DISCOVERY_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _agent_discovery_clear(port=None):
    path = AGENT_DISCOVERY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid() and (
                port is None or int(data.get("port") or 0) == int(port)):
            os.remove(path)
    except (OSError, ValueError):
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
        self.agent_enabled = False
        self.agent_embedded = False
        self.url = "http://%s:%d" % (LISTEN_HOST, port)

    def enable_agent(self, embedded=False):
        """宿主把 TASK_SINK 接好之后再写发现文件，避免 agent 抢跑落到独立队列。"""
        if self.agent_enabled:
            return True
        self.agent_embedded = bool(embedded)
        self.agent_enabled = _agent_discovery_write(self.port, self.agent_embedded)
        return self.agent_enabled

    def shutdown(self, timeout=5.0):
        """停掉服务并释放端口。可重复调用（宿主退出与 finally 都会调）。"""
        if self._closed:
            return
        self._closed = True
        if self.agent_enabled:
            # 先删发现文件：即使进程随后被强杀，也不会留下带 token 的陈旧文件
            _agent_discovery_clear(self.port)
            self.agent_enabled = False
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
        global ACTIVE_SERVICE
        if ACTIVE_SERVICE is self:
            ACTIVE_SERVICE = None


def _port_taken(port):
    """端口上是否已经有人在监听。

    ThreadingHTTPServer 默认 allow_reuse_address=1，而 Windows 的 SO_REUSEADDR
    语义是「可以绑定别人已在监听的端口」——直接 bind 会静默抢同一个端口，
    结果两个实例（比如桌面版和网页版）看起来都起来了，其实只有一个能收到请求。
    所以先探一下，有人听就跳过。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((LISTEN_HOST, port)) == 0
    except OSError:
        return False


def pick_port(port=None):
    """占一个端口：优先用指定的，否则在 PORT_RANGE 里避让。

    返回 (port, httpd, 被占用的列表)；全被占满时 port 和 httpd 都是 None。
    """
    ports = [port] if port else list(PORT_RANGE)
    skipped = []
    for p in ports:
        if _port_taken(p):
            skipped.append(p)
            continue
        try:
            httpd = ThreadingHTTPServer((LISTEN_HOST, p), Handler)
            return p, httpd, skipped
        except OSError:
            skipped.append(p)
    return None, None, skipped


def start_service(port=None, agent=False):
    """启动服务并立刻返回 Service；失败抛 OSError（端口全被占）。

    宿主可以完全不管标准输出：这里不 print、不开浏览器。
    agent=True 时写 agent 发现文件（端口 + 临时 token）；桌面宿主会等
    TASK_SINK 接好后再调 Service.enable_agent()，避免抢跑。
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
    if agent:
        svc.enable_agent(embedded=False)
    global ACTIVE_SERVICE
    ACTIVE_SERVICE = svc
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
        svc = start_service(arg_port, agent=True)
    except OSError as e:
        print(e)
        return 1

    print("=" * 56)
    print("  鱿鱼浏览器工具包 · 扩展服务已启动（无网页界面）")
    print(f"  服务地址：  {svc.url}")
    print("  访问范围：  仅本机（不监听局域网，手机无法访问）")
    print("  用途：      浏览器扩展直连；日常请用桌面版 YtDlpGUI\\启动.bat")
    print("  停止服务：  关闭本窗口，或按 Ctrl+C")
    print("=" * 56)
    if svc.skipped:
        print("  注意：端口 %s 已被占用（可能是另一个实例还在运行），已改用 %s。"
              % (", ".join(str(x) for x in svc.skipped), svc.port))
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
