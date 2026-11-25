# -*- coding: utf-8 -*-
"""桌面版宿主：事件泵 + 配置 + 历史 + 任务流水线（无 UI 依赖，可单测）。

事件出口是注入的 emit 回调（默认空操作）；main.py 把它接成 evaluate_js。
工作线程只往队列投事件，泵统一合并/节流后推出，绝不碰 UI。
"""
import json
import os
import queue
import threading
import time

import ytdlp_core as core

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

    # ---------------- 历史（Task 3 补全） ----------------
    def _load_history(self):
        return []