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