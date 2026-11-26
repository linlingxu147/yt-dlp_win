# -*- coding: utf-8 -*-
"""共享下载队列内核：Task + QueueManager + 统一进程泵。

核心只拥有槽位计数、FIFO 调度、状态机、进程注册与暂停/取消信号；
执行差异（自愈、历史、通知）由调用方提供的 executor 承担。
"""
import collections
import os
import threading
import time

import ytdlp_core as core

MAX_PENDING = 200
KEEP_FINISHED = 50
LOG_RING = 500
ACTIVE = ("queued", "running", "paused")


class QueueFull(Exception):
    pass


class Task:
    def __init__(self, tid, spec):
        self.id = tid
        self.source = spec.get("source", "gui")
        self.urls = list(spec.get("urls") or [])
        self.cfg = dict(spec.get("cfg") or {})
        self.outdir = spec.get("outdir") or core.DOWNLOAD_DIR
        self.no_playlist = bool(spec.get("no_playlist", False))
        self.title = spec.get("title") or (self.urls[0] if self.urls else "")
        self.state = "queued"
        self.pstate = core.new_progress_state()
        self.log_ring = collections.deque(maxlen=LOG_RING)
        self.exit_code = None
        self.proc = None
        self.created_at = time.time()
        self.started_at = None
        self.ended_at = None
        self.pause_requested = False
        self.cancel_requested = False
        self.net_tried = set()
        self.net_name = "normal"
        self.diag_buf = ""
        self.cookie_warned = False
        self.ext_cookie_file = spec.get("ext_cookie_file")
        self.ext_emit = spec.get("emit")

    def snapshot(self):
        return {"id": self.id, "source": self.source, "title": self.title,
                "urls": self.urls, "state": self.state, "outdir": self.outdir,
                "pct": self.pstate.get("pct", 0.0),
                "speed": self.pstate.get("speed", ""),
                "eta": self.pstate.get("eta", ""),
                "size": self.pstate.get("size", ""),
                "file": self.pstate.get("file", ""),
                "exit_code": self.exit_code,
                "created_at": self.created_at, "started_at": self.started_at,
                "ended_at": self.ended_at}


class QueueManager:
    def __init__(self, max_concurrent=2, emit=None):
        self._cv = threading.Condition()
        self._max = max(1, min(5, int(max_concurrent)))
        self._emit = emit or (lambda ev: None)
        self._tasks = {}
        self._order = []
        self._next_id = 0
        self._running = 0
        self._closed = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---- 查询 ----
    def get(self, tid):
        with self._cv:
            return self._tasks.get(tid)

    def any_active(self):
        with self._cv:
            return any(t.state in ACTIVE for t in self._tasks.values())

    def snapshot(self):
        with self._cv:
            tasks = [self._tasks[i].snapshot() for i in self._order
                     if i in self._tasks]
            running = sum(1 for t in self._tasks.values() if t.state == "running")
            queued = sum(1 for t in self._tasks.values() if t.state == "queued")
            return {"tasks": tasks, "running": running, "queued": queued,
                    "max": self._max}

    def set_max(self, n):
        with self._cv:
            self._max = max(1, min(5, int(n)))
            self._cv.notify_all()

    # ---- 提交 ----
    def submit(self, spec, executor):
        with self._cv:
            pending = sum(1 for t in self._tasks.values() if t.state in ACTIVE)
            if pending >= MAX_PENDING:
                raise QueueFull("队列已满（%d 个未完成任务）" % MAX_PENDING)
            self._next_id += 1
            task = Task(self._next_id, spec)
            task.executor = executor
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._cv.notify_all()
        self._emit({"type": "queue", "action": "added", "task": task.snapshot()})
        self._emit_changed()
        return task.id

    # ---- 调度 ----
    def _loop(self):
        while True:
            started = None
            with self._cv:
                while not self._closed:
                    nxt = self._next_queued()
                    if nxt is not None and self._running < self._max:
                        nxt.state = "running"
                        nxt.started_at = time.time()
                        self._running += 1
                        threading.Thread(target=self._run_worker,
                                         args=(nxt,), daemon=True).start()
                        self._cv.notify_all()
                        started = nxt
                        break
                    self._cv.wait(0.2)
                if self._closed:
                    return
            if started is not None:
                self._emit({"type": "queue", "action": "state",
                            "task": started.snapshot()})
                self._emit_changed()

    def _next_queued(self):
        for i in self._order:
            t = self._tasks.get(i)
            if t is not None and t.state == "queued":
                return t
        return None

    def _run_worker(self, task):
        try:
            task.executor(task)
        except Exception:
            task.exit_code = -1
        finally:
            self._finalize(task)

    def _finalize(self, task):
        with self._cv:
            if task.state == "running":
                self._running = max(0, self._running - 1)
            if task.cancel_requested:
                task.state = "canceled"
            elif task.pause_requested:
                task.state = "paused"
            elif task.exit_code == 0:
                task.state = "done"
            else:
                task.state = "failed"
            task.ended_at = time.time()
            canceled = task.state == "canceled"
            self._trim_finished()
            self._cv.notify_all()
        if canceled:
            purge_partials(task)
        self._emit({"type": "queue", "action": "state", "task": task.snapshot()})
        self._emit_changed()

    # ---- 暂停 / 继续 / 取消 ----
    def pause(self, tid):
        with self._cv:
            t = self._tasks.get(tid)
            if not t or t.state not in ("queued", "running"):
                return False
            t.pause_requested = True
            if t.state == "queued":
                t.state = "paused"
                t.ended_at = time.time()
                self._cv.notify_all()
                paused_now = True
            else:
                paused_now = False
            proc = t.proc
        if proc is not None:
            core.kill_tree(proc)
        if paused_now:
            self._emit({"type": "queue", "action": "state", "task": t.snapshot()})
            self._emit_changed()
        return True

    def resume(self, tid):
        with self._cv:
            t = self._tasks.get(tid)
            if not t or t.state != "paused":
                return False
            t.pause_requested = False
            t.cancel_requested = False
            t.net_tried = set()
            t.net_name = "normal"
            t.exit_code = None
            t.state = "queued"
            if tid in self._order:
                self._order.remove(tid)
            self._order.insert(0, tid)
            self._cv.notify_all()
        self._emit({"type": "queue", "action": "state", "task": t.snapshot()})
        self._emit_changed()
        return True

    def cancel(self, tid):
        with self._cv:
            t = self._tasks.get(tid)
            if not t or t.state not in ACTIVE:
                return False
            t.cancel_requested = True
            direct = t.state in ("queued", "paused")
            if direct:
                t.state = "canceled"
                t.ended_at = time.time()
                self._trim_finished()
                self._cv.notify_all()
            proc = t.proc
        if proc is not None:
            core.kill_tree(proc)
        if direct:
            purge_partials(t)
            self._emit({"type": "queue", "action": "state", "task": t.snapshot()})
            self._emit_changed()
        return True

    def stop_all(self):
        with self._cv:
            ids = [i for i, t in self._tasks.items() if t.state in ACTIVE]
        n = 0
        for i in ids:
            if self.cancel(i):
                n += 1
        return n

    def _trim_finished(self):
        finished = [i for i in self._order
                    if i in self._tasks and self._tasks[i].state not in ACTIVE]
        for i in finished[:-KEEP_FINISHED]:
            self._tasks.pop(i, None)
            self._order.remove(i)

    def _emit_changed(self):
        s = self.snapshot()
        self._emit({"type": "queue_changed", "running": s["running"],
                    "queued": s["queued"]})

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()


def run_process(task, cmd, cwd, on_line, stall_seconds=300):
    """统一进程泵：起进程、逐行回调、注册 task.proc，暂停/取消可中断。

    返回 (code, tail, why)：code 退出码（启动失败为 None）、tail 末尾 300 行、
    why 为 iter_lines_watch 的原因字典（spawn 失败时 {"reason": "spawn"}）。
    """
    why = {}
    try:
        proc = core.spawn(cmd, cwd)
    except Exception:
        return None, "", {"reason": "spawn"}
    task.proc = proc
    buf = []

    def abort():
        return task.pause_requested or task.cancel_requested

    try:
        for line in core.iter_lines_watch(proc, abort=abort,
                                          stall_seconds=stall_seconds, why=why):
            buf.append(line)
            on_line(line)
    finally:
        code = proc.wait() if proc else -1
        task.proc = None
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
    return code, "\n".join(buf[-300:]), why


def purge_partials(task):
    """取消时清理该任务的 .part / .aria2 残留（按已知输出路径，避免误删他人文件）。"""
    removed = []
    bases = set()
    raw = task.pstate.get("raw_path") or ""
    if raw:
        bases.add(raw)
    f = task.pstate.get("file") or ""
    if f:
        bases.add(os.path.join(task.outdir, f))
    for d in task.pstate.get("done") or ():
        bases.add(str(d))
    for b in bases:
        for suffix in (".part", ".aria2"):
            p = b + suffix
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(p)
            except OSError:
                pass
    return removed