# 多任务下载队列 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让桌面版与独立扩展服务支持并发下载队列（默认 2，可配 1–5），逐链接任务、暂停/继续/取消、按任务日志与历史。

**Architecture:** 新增共享内核 `ytdlp_queue.py`（Task + QueueManager + 统一进程泵 run_process）。核心只管槽位/调度/状态机/进程生命周期；执行差异由 host（富功能）与 server（简化）各自的 executor 承担。桌面扩展任务经 TASK_SINK 入同一队列；独立服务自建队列。接口字段只增不删。

**Tech Stack:** Python 3 标准库（threading/queue/subprocess）、pywebview 6 + 原生 ES 模块前端（零构建）、stdlib unittest。

**Spec:** `docs/superpowers/specs/2026-11-26-download-queue-design.md`（上游路线图 `2026-11-26-idm-like-roadmap.md`）

## Global Constraints

- 测试命令：`py -3 -m unittest discover -s tests -v`（标准库 unittest，**不用 pytest**）；扩展冒烟 `node tests/extension_smoke.js`。
- 测试端口避开 8765–8775（用 8799 等）；host/下载测试必须 `open_after=False`，禁止弹资源管理器。
- `YtDlpWeb/server.py` 接口**只增字段不删**，保持旧扩展兼容；`sniffer_extension/sniff_rules.js` 判定规则不改。
- `ytdlp_core.py` 仅新增 `queue_n` 默认值与校验，不动既有下载命令构建行为。
- 前端零构建、原生 ES 模块、WebView2 Baseline；进度推送 ≤5 次/秒（按任务）。
- **提交政策：未经用户明确要求不提交、不推送。** 各任务末尾的「提交」步骤仅在用户明确要求时执行，提交信息用中文。
- 队列上限：未完成任务 ≥200 拒绝（409）；内存保留活跃任务 + 最近 50 条已结束任务。
- 暂停保留 `.part`/`.aria2` 并释放槽位；恢复插队首、自愈阶梯从 normal 重开；取消经 UI 确认后清理 `.part`/`.aria2`。

## File Structure

| 文件 | 责任 | 动作 |
|------|------|------|
| `ytdlp_queue.py` | Task / QueueManager / run_process / purge_partials | 新建 |
| `ytdlp_core.py` | `queue_n` 默认值 + `_INT_FIELDS` 校验 | 修改（983/1090 附近） |
| `YtDlpGUI/host.py` | 队列实例、per-task executor、辅助分闸、更新独占、历史/退出 | 修改 |
| `YtDlpGUI/api.py` | pause/resume/cancel/stop_all/queue_snapshot 桥接 | 修改 |
| `YtDlpWeb/server.py` | 独立队列、TASK_SINK task_id、QUEUE_STATUS、接口增字段、移除 Runner | 修改 |
| `YtDlpGUI/ui/js/queue.js` | 任务列表渲染 + 行内控制 + 日志选择 | 新建 |
| `YtDlpGUI/ui/js/app.js` | 事件接线改按任务、工具栏闸门 | 修改 |
| `YtDlpGUI/ui/js/log.js` | 按任务日志缓冲与切换 | 修改 |
| `YtDlpGUI/ui/js/download.js` | FIELDS 加 queue_n | 修改 |
| `YtDlpGUI/ui/js/history.js` | canceled 徽标 + 继续下载 | 修改 |
| `YtDlpGUI/ui/index.html` | 任务列表容器 + 设置项 | 修改 |
| `sniffer_extension/background.js` | 提交后显示排队位置 | 修改（最小） |
| `tests/test_queue.py` | 队列内核单测（fake executor） | 新建 |
| `tests/test_core.py` / `test_hardening.py` / `test_server.py` | 适配 + 新增用例 | 修改 |

---

### Task 1: queue_n 配置默认值与校验

**Files:**
- Modify: `ytdlp_core.py:983-1000`（DEFAULT_CONFIG）、`ytdlp_core.py:1090-1091`（_INT_FIELDS）
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `core.DEFAULT_CONFIG["queue_n"] == "2"`；`coerce_types` 把 `queue_n` 校正为 1–5 的字符串。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_core.py`）

```python
class TestQueueNConfig(unittest.TestCase):
    def test_default_queue_n(self):
        self.assertEqual(core.DEFAULT_CONFIG.get("queue_n"), "2")

    def test_coerce_queue_n_range(self):
        m = {"queue_n": "3"}
        core.coerce_types(m)
        self.assertEqual(m["queue_n"], "3")

    def test_coerce_queue_n_out_of_range_falls_back(self):
        m = {"queue_n": "9"}
        warns = []
        core.coerce_types(m, warns)
        self.assertEqual(m["queue_n"], "2")
        self.assertTrue(any("同时下载数" in w for w in warns))
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_core.TestQueueNConfig -v`
Expected: FAIL（KeyError / 断言不成立）

- [ ] **Step 3: 实现**

`DEFAULT_CONFIG` 中 `"conc": True, "conc_n": "4", ...` 一行附近新增键：

```python
    "queue_n": "2",
```

`_INT_FIELDS` 增加一项：

```python
_INT_FIELDS = {"retries": ("重试次数", 0, 999), "conc_n": ("分片并发", 1, 64),
               "socket_timeout": ("超时秒数", 1, 600),
               "queue_n": ("同时下载数", 1, 5)}
```

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest tests.test_core.TestQueueNConfig -v`
Expected: PASS（3 个）

- [ ] **Step 5: 全量回归**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 既有用例全绿。

- [ ] **Step 6: 提交（仅用户明确要求时）**

```bash
git add ytdlp_core.py tests/test_core.py
git commit -m "配置新增 queue_n（同时下载数，1-5 默认 2）"
```

---

### Task 2: 队列内核 — Task 与准入调度

**Files:**
- Create: `ytdlp_queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Consumes: `core.new_progress_state`、`core.DOWNLOAD_DIR`
- Produces:
  - `Task(tid, spec)`，字段见代码；`Task.snapshot() -> dict`
  - `QueueManager(max_concurrent=2, emit=None)`
  - `.submit(spec, executor) -> int`（spec 键：`urls, cfg, outdir, no_playlist, source, title, emit, ext_cookie_file`；executor 为 `callable(task)`）
  - `.snapshot() -> {"tasks":[snapshot...], "running":int, "queued":int, "max":int}`
  - `.get(id) -> Task|None`、`.any_active() -> bool`、`.set_max(n)`、`.close()`
  - 常量 `MAX_PENDING=200`、`KEEP_FINISHED=50`、`LOG_RING=500`
  - 事件：`{"type":"queue","action":"added|state","task":snapshot}`、`{"type":"queue_changed","running":n,"queued":m}`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_queue.py`）

```python
# -*- coding: utf-8 -*-
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytdlp_queue as q


def spec(url="http://example.com/a", **kw):
    d = {"urls": [url], "cfg": {}, "outdir": "", "source": "gui"}
    d.update(kw)
    return d


class FakeExec:
    """可控 executor：block 直到 release()，记录运行中的任务。"""
    def __init__(self):
        self.running = []
        self.finished = []
        self._gate = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, task):
        with self.lock:
            self.running.append(task.id)
        self._gate.wait(5)
        with self.lock:
            self.running.remove(task.id)
            self.finished.append(task.id)
        task.exit_code = 0

    def release(self):
        self._gate.set()


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.mgr = q.QueueManager(max_concurrent=2, emit=self.events.append)
        self.exe = FakeExec()

    def tearDown(self):
        self.exe.release()
        self.mgr.close()

    def wait_running(self, n, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if self.mgr.snapshot()["running"] >= n:
                return True
            time.sleep(0.02)
        return False

    def test_fifo_and_cap(self):
        ids = [self.mgr.submit(spec("http://e/%d" % i), self.exe) for i in range(4)]
        self.assertTrue(self.wait_running(2))
        snap = self.mgr.snapshot()
        self.assertEqual(snap["running"], 2)
        self.assertEqual(snap["queued"], 2)
        self.assertEqual(sorted(self.exe.running), sorted(ids[:2]))

    def test_submit_returns_incrementing_ids(self):
        a = self.mgr.submit(spec(), self.exe)
        b = self.mgr.submit(spec(), self.exe)
        self.assertEqual(b, a + 1)

    def test_pending_cap_200(self):
        for i in range(q.MAX_PENDING):
            self.mgr.submit(spec("http://e/%d" % i), self.exe)
        with self.assertRaises(q.QueueFull):
            self.mgr.submit(spec("http://e/overflow"), self.exe)

    def test_set_max_grows_immediately(self):
        for i in range(4):
            self.mgr.submit(spec("http://e/%d" % i), self.exe)
        self.assertTrue(self.wait_running(2))
        self.mgr.set_max(4)
        self.assertTrue(self.wait_running(4))

    def test_events_emitted(self):
        self.mgr.submit(spec(), self.exe)
        self.assertTrue(self.wait_running(1))
        self.assertTrue(any(e.get("type") == "queue" for e in self.events))
        self.assertTrue(any(e.get("type") == "queue_changed" for e in self.events))
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_queue -v`
Expected: FAIL（ModuleNotFoundError: ytdlp_queue）

- [ ] **Step 3: 实现内核（准入/调度部分）**

新建 `ytdlp_queue.py`：

```python
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
                        break
                    self._cv.wait(0.2)
                if self._closed:
                    return

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
            self._trim_finished()
            self._cv.notify_all()
        self._emit({"type": "queue", "action": "state", "task": task.snapshot()})
        self._emit_changed()

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
```

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest tests.test_queue -v`
Expected: PASS（5 个）

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add ytdlp_queue.py tests/test_queue.py
git commit -m "新增共享队列内核：Task 与 FIFO 准入调度"
```

---

### Task 3: 队列内核 — 暂停/继续/取消/stop_all

**Files:**
- Modify: `ytdlp_queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Produces: `.pause(id)->bool`、`.resume(id)->bool`、`.cancel(id)->bool`、`.stop_all()->int`、`purge_partials(task)->list`
- 语义：暂停 running 任务 → 杀进程、worker 收尾置 paused、释放槽位；暂停 queued → 直接 paused。resume 置 queued 并移到队首、清 pause/cancel 标志、自愈上下文重置。cancel 置 cancel_requested 并杀进程；queued/paused 直接收尾 canceled。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_queue.py`）

```python
class TestPauseResumeCancel(unittest.TestCase):
    def setUp(self):
        self.mgr = q.QueueManager(max_concurrent=1)
        self.exe = FakeExec()

    def tearDown(self):
        self.exe.release()
        self.mgr.close()

    def wait_state(self, tid, state, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            t = self.mgr.get(tid)
            if t and t.state == state:
                return True
            time.sleep(0.02)
        return False

    def test_pause_running_frees_slot(self):
        a = self.mgr.submit(spec("http://e/a"), self.exe)
        b = self.mgr.submit(spec("http://e/b"), self.exe)
        self.assertTrue(self.wait_state(a, "running"))
        self.mgr.pause(a)
        self.assertTrue(self.wait_state(a, "paused"))
        self.assertTrue(self.wait_state(b, "running"))

    def test_resume_goes_to_front(self):
        a = self.mgr.submit(spec("http://e/a"), self.exe)
        b = self.mgr.submit(spec("http://e/b"), self.exe)
        c = self.mgr.submit(spec("http://e/c"), self.exe)
        self.assertTrue(self.wait_state(a, "running"))
        self.mgr.pause(a)
        self.assertTrue(self.wait_state(a, "paused"))
        self.assertTrue(self.wait_state(b, "running"))
        self.mgr.resume(a)
        self.exe.release()
        self.assertTrue(self.wait_state(b, "done"))
        # a 插队首，应先于 c 运行
        self.assertTrue(self.wait_state(a, "running"))
        self.assertEqual(self.mgr.get(c).state, "queued")

    def test_cancel_queued(self):
        a = self.mgr.submit(spec("http://e/a"), self.exe)
        b = self.mgr.submit(spec("http://e/b"), self.exe)
        self.assertTrue(self.wait_state(a, "running"))
        self.mgr.cancel(b)
        self.assertTrue(self.wait_state(b, "canceled"))

    def test_stop_all(self):
        for i in range(3):
            self.mgr.submit(spec("http://e/%d" % i), self.exe)
        n = self.mgr.stop_all()
        self.assertGreaterEqual(n, 1)
        end = time.time() + 3
        while time.time() < end and self.mgr.any_active():
            time.sleep(0.02)
        self.assertFalse(self.mgr.any_active())
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_queue.TestPauseResumeCancel -v`
Expected: FAIL（AttributeError: pause）

- [ ] **Step 3: 实现**（在 `QueueManager` 内追加方法；`purge_partials` 为模块函数）

```python
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
                if t.state == "running":
                    self._running = max(0, self._running - 1)
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
```

模块级函数：

```python
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
```

并在 `_finalize` 中：`canceled` 分支收尾后调用 `purge_partials(task)`（运行中被取消的情况）。

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest tests.test_queue -v`
Expected: PASS（全部）

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add ytdlp_queue.py tests/test_queue.py
git commit -m "队列内核：暂停释放槽位、恢复插队首、取消清理残留"
```

---

### Task 4: 统一进程泵 run_process

**Files:**
- Modify: `ytdlp_queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Produces: `run_process(task, cmd, cwd, on_line, stall_seconds=300) -> (code, tail, why)`
  - `code`：退出码；启动失败返回 `None`
  - `tail`：末尾 300 行拼接；`why`：`iter_lines_watch` 的原因字典
  - abort 条件：`task.pause_requested or task.cancel_requested`

- [ ] **Step 1: 写失败测试**（追加；用真实子进程打印行）

```python
class TestRunProcess(unittest.TestCase):
    def test_collects_lines_and_code(self):
        mgr = q.QueueManager(max_concurrent=1)
        lines = []
        code_box = {}

        def exe(task):
            cmd = [sys.executable, "-c",
                   "import sys;[sys.stdout.write('L%d\\n'%i) or sys.stdout.flush() for i in range(3)]"]
            code, tail, why = q.run_process(task, cmd, None, lines.append)
            code_box["code"] = code

        tid = mgr.submit(spec(), exe)
        end = time.time() + 5
        while time.time() < end and mgr.get(tid).state == "running":
            time.sleep(0.02)
        mgr.close()
        self.assertEqual(code_box.get("code"), 0)
        self.assertEqual(len(lines), 3)

    def test_pause_aborts(self):
        mgr = q.QueueManager(max_concurrent=1)
        started = threading.Event()

        def exe(task):
            cmd = [sys.executable, "-c",
                   "import time,sys;sys.stdout.write('go\\n');sys.stdout.flush();time.sleep(30)"]
            q.run_process(task, cmd, None, lambda l: started.set())

        tid = mgr.submit(spec(), exe)
        self.assertTrue(started.wait(5))
        mgr.pause(tid)
        end = time.time() + 5
        while time.time() < end and mgr.get(tid).state != "paused":
            time.sleep(0.02)
        mgr.close()
        self.assertEqual(mgr.get(tid).state, "paused")
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_queue.TestRunProcess -v`
Expected: FAIL（AttributeError: run_process）

- [ ] **Step 3: 实现**（模块级函数）

```python
def run_process(task, cmd, cwd, on_line, stall_seconds=300):
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
    return code, "\n".join(buf[-300:]), why
```

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest tests.test_queue -v`
Expected: PASS（全部）

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add ytdlp_queue.py tests/test_queue.py
git commit -m "队列内核：统一进程泵 run_process（暂停/取消中断）"
```

---

### Task 5: host 接入队列（M1，无暂停 UI）

**Files:**
- Modify: `YtDlpGUI/host.py`（`__init__`、`start_download`、`_begin_download`/`_run`/`_handle_line`/`_net_heal`/`on_done` 重构为 per-task executor、`pump_once`、`_task_ev`、`submit_server_task`/`start_external`、`sniff_download`、`shutdown`）
- Modify: `YtDlpGUI/api.py`（新增 `queue_snapshot`）
- Test: `tests/test_hardening.py`、`tests/test_core.py`（host 相关用例适配）

**Interfaces:**
- Consumes: Task 2–4 的 `QueueManager`/`run_process`
- Produces:
  - `host.queue`（QueueManager，`max_concurrent=int(cfg.get("queue_n") or 2)`，`emit=self._put`）
  - `host.start_download(urls, cfg, no_playlist=False) -> {"ok":True,"task_ids":[...]}`
  - `host._exec_download(task)`：per-task executor
  - `host.aux_kind`（detect/trouble/formats 单闸）、`host.updating`（bool）
  - 事件 `task` 改为 `{"type":"task","kind":aux或update,"busy":bool,"downloading":bool}`
  - `api.queue_snapshot() -> {"ok":True, **host.queue.snapshot()}`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_hardening.py`；用 fake executor 注入避免真实下载）

```python
class TestHostQueue(unittest.TestCase):
    def make_host(self, tmp):
        import importlib
        host_mod = importlib.import_module("host")
        cfg = os.path.join(tmp, "config.json")
        core.save_config(cfg, {"outdir": tmp, "open_after": False, "queue_n": "2"})
        return host_mod.Host(cfg)

    def test_start_download_splits_urls(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            h = self.make_host(tmp)
            calls = []
            h.queue.submit = lambda spec, exe: calls.append(spec) or len(calls)
            r = h.start_download(["http://e/a", "http://e/b"], {"outdir": tmp})
            self.assertTrue(r["ok"])
            self.assertEqual(len(r["task_ids"]), 2)
            self.assertEqual(len(calls), 2)
            h.queue.close()
```

注：`host` 模块导入需把 `YtDlpGUI` 加入 sys.path（测试文件顶部已有或补充 `sys.path.insert(0, .../YtDlpGUI)`）。

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_hardening.TestHostQueue -v`
Expected: FAIL（start_download 仍返回旧结构 / 无 queue）

- [ ] **Step 3: 实现 host 改造**

host.py 模块顶层（`import ytdlp_core as core` 之后）新增 `import ytdlp_queue`（**必须模块级**，`start_download` 会引用 `ytdlp_queue.QueueFull`）。`__init__` 末尾新增（保留旧字段供辅助操作过渡，下载相关字段移入 Task）：

```python
        self.queue = ytdlp_queue.QueueManager(
            max_concurrent=int(str(self.cfg.get("queue_n") or "2")),
            emit=self._put)
        self.aux_kind = None
        self.updating = False
        self._open_after_ts = {}
```

`start_download` 改为逐链接入队（保留原有校验/save_config）：

```python
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
        self.save_config(cfg)
        ids = []
        try:
            for u in good:
                spec = {"urls": [u], "cfg": dict(cfg), "outdir": outdir,
                        "no_playlist": no_playlist, "source": "gui"}
                ids.append(self.queue.submit(spec, self._exec_download))
        except ytdlp_queue.QueueFull as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "task_ids": ids}
```

per-task executor（把原 `_begin_download/_run/_handle_line/_net_heal/on_done` 的逻辑搬入，全部用 `task.*` 替代 `self.dl_*`/`self.pstate` 等）：

```python
    def _exec_download(self, task):
        cfg = task.cfg
        task._extra = {}
        warns = []
        while True:
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
            if code is None:
                self._task_log(task, "启动失败\n", "err")
                task.exit_code = -1
                return
            if why.get("reason") == "stall":
                self._task_log(task, "[长时间没有输出，已自动终止本次任务]\n", "warn")
            if task.pause_requested or task.cancel_requested:
                task.exit_code = code
                return
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
            if not task.cookie_warned:
                task.cookie_warned = True
                self._task_log(task, "[提示] 浏览器 Cookies 数据库被占用，已忽略 Cookies 继续\n", "warn")
            return
        self._task_log(task, line + "\n", classify_line(line))

    def _task_heal(self, task, cfg, tail):
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
        if task.ext_cookie_file and os.path.isfile(task.ext_cookie_file):
            try:
                os.remove(task.ext_cookie_file)
            except OSError:
                pass
        if code == 0:
            self._task_log(task, "=== 完成 · 耗时 %d 秒 ===\n" % int(elapsed), "ok")
            self._maybe_open_after(task)
        else:
            self._task_log(task, "=== 结束，退出码 %s ===\n" % code, "err")
        self._put({"type": "done", "code": code, "task_id": task.id,
                   "elapsed": round(elapsed, 1)})

    def _maybe_open_after(self, task):
        if not task.cfg.get("open_after"):
            return
        d = task.outdir
        now = time.time()
        if now - self._open_after_ts.get(d, 0) < 10:
            return
        self._open_after_ts[d] = now
        self.open_outdir(d)
```

`pump_once`：移除 `if t == "done" and self.running: self.on_done(...)`（完成已在 executor 内处理）；`exttask` 分支保留但 `start_external` 改为入队；log 批量合并改为**按 task_id 分组**发出 `{"type":"logbatch","task_id":id,"items":[...]}`（无 task_id 的辅助日志归入 `task_id=0`）。`_task_ev` 改为：

```python
    def _task_ev(self):
        kind = self.aux_kind or ("update" if self.updating else None)
        self._put({"type": "task", "kind": kind, "busy": bool(kind),
                   "downloading": self.queue.any_active()})
```

`submit_server_task`/`start_external`：改为构造 spec（含 `emit`/`ext_cookie_file`/`source="ext"`）并 `self.queue.submit(spec, self._exec_download)`，返回 task_id（truthy 兼容）；`sniff_download` 同理逐条入队、`source="sniff"`。`shutdown` 用 `self.queue.stop_all()` 替代 `self.stop()` 的下载部分。

`api.py` 新增：

```python
    def queue_snapshot(self):
        return {"ok": True, **self.host.queue.snapshot()}
```

- [ ] **Step 4: 运行确认通过 + 适配旧用例**

Run: `py -3 -m unittest tests.test_hardening.TestHostQueue -v` → PASS
把 `test_hardening.py`/`test_core.py` 中依赖 `host.task_kind=="download"`、`host.dl_urls`、单任务 `on_done` 的用例改为按 `host.queue` 断言（例如 aria2c 降级用例改为检查 `build_download_cmd` 输出不变 + 队列提交成功）。
Run: `py -3 -m unittest discover -s tests -v` → 全绿。

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add YtDlpGUI/host.py YtDlpGUI/api.py tests/test_hardening.py tests/test_core.py
git commit -m "桌面接入下载队列：逐链接任务与 per-task 执行器"
```

---

### Task 6: 辅助操作分闸 + 更新独占

**Files:**
- Modify: `YtDlpGUI/host.py`（`detect`/`troubleshoot`/`fetch_formats`/`update_ytdlp`/`_reserve`/`stop`）
- Test: `tests/test_hardening.py`

**Interfaces:**
- Produces: `host._reserve_aux(kind)->bool`（aux_kind 单闸）；`update_ytdlp` 在 `queue.any_active()` 或 `aux_kind` 时拒绝；下载在 `updating` 时拒绝（Task 5 已含）。

- [ ] **Step 1: 写失败测试**

```python
class TestAuxGate(unittest.TestCase):
    def test_update_blocked_while_downloading(self):
        import tempfile, importlib
        host_mod = importlib.import_module("host")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            core.save_config(cfg, {"outdir": tmp, "open_after": False})
            h = host_mod.Host(cfg)
            h.queue.submit({"urls": ["http://e/a"], "cfg": {"outdir": tmp},
                            "outdir": tmp, "source": "gui"}, lambda t: None)
            r = h.update_ytdlp("", "")
            self.assertFalse(r["ok"])
            h.queue.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_hardening.TestAuxGate -v`
Expected: FAIL

- [ ] **Step 3: 实现**

新增 `_reserve_aux`：

```python
    def _reserve_aux(self, kind):
        if self.aux_kind or self.updating:
            return False
        self.aux_kind = kind
        return True
```

`detect`/`troubleshoot`/`fetch_formats`：把 `if self.task_kind: return self._busy()` 与 `_reserve(kind)` 改为 `_reserve_aux(kind)`；完成回调（`on_detect_done`/`on_trodone`/formats 收尾）把 `self.task_kind=None` 改为 `self.aux_kind=None`，并去掉 `_release_runner()`（下载不再占 RUNNER）。这些辅助线程仍可用 `self.proc`/`self.user_stopped`（与下载任务互不干扰，下载进程在 task.proc）。

`update_ytdlp`：

```python
    def update_ytdlp(self, proxy, mirror=""):
        if self.aux_kind or self.updating or self.queue.any_active():
            return {"ok": False, "error": "有任务正在进行，请等待结束后再更新"}
        ...
        self.updating = True
        self._task_ev()
        ...
```

`on_trodone(task=="update")` 收尾置 `self.updating=False`。`stop()`：无下载单任务时，若 `aux_kind`/`updating` 则置 `user_stopped=True` 停辅助；否则返回 False。

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest discover -s tests -v` → 全绿。

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add YtDlpGUI/host.py tests/test_hardening.py
git commit -m "辅助操作与下载分闸，更新 yt-dlp 独占"
```

---

### Task 7: host 暂停/继续/取消 API + 退出确认

**Files:**
- Modify: `YtDlpGUI/host.py`、`YtDlpGUI/api.py`、`YtDlpGUI/main.py`（on_closing 文案）
- Test: `tests/test_hardening.py`

**Interfaces:**
- Produces: `api.pause_task(id)/resume_task(id)/cancel_task(id)/stop_all()`，均返回 `{"ok":bool}`；`host.queue` 对应方法。

- [ ] **Step 1: 写失败测试**

```python
class TestTaskControls(unittest.TestCase):
    def test_pause_resume_cancel_api(self):
        import tempfile, importlib
        host_mod = importlib.import_module("host")
        api_mod = importlib.import_module("api")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            core.save_config(cfg, {"outdir": tmp, "open_after": False})
            h = host_mod.Host(cfg)
            a = api_mod.Api(h)
            gate = threading.Event()
            tid = h.queue.submit({"urls": ["http://e/a"], "cfg": {"outdir": tmp},
                                  "outdir": tmp, "source": "gui"},
                                 lambda t: gate.wait(5))
            self.assertTrue(a.pause_task(tid)["ok"])
            self.assertTrue(a.resume_task(tid)["ok"])
            self.assertTrue(a.cancel_task(tid)["ok"])
            gate.set()
            h.queue.close()
```

（测试文件顶部需 `import threading`。）

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_hardening.TestTaskControls -v`
Expected: FAIL（AttributeError: pause_task）

- [ ] **Step 3: 实现**

`api.py`：

```python
    def pause_task(self, tid):
        return {"ok": self.host.queue.pause(int(tid))}

    def resume_task(self, tid):
        return {"ok": self.host.queue.resume(int(tid))}

    def cancel_task(self, tid):
        return {"ok": self.host.queue.cancel(int(tid))}

    def stop_all(self):
        return {"ok": True, "stopped": self.host.queue.stop_all()}
```

`main.py` `on_closing`：

```python
    def on_closing():
        snap = host.queue.snapshot()
        if snap["running"] or snap["queued"]:
            host._put({"type": "exit_confirm",
                       "text": "%d 个下载中 / %d 个排队，退出将停止全部任务。"
                               % (snap["running"], snap["queued"])})
            return False
        host.shutdown()
        return True
```

（前端 exit_confirm 对话框文案改用 `ev.text`，见 Task 10。）

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest discover -s tests -v` → 全绿。

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add YtDlpGUI/host.py YtDlpGUI/api.py YtDlpGUI/main.py tests/test_hardening.py
git commit -m "任务暂停/继续/取消 API 与退出确认计数"
```

---

### Task 8: server 独立模式队列 + 接口增字段（M4 前半）

**Files:**
- Modify: `YtDlpWeb/server.py`（移除 `Runner`/`RUNNER`，`run_download`/`_download_attempt` 改 per-task，`/api/sniff` 独立分支入队，`/api/status`、`/api/sniff/log`、`/api/stop` 增字段）
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `ytdlp_queue`
- Produces:
  - 模块级 `QUEUE = ytdlp_queue.QueueManager(max_concurrent=..., emit=...)`（独立模式）
  - `server_executor(task)`：复用自愈阶梯
  - `POST /api/sniff` 响应增 `task_id`、`queue_position`
  - `GET /api/status` 增 `tasks`、`running`、`queued`（保留 `busy/kind/elapsed`）
  - `POST /api/stop` 支持 body/query `id`；不传停最近扩展任务
  - `QUEUE_STATUS` 钩子（桌面模式上报 host 快照）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_server.py`，端口用 8799）

```python
class TestServerQueueFields(unittest.TestCase):
    def test_status_has_queue_fields(self):
        # 启动独立服务（pick_port(8799)），GET /api/status
        data = self.get_json("/api/status")
        self.assertIn("tasks", data)
        self.assertIn("running", data)
        self.assertIn("queued", data)
        self.assertIn("busy", data)  # 旧字段保留
```

（沿用该文件已有的服务启动/请求辅助；若无则用 `server.start_service(port=8799)` + `urllib.request`。）

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_server.TestServerQueueFields -v`
Expected: FAIL（KeyError: tasks）

- [ ] **Step 3: 实现**

移除 `Runner` 类与 `RUNNER`。新增：

```python
import ytdlp_queue

def _server_executor(task):
    cfg = task.cfg
    tried, name, extra = set(), "normal", {}
    while True:
        c = cfg if not extra else dict(cfg, **extra)
        warns = []
        cmd = build_download_cmd(c, task.urls, no_playlist=task.no_playlist, warn=warns)
        for w in warns:
            _mirror(task, {"type": "log", "level": "warn", "text": w})
        _mirror(task, {"type": "cmd", "text": command_text(cmd)})
        code, tail, why = ytdlp_queue.run_process(
            task, cmd, task.outdir, lambda ln: _server_line(task, ln))
        if code is None:
            task.exit_code = -1
            return
        if task.pause_requested or task.cancel_requested:
            task.exit_code = code
            return
        if code == 0:
            task.exit_code = 0
            _mirror(task, {"type": "done", "code": 0})
            return
        tried.add(name)
        nxt = next_cookie_plan(tail, tried, cfg)
        if not nxt and cfg.get("net_hard", True) and is_net_failure(tail):
            nxt = next_net_plan(tail, tried, cfg, url=task.urls[0])
        if not nxt:
            task.exit_code = code
            _mirror(task, {"type": "done", "code": code})
            return
        name, extra = nxt

QUEUE = ytdlp_queue.QueueManager(max_concurrent=2)
LAST_EXT_TASK = {"id": None}
QUEUE_STATUS = None  # 桌面模式由 host 注入快照回调
```

`_mirror(task, obj)`：把事件写入 `SNIFF_STATE`（仅当 `task.id == LAST_EXT_TASK["id"]`，沿用 token 隔离）；`_server_line` 复用 `progress_event`/`classify_line` 生成 progress/log 镜像。

`/api/sniff` 独立分支（`TASK_SINK is None`）：

```python
            try:
                tid = QUEUE.submit({"urls": [url], "cfg": cfg, "outdir": cfg.get("outdir") or core.DOWNLOAD_DIR,
                                    "no_playlist": True, "source": "ext", "title": str(body.get("title") or "")[:200],
                                    "ext_cookie_file": cfg.get("_sniff_cookie_file")}, _server_executor)
            except ytdlp_queue.QueueFull as e:
                _drop_cookie(cfg)
                return self._json({"ok": False, "busy": True, "error": str(e)}, 409)
            LAST_EXT_TASK["id"] = tid
            with SNIFF_LOCK:
                SNIFF_STATE.update({"lines": [], "prog": None, "done": False, "code": None,
                                    "token": tid, "url": url, "title": str(body.get("title") or "")[:200], "kind": kind})
            pos = QUEUE.snapshot()["queued"]
            return self._json({"ok": True, "busy": False, "url": url, "task_id": tid, "queue_position": pos})
```

`/api/status`：

```python
        if p == "/api/status":
            snap = QUEUE_STATUS() if QUEUE_STATUS else QUEUE.snapshot()
            first = next((t for t in snap["tasks"] if t["state"] == "running"), None)
            return self._json({
                "app": APP_ID, "version": APP_VERSION,
                "busy": bool(first), "kind": "download" if first else None,
                "elapsed": round(time.time() - first["started_at"], 1) if first and first.get("started_at") else 0,
                "tasks": snap["tasks"], "running": snap["running"], "queued": snap["queued"],
            })
```

`/api/stop`：

```python
        if p == "/api/stop":
            tid = body.get("id") or parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").get("id", [None])[0]
            if QUEUE_STATUS is not None and STOP_HOOK is not None:
                ok = STOP_HOOK(int(tid) if tid else None)
            elif tid:
                ok = QUEUE.cancel(int(tid))
            else:
                ok = QUEUE.cancel(LAST_EXT_TASK["id"]) if LAST_EXT_TASK["id"] else False
            return self._json({"ok": bool(ok)})
```

`sniff_log_json` 增 `"task_id": SNIFF_STATE.get("token")`，`busy` 改用 `QUEUE.any_active()`（桌面模式用 QUEUE_STATUS）。

- [ ] **Step 4: 运行确认通过 + 适配旧 server 用例**

把 `test_server.py` 中 `RUNNER`/单任务 409 用例改为队列表述（第二个任务被接收而非 409；停止只停指定任务）。
Run: `py -3 -m unittest discover -s tests -v` → 全绿。

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add YtDlpWeb/server.py tests/test_server.py
git commit -m "独立扩展服务接入队列，接口增 task_id/队列字段（向后兼容）"
```

---

### Task 9: server 桌面模式接线（TASK_SINK task_id + QUEUE_STATUS）

**Files:**
- Modify: `YtDlpWeb/server.py`（`/api/sniff` 桌面分支、`set_task_sink`）、`YtDlpGUI/host.py`（`start_ext_service` 注入 QUEUE_STATUS、`submit_server_task` 返回 task_id、`stop` 支持 id）
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: Task 5 `host.queue`、Task 8 `QUEUE_STATUS`
- Produces: `set_task_sink(sink, stop=None, queue_status=None)`；`TASK_SINK(task)->task_id|None`；`STOP_HOOK(id|None)`。

- [ ] **Step 1: 写失败测试**

```python
class TestDesktopSink(unittest.TestCase):
    def test_sink_returns_task_id(self):
        import importlib
        server = importlib.import_module("server")  # 需 sys.path 含 YtDlpWeb
        seen = {}
        def sink(task):
            seen["task"] = task
            return 42
        server.set_task_sink(sink, stop=lambda tid=None: True, queue_status=lambda: {"tasks": [], "running": 0, "queued": 0})
        self.assertEqual(server.TASK_SINK({"url": "http://e/a"}), 42)
        self.assertIsNotNone(server.QUEUE_STATUS)
        server.set_task_sink(None, stop=None, queue_status=None)
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_server.TestDesktopSink -v`
Expected: FAIL（set_task_sink 不接受 queue_status）

- [ ] **Step 3: 实现**

`set_task_sink`：

```python
def set_task_sink(sink, stop=None, queue_status=None):
    global TASK_SINK, STOP_HOOK, QUEUE_STATUS
    TASK_SINK, STOP_HOOK, QUEUE_STATUS = sink, stop, queue_status
```

`/api/sniff` 桌面分支：`tid = TASK_SINK(task)`；`if not tid: ...409`；成功响应增 `"task_id": tid, "queue_position": ...`（位置可由 QUEUE_STATUS 快照计算或省略为 0）。`SNIFF_STATE["token"]=tid`。

`host.start_ext_service`：`mod.set_task_sink(self.submit_server_task, stop=self.stop, queue_status=self.queue.snapshot)`。
`host.submit_server_task(task)`：构造 spec 入队返回 task_id（Task 5 已改）；`host.stop(tid=None)`：`tid` 给定则 `queue.cancel(tid)`，否则 `queue.stop_all()`（供 STOP_HOOK）。

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest discover -s tests -v` → 全绿。

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add YtDlpWeb/server.py YtDlpGUI/host.py tests/test_server.py
git commit -m "桌面模式扩展任务入同一队列，停止按任务作用域"
```

---

### Task 10: 前端任务列表 + 日志切换 + 设置项（M3）

**Files:**
- Create: `YtDlpGUI/ui/js/queue.js`
- Modify: `YtDlpGUI/ui/index.html`（任务列表容器、设置 queue_n、exit_confirm 文案）、`app.js`、`log.js`、`download.js`、`history.js`
- Test: 真实入口 UI 复测（Task 12）

**Interfaces:**
- Consumes: `api.queue_snapshot/pause_task/resume_task/cancel_task/stop_all`；事件 `queue/queue_changed/progress(task_id)/log(task_id)/done(task_id)/task`
- Produces: `initQueue({dialogs})` 返回 `{select(id)}`；`log.js` 按 task_id 缓冲。

- [ ] **Step 1: index.html 结构**

把下载页「下载进度」单块替换为：

```html
<div class="group">
  <div class="row">
    <strong>下载队列</strong>
    <span id="queue-stat" class="hint">0 下载中 · 0 排队</span>
    <button type="button" id="btn-stop-all">全部停止</button>
  </div>
  <div id="queue-list" role="list" aria-label="下载任务列表"></div>
</div>
```

设置页 ⑦ 网络与加速新增：

```html
<label>同时下载数 <input id="queue_n" type="number" min="1" max="5" value="2"></label>
```

- [ ] **Step 2: download.js FIELDS 增项**

```javascript
  conc: ['conc', 'bool'], conc_n: ['conc_n', 'text'],
  queue_n: ['queue_n', 'text'],
```

- [ ] **Step 3: 新建 queue.js**

```javascript
import { call, on } from './bridge.js';

const STATE = { queued: '排队', running: '下载中', paused: '已暂停',
                done: '完成', failed: '失败', canceled: '已取消' };
let selected = null;
let onSelect = () => {};

function row(t) {
  const el = document.createElement('div');
  el.className = 'task-row';
  el.dataset.id = t.id;
  el.setAttribute('role', 'listitem');
  const title = document.createElement('span');
  title.className = 'ellipsis';
  title.textContent = t.title || (t.urls || [])[0] || ('任务 ' + t.id);
  title.title = title.textContent;
  const badge = document.createElement('span');
  badge.className = 'badge ' + t.state;
  badge.textContent = STATE[t.state] || t.state;
  const bar = document.createElement('progress');
  bar.max = 100; bar.value = Math.round(t.pct || 0);
  const detail = document.createElement('span');
  detail.className = 'hint';
  detail.textContent = [t.speed, t.eta, t.size].filter(Boolean).join(' · ') || '—';
  const ops = document.createElement('span');
  const mk = (label, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.addEventListener('click', async (e) => { e.stopPropagation(); await fn(); });
    return b;
  };
  if (t.state === 'running') ops.append(mk('暂停', () => call('pause_task', t.id)));
  if (t.state === 'paused' || t.state === 'queued') ops.append(mk('继续', () => call('resume_task', t.id)));
  if (['queued', 'running', 'paused'].includes(t.state))
    ops.append(mk('取消', async () => {
      const ok = await window.__confirm('取消任务', '取消将删除已下载的部分文件，确定？', '取消任务');
      if (ok) await call('cancel_task', t.id);
    }));
  el.append(title, badge, bar, detail, ops);
  el.addEventListener('click', () => { selected = t.id; onSelect(t.id); render(); });
  if (t.id === selected) el.classList.add('selected');
  return el;
}

let cache = [];
function render() {
  const box = document.getElementById('queue-list');
  box.textContent = '';
  if (!cache.length) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.textContent = '还没有任务';
    box.appendChild(empty);
    return;
  }
  for (const t of cache) box.appendChild(row(t));
}

export function initQueue({ dialogs }) {
  window.__confirm = dialogs.confirm;
  onSelect = (id) => window.__selectLog && window.__selectLog(id);
  document.getElementById('btn-stop-all').addEventListener('click', async () => {
    const ok = await dialogs.confirm('全部停止', '停止所有运行中任务并清空排队？', '全部停止');
    if (ok) await call('stop_all');
  });
  on('queue_changed', ev => {
    document.getElementById('queue-stat').textContent =
      `${ev.running} 下载中 · ${ev.queued} 排队`;
  });
  on('queue', async () => {
    const r = await call('queue_snapshot');
    cache = r.tasks || [];
    if (selected == null) {
      const run = cache.find(t => t.state === 'running');
      if (run) { selected = run.id; onSelect(run.id); }
    }
    render();
  });
  on('progress', ev => {
    const t = cache.find(x => x.id === ev.task_id);
    if (!t) return;
    Object.assign(t, { pct: ev.pct, speed: ev.speed, eta: ev.eta, size: ev.size, file: ev.file });
    const el = document.querySelector(`.task-row[data-id="${ev.task_id}"]`);
    if (el) {
      el.querySelector('progress').value = Math.round(ev.pct || 0);
      el.querySelector('.hint').textContent = [ev.speed, ev.eta, ev.size].filter(Boolean).join(' · ') || '—';
    }
  });
  call('queue_snapshot').then(r => { cache = r.tasks || []; render(); });
  return { select: (id) => { selected = id; render(); } };
}
```

- [ ] **Step 4: log.js 按任务缓冲**

```javascript
import { on } from './bridge.js';
const MAX = 4000;
const buffers = new Map();   // task_id -> [{text,tag}]
let current = null;
let box = null, progEl = null;

function paint(items) {
  box.textContent = ''; progEl = null;
  for (const it of items) appendDom(it.text, it.tag);
}
function appendDom(text, tag) {
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  if (tag === 'prog') {
    if (!progEl) { progEl = document.createElement('div'); progEl.className = 'prog'; box.appendChild(progEl); }
    progEl.textContent = text.replace(/\n$/, '');
  } else {
    if (progEl) { progEl.remove(); progEl = null; }
    const div = document.createElement('div');
    if (tag) div.className = tag;
    div.textContent = text.replace(/\n$/, '');
    box.appendChild(div);
  }
  while (box.childElementCount > MAX) box.removeChild(box.firstChild);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}
export function initLog() {
  box = document.getElementById('log');
  window.__selectLog = (id) => { current = id; paint(buffers.get(id) || []); };
  on('logbatch', ev => {
    const id = ev.task_id ?? current ?? 0;
    const arr = buffers.get(id) || [];
    for (const it of ev.items) {
      arr.push(it);
      if (id === current) appendDom(it.text, it.tag);
    }
    while (arr.length > MAX) arr.shift();
    buffers.set(id, arr);
  });
  document.getElementById('btn-clearlog').addEventListener('click', () => {
    buffers.set(current ?? 0, []); paint([]);
  });
  return { clear: () => { buffers.clear(); paint([]); } };
}
```

注：host `_task_log` 发出的 log 事件需带 `task_id`，且 `pump_once` 的 logbatch 合并要保留 `task_id`（修改 `pump_once`：log 事件按 task_id 分组批量发出 `{"type":"logbatch","task_id":id,"items":[...]}`）。

- [ ] **Step 5: app.js 接线**

- `import { initQueue } from './queue.js';` 并 `initQueue({ dialogs });`
- `on('task', ...)` 改为：

```javascript
on('task', ev => {
  document.getElementById('btn-start').disabled = ev.kind === 'update';
  document.getElementById('btn-detect').disabled = ev.busy;
  document.getElementById('btn-trouble').disabled = ev.busy;
  document.getElementById('btn-formats').disabled = ev.busy;
  document.getElementById('btn-update').disabled = ev.busy || ev.downloading;
  document.getElementById('btn-stop').disabled = !ev.downloading;
});
```

- `btn-stop` 改为 `call('stop_all')`；移除旧 `on('progress')`/`on('done')` 对单进度条的写入（由 queue.js 接管）；`on('done')` 保留触发历史刷新与失败 toast：

```javascript
on('done', ev => {
  if (ev.code !== 0) dialogs.toast('任务 ' + (ev.task_id ?? '') + ' 失败或中断，见日志', 'err');
});
```

- `btn-start` 成功提示改为 `已加入 ${res.task_ids.length} 个任务`。
- exit_confirm 对话框文案用 `ev.text`（dialogs.js 对应处理）。

- [ ] **Step 6: history.js canceled + 继续**

```javascript
const STATUS = { ok: '成功', failed: '失败', stopped: '已停止', canceled: '已取消' };
```

对 `failed`/`stopped` 记录额外加「继续下载」按钮（调用 `history_redownload`，复用 `.part`）。

- [ ] **Step 7: 真实入口冒烟**

启动 `启动.bat`，粘贴 2 条本地限速源链接，确认任务列表出现两行、进度各自更新、点击行切换日志、暂停/继续/取消可用。`open_after` 测试时关闭。

- [ ] **Step 8: 提交（仅用户明确要求时）**

```bash
git add YtDlpGUI/ui YtDlpGUI/host.py
git commit -m "前端任务队列列表、按任务日志与同时下载数设置"
```

---

### Task 11: 扩展排队位置提示 + 冒烟更新

**Files:**
- Modify: `sniffer_extension/background.js`（提交响应处理）、`sniffer_extension/manifest.json`（版本号小升）
- Test: `tests/extension_smoke.js`

**Interfaces:**
- Consumes: `POST /api/sniff` 新字段 `task_id`/`queue_position`
- Produces: 扩展面板提示「已加入队列，第 N 位」。

- [ ] **Step 1: 写失败断言**（在 `tests/extension_smoke.js` 增一项：模拟响应含 queue_position 时文案包含「队列」）

- [ ] **Step 2: 运行确认失败**

Run: `node tests/extension_smoke.js`
Expected: 新断言 FAIL

- [ ] **Step 3: 实现**

`background.js` 提交成功回调：

```javascript
const pos = resp.queue_position;
const msg = pos ? `已加入队列，第 ${pos} 位` : '已开始下载';
```

（保留旧字段读取，`resp.task_id` 透传备用。）`manifest.json` version 1.10.1 → 1.11.0。

- [ ] **Step 4: 运行确认通过**

Run: `node tests/extension_smoke.js`
Expected: 全绿（含新断言）

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add sniffer_extension tests/extension_smoke.js
git commit -m "扩展提示排队位置，版本升至 1.11.0"
```

---

### Task 12: 全量回归 + 真实入口验收（M5）

**Files:**
- Test: 全部

- [ ] **Step 1: 全量单测**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 全绿。

- [ ] **Step 2: 扩展冒烟**

Run: `node tests/extension_smoke.js`
Expected: 全绿。

- [ ] **Step 3: 真实入口验收清单**（限速本地源，`open_after=False`，端口 8799）

1. 3 个链接并发：任务列表三行，进度/日志各自正确
2. 暂停一个任务 ≥5 分钟后继续：`.part`/`.aria2` 续传成功
3. 扩展连提 2 个任务：被接收（运行或排队）而非 409；扩展「停止」不影响 GUI 任务
4. 下载中点「更新 yt-dlp」被拒；探测/格式表可并行
5. （2026-11-26 修订：当前分支无播放态实现，跳过边下边播项）退出确认显示「N 下载中 / M 排队」
6. 取消任务后 `.part` 被清理；历史出现 canceled 记录

- [ ] **Step 4: 提醒用户重载**

报告里写明：完全退出桌面版再启动；扩展页重新加载 + 页面 F5。

- [ ] **Step 5: 提交（仅用户明确要求时）**

```bash
git add -A
git commit -m "多任务下载队列：全量回归与真实验收通过"
```

---

## Self-Review 记录

- **Spec 覆盖**：内存队列（无持久化任务）✔；并发 1–5 默认 2（Task 1/2）✔；逐链接任务（Task 5）✔；共享 queue_core（Task 2–4）✔；辅助并行/更新独占（Task 6）✔；暂停/继续/取消语义（Task 3/7）✔；server 增字段兼容（Task 8/9）✔；UI 列表/日志/设置/历史（Task 10）✔；扩展排队提示（Task 11）✔；测试与验收（各 Task + 12）✔。
- **占位符**：无 TBD/TODO；所有代码步骤含实际代码。
- **类型一致**：`QueueManager.submit(spec, executor)`、`run_process(task, cmd, cwd, on_line, stall_seconds)`、`pause/resume/cancel/stop_all/set_max/snapshot/get/any_active/close`、`Task.snapshot()` 字段在 Task 2–10 一致；`task_id` 事件字段贯穿 host/server/UI。
- **已知衔接点**：Task 5 需同步修改 `pump_once` 的 logbatch 按 task_id 分组（Task 10 Step 4 注明）；`_exec_download` 使用 `task._extra`（在 `_task_heal` 中赋值，初始为 `{}`，需在 Task 5 实现时于 executor 开头 `task._extra = {}`）。