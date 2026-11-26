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