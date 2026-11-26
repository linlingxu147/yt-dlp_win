# -*- coding: utf-8 -*-
"""桌面版宿主：事件泵 + 配置 + 历史 + 任务流水线（无 UI 依赖，可单测）。

事件出口是注入的 emit 回调（默认空操作）；main.py 把它接成 evaluate_js。
工作线程只往队列投事件，泵统一合并/节流后推出，绝不碰 UI。
"""
import base64
import json
import os
import queue
import sys
import threading
import time

import ytdlp_core as core
from ytdlp_core import (
    MODE_CHOICES, MODE_FMT, MODE_NAME, mode_key, TEMPLATES, BROWSERS,
    diagnose, diagnose_pairs, find_tool, fmt_dur, fmt_size, info_from_json_raw,
    normalize_headers, sanitize_urls, unsafe_arg_in,
    build_detect_cmd, build_formats_cmd, build_probe_cmd, PROBE_VARIANTS,
    spawn, iter_lines_watch, kill_tree, command_text,
    new_progress_state, progress_event, progress_text, classify_line,
    is_net_failure, next_net_plan, next_cookie_plan,
    NET_PLAN_LABEL, COOKIE_PLAN_LABEL, PROG_MARK,
    fetch_thumb, real_download_path, probe_playable, make_preview_link,
    clear_preview_links, fmt_is_separated, path_inside,
)

HISTORY_MAX = 500
PUMP_SECONDS = 0.1
PROGRESS_HZ = 5
GUI_STALL_SECONDS = 300
TASK_LABELS = {"download": "下载", "detect": "探测资源", "trouble": "自动排障",
               "update": "更新 yt-dlp", "formats": "读取格式表"}


class Host:
    def __init__(self, config_file, emit=None):
        self.config_file = config_file
        self.emit = emit or (lambda ev: None)
        self.q = queue.Queue()
        self.ready = False
        self._stop_flag = False
        self.cfg_warnings = []
        self.cfg = core.load_config(config_file, warn=self.cfg_warnings.append)
        self.history_file = os.path.join(os.path.dirname(config_file), "history.json")
        self.history = self._load_history()
        self.task_kind = None
        self.running = False
        self.proc = None
        self.user_stopped = False
        self.pstate = core.new_progress_state()
        self._progress_pending = None
        self._last_progress_ts = 0.0
        self._web_mod = None
        self.web_svc = None
        self.ext_emit = None
        self.ext_cfg = None
        self.ext_cookie_file = None
        self.dl_urls = []
        self.dl_no_playlist = False
        self.dl_outdir = ""
        self.net_tried = set()
        self.net_name = "normal"
        self.diag_buf = ""
        self.cookie_warned = False
        self.results = []
        self._play_raw = ""
        self._play_ok = False
        self._sniff_clip = ""
        self._worker = None

    # ---------------- 事件泵 ----------------
    def mark_ready(self):
        self.ready = True
        for w in self.cfg_warnings:
            self._put({"type": "log", "text": "[配置] " + str(w), "tag": "warn"})

    def _put(self, ev):
        self.q.put(ev)

    def _log(self, text, tag=None):
        self._put({"type": "log", "text": text, "tag": tag})
        if self.ext_emit:
            self.ext_emit({"type": "log", "text": text.rstrip("\n")})

    def _status(self, text, tone="info"):
        self._put({"type": "status", "text": text, "tone": tone})

    def _task_ev(self):
        self._put({"type": "task", "kind": self.task_kind, "busy": bool(self.task_kind)})

    def pump_once(self):
        if not self.ready:
            return
        now = time.time()
        if self._progress_pending and now - self._last_progress_ts >= 1.0 / PROGRESS_HZ:
            self.emit(self._progress_pending)
            self._last_progress_ts = now
            self._progress_pending = None
        logs, progress = [], None
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            t = ev.get("type")
            if t == "log":
                logs.append({"text": ev["text"], "tag": ev.get("tag")})
            elif t == "progress":
                progress = ev
            else:
                if logs:
                    self.emit({"type": "logbatch", "items": logs})
                    logs = []
                if t == "done" and self.running:
                    self.on_done(ev["code"])
                    continue
                if t == "exttask":
                    self.start_external(ev["task"])
                    continue
                if t == "detdone" and self.task_kind == "detect":
                    self.on_detect_done(ev["code"])
                    continue
                if t == "trodone" and self.task_kind in ("trouble", "update"):
                    self.on_trodone(ev["ok"], ev.get("task", "trouble"))
                    continue
                self.emit(ev)
        if logs:
            self.emit({"type": "logbatch", "items": logs})
        if progress:
            if now - self._last_progress_ts >= 1.0 / PROGRESS_HZ:
                self.emit(progress)
                self._last_progress_ts = now
            else:
                self._progress_pending = progress

    def start_pump(self):
        def loop():
            while not self._stop_flag:
                self.pump_once()
                time.sleep(PUMP_SECONDS)
        threading.Thread(target=loop, daemon=True).start()

    # ---------------- 配置 ----------------
    def get_env(self):
        return {"has_aria2c": bool(core.which_tool("aria2c.exe")),
                "yt_dlp_path": self.cfg.get("yt_dlp_path") or core.find_tool("yt-dlp.exe"),
                "svc_url": self.web_svc.url if self.web_svc else ""}

    def save_config(self, cfg):
        c = dict(self.cfg)
        c.update(cfg or {})
        c["yt_dlp_path"] = core.find_tool("yt-dlp.exe")
        warns = []
        if core.save_config(self.config_file, c, warn=warns.append):
            self.cfg = c
            for w in warns:
                self._log("[配置] " + str(w) + "\n", "warn")
            return True
        return False

    # ---------------- 下载历史 ----------------
    def _load_history(self):
        try:
            with open(self.history_file, encoding="utf-8") as f:
                data = json.load(f)
            return [r for r in data if isinstance(r, dict)][-HISTORY_MAX:]
        except Exception:
            return []

    def record_history(self, entry):
        rec = {"ts": time.time(), "urls": [], "title": "", "outdir": "",
               "status": "failed", "code": -1, "elapsed": 0, "done_count": 0,
               "mode": "", "source": "gui"}
        rec.update(entry or {})
        self.history.append(rec)
        del self.history[:-HISTORY_MAX]
        try:
            core.atomic_write_text(self.history_file,
                                   json.dumps(self.history, ensure_ascii=False, indent=1))
        except Exception:
            pass

    def history_list(self):
        return list(reversed(self.history))

    def history_clear(self):
        self.history = []
        try:
            core.atomic_write_text(self.history_file, "[]")
        except Exception:
            pass

    # ---------------- 闸门 ----------------
    def _reserve(self, kind):
        if self.task_kind:
            return False
        if self._web_mod and not self._web_mod.RUNNER.reserve(kind):
            return False
        self.task_kind = kind
        return True

    def _release_runner(self):
        if self._web_mod:
            self._web_mod.RUNNER.finish()

    def _task_cfg(self):
        return dict(self.ext_cfg) if self.ext_cfg is not None else dict(self.cfg)

    def _busy(self):
        return {"ok": False, "busy": True,
                "error": "「%s」正在进行，请先停止或等待结束"
                         % TASK_LABELS.get(self.task_kind, "任务")}

    # ---------------- 下载 ----------------
    def start_download(self, urls, cfg, no_playlist=False):
        if self.task_kind:
            return self._busy()
        bad = unsafe_arg_in(cfg.get("extra", "")) or unsafe_arg_in(cfg.get("aria_args", ""))
        if bad:
            return {"ok": False, "error": "附加参数里有被禁用的参数 %s（会执行命令或读本机文件），已阻止" % bad}
        good, bad_urls = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的下载链接"}
        outdir = (cfg.get("outdir") or "").strip()
        if not outdir:
            return {"ok": False, "error": "请先选择输出目录"}
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": "无法创建输出目录：%s" % e}
        if bad_urls:
            self._log("[提示] 已忽略 %d 个无法识别的链接\n" % len(bad_urls), "warn")
        self.save_config(cfg)
        if not self._reserve("download"):
            return self._busy()
        self._begin_download(good, cfg, no_playlist, outdir, source="gui")
        return {"ok": True}

    def _begin_download(self, urls, cfg, no_playlist, outdir, source):
        self.ext_cfg = dict(cfg) if source != "gui" else None
        self.dl_urls, self.dl_no_playlist, self.dl_outdir = list(urls), no_playlist, outdir
        self.dl_source = source
        self.net_tried, self.net_name = set(), "normal"
        self.cookie_warned = False
        self.user_stopped = False
        self.running = True
        self.pstate = new_progress_state()
        self.t_start = time.time()
        self._task_ev()
        self._status("下载中…", "run")
        warns = []
        cmd = core.build_download_cmd(cfg, urls, no_playlist=no_playlist, warn=warns)
        for w in warns:
            self._log("! " + w + "\n", "warn")
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._run, args=(cmd, outdir), daemon=True)
        self._worker.start()

    def submit_server_task(self, task):
        if not self._reserve("download"):
            return False
        self._put({"type": "exttask", "task": task})
        return True

    def start_external(self, task):
        cfg = dict(task.get("cfg") or {})
        urls = list(task.get("urls") or ([task["url"]] if task.get("url") else []))
        emit = task.get("emit")
        if not urls:
            self._release_runner()
            self.task_kind = None
            return
        outdir = cfg.get("outdir") or os.path.join(core.BASE_DIR, "downloads")
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            if emit:
                emit({"type": "log", "level": "err", "text": "无法创建输出目录：%s" % e})
                emit({"type": "done", "code": -1})
            self._release_runner()
            self.task_kind = None
            return
        self.ext_emit, self.ext_cfg = emit, cfg
        self.ext_cookie_file = cfg.get("_sniff_cookie_file")
        self.dl_urls = urls
        self.dl_no_playlist = bool(task.get("no_playlist", True))
        self.dl_outdir = outdir
        self.dl_source = task.get("source") or "ext"
        self.net_tried, self.net_name = set(), "normal"
        self.cookie_warned = False
        self.user_stopped = False
        self.running = True
        self.task_kind = "download"
        self.pstate = new_progress_state()
        self.t_start = time.time()
        self._task_ev()
        self._status("下载中…（浏览器扩展）", "run")
        warns = []
        cmd = core.build_download_cmd(cfg, urls, no_playlist=self.dl_no_playlist, warn=warns)
        for w in warns:
            self._log("! " + w + "\n", "warn")
        self._log("[浏览器扩展] 嗅探地址交给桌面版下载\n", "ok")
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._run, args=(cmd, outdir), daemon=True)
        self._worker.start()

    def _run(self, cmd, outdir):
        try:
            proc = spawn(cmd, outdir)
        except FileNotFoundError:
            self._log("找不到 yt-dlp：%s\n" % cmd[0], "err")
            self._put({"type": "done", "code": -1})
            return
        except Exception as e:
            self._log("启动失败：%s\n" % e, "err")
            self._put({"type": "done", "code": -1})
            return
        self.proc = proc
        buf = []
        why = {}
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                         stall_seconds=GUI_STALL_SECONDS, why=why):
                buf.append(line)
                self._handle_line(line)
        except Exception as e:
            self._log("读取输出出错：%s\n" % e, "err")
        finally:
            if why.get("reason") == "stall":
                self._log("[长时间没有输出，已自动终止本次任务]\n", "warn")
            self.diag_buf = "\n".join(buf[-300:])
            self.proc = None
            self._put({"type": "done", "code": proc.wait()})

    def _handle_line(self, line):
        ev = progress_event(self.pstate, line)
        if ev:
            st = self.pstate
            self._put({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                       "eta": st["eta"], "size": st["size"], "file": st["file"],
                       "item_i": st["item_i"], "item_total": st["item_total"],
                       "done_count": len(st["done"])})
            if self.ext_emit:
                self.ext_emit({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                               "eta": st["eta"], "size": st["size"], "file": st["file"],
                               "item_i": st["item_i"], "item_total": st["item_total"]})
            if ev["kind"] == "file":
                self._log((progress_text(st) if PROG_MARK in line else line) + "\n", "ok")
                self._maybe_start_play_probe()
            elif ev["kind"] == "item":
                self._log(line + "\n", None)
            else:
                self._log(progress_text(st) + "\n", "prog")
            return
        tag = classify_line(line)
        if core.cookie_copy_failed(line):
            # 浏览器锁库是可预期的首次失败，随后会自动去掉 Cookies 重试；
            # 按错误红字展示只会吓人（yt-dlp 还会重复打印两遍），压成一条黄色提示。
            if not self.cookie_warned:
                self.cookie_warned = True
                self._log("[提示] 浏览器 Cookies 数据库被占用（浏览器可能正开着），"
                          "已忽略 Cookies 继续\n", "warn")
            return
        self._log(line + "\n", tag)

    def stop(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            self._status("当前没有进行中的任务", "idle")
            return False
        self.user_stopped = True
        kill_tree(proc)
        self._status("已停止", "err")
        self._log("[已手动停止：%s]\n" % TASK_LABELS.get(self.task_kind, "任务"), "warn")
        return True

    def _finish_task(self, code):
        self._release_runner()
        emit, cookie = self.ext_emit, self.ext_cookie_file
        self.ext_emit = self.ext_cfg = self.ext_cookie_file = None
        if self._web_mod:
            with self._web_mod.PLAY_LOCK:
                self._web_mod.PLAY_STATE.update({"raw_path": "", "ok": False,
                                                 "reason": "还没有开始下载"})
        if emit:
            emit({"type": "done", "code": code})
        if cookie and os.path.isfile(cookie):
            try:
                os.remove(cookie)
            except Exception:
                pass

    def on_done(self, code):
        base = self._task_cfg()
        was_ext = self.ext_cfg is not None
        clear_preview_links(base.get("outdir") or os.path.join(core.BASE_DIR, "downloads"))
        self._play_raw = ""
        self._set_playable(False, "还没有开始下载")
        self.running = False
        self.proc = None
        self.task_kind = None
        elapsed = time.time() - getattr(self, "t_start", 0) if getattr(self, "t_start", 0) else 0
        done_count = len(self.pstate.get("done") or [])
        if code == 0:
            self._status("完成", "ok")
            self._log("=== 完成 · 耗时 %d 秒 ===\n" % int(elapsed), "ok")
            self.record_history({"urls": self.dl_urls, "outdir": self.dl_outdir,
                                 "status": "ok", "code": 0, "elapsed": round(elapsed, 1),
                                 "done_count": done_count, "mode": base.get("mode", ""),
                                 "source": getattr(self, "dl_source", "gui")})
            self._finish_task(code)
            self._task_ev()
            if base.get("open_after"):
                self.open_outdir(base.get("outdir"))
        else:
            if self._net_heal():
                return
            status = "stopped" if self.user_stopped else "failed"
            self._status("已停止" if self.user_stopped else "已结束（退出码 %s）" % code, "err")
            self._log("=== 结束，退出码 %s ===\n" % code, "err")
            self._log_diag()
            self.record_history({"urls": self.dl_urls, "outdir": self.dl_outdir,
                                 "status": status, "code": code, "elapsed": round(elapsed, 1),
                                 "done_count": done_count, "mode": base.get("mode", ""),
                                 "source": getattr(self, "dl_source", "gui")})
            self._finish_task(code)
            self._task_ev()
        self._put({"type": "done", "code": code, "elapsed": round(elapsed, 1),
                   "done_count": done_count})

    def _net_heal(self):
        if self.user_stopped or not self.dl_urls:
            return False
        cfg = self._task_cfg()
        if not cfg.get("net_hard", True):
            return False
        tail = self.diag_buf or ""
        self.net_tried.add(self.net_name)
        nxt = next_cookie_plan(tail, self.net_tried, cfg)
        if not nxt:
            if not is_net_failure(tail):
                return False
            nxt = next_net_plan(tail, self.net_tried, cfg, url=self.dl_urls[0])
        if not nxt:
            return False
        self.net_name, extra = nxt
        self._log("[自动重试] 换用%s重试（已下载部分会续传）\n"
                  % (NET_PLAN_LABEL.get(self.net_name) or COOKIE_PLAN_LABEL), "warn")
        cfg2 = dict(cfg, **extra)
        self.ext_cfg = cfg2 if self.ext_cfg is not None else None
        cmd = core.build_download_cmd(cfg2, self.dl_urls, no_playlist=self.dl_no_playlist)
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self.pstate = new_progress_state()
        self.t_start = time.time()
        self.running = True
        self.task_kind = "download"
        self._task_ev()
        self._status("下载中…（自动重试）", "run")
        self._worker = threading.Thread(target=self._run, args=(cmd, self.dl_outdir), daemon=True)
        self._worker.start()
        return True

    def _log_diag(self):
        diag = diagnose_pairs(self.diag_buf or "")
        if not diag:
            return
        self._log("\n=== 可能的原因与建议 ===\n", "warn")
        for title, advices in diag:
            self._log("· " + title + "\n", "warn")
            for a in advices:
                self._log("    - " + a + "\n", None)

    def open_outdir(self, path=None):
        d = (path or self.cfg.get("outdir") or "").strip()
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except Exception:
            pass

    # ---------------- 封面预览 ----------------
    def get_thumb(self, url, cfg):
        if not core.is_public_http_url(url):
            return {"ok": False, "err": "封面地址不合法（非公网 http/https）"}
        path, err = fetch_thumb(url, cfg)
        if err or not path:
            return {"ok": False, "err": err or "未知原因"}
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return {"ok": False, "err": str(e)}
        ext = os.path.splitext(path)[1].lower()
        mime = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
                ".bmp": "image/bmp"}.get(ext, "image/jpeg")
        return {"ok": True,
                "data_url": "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())}

    # ---------------- 边下边播 ----------------
    def _maybe_start_play_probe(self):
        cfg = self._task_cfg()
        raw = self.pstate.get("raw_path") or ""
        if not raw or not cfg.get("play_while") or raw == self._play_raw:
            return
        self._play_raw = raw
        if self._web_mod:
            with self._web_mod.PLAY_LOCK:
                self._web_mod.PLAY_STATE.update({"raw_path": raw, "outdir": self.dl_outdir,
                                                 "cfg": dict(cfg), "ok": False,
                                                 "reason": "正在判断能否边下边播…"})
        self._set_playable(False, "正在判断能否边下边播…")
        threading.Thread(target=self._play_probe_loop, args=(raw, dict(cfg)), daemon=True).start()

    def _play_probe_loop(self, raw, cfg):
        if fmt_is_separated(cfg):
            self._set_playable(False, "此源音视频分离，边下边播不适用")
            return
        if cfg.get("aria"):
            self._set_playable(False, "aria2c 外部下载器不支持边下边播")
            return
        reason = "文件还没写出来"
        deadline = time.time() + 3
        for _ in range(3):
            while time.time() < deadline:
                p = real_download_path(raw)
                try:
                    if p and os.path.getsize(p) >= 256 * 1024:
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            p = real_download_path(raw)
            if p:
                v = probe_playable(p)
                if v["ok"]:
                    self._set_playable(True, "")
                    return
                reason = v["reason"]
            deadline = time.time() + 5
        self._set_playable(False, "此源要下完才能播：%s" % reason)

    def _set_playable(self, ok, reason):
        self._play_ok = bool(ok)
        self._put({"type": "playable", "ok": bool(ok), "reason": reason})
        if self.ext_emit:
            self.ext_emit({"type": "playable", "ok": bool(ok), "reason": reason})
        if self._web_mod:
            with self._web_mod.PLAY_LOCK:
                self._web_mod.PLAY_STATE["ok"] = bool(ok)
                self._web_mod.PLAY_STATE["reason"] = reason

    def play_now(self):
        if not self._play_ok:
            return {"ok": False, "error": "此源不支持边下边播"}
        cfg = self._task_cfg()
        outdir = cfg.get("outdir") or os.path.join(core.BASE_DIR, "downloads")
        real = real_download_path(self._play_raw)
        if not real:
            return {"ok": False, "error": "文件还没写出来，稍等再试"}
        link, err = make_preview_link(real, outdir)
        if err:
            return {"ok": False, "error": err}
        try:
            os.startfile(link)
        except Exception as e:
            return {"ok": False, "error": "打开播放器失败：%s（建议安装 VLC 或 PotPlayer）" % e}
        return {"ok": True}

    # ---------------- 探测 ----------------
    def detect(self, urls, cfg):
        if self.task_kind:
            return self._busy()
        good, _ = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的链接"}
        self.save_config(cfg)
        if not self._reserve("detect"):
            return self._busy()
        self.results = []
        self.cookie_warned = False
        self.user_stopped = False
        self._task_ev()
        self._status("探测中…", "run")
        cmd = build_detect_cmd(cfg, good)
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._detect_run,
                                        args=(cmd, cfg, good), daemon=True)
        self._worker.start()
        return {"ok": True}

    def _detect_run(self, cmd, cfg, urls):
        retried = False
        while True:
            why = {}
            try:
                proc = spawn(cmd, core.BASE_DIR)
            except Exception as e:
                self._log("启动失败：%s\n" % e, "err")
                self._put({"type": "detdone", "code": -1})
                return
            self.proc = proc
            buf = []
            try:
                for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                             stall_seconds=GUI_STALL_SECONDS, why=why):
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("{"):
                        try:
                            info = info_from_json_raw(json.loads(line))
                            self.results.append(info)
                            self._put({"type": "detrow", "info": info})
                            continue
                        except Exception:
                            pass
                    buf.append(line)
                    self._handle_line(line)
            except Exception as e:
                self._log("读取输出出错：%s\n" % e, "err")
            if why.get("reason") == "stall":
                self._log("[长时间没有输出，已自动停止探测]\n", "warn")
            self.diag_buf = "\n".join(buf[-300:])
            self.proc = None
            code = proc.wait()
            if not retried and not self.user_stopped and not self.results:
                nxt = next_cookie_plan(self.diag_buf, set(), cfg)
                if nxt:
                    self._log("[自动重试] 换用%s…\n" % COOKIE_PLAN_LABEL, "warn")
                    cfg = dict(cfg, **nxt[1])
                    cmd = build_detect_cmd(cfg, urls)
                    self._log("> " + command_text(cmd) + "\n", "cmd")
                    self._status("探测中…（自动重试）", "run")
                    retried = True
                    continue
            break
        self._put({"type": "detdone", "code": code})

    def on_detect_done(self, code):
        self.task_kind = None
        self._release_runner()
        self._task_ev()
        n = len(self.results)
        if n:
            self._status("探测完成 · %d 条" % n, "ok")
            self._log("=== 探测完成：%d 条 ===\n" % n, "ok")
        else:
            self._status("探测结束，没有拿到条目", "err")
            self._log("=== 探测结束，退出码 %s ===\n" % code, "err")
            self._log_diag()
        self._put({"type": "detdone", "code": code, "count": n})

    # ---------------- 排障 ----------------
    def troubleshoot(self, urls, cfg):
        if self.task_kind:
            return self._busy()
        good, _ = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的链接"}
        self.save_config(cfg)
        if not self._reserve("trouble"):
            return self._busy()
        self.user_stopped = False
        self._task_ev()
        self._status("排障中…", "run")
        self._log("=== 自动排障：依次尝试 4 套访问配置（每套最多 25 秒）===\n", "cmd")
        self._worker = threading.Thread(target=self._trouble_run, args=(good[0], dict(cfg)),
                                        daemon=True)
        self._worker.start()
        return {"ok": True}

    def _trouble_run(self, url, cfg):
        collected, hit = [], None
        for key, label, fix in PROBE_VARIANTS:
            if self.user_stopped:
                self._log("[已取消，剩余方案未尝试]\n", "warn")
                break
            cmd = build_probe_cmd(cfg, url, key)
            self._log("> " + command_text(cmd) + "\n", "cmd")
            ok, msg, out = self._probe_once(cmd, timeout=25)
            self._log(("✓ " if ok else "✗ ") + label + "：" + msg + "\n", "ok" if ok else "warn")
            collected.append(out)
            if ok:
                hit = (label, fix, url)
                break
        if hit:
            label, fix, u = hit
            touched, manual = core.apply_probe_fix(cfg, u, fix)
            self.save_config(cfg)
            self._put({"type": "applyfix", "cfg": cfg, "touched": touched, "manual": manual})
            self._log("=== 排障「%s」：已应用 %s ===\n" % (label, "、".join(touched) or "无"), "ok")
        else:
            diag = diagnose_pairs("\n".join(collected))
            for title, advices in diag:
                self._log("· " + title + "\n", "warn")
                for a in advices:
                    self._log("    - " + a + "\n", None)
        self._put({"type": "trodone", "ok": 1 if hit else 0, "task": "trouble"})

    def _probe_once(self, cmd, timeout=25):
        try:
            proc = spawn(cmd, core.BASE_DIR)
        except Exception as e:
            return False, "启动失败：" + str(e), ""
        self.proc = proc
        lines, timed_out = [], []
        t0 = time.time()

        def expired():
            if self.user_stopped:
                return True
            if time.time() - t0 >= timeout:
                timed_out.append(True)
                return True
            return False

        got_json = False
        try:
            for line in iter_lines_watch(proc, abort=expired):
                s = line.strip()
                if not s:
                    continue
                lines.append(s)
                if s.startswith("{") and core.json_ok(s):
                    got_json = True
                    break
        finally:
            if proc.poll() is None:
                kill_tree(proc)
            proc.wait()
            self.proc = None
        if got_json:
            msg = "成功解析出条目"
        elif timed_out:
            msg = "超时 %ss" % timeout
        elif self.user_stopped:
            msg = "已取消"
        else:
            msg = lines[-1][:120] if lines else "无输出"
        return got_json, msg, "\n".join(lines[-25:])

    def on_trodone(self, ok, task):
        self.task_kind = None
        self._release_runner()
        self._task_ev()
        if task == "update":
            self._status("更新完成" if ok else "更新失败", "ok" if ok else "err")
        else:
            self._status("排障完成 · 已找到可用方案" if ok else "排障结束 · 四套方案都失败",
                         "ok" if ok else "err")
        self._put({"type": "trodone", "ok": ok, "task": task})

    # ---------------- 更新 ----------------
    def update_ytdlp(self, proxy):
        if self.task_kind:
            return self._busy()
        yt = self.cfg.get("yt_dlp_path") or find_tool("yt-dlp.exe")
        cmd = [yt, "-U"]
        if (proxy or "").strip():
            cmd += ["--proxy", proxy.strip()]
        if not self._reserve("update"):
            return self._busy()
        self.user_stopped = False
        self._task_ev()
        self._status("更新 yt-dlp…", "run")
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._update_run, args=(cmd,), daemon=True)
        self._worker.start()
        return {"ok": True}

    def _update_run(self, cmd):
        try:
            proc = spawn(cmd, core.BASE_DIR)
        except Exception as e:
            self._log("启动失败：%s\n" % e, "err")
            self._put({"type": "trodone", "ok": 0, "task": "update"})
            return
        self.proc = proc
        code = -1
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped, keep_blank=True):
                self._log(line + "\n", None)
            code = proc.wait()
        except Exception as e:
            self._log("更新出错：%s\n" % e, "err")
        finally:
            self.proc = None
        self._log("=== 更新完成，请重新探测 ===\n" if code == 0
                  else "=== 更新失败，通常是网络/代理不通 ===\n", "ok" if code == 0 else "err")
        self._put({"type": "trodone", "ok": 1 if code == 0 else 0, "task": "update"})

    # ---------------- 格式表 ----------------
    def fetch_formats(self, url, cfg):
        if self.task_kind:
            return self._busy()
        good, _ = sanitize_urls([url])
        if not good:
            return {"ok": False, "error": "请先填写一个有效链接"}
        if not self._reserve("formats"):
            return self._busy()
        self.user_stopped = False
        self._task_ev()
        cmd = build_formats_cmd(cfg, good[0])
        self._worker = threading.Thread(target=self._formats_run,
                                        args=(cmd, cfg, good[0]), daemon=True)
        self._worker.start()
        return {"ok": True}

    def _formats_run(self, cmd, cfg, url):
        retried = False
        while True:
            try:
                proc = spawn(cmd, core.BASE_DIR)
                self.proc = proc
                out = list(iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                            keep_blank=True, stall_seconds=120))
                code = proc.wait()
            except Exception as e:
                out, code = [str(e)], -1
            self.proc = None
            if code != 0 and not retried and not self.user_stopped:
                nxt = next_cookie_plan("\n".join(out[-300:]), set(), cfg)
                if nxt:
                    self._log("[自动重试] 换用%s…\n" % COOKIE_PLAN_LABEL, "warn")
                    cfg = dict(cfg, **nxt[1])
                    cmd = build_formats_cmd(cfg, url)
                    self._log("> " + command_text(cmd) + "\n", "cmd")
                    retried = True
                    continue
            break
        if self.task_kind == "formats":
            self.task_kind = None
            self._release_runner()
            self._task_ev()
        self._put({"type": "formats", "lines": out, "code": code})

    def copy_cmd(self, urls, cfg):
        good, _ = sanitize_urls(urls or [])
        cmd = core.build_download_cmd(cfg, good)
        return {"ok": True, "text": command_text(cmd)}

    # ---------------- 内嵌扩展服务 ----------------
    def load_web_server(self):
        import importlib.util
        path = os.path.join(core.BASE_DIR, "YtDlpWeb", "server.py")
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("ytdlp_web_server", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def start_ext_service(self):
        if self.web_svc is not None:
            return {"ok": True}
        try:
            mod = self.load_web_server()
            if mod is None:
                self._put({"type": "svc", "text": "扩展服务：缺少 YtDlpWeb/server.py", "ok": False})
                return {"ok": False, "error": "缺少 YtDlpWeb/server.py"}
            mod.set_config_file(self.config_file)
            self._web_mod = mod
            self.web_svc = mod.start_service()
            mod.set_task_sink(self.submit_server_task, stop=self.stop)
        except Exception as e:
            self.web_svc = None
            self._web_mod = None
            self._put({"type": "svc", "text": "扩展服务启动失败", "ok": False})
            return {"ok": False, "error": str(e)}
        self._put({"type": "svc", "text": "扩展服务：%s" % self.web_svc.url.replace("http://", ""),
                   "ok": True})
        self._log("[扩展服务] 已监听 %s\n" % self.web_svc.url, "info")
        return {"ok": True}

    def stop_ext_service(self):
        if self.web_svc is None:
            return
        if self._web_mod:
            self._web_mod.set_task_sink(None, stop=None)
        try:
            self.web_svc.shutdown()
        except Exception:
            pass
        self.web_svc = None
        self._web_mod = None
        self._put({"type": "svc", "text": "扩展服务：已停止", "ok": False})

    def set_ext_service(self, on):
        if on:
            r = self.start_ext_service()
        else:
            self.stop_ext_service()
            r = {"ok": True}
        self.save_config({"ext_service": bool(on)})
        return r

    # ---------------- 扩展嗅探 ----------------
    def sniff_items(self):
        if self._web_mod is None:
            return {"ok": True, "items": [], "busy": bool(self.task_kind), "svc": False}
        d = self._web_mod.sniff_items_json()
        d["svc"] = True
        return d

    def sniff_clear(self):
        if self._web_mod is not None:
            self._web_mod.clear_sniff_items()
        return {"ok": True}

    def sniff_download(self, items):
        if not items:
            return {"ok": False, "error": "先选中要下载的条目"}
        if self.task_kind:
            return self._busy()
        cfg = dict(self.cfg)
        cfg.update({"fmt": "b", "play_while": True, "thumb": False, "meta": False,
                    "sub": False, "embedsub": False})
        if len(items) == 1:
            it = items[0]
            if it.get("referer") or it.get("page_url"):
                cfg["referer"] = it.get("referer") or it.get("page_url")
            if it.get("ua"):
                cfg["ua"] = it["ua"]
        task = {"urls": [it["url"] for it in items], "kind": "media", "cfg": cfg,
                "emit": None, "no_playlist": True, "source": "sniff"}
        if not self.submit_server_task(task):
            return self._busy()
        self._put({"type": "sniff_started"})
        return {"ok": True}

    def sniff_preview(self, url):
        if self._web_mod is None:
            return {"ok": False, "error": "扩展服务未启用"}
        try:
            res = self._web_mod.build_sniff_preview(url)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "未知原因"}
        out = {"ok": True, "duration": res.get("duration"),
               "thumb_data_url": "", "has_clip": False}
        if res.get("thumb"):
            path, _ = self._web_mod.preview_token_lookup(res["thumb"], "thumb")
            if path:
                try:
                    with open(path, "rb") as f:
                        out["thumb_data_url"] = ("data:image/png;base64,"
                                                 + base64.b64encode(f.read()).decode())
                except OSError:
                    pass
        if res.get("clip"):
            path, _ = self._web_mod.preview_token_lookup(res["clip"], "clip")
            self._sniff_clip = path or ""
            out["has_clip"] = bool(self._sniff_clip)
        return out

    def sniff_play_clip(self):
        if not self._sniff_clip:
            return {"ok": False, "error": "没有可播放的片段"}
        try:
            os.startfile(self._sniff_clip)
        except Exception as e:
            return {"ok": False, "error": "打开播放器失败：%s" % e}
        return {"ok": True}

    # ---------------- 收尾 ----------------
    def shutdown(self):
        self.save_config({})
        try:
            self.stop()
        except Exception:
            pass
        self.stop_ext_service()
        try:
            clear_preview_links(self.cfg.get("outdir") or os.path.join(core.BASE_DIR, "downloads"))
        except Exception:
            pass
        self._stop_flag = True