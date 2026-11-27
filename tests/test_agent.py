# -*- coding: utf-8 -*-
"""AI Agent 入口：本地服务 API、CLI 客户端与 MCP 工具层。

全部使用假队列/TASK_SINK，不启动 GUI、不联网、不跑 yt-dlp。
"""
import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, os.path.join(_ROOT, "YtDlpWeb"), os.path.join(_ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


server = _load("ytdlp_web_server_agent_test",
               os.path.join(_ROOT, "YtDlpWeb", "server.py"))
import agent_client  # noqa: E402
import ytdlp_agent  # noqa: E402
import ytdlp_mcp  # noqa: E402


class AgentServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = "agent-test-token"
        server.AGENT_TOKEN = self.token
        server.AGENT_JOBS.clear()
        server.AGENT_DISCOVERY_FILE = os.path.join(self.tmp.name, "agent.json")
        self._old_load = server.load_config
        server.load_config = lambda: {"outdir": os.path.join(self.tmp.name, "out")}
        self.tasks = []
        self.stops = []

        def sink(task):
            self.tasks.append(task)
            return 100 + len(self.tasks)

        def stop(tid):
            self.stops.append(tid)
            return True

        server.set_task_sink(sink, stop=stop,
                             queue_status=lambda: {"tasks": [], "running": 0,
                                                   "queued": 0, "max": 2})
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.svc = server.start_service(port=port)
        self.addCleanup(self.svc.shutdown)

    def tearDown(self):
        server.set_task_sink(None, stop=None, queue_status=None)
        server.load_config = self._old_load
        server.AGENT_JOBS.clear()

    def _request(self, method, path, body=None, token="__default__"):
        token = self.token if token == "__default__" else token
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(self.svc.url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _submit(self, urls=("https://example.com/v.mp4",), **kw):
        return self._request("POST", "/api/agent/v1/jobs", {"urls": list(urls), **kw})

    def test_auth_and_extension_routes_are_separate(self):
        code, _ = self._request("GET", "/api/agent/v1/status", token=None)
        self.assertEqual(code, 401)
        code, obj = self._request("GET", "/api/agent/v1/status")
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["api_version"], 1)
        # 扩展端点不受 token 影响（无 Origin 的本机脚本仍可读）
        code, obj = self._request("GET", "/api/status", token=None)
        self.assertEqual(code, 200)
        self.assertEqual(obj["app"], server.APP_ID)

    def test_submit_status_files_and_open_after_override(self):
        code, obj = self._submit()
        self.assertEqual(code, 200)
        job_id = obj["job_id"]
        self.assertEqual(obj["task_id"], 101)
        task = self.tasks[-1]
        self.assertEqual(task["source"], "agent")
        self.assertFalse(task["cfg"]["open_after"])
        self.assertTrue(task["no_playlist"])
        self.assertFalse(task["cfg"]["pl"])
        self.assertEqual(task["cfg"]["mode"], "best")
        self.assertEqual(task["cfg"]["merge"], "mp4")
        self.assertTrue(task["outdir"].startswith(os.path.join(self.tmp.name, "out", "agent")))
        os.makedirs(task["outdir"], exist_ok=True)
        with open(os.path.join(task["outdir"], "video.mp4"), "wb") as f:
            f.write(b"x" * 10)
        emit = task["emit"]
        emit({"type": "log", "text": "hello"})
        emit({"type": "progress", "pct": 42.0, "speed": "1MiB/s", "eta": "2s", "file": "video.mp4"})
        emit({"type": "done", "code": 0})
        code, job = self._request("GET", "/api/agent/v1/jobs/" + job_id)
        self.assertEqual(code, 200)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["pct"], 42.0)
        self.assertEqual(job["logs"][-2:], ["hello"])  # progress 不是日志
        self.assertEqual(len(job["files"]), 1)
        self.assertEqual(job["files"][0]["name"], "video.mp4")
        self.assertEqual(job["outdir"], task["outdir"])

    def test_preset_playlist_and_format(self):
        code, obj = self._submit(preset="audio", playlist=True, format="140")
        self.assertEqual(code, 200)
        task = self.tasks[-1]
        self.assertEqual(task["cfg"]["mode"], "audio_mp3")
        self.assertEqual(task["cfg"]["fmt"], "140")
        self.assertFalse(task["no_playlist"])
        self.assertTrue(task["cfg"]["pl"])
        code, obj = self._submit(format="--exec")
        self.assertEqual(code, 400)
        self.assertEqual(obj["code"], "bad_format")
        code, obj = self._submit(urls=("not a url",))
        self.assertEqual(code, 400)
        self.assertEqual(obj["code"], "bad_url")

    def test_cancel_only_own_live_job(self):
        code, obj = self._submit()
        job_id = obj["job_id"]
        code, out = self._request("POST", "/api/agent/v1/jobs/" + job_id + "/cancel", {})
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(self.stops, [101])
        self.tasks[-1]["emit"]({"type": "done", "code": 0})
        code, out = self._request("POST", "/api/agent/v1/jobs/" + job_id + "/cancel", {})
        self.assertEqual(code, 409)
        code, out = self._request("POST", "/api/agent/v1/jobs/nope/cancel", {})
        self.assertEqual(code, 404)

    def test_discovery_file_lifecycle(self):
        self.svc.enable_agent()
        path = server.AGENT_DISCOVERY_FILE
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["port"], self.svc.port)
        self.assertEqual(data["token"], self.token)
        self.assertEqual(data["url"], self.svc.url)
        self.assertFalse(data["embedded"])
        self.svc.shutdown()
        self.assertFalse(os.path.exists(path))

    def test_shutdown_rules(self):
        old = server.ACTIVE_SERVICE

        class Fake:
            agent_embedded = False

            def __init__(self):
                self.down = False

            def shutdown(self):
                self.down = True

        fake = Fake()
        server.ACTIVE_SERVICE = fake
        try:
            code, obj = server._agent_shutdown()
            self.assertEqual(code, 200)
            time.sleep(0.05)
            self.assertTrue(fake.down)
            emb = Fake()
            emb.agent_embedded = True
            server.ACTIVE_SERVICE = emb
            code, obj = server._agent_shutdown()
            self.assertEqual(code, 409)
            self.assertEqual(obj["code"], "embedded")
            self.assertFalse(emb.down)
        finally:
            server.ACTIVE_SERVICE = old

    def test_standalone_event_forwarding(self):
        calls = []
        task = server.ytdlp_queue.Task(7, {"urls": [], "emit": calls.append})
        server._mirror(task, {"type": "log", "text": "x"})
        self.assertEqual(calls[-1]["text"], "x")

    def test_standalone_submit_uses_queue(self):
        server.set_task_sink(None, stop=None, queue_status=None)
        captured = {}

        def fake_submit(spec, executor):
            captured.update(spec)
            return 55

        with mock.patch.object(server.QUEUE, "submit", side_effect=fake_submit):
            code, obj = self._submit()
        self.assertEqual(code, 200)
        self.assertEqual(obj["task_id"], 55)
        self.assertEqual(captured["source"], "agent")
        self.assertTrue(callable(captured.get("emit")))
        captured["emit"]({"type": "progress", "pct": 10.0})
        captured["emit"]({"type": "done", "code": 0})
        code, job = self._request("GET", "/api/agent/v1/jobs/" + obj["job_id"])
        self.assertEqual(code, 200)
        self.assertEqual(job["state"], "done")

    def test_client_discovers_written_file(self):
        self.svc.enable_agent()
        old = agent_client.DISCOVERY_FILE
        agent_client.DISCOVERY_FILE = server.AGENT_DISCOVERY_FILE
        try:
            data = agent_client.load_discovery()
            self.assertIsNotNone(data)
            client = agent_client.AgentClient()
            self.assertTrue(client.status()["ok"])
        finally:
            agent_client.DISCOVERY_FILE = old

    def test_client_helpers(self):
        client = agent_client.AgentClient(url=self.svc.url, token=self.token)
        sub = client.submit(["https://example.com/v"])
        self.assertTrue(sub["ok"])
        job = client.wait(sub["job_id"], timeout=2, interval=0.2)
        self.assertIn(job["state"], ("queued", "running"))
        code, out = self._request("POST", "/api/agent/v1/jobs/" + sub["job_id"] + "/cancel", {})
        self.assertEqual(code, 200)
        list_out = client.jobs()
        self.assertEqual(list_out["jobs"][0]["job_id"], sub["job_id"])

    def test_cli_submit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ytdlp_agent.main(["--url", self.svc.url, "--token", self.token,
                                   "download", "https://example.com/v"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["job_id"])

    def test_mcp_protocol(self):
        self.assertEqual(ytdlp_mcp.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}), None)
        init = ytdlp_mcp.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], ytdlp_mcp.SERVER_NAME)
        tools = ytdlp_mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools["result"]["tools"]}
        self.assertIn("download_video", names)
        self.assertIn("job_status", names)

        class FakeClient:
            def submit(self, urls, **kw):
                return {"ok": True, "job_id": "j1", "state": "queued"}

        called = ytdlp_mcp.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "download_video",
                       "arguments": {"urls": ["https://example.com/v"]}}},
            client_factory=lambda: FakeClient())
        self.assertFalse(called["result"]["isError"])
        payload = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(payload["job_id"], "j1")


if __name__ == "__main__":
    unittest.main(verbosity=2)