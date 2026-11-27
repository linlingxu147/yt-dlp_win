# -*- coding: utf-8 -*-
"""本地 agent 服务的客户端：发现端口/token，提交/查询下载任务。

纯标准库；桌面版或独立服务启动后会把发现文件写到 runtime/agent_service.json。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ytdlp_core as core  # noqa: E402

API = "/api/agent/v1"
DISCOVERY_FILE = os.environ.get("YTDLP_AGENT_DISCOVERY") or core.AGENT_DISCOVERY_FILE
TERMINAL_STATES = ("done", "failed", "canceled", "paused")


class AgentError(Exception):
    def __init__(self, status, payload):
        self.status = int(status or 0)
        self.payload = payload if isinstance(payload, dict) else {
            "error": str(payload or "未知错误")}
        super().__init__(self.payload.get("error") or "agent 调用失败")


def load_discovery():
    try:
        with open(DISCOVERY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("url") or not data.get("token"):
        return None
    return data


def start_local_service(wait_seconds=8.0):
    """若没有可用服务，后台启动独立 YtDlpWeb 服务；成功后返回发现信息。"""
    found = load_discovery()
    if found:
        try:
            AgentClient(found["url"], found["token"], timeout=2).status()
            return found
        except AgentError:
            pass
    server = os.path.join(ROOT, "YtDlpWeb", "server.py")
    if not os.path.isfile(server):
        raise AgentError(0, {"error": "找不到 YtDlpWeb/server.py", "code": "service_start_failed"})
    flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [sys.executable, server], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
    except Exception as e:
        raise AgentError(0, {"error": "启动本地服务失败：%s" % e, "code": "service_start_failed"})
    deadline = time.time() + max(1.0, float(wait_seconds))
    while time.time() < deadline:
        data = load_discovery()
        if data:
            try:
                AgentClient(data["url"], data["token"], timeout=1.5).status()
                return data
            except AgentError:
                pass
        time.sleep(0.25)
    raise AgentError(0, {"error": "本地 agent 服务未能在 %.0f 秒内启动" % wait_seconds,
                         "code": "service_start_failed"})


class AgentClient:
    def __init__(self, url="", token="", timeout=10.0):
        if not url or not token:
            data = load_discovery() or {}
            url = url or data.get("url") or ""
            token = token or data.get("token") or ""
        if not url or not token:
            raise AgentError(0, {
                "error": "agent 服务未启动：请打开桌面版，或运行 "
                         "`python tools/ytdlp_agent.py service start`",
                "code": "service_not_found"})
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = float(timeout)

    def _call(self, method, path, body=None):
        data = None
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = {"error": raw.decode("utf-8", "replace")[:500]}
            raise AgentError(e.code, payload)
        except URLError as e:
            raise AgentError(0, {"error": "连不上本地 agent 服务：%s" % (e.reason,),
                                 "code": "service_unreachable"})
        except ValueError as e:
            raise AgentError(0, {"error": "服务返回的不是 JSON：%s" % e,
                                 "code": "bad_response"})

    def status(self):
        return self._call("GET", API + "/status")

    def submit(self, urls, preset="best", format="", playlist=False, title=""):
        return self._call("POST", API + "/jobs", {
            "urls": urls, "preset": preset, "format": format,
            "playlist": bool(playlist), "title": title})

    def job(self, job_id):
        return self._call("GET", API + "/jobs/" + str(job_id))

    def jobs(self, limit=20):
        return self._call("GET", API + "/jobs?limit=%d" % int(limit))

    def cancel(self, job_id):
        return self._call("POST", API + "/jobs/" + str(job_id) + "/cancel", {})

    def shutdown(self):
        return self._call("POST", API + "/shutdown", {})

    def wait(self, job_id, timeout=0.0, interval=1.0):
        deadline = time.time() + float(timeout) if timeout else None
        last = None
        while True:
            last = self.job(job_id)
            if last.get("state") in TERMINAL_STATES:
                return last
            if deadline is not None and time.time() >= deadline:
                last = dict(last)
                last["timed_out"] = True
                return last
            time.sleep(max(0.2, float(interval)))