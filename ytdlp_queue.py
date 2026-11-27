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
        self._no_purge = False
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
            paused = sum(1 for t in self._tasks.values() if t.state == "paused")
            return {"tasks": tasks, "running": running, "queued": queued,
                    "paused": paused, "max": self._max}

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
            failed = []
            with self._cv:
                while not self._closed:
                    nxt = self._next_queued()
                    if nxt is not None and self._running < self._max:
                        started = self._start_task(nxt)
                        if started is None:
                            failed.append(nxt)   # 线程没起来：已标 failed，接着试下一个
                        break
                    self._cv.wait(0.2)
                closed = self._closed
            for t in failed:
                self._safe_emit({"type": "queue", "action": "state",
                                 "task": t.snapshot()})
            if failed:
                self._emit_changed()
            if closed:
                return
            if started is not None:
                self._safe_emit({"type": "queue", "action": "state",
                                 "task": started.snapshot()})
                self._emit_changed()

    def _start_task(self, task):
        """调度锁内起 worker：线程起不来就收回槽位并把任务标 failed，调度器不退出。"""
        task.state = "running"
        task.started_at = time.time()
        self._running += 1
        try:
            threading.Thread(target=self._run_worker, args=(task,),
                             daemon=True).start()
        except Exception:
            self._running = max(0, self._running - 1)
            task.state = "failed"
            task.exit_code = -1
            task.ended_at = time.time()
            self._cv.notify_all()
            return None
        self._cv.notify_all()
        return task

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
        if canceled and not task._no_purge:
            purge_partials(task)
        self._safe_emit({"type": "queue", "action": "state", "task": task.snapshot()})
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
            t._no_purge = False
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

    def cancel(self, tid, purge=True):
        """取消任务；purge=False 保留 .part/.aria2（退出停止，供历史继续下载）。"""
        with self._cv:
            t = self._tasks.get(tid)
            if not t or t.state not in ACTIVE:
                return False
            t.cancel_requested = True
            if not purge:
                t._no_purge = True
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
            if not t._no_purge:
                purge_partials(t)
            self._emit({"type": "queue", "action": "state", "task": t.snapshot()})
            self._emit_changed()
        return True

    def stop_all(self, purge=True):
        with self._cv:
            ids = [i for i, t in self._tasks.items() if t.state in ACTIVE]
        n = 0
        for i in ids:
            if self.cancel(i, purge=purge):
                n += 1
        return n

    def remove(self, tid):
        """把一条已结束的任务从列表里摘掉（完成 / 失败 / 已取消都能删）。

        活动任务要先取消：还在排队或下载中的不能直接删，否则槽位计数与进程注册
        会对不上。只动内存里的队列，不碰磁盘——失败与停止的任务留着 .part，
        历史页「继续下载」还要靠它续传。
        """
        with self._cv:
            t = self._tasks.get(tid)
            if t is None or t.state in ACTIVE:
                return False
            self._tasks.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)
            self._cv.notify_all()
        self._safe_emit({"type": "queue", "action": "removed",
                         "task": t.snapshot()})
        self._emit_changed()
        return True

    def remove_finished(self):
        """清空所有已结束任务，返回摘掉的条数（同 remove：只清列表，不动文件）。"""
        with self._cv:
            ids = [i for i in self._order
                   if i in self._tasks and self._tasks[i].state not in ACTIVE]
            for i in ids:
                self._tasks.pop(i, None)
                self._order.remove(i)
            if ids:
                self._cv.notify_all()
        if ids:
            self._safe_emit({"type": "queue", "action": "removed_finished",
                             "count": len(ids)})
            self._emit_changed()
        return len(ids)

    def _trim_finished(self):
        finished = [i for i in self._order
                    if i in self._tasks and self._tasks[i].state not in ACTIVE]
        for i in finished[:-KEEP_FINISHED]:
            self._tasks.pop(i, None)
            self._order.remove(i)

    def _safe_emit(self, ev):
        """emit 是外部注入的回调：回调抛异常不能连带杀死调度/worker 线程。"""
        try:
            self._emit(ev)
        except Exception:
            pass

    def _emit_changed(self):
        s = self.snapshot()
        self._safe_emit({"type": "queue_changed", "running": s["running"],
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
    buf = collections.deque(maxlen=300)   # 只留末尾 300 行，长任务不把整段输出攒在内存里

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
    return code, "\n".join(buf), why


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
        # aria2c 的控制文件叫在 .part 后面：xxx.mp4.part.aria2，
        # 只查 base+".aria2" 会漏掉，取消后留下几十字节的孤儿。
        for suffix in (".part", ".aria2", ".part.aria2"):
            p = b + suffix
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(p)
            except OSError:
                pass
    return removed