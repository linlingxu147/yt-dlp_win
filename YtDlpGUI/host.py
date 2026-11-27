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
import ytdlp_queue
try:
    from . import updater
except ImportError:          # 真实入口把 YtDlpGUI/ 放进 sys.path，按顶层模块导入
    import updater
from ytdlp_core import (
    MODE_CHOICES, MODE_FMT, MODE_NAME, mode_key, TEMPLATES, BROWSERS,
    diagnose, diagnose_pairs, find_tool, fmt_dur, fmt_size, info_from_json_raw,
    normalize_headers, sanitize_urls, unsafe_arg_in,
    build_detect_cmd, build_formats_cmd, build_probe_cmd, PROBE_VARIANTS,
    spawn, iter_lines_watch, kill_tree, command_text,
    new_progress_state, progress_event, progress_text, classify_line,
    is_net_failure, next_net_plan, next_cookie_plan,
    NET_PLAN_LABEL, COOKIE_PLAN_LABEL, PROG_MARK,
    fetch_thumb, path_inside,
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
        self._history_lock = threading.Lock()
        # 辅助操作（探测/排障/格式表）共用 aux_kind 单闸，与下载并行；
        # 更新 yt-dlp 用 updating 独占；下载全部进队列。
        self.proc = None
        self.user_stopped = False
        self.pstate = core.new_progress_state()
        self._progress_pending = {}
        self._last_progress_ts = {}
        self._web_mod = None
        self._web_server_mod = None      # YtDlpWeb/server.py 模块缓存（嗅探登记表不依赖 HTTP）
        self.web_svc = None
        self.diag_buf = ""
        self.cookie_warned = False
        self.results = []
        self._sniff_clip = ""
        self._worker = None
        self.embedded = None
        self.queue = ytdlp_queue.QueueManager(
            max_concurrent=int(str(self.cfg.get("queue_n") or "2")),
            emit=self._put)
        self.aux_kind = None
        self.updating = False
        self._open_after_ts = {}

    # ---------------- 事件泵 ----------------
    def mark_ready(self):
        self.ready = True
        for w in self.cfg_warnings:
            self._put({"type": "log", "text": "[配置] " + str(w), "tag": "warn"})

    def _put(self, ev):
        self.q.put(ev)

    def _log(self, text, tag=None):
        self._put({"type": "log", "text": text, "tag": tag})

    def _status(self, text, tone="info"):
        self._put({"type": "status", "text": text, "tone": tone})

    def _task_payload(self):
        kind = self.aux_kind or ("update" if self.updating else None)
        return {"type": "task", "kind": kind, "busy": bool(kind),
                "downloading": self.queue.any_active()}

    def _task_ev(self):
        self._put(self._task_payload())

    def pump_once(self):
        if not self.ready:
            return
        now = time.time()
        for tid in list(self._progress_pending):
            if now - self._last_progress_ts.get(tid, 0.0) >= 1.0 / PROGRESS_HZ:
                self.emit(self._progress_pending.pop(tid))
                self._last_progress_ts[tid] = now
        logs, progress = {}, {}

        def flush_logs():
            for tid, items in logs.items():
                self.emit({"type": "logbatch", "task_id": tid, "items": items})
            logs.clear()

        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            t = ev.get("type")
            if t == "log":
                logs.setdefault(ev.get("task_id") or 0, []).append(
                    {"text": ev["text"], "tag": ev.get("tag")})
            elif t == "progress":
                progress[ev.get("task_id") or 0] = ev
            else:
                flush_logs()
                if t == "exttask":
                    self.start_external(ev["task"])
                    continue
                if t == "detdone" and self.aux_kind == "detect":
                    self.on_detect_done(ev["code"])
                    continue
                if t == "trodone" and (self.aux_kind == "trouble" or self.updating):
                    self.on_trodone(ev["ok"], ev.get("task", "trouble"))
                    continue
                if t == "queue_changed":
                    self.emit(ev)
                    self.emit(self._task_payload())
                    continue
                self.emit(ev)
        flush_logs()
        for tid, ev in progress.items():
            if now - self._last_progress_ts.get(tid, 0.0) >= 1.0 / PROGRESS_HZ:
                self.emit(ev)
                self._last_progress_ts[tid] = now
            else:
                self._progress_pending[tid] = ev

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
            try:
                self.queue.set_max(int(str(c.get("queue_n") or "2")))
            except (TypeError, ValueError):
                pass
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
        # 多个下载 executor 会并发写历史：追加 + 落盘要在同一把锁里
        with self._history_lock:
            self.history.append(rec)
            del self.history[:-HISTORY_MAX]
            try:
                core.atomic_write_text(self.history_file,
                                       json.dumps(self.history, ensure_ascii=False, indent=1))
            except Exception:
                pass

    def _record_task_history(self, task, status):
        """按任务上下文记一条历史（取消/退出停止共用；正常结束走 _task_done）。"""
        self.record_history({"urls": task.urls, "outdir": task.outdir, "status": status,
                             "elapsed": round(time.time() - (task.started_at or task.created_at), 1),
                             "done_count": len(task.pstate.get("done") or []),
                             "mode": task.cfg.get("mode", ""), "source": task.source,
                             "task_id": task.id})

    def history_list(self):
        with self._history_lock:
            return list(reversed(self.history))

    def history_clear(self):
        with self._history_lock:
            self.history = []
            try:
                core.atomic_write_text(self.history_file, "[]")
            except Exception:
                pass

    # ---------------- 闸门（辅助操作单闸；下载走队列；更新独占） ----------------
    def _reserve_aux(self, kind):
        if self.aux_kind or self.updating:
            return False
        self.aux_kind = kind
        return True

    def _busy(self):
        kind = self.aux_kind or ("update" if self.updating else None)
        return {"ok": False, "busy": True,
                "error": "「%s」正在进行，请先停止或等待结束"
                         % TASK_LABELS.get(kind, "任务")}

    # ---------------- 下载（逐链接入队，per-task executor） ----------------
    def start_download(self, urls, cfg, no_playlist=False):
        if self.updating:
            return {"ok": False, "error": "正在更新 yt-dlp，请等待完成"}
        bad = unsafe_arg_in(cfg.get("extra", "")) or unsafe_arg_in(cfg.get("aria_args", ""))
        if bad:
            return {"ok": False, "error": "附加参数里有被禁用的参数 %s，已阻止" % bad}
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
        todo = self._dedupe_urls(good, outdir)
        if not todo:
            return {"ok": False, "error": "这些链接已在队列中下载"}
        self.save_config(cfg)
        ids = []
        try:
            for u in todo:
                spec = {"urls": [u], "cfg": dict(cfg), "outdir": outdir,
                        "no_playlist": no_playlist, "source": "gui"}
                ids.append(self.queue.submit(spec, self._exec_download))
        except ytdlp_queue.QueueFull as e:
            # 部分入队：已加入的任务留在队列里照常执行，只把剩余未加入的情况报给界面
            if ids:
                return {"ok": False, "task_ids": ids,
                        "error": "队列已满：已加入 %d 个任务，其余未加入（%s）"
                                 % (len(ids), e)}
            return {"ok": False, "error": str(e)}
        return {"ok": True, "task_ids": ids}

    def _active_task_has(self, url, outdir):
        """队列里是否已有同 URL、同输出目录的未完成任务（排队/进行中/暂停）。

        两个任务写同一个输出文件会互相覆盖，所以「同链接 + 同目录」只留一个。
        """
        if not url:
            return False
        for row in self.queue.snapshot()["tasks"]:
            if (row["state"] in ("queued", "running", "paused")
                    and row["outdir"] == outdir and url in (row["urls"] or ())):
                return True
        return False

    def _dedupe_urls(self, urls, outdir):
        """去掉本批重复链接（保序）与队列中已在下载的同链接，返回待入队列表。"""
        seen, todo, skipped = set(), [], 0
        for u in urls:
            if u in seen:
                skipped += 1
                continue
            seen.add(u)
            if self._active_task_has(u, outdir):
                skipped += 1
                continue
            todo.append(u)
        if skipped:
            self._log("[提示] 已跳过 %d 个重复链接（相同链接已在队列中下载）\n"
                      % skipped, "warn")
        return todo

    def _exec_download(self, task):
        cfg = task.cfg
        task._extra = {}
        if task.source == "agent":
            self._task_log(task, "[AI] 任务交给桌面版下载\n", "ok")
        elif task.source == "embedded-page":
            self._task_log(task, "[内嵌浏览器] 当前页面交给 yt-dlp 整段下载\n", "ok")
        elif task.source != "gui":
            self._task_log(task, "[浏览器扩展] 嗅探地址交给桌面版下载\n", "ok")
        while True:
            warns = []
            c = cfg if task.net_name == "normal" else dict(cfg, **task._extra)
            cmd = core.build_download_cmd(c, task.urls,
                                          no_playlist=task.no_playlist, warn=warns)
            for w in warns:
                self._task_log(task, "! " + w + "\n", "warn")
            self._task_log(task, "> " + command_text(cmd) + "\n", "cmd")
            code, tail, why = ytdlp_queue.run_process(
                task, cmd, task.outdir, lambda ln: self._task_line(task, ln),
                stall_seconds=GUI_STALL_SECONDS)
            task.diag_buf = tail
            # 暂停/取消早退先于启动失败分支：取消已由控制方法记历史，不能再走 _task_done
            if task.pause_requested or task.cancel_requested:
                task.exit_code = -1 if code is None else code
                if task.cancel_requested:
                    if task.ext_emit:
                        # 运行中被取消：给扩展镜像补发 done（排队中取消由 cancel_task 补发）
                        task.ext_emit({"type": "done", "code": task.exit_code})
                    self._clean_ext_cookie(task)
                # 暂停不删临时 Cookie：继续下载还要用它（done/failed/取消/退出才清）
                return
            if code is None:
                self._task_log(task, "启动失败\n", "err")
                task.exit_code = -1
                self._task_done(task, -1)
                return
            if why.get("reason") == "stall":
                self._task_log(task, "[长时间没有输出，已自动终止本次任务]\n", "warn")
            if code == 0:
                task.exit_code = 0
                self._task_done(task, 0)
                return
            healed = self._task_heal(task, cfg, tail)
            if healed:
                continue
            task.exit_code = code
            self._task_done(task, code)
            return

    def _task_log(self, task, text, tag=None):
        task.log_ring.append({"text": text, "tag": tag})
        self._put({"type": "log", "text": text, "tag": tag, "task_id": task.id})
        if task.ext_emit:
            task.ext_emit({"type": "log", "text": text.rstrip("\n")})

    def _task_line(self, task, line):
        ev = progress_event(task.pstate, line)
        if ev:
            st = task.pstate
            self._put({"type": "progress", "task_id": task.id, "pct": st["pct"],
                       "speed": st["speed"], "eta": st["eta"], "size": st["size"],
                       "file": st["file"], "item_i": st["item_i"],
                       "item_total": st["item_total"], "done_count": len(st["done"])})
            if task.ext_emit:
                task.ext_emit({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                               "eta": st["eta"], "size": st["size"], "file": st["file"],
                               "item_i": st["item_i"], "item_total": st["item_total"]})
            if ev["kind"] == "file":
                self._task_log(task, (progress_text(st) if PROG_MARK in line else line) + "\n", "ok")
            elif ev["kind"] == "item":
                self._task_log(task, line + "\n", None)
            else:
                self._task_log(task, progress_text(st) + "\n", "prog")
            return
        if core.cookie_copy_failed(line):
            # 浏览器锁库是可预期的首次失败，随后会自动去掉 Cookies 重试；
            # 按错误红字展示只会吓人（yt-dlp 还会重复打印两遍），压成一条黄色提示。
            if not task.cookie_warned:
                task.cookie_warned = True
                self._task_log(task, "[提示] 浏览器 Cookies 数据库被占用，已忽略 Cookies 继续\n", "warn")
            return
        self._task_log(task, line + "\n", classify_line(line))

    def _task_heal(self, task, cfg, tail):
        """自愈阶梯：同一任务内换配置重试，不释放槽位。"""
        if not cfg.get("net_hard", True):
            return False
        task.net_tried.add(task.net_name)
        nxt = next_cookie_plan(tail, task.net_tried, cfg)
        if not nxt and is_net_failure(tail):
            nxt = next_net_plan(tail, task.net_tried, cfg, url=task.urls[0])
        if not nxt:
            return False
        task.net_name, task._extra = nxt
        self._task_log(task, "[自动重试] 换用%s重试（已下载部分会续传）\n"
                       % (NET_PLAN_LABEL.get(task.net_name) or COOKIE_PLAN_LABEL), "warn")
        task.pstate = core.new_progress_state()
        return True

    def _task_done(self, task, code):
        elapsed = time.time() - (task.started_at or task.created_at)
        status = "ok" if code == 0 else ("canceled" if task.cancel_requested
                                        else "stopped" if task.pause_requested else "failed")
        self.record_history({"urls": task.urls, "outdir": task.outdir, "status": status,
                             "code": code, "elapsed": round(elapsed, 1),
                             "done_count": len(task.pstate.get("done") or []),
                             "mode": task.cfg.get("mode", ""), "source": task.source,
                             "task_id": task.id})
        if task.ext_emit:
            task.ext_emit({"type": "done", "code": code})
        self._clean_ext_cookie(task)
        if code == 0:
            self._task_log(task, "=== 完成 · 耗时 %d 秒 ===\n" % int(elapsed), "ok")
            self._maybe_open_after(task)
        else:
            self._task_log(task, "=== 结束，退出码 %s ===\n" % code, "err")
            self._task_diag(task)
        self._put({"type": "done", "code": code, "task_id": task.id,
                   "elapsed": round(elapsed, 1)})

    def _clean_ext_cookie(self, task):
        """临时嗅探 Cookie 文件按任务清理：done/failed、取消、退出停止时才删。

        暂停必须保留：继续下载要用同一个 jar 才能带上登录态。
        """
        if task.ext_cookie_file and os.path.isfile(task.ext_cookie_file):
            try:
                os.remove(task.ext_cookie_file)
            except OSError:
                pass

    def _task_diag(self, task):
        diag = diagnose_pairs(task.diag_buf or "")
        if not diag:
            return
        self._task_log(task, "\n=== 可能的原因与建议 ===\n", "warn")
        for title, advices in diag:
            self._task_log(task, "· " + title + "\n", "warn")
            for a in advices:
                self._task_log(task, "    - " + a + "\n", None)

    def _maybe_open_after(self, task):
        """同一输出目录 10 秒内只弹一次资源管理器，避免并发任务各开一个。"""
        if not task.cfg.get("open_after"):
            return
        d = task.outdir
        now = time.time()
        if now - self._open_after_ts.get(d, 0) < 10:
            return
        self._open_after_ts[d] = now
        self.open_outdir(d)

    # ---------------- 扩展 / 嗅探提交 ----------------
    def _ext_spec(self, task):
        cfg = dict(task.get("cfg") or {})
        urls = list(task.get("urls") or ([task["url"]] if task.get("url") else []))
        if not urls:
            return None
        outdir = cfg.get("outdir") or core.DOWNLOAD_DIR
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            emit = task.get("emit")
            if emit:
                emit({"type": "log", "level": "err", "text": "无法创建输出目录：%s" % e})
                emit({"type": "done", "code": -1})
            return None
        return {"urls": urls, "cfg": cfg, "outdir": outdir,
                "no_playlist": bool(task.get("no_playlist", True)),
                "source": task.get("source") or "ext",
                "title": str(task.get("title") or ""),
                "emit": task.get("emit"),
                "ext_cookie_file": cfg.get("_sniff_cookie_file")}

    def submit_server_task(self, task):
        """扩展提交入队，返回 task_id（truthy 兼容旧 bool 调用）；拒绝返回 None。

        镜像 token 由 server 侧直接对齐返回的 task_id（T8），这里不再预分配任务号。
        """
        if self.updating:
            return None
        spec = self._ext_spec(task)
        if spec is None:
            return None
        for u in spec["urls"]:
            if self._active_task_has(u, spec["outdir"]):
                self._log("[提示] 跳过重复提交（相同链接已在队列中下载）：%s\n" % u, "warn")
                return None
        try:
            tid = self.queue.submit(spec, self._exec_download)
        except ytdlp_queue.QueueFull:
            return None
        return tid

    def start_external(self, task):
        return self.submit_server_task(task)

    def _handle_line(self, line):
        """辅助操作（探测）共用的逐行处理；下载走 _task_line。"""
        ev = progress_event(self.pstate, line)
        if ev:
            st = self.pstate
            self._put({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                       "eta": st["eta"], "size": st["size"], "file": st["file"],
                       "item_i": st["item_i"], "item_total": st["item_total"],
                       "done_count": len(st["done"])})
            if ev["kind"] == "file":
                self._log((progress_text(st) if PROG_MARK in line else line) + "\n", "ok")
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
        """只停辅助操作/更新；下载经队列的暂停/取消控制（T7）。"""
        kind = self.aux_kind or ("update" if self.updating else None)
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self.user_stopped = True
            kill_tree(proc)
        elif kind:
            # 镜像下载没有子进程，stop 也要能把更新线程叫停
            self.user_stopped = True
        else:
            self._status("当前没有进行中的任务", "idle")
            return False
        self._status("已停止", "err")
        self._log("[已手动停止：%s]\n" % TASK_LABELS.get(kind, "任务"), "warn")
        return True

    # ---------------- 任务控制（T7） ----------------
    def cancel_task(self, tid):
        """API 取消：先取消、成功才记 canceled 历史（任务恰好跑完时不留假记录），
        并清理部分下载（purge 默认 True）。"""
        tid = int(tid)
        ok = self.queue.cancel(tid)
        task = self.queue.get(tid)
        if task is not None:
            if ok:
                self._record_task_history(task, "canceled")
                # 排队/暂停中被取消：执行器不会再跑，done 在这里补发给扩展镜像；
                # 运行中取消由执行器早退分支补发，避免面板永远卡在「进行中」。
                if task.state == "canceled" and task.ext_emit:
                    task.ext_emit({"type": "done",
                                   "code": task.exit_code if task.exit_code is not None else -1})
            self._clean_ext_cookie(task)
        return ok

    def stop_ext_task(self, tid):
        """扩展 /api/stop 钩子（T9）：只取消指定队列任务，不误杀 GUI 任务、不 stop_all。

        tid 缺失/非法返回 False；复用 cancel_task 的先取消后记历史与 Cookie 清理。
        """
        if tid is None:
            return False
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return False
        return self.cancel_task(tid)

    def remove_task(self, tid):
        """删除列表里的一条已结束任务（完成 / 失败 / 已取消）；活动任务返回 False。

        只清显示列表，磁盘一律不动：失败与停止的任务还留着 .part，历史页
        「继续下载」要用它续传，所以这里绝不能顺手 purge。临时 Cookie 罐按任务
        清理（与取消一致，重复调用无副作用）。
        """
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return False
        task = self.queue.get(tid)
        if task is None or not self.queue.remove(tid):
            return False
        self._clean_ext_cookie(task)
        return True

    def remove_finished_tasks(self):
        """清空所有已结束任务（同 remove_task：只清列表，不动文件）。"""
        return {"ok": True, "removed": self.queue.remove_finished()}

    def stop_all_downloads(self):
        """全部停止/退出：运行中与已暂停记 stopped 并保留部分下载；排队直接丢弃（spec §4）。

        暂停任务已有 .part，必须记一条 stopped 历史，否则退出后无法从历史「继续下载」。
        """
        for row in self.queue.snapshot()["tasks"]:
            if row["state"] not in ("running", "paused"):
                continue
            task = self.queue.get(row["id"])
            if task is None or task.state not in ("running", "paused"):
                continue
            self._record_task_history(task, "stopped")
        active = [row["id"] for row in self.queue.snapshot()["tasks"]
                  if row["state"] in ("running", "paused", "queued")]
        n = self.queue.stop_all(purge=False)
        # 退出后不留明文 jar：被停/被清的任务（运行中、暂停、排队）都要删临时 Cookie
        for tid in active:
            task = self.queue.get(tid)
            if task is not None:
                self._clean_ext_cookie(task)
        return {"ok": True, "stopped": n}

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
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            # 换机器/换盘后，历史记录里存的是上一台机器的绝对路径，建目录必然失败；
            # 直接 return 会让「打开目录」毫无反应，退到当前下载目录并说明原因。
            alt = os.path.abspath(self.cfg.get("outdir") or core.DOWNLOAD_DIR)
            self._log("[目录] 无法访问 %s，改为打开 %s\n" % (d, alt), "warn")
            try:
                os.makedirs(alt, exist_ok=True)
            except OSError:
                return
            d = alt
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

    # ---------------- 探测 ----------------
    def detect(self, urls, cfg):
        good, _ = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的链接"}
        self.save_config(cfg)
        if not self._reserve_aux("detect"):
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
        self.aux_kind = None
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
        good, _ = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的链接"}
        self.save_config(cfg)
        if not self._reserve_aux("trouble"):
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
        if task == "update":
            self.updating = False
        else:
            self.aux_kind = None
        self._task_ev()
        if task == "update":
            self._status("更新完成" if ok else "更新失败", "ok" if ok else "err")
        else:
            self._status("排障完成 · 已找到可用方案" if ok else "排障结束 · 四套方案都失败",
                         "ok" if ok else "err")
        self._put({"type": "trodone", "ok": ok, "task": task})

    # ---------------- 更新 ----------------
    def update_ytdlp(self, proxy, mirror=""):
        if self.aux_kind or self.updating or self.queue.any_active():
            return {"ok": False, "busy": True,
                    "error": "有任务正在进行，请等待结束后再更新"}
        # 守卫一过立即占独占闸，缩小 save_config/find_tool 期间的 TOCTOU 窗口
        self.updating = True
        try:
            proxy = str(proxy or "").strip()
            mirror = str(mirror or "").strip()
            self.save_config({"proxy": proxy, "update_mirror": mirror})
            yt = self.cfg.get("yt_dlp_path") or find_tool("yt-dlp.exe")
            cmd = [yt, "-U"]
            if proxy:
                cmd += ["--proxy", proxy]
            self.user_stopped = False
            self._task_ev()
            self._status("更新 yt-dlp…", "run")
            self._log("> " + command_text(cmd) + "\n", "cmd")
            self._worker = threading.Thread(
                target=self._update_run, args=(cmd, proxy, mirror), daemon=True)
            self._worker.start()
        except Exception:
            self.updating = False
            raise
        return {"ok": True}

    def _update_run(self, cmd, proxy="", mirror=""):
        ok = False
        try:
            try:
                proc = spawn(cmd, core.BASE_DIR)
            except Exception as e:
                self._log("启动失败：%s\n" % e, "err")
                proc = None
            if proc is not None:
                self.proc = proc
                code = -1
                try:
                    for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                                 keep_blank=True):
                        self._log(line + "\n", None)
                    code = proc.wait()
                except Exception as e:
                    self._log("更新出错：%s\n" % e, "err")
                finally:
                    self.proc = None
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass
                ok = code == 0
            if not ok and not mirror and not self.user_stopped:
                self._log("[更新] 官方源失败；可在「设置 → 网络与加速 → 更新镜像」"
                          "填写 https://gh-proxy.com 后重试\n", "warn")
            if not ok and mirror and not self.user_stopped:
                self._log("[更新] 官方源失败，尝试镜像：%s\n" % mirror, "warn")
                self._status("更新：尝试镜像…", "run")
                ok = self._update_via_mirror(mirror, proxy)
            if ok:
                self._log("=== 更新完成，请重新探测 ===\n", "ok")
            elif self.user_stopped:
                self._log("=== 更新已停止 ===\n", "warn")
            else:
                self._log("=== 更新失败，通常是网络/代理不通 ===\n", "err")
        except Exception as e:
            self._log("更新出错：%s\n" % e, "err")
        finally:
            # 无论哪里抛异常（含镜像初始化），都要发 trodone 收回独占闸
            self._put({"type": "trodone", "ok": 1 if ok else 0, "task": "update"})

    def _update_via_mirror(self, mirror, proxy=""):
        """官方 -U 失败后的回退：镜像下载 onedir zip → 校验 → 替换 runtime。"""
        import shutil
        import tempfile
        sums_url = updater.mirror_url(mirror, updater.LATEST_URL + updater.SUMS_ASSET)
        zip_url = updater.mirror_url(mirror, updater.LATEST_URL + updater.ZIP_ASSET)
        if not sums_url or not zip_url:
            self._log("[更新] 镜像地址无效（需要 http/https 前缀）：%s\n" % mirror, "err")
            return False
        dl = tempfile.mkdtemp(prefix="ytdlp_update_dl_")
        try:
            sums_path = os.path.join(dl, updater.SUMS_ASSET)
            zip_path = os.path.join(dl, updater.ZIP_ASSET)
            self._fetch_mirror(sums_url, sums_path, proxy, "校验文件")
            with open(sums_path, encoding="utf-8", errors="replace") as f:
                want = updater.parse_checksum(f.read(), updater.ZIP_ASSET)
            if not want:
                self._log("[更新] 镜像校验文件里没有 %s\n" % updater.ZIP_ASSET, "err")
                return False
            self._log("[更新] 镜像校验值：%s…\n" % want[:12], None)
            self._fetch_mirror(zip_url, zip_path, proxy, updater.ZIP_ASSET)
            got = updater.sha256_file(zip_path)
            if got.lower() != want.lower():
                self._log("[更新] SHA256 不一致，已拒绝安装\n", "err")
                return False
            self._log("[更新] 校验通过，替换 runtime/yt-dlp.exe + _internal…\n", None)
            ok, version, backup, err = updater.install_zip(
                zip_path, core.RUNTIME_DIR, verify=updater.verify_version)
            if not ok:
                self._log("[更新] 镜像安装失败：%s\n" % err, "err")
                return False
            self._log("[更新] 镜像更新完成：%s（备份：%s）\n"
                      % (version or "未知版本", backup or "无"), "ok")
            return True
        except updater.UpdateCancelled:
            self._log("[更新] 镜像下载已停止\n", "warn")
            return False
        except Exception as e:
            self._log("[更新] 镜像更新失败：%s\n" % e, "err")
            return False
        finally:
            shutil.rmtree(dl, ignore_errors=True)

    def _fetch_mirror(self, url, dest, proxy, label):
        last = [0]

        def progress(done, total):
            if done - last[0] < 5 * 1024 * 1024 and not (total and done >= total):
                return
            last[0] = done
            if total:
                self._log("[更新] %s：%d%%\n" % (label, done * 100 // total), None)
            else:
                self._log("[更新] %s：%d MB\n" % (label, done // 1024 // 1024), None)

        try:
            updater.download(url, dest, proxy="", abort=lambda: self.user_stopped,
                             progress=progress)
            return
        except updater.UpdateCancelled:
            raise
        except Exception as e:
            if not proxy:
                raise
            self._log("[更新] 镜像直连失败（%s），改用代理重试…\n" % e, "warn")
            updater.download(url, dest, proxy=proxy, abort=lambda: self.user_stopped,
                             progress=progress)

    # ---------------- 格式表 ----------------
    def fetch_formats(self, url, cfg):
        good, _ = sanitize_urls([url])
        if not good:
            return {"ok": False, "error": "请先填写一个有效链接"}
        if not self._reserve_aux("formats"):
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
        if self.aux_kind == "formats":
            self.aux_kind = None
            self._task_ev()
        self._put({"type": "formats", "lines": out, "code": code})

    def copy_cmd(self, urls, cfg):
        good, _ = sanitize_urls(urls or [])
        cmd = core.build_download_cmd(cfg, good)
        return {"ok": True, "text": command_text(cmd)}

    # ---------------- 内嵌扩展服务 ----------------
    def load_web_server(self):
        """加载 YtDlpWeb/server.py 并缓存。

        嗅探登记表、预览与 cookie 罐都是这个模块里的纯函数，不启动 HTTP 也能用；
        扩展服务启用时复用同一个模块对象，两边看到的是同一份登记表。
        """
        if self._web_server_mod is not None:
            return self._web_server_mod
        import importlib.util
        path = os.path.join(core.BASE_DIR, "YtDlpWeb", "server.py")
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("ytdlp_web_server", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self._web_server_mod = mod
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
            # T9：扩展任务入同一队列；停止按任务作用域；状态/排队位置读 host 队列快照
            mod.set_task_sink(self.submit_server_task, stop=self.stop_ext_task,
                              queue_status=self.queue.snapshot)
            try:
                if hasattr(self.web_svc, "enable_agent"):
                    self.web_svc.enable_agent(embedded=True)
            except Exception:
                pass
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
            self._web_mod.set_task_sink(None, stop=None, queue_status=None)
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

    # ---------------- 内嵌浏览器 ----------------
    def open_embedded_browser(self, window=None, theme=""):
        try:
            from embedded import EmbeddedBrowser
        except Exception as e:
            return {"ok": False, "error": "内嵌浏览器模块不可用：%s" % e}
        if self.embedded is None:
            self.embedded = EmbeddedBrowser(self.register_embedded_sniff,
                                            self.download_embedded_sniff,
                                            self.download_embedded_page)
        return self.embedded.open(window, theme)

    def _sniff_api(self):
        """嗅探用的服务端模块：扩展服务在跑就用它，否则直接用缓存模块。

        这样内嵌浏览器不装扩展、不开 8765 也能登记/预览；扩展服务启动后
        因为共用同一个模块对象，登记表自然合并，不会出现两份列表。
        """
        if self._web_mod is not None:
            return self._web_mod
        return self.load_web_server()

    def register_embedded_sniff(self, items, page_url=""):
        """内嵌浏览器合批后的条目 → 服务端登记表（同址去重/300 条上限）。"""
        mod = self._sniff_api()
        if mod is None:
            return False
        try:
            mod.register_sniff_items(items, page_url)
            return True
        except Exception:
            return False

    def download_embedded_sniff(self, urls, page_url=""):
        """页面浮条点下载 → 只认登记表里真实存在的条目，再走同一条下载流水线。"""
        mod = self._sniff_api()
        if mod is None:
            return {"ok": False, "error": "YtDlpWeb/server.py 不可用"}
        try:
            snapshot = mod.sniff_items_json().get("items") or []
        except Exception as e:
            return {"ok": False, "error": "读取嗅探列表失败：%s" % e}
        wanted = set()
        for u in urls or []:
            u = str(u or "").strip()
            if u:
                wanted.add(u)
        items = [it for it in snapshot if str(it.get("url") or "") in wanted]
        if not items:
            return {"ok": False, "error": "这些地址不在本机嗅探列表里"}
        return self.sniff_download(items)

    def download_embedded_page(self, page_url=""):
        """浮条「下载本页」：按页面 URL 走 yt-dlp 的站点解析。

        嗅探列表里的单条 B 站地址只是 DASH 分片（视频/音频分开），直接下
        只能得到片段；页面级下载才能让 yt-dlp 找到完整格式并合并。
        """
        page = str(page_url or "").strip()
        good, _bad = core.sanitize_urls([page])
        if not good:
            return {"ok": False, "error": "当前页面地址无效"}
        page = good[0]
        if self.updating:
            return {"ok": False, "error": "正在更新 yt-dlp，请等待完成"}
        cfg = dict(self.cfg)
        # 页面级下载强制视频+音频，避免 B 站 DASH 只拿到无声视频；
        # 这条路径不保存配置，临时 cookie 罐也不会写进 config.json。
        cfg.pop("_sniff_cookie_file", None)
        cfg.update({"mode": "best", "fmt": "bv*+ba/b",
                    "thumb": False, "meta": False, "sub": False,
                    "embedsub": False})
        outdir = (cfg.get("outdir") or "").strip() or core.DOWNLOAD_DIR
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": "无法创建输出目录：%s" % e}
        if self._active_task_has(page, outdir):
            return {"ok": False, "error": "这个页面已经在下载队列里了"}
        jar = self._embedded_cookie_file({"url": page, "page_url": page,
                                          "referer": page})
        if jar:
            cfg["_sniff_cookie_file"] = jar
        cfg["referer"] = page
        cfg["_referer_explicit"] = True
        spec = {"urls": [page], "cfg": cfg, "outdir": outdir,
                "no_playlist": True, "source": "embedded-page",
                "title": "", "ext_cookie_file": jar}
        try:
            tid = self.queue.submit(spec, self._exec_download)
        except ytdlp_queue.QueueFull as e:
            self._drop_cookie_file(jar)
            return {"ok": False, "error": str(e)}
        self._put({"type": "sniff_started"})
        return {"ok": True, "task_ids": [tid]}

    def close_embedded_browser(self):
        b, self.embedded = self.embedded, None
        if b is not None:
            try:
                b.close()
            except Exception:
                pass

    # ---------------- 嗅探列表 ----------------
    def sniff_items(self):
        mod = self._sniff_api()
        if mod is None:
            return {"ok": True, "items": [],
                    "busy": bool(self.aux_kind or self.updating), "svc": False}
        d = mod.sniff_items_json()
        # svc 只表示「扩展上报通道」是否在跑；列表本身内嵌浏览器就能填，
        # 前端不能因为 svc=False 就不渲染。
        d["svc"] = self._web_mod is not None
        return d

    def sniff_clear(self):
        mod = self._sniff_api()
        if mod is not None:
            mod.clear_sniff_items()
        return {"ok": True}

    def sniff_download(self, items):
        if not items:
            return {"ok": False, "error": "先选中要下载的条目"}
        if self.updating:
            return {"ok": False, "error": "正在更新 yt-dlp，请等待完成"}
        cfg = dict(self.cfg)
        cfg.update({"fmt": "b", "thumb": False, "meta": False,
                    "sub": False, "embedsub": False})
        outdir = (cfg.get("outdir") or "").strip() or core.DOWNLOAD_DIR
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": "无法创建输出目录：%s" % e}
        ids, skipped = [], 0
        for it in items:
            if self._active_task_has(it.get("url"), outdir):
                skipped += 1
                continue
            c = dict(cfg)
            # 条目自带的源类型：直链音视频才让 build_download_cmd 上 aria2c
            # （清单/分片/小文件交给原生下载器，HLS 提速走 -N）
            cat = str(it.get("cat") or it.get("kind") or "").strip().lower()
            if cat:
                c["_sniff_cat"] = cat
            c["_sniff_seg"] = bool(it.get("seg"))
            c["_sniff_manifest"] = bool(it.get("manifest"))
            try:
                c["_sniff_size"] = int(it.get("size") or 0)
            except (TypeError, ValueError):
                c["_sniff_size"] = 0
            if len(items) == 1:
                if it.get("referer") or it.get("page_url"):
                    c["referer"] = it.get("referer") or it.get("page_url")
                    c["_referer_explicit"] = True
                if it.get("ua"):
                    c["ua"] = it["ua"]
            # 内嵌浏览器条目：把 WebView2 会话里的 cookie 写成临时罐。
            # 与扩展提交走同一套 _sniff_cookie_file 约定，任务结束时按任务清理。
            jar = ""
            if str(it.get("entry") or "").strip().lower() == "embedded":
                jar = self._embedded_cookie_file(it)
            if jar:
                c["_sniff_cookie_file"] = jar
            spec = {"urls": [it["url"]], "cfg": c, "outdir": outdir,
                    "no_playlist": True, "source": "sniff",
                    "title": str(it.get("title") or ""),
                    "ext_cookie_file": c.get("_sniff_cookie_file")}
            try:
                ids.append(self.queue.submit(spec, self._exec_download))
            except ytdlp_queue.QueueFull as e:
                self._drop_cookie_file(jar)
                return {"ok": False, "error": str(e)}
        if skipped:
            self._log("[提示] 已跳过 %d 个重复链接（相同链接已在队列中下载）\n"
                      % skipped, "warn")
        if not ids:
            return {"ok": False, "error": "这些链接已在队列中下载"}
        self._put({"type": "sniff_started"})
        return {"ok": True, "task_ids": ids}

    def _embedded_cookie_file(self, item):
        """内嵌浏览器条目 → 临时 cookie 罐路径；取不到就返回空串。

        优先媒体地址自己的 cookie，其次页面地址（不少站点的登录态只落在页面域）。
        视图没开时 export_cookies 直接返回空，退回原来的 --cookies-from-browser。
        """
        emb = self.embedded
        web = self._sniff_api()
        if emb is None or web is None or not hasattr(web, "write_cookie_jar"):
            return ""
        seen = set()
        for u in (item.get("url"), item.get("referer"), item.get("page_url")):
            u = str(u or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            try:
                jar = emb.export_cookies(u)
            except Exception:
                jar = []
            if not jar:
                continue
            try:
                return str(web.write_cookie_jar(jar, item.get("url") or u) or "")
            except Exception:
                return ""
        return ""

    @staticmethod
    def _drop_cookie_file(path):
        """任务没提交成功时也要删掉刚写的临时罐，别在临时目录留明文 cookie。"""
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def sniff_preview(self, url):
        mod = self._sniff_api()
        if mod is None:
            return {"ok": False, "error": "缺少 YtDlpWeb/server.py，无法生成预览"}
        try:
            res = mod.build_sniff_preview(url)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "未知原因"}
        out = {"ok": True, "duration": res.get("duration"),
               "thumb_data_url": "", "has_clip": False}
        if res.get("thumb"):
            path, _ = mod.preview_token_lookup(res["thumb"], "thumb")
            if path:
                try:
                    with open(path, "rb") as f:
                        out["thumb_data_url"] = ("data:image/png;base64,"
                                                 + base64.b64encode(f.read()).decode())
                except OSError:
                    pass
        if res.get("clip"):
            path, _ = mod.preview_token_lookup(res["clip"], "clip")
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
            # 退出停止：保留 .part/.aria2 供历史「继续下载」，运行中记 stopped
            self.stop_all_downloads()
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass
        self.queue.close()
        self.close_embedded_browser()
        self.stop_ext_service()
        self._stop_flag = True