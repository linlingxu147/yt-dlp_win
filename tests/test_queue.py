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