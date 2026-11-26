# -*- coding: utf-8 -*-
import os
import sys
import tempfile
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
    """可控 executor：每次调用 block 直到 release() 或 pause/cancel 请求。

    模拟真实 run_process 的行为：轮询 pause_requested/cancel_requested 提前返回；
    门闸按调用隔离，release() 只放行当时在等的调用（不影响之后启动的任务）。
    """
    def __init__(self):
        self.running = []
        self.finished = []
        self._gate = threading.Event()  # 兼容 SecondGateExec 的首个门闸
        self._gates = {}
        self.lock = threading.Lock()

    def __call__(self, task):
        gate = threading.Event()
        with self.lock:
            self.running.append(task.id)
            self._gates[task.id] = gate
        end = time.time() + 5
        while not gate.wait(0.02):
            if task.pause_requested or task.cancel_requested:
                break
            if time.time() >= end:
                break
        with self.lock:
            self.running.remove(task.id)
            self.finished.append(task.id)
            self._gates.pop(task.id, None)
        task.exit_code = 0

    def release(self):
        self._gate.set()
        with self.lock:
            for g in self._gates.values():
                g.set()


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


class SecondGateExec(FakeExec):
    """首个任务受 release() 控制，后续任务受 release_second() 控制。"""
    def __init__(self):
        super().__init__()
        self._gate2 = threading.Event()
        self._calls = 0

    def __call__(self, task):
        with self.lock:
            self.running.append(task.id)
            self._calls += 1
            first = self._calls == 1
        (self._gate if first else self._gate2).wait(5)
        with self.lock:
            self.running.remove(task.id)
            self.finished.append(task.id)
        task.exit_code = 0

    def release_second(self):
        self._gate2.set()


class TestSlotRecycle(unittest.TestCase):
    """槽位回收：_finalize 递减 _running，排队任务在槽位释放后接管。"""
    def setUp(self):
        self.events = []
        self.mgr = q.QueueManager(max_concurrent=1, emit=self.events.append)
        self.exe = SecondGateExec()

    def tearDown(self):
        self.exe.release()
        self.exe.release_second()
        self.mgr.close()

    def wait_until(self, pred, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_start_emits_running_state_event(self):
        a = self.mgr.submit(spec(), self.exe)
        self.assertTrue(self.wait_until(lambda: any(
            e.get("type") == "queue" and e.get("action") == "state"
            and e["task"]["id"] == a and e["task"]["state"] == "running"
            for e in self.events)))
        self.assertTrue(any(e.get("type") == "queue_changed"
                            and e.get("running") == 1 for e in self.events))

    def test_slot_freed_and_reused(self):
        a = self.mgr.submit(spec("http://e/a"), self.exe)
        b = self.mgr.submit(spec("http://e/b"), self.exe)
        self.assertTrue(self.wait_until(
            lambda: self.mgr.snapshot()["running"] == 1))
        with self.exe.lock:
            self.assertEqual(list(self.exe.running), [a])
        self.assertEqual(self.mgr.snapshot()["queued"], 1)
        self.exe.release()
        # 首个任务完成：_finalize 置 done 并递减 _running
        self.assertTrue(self.wait_until(
            lambda: self.mgr.get(a).state == "done"))
        # 槽位复用：第二个任务接管唯一槽位（不递减 _running 的内核会在此超时失败）
        self.assertTrue(self.wait_until(
            lambda: self.mgr.snapshot()["running"] == 1
            and self.mgr.get(b).state == "running"))
        with self.exe.lock:
            self.assertEqual(list(self.exe.running), [b])
        snap = self.mgr.snapshot()
        self.assertEqual(snap["running"], 1)
        self.assertEqual(snap["queued"], 0)
        self.exe.release_second()
        self.assertTrue(self.wait_until(
            lambda: self.mgr.get(b).state == "done"))
        self.assertTrue(self.wait_until(
            lambda: self.mgr.snapshot()["running"] == 0))
        self.assertEqual(self.mgr.snapshot()["queued"], 0)
        self.assertFalse(self.mgr.any_active())


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
        # 等 executor 真正跑完（提交瞬间还是 queued，按 state=="running" 等会直接跳过）
        end = time.time() + 5
        while time.time() < end and "code" not in code_box:
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

    def test_spawn_failure(self):
        task = q.Task(1, spec())
        code, tail, why = q.run_process(
            task, [os.path.join(tempfile.gettempdir(), "no-such-exe-t4.exe")],
            None, lambda l: None)
        self.assertIsNone(code)
        self.assertEqual(tail, "")
        self.assertEqual(why, {"reason": "spawn"})
        self.assertIsNone(task.proc)

    def test_tail_keeps_last_300_lines(self):
        task = q.Task(1, spec())
        lines = []
        cmd = [sys.executable, "-c",
               "import sys;[sys.stdout.write('L%d\\n'%i) for i in range(350)]"]
        code, tail, why = q.run_process(task, cmd, None, lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 350)
        self.assertEqual(tail.split("\n"), ["L%d" % i for i in range(50, 350)])
        self.assertIsNone(task.proc)

    def test_cancel_running_ends_canceled(self):
        """运行中取消必须落到 canceled（不是 done/failed）：真实子进程 + run_process。"""
        mgr = q.QueueManager(max_concurrent=1)
        started = threading.Event()

        def exe(task):
            cmd = [sys.executable, "-c",
                   "import time,sys;sys.stdout.write('go\\n');sys.stdout.flush();time.sleep(30)"]
            code, tail, why = q.run_process(task, cmd, None,
                                            lambda l: started.set())
            task.exit_code = code

        tid = mgr.submit(spec(), exe)
        try:
            self.assertTrue(started.wait(5))
            self.assertEqual(mgr.get(tid).state, "running")
            mgr.cancel(tid)
            end = time.time() + 5
            while time.time() < end and mgr.get(tid).state == "running":
                time.sleep(0.02)
            self.assertEqual(mgr.get(tid).state, "canceled")
        finally:
            mgr.close()


class TestNoPurgeCancel(unittest.TestCase):
    """退出语义（spec §4）：purge=False 的取消/全部停止保留 .part/.aria2 供续传。"""

    def _running_task(self, mgr, tmp, started):
        def exe(task):
            task.pstate["file"] = "v.mp4"
            started.set()
            end = time.time() + 5
            while time.time() < end:
                if task.pause_requested or task.cancel_requested:
                    task.exit_code = -1
                    return
                time.sleep(0.02)
            task.exit_code = 0
        return mgr.submit(spec(outdir=tmp), exe)

    def _wait_canceled(self, mgr, tid, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            t = mgr.get(tid)
            if t is not None and t.state == "canceled":
                return
            time.sleep(0.02)
        self.fail("任务 %s 未在超时内取消" % tid)

    def test_stop_all_no_purge_keeps_running_partials(self):
        mgr = q.QueueManager(max_concurrent=1)
        started = threading.Event()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                part = os.path.join(tmp, "v.mp4.part")
                with open(part, "wb") as f:
                    f.write(b"x")
                tid = self._running_task(mgr, tmp, started)
                self.assertTrue(started.wait(5))
                self.assertEqual(mgr.stop_all(purge=False), 1)
                self._wait_canceled(mgr, tid)
                self.assertTrue(os.path.isfile(part))
        finally:
            mgr.close()

    def test_cancel_no_purge_keeps_partials_default_still_purges(self):
        mgr = q.QueueManager(max_concurrent=1)
        exe = FakeExec()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                a = mgr.submit(spec(outdir=tmp), exe)
                b = mgr.submit(spec(outdir=tmp), exe)
                c = mgr.submit(spec(outdir=tmp), exe)
                end = time.time() + 3
                while time.time() < end and mgr.get(a).state != "running":
                    time.sleep(0.02)
                for t, name in ((mgr.get(b), "b.mp4"), (mgr.get(c), "c.mp4")):
                    t.pstate["file"] = name
                    with open(os.path.join(tmp, name + ".part"), "wb") as f:
                        f.write(b"x")
                mgr.cancel(b, purge=False)      # 直落路径（queued/paused）
                mgr.cancel(c)                   # 默认仍清理
                self.assertEqual(mgr.get(b).state, "canceled")
                self.assertEqual(mgr.get(c).state, "canceled")
                self.assertTrue(os.path.isfile(os.path.join(tmp, "b.mp4.part")))
                self.assertFalse(os.path.exists(os.path.join(tmp, "c.mp4.part")))
        finally:
            exe.release()
            mgr.close()


class TestPurgePartials(unittest.TestCase):
    def test_removes_only_task_partials(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = q.Task(1, spec(outdir=tmp))
            raw = os.path.join(tmp, "video.mp4")
            task.pstate["raw_path"] = raw
            for p in (raw + ".part", raw + ".aria2",
                      os.path.join(tmp, "other.part")):
                with open(p, "wb") as f:
                    f.write(b"x")
            removed = q.purge_partials(task)
            self.assertFalse(os.path.exists(raw + ".part"))
            self.assertFalse(os.path.exists(raw + ".aria2"))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "other.part")))
            self.assertEqual(sorted(removed),
                             sorted([raw + ".part", raw + ".aria2"]))