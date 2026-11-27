# -*- coding: utf-8 -*-
"""鱿鱼浏览器工具包的 MCP 工具服务（stdio JSON-RPC）。

由支持 MCP 的 AI 客户端启动；工具调本地 agent 服务，不依赖 GUI/视觉。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "YtDlpWeb")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_client import (  # noqa: E402
    AgentClient, AgentError, load_discovery, start_local_service,
)

SERVER_NAME = "ytdlp-agent"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "download_video",
        "description": ("提交视频/播放列表下载，立即返回 job_id。任务进入本机 yt-dlp "
                        "队列，默认输出到 downloads/agent/<job_id>/，不会弹资源管理器。"
                        "用 job_status 或 wait_for_job 查询进度与最终文件路径。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                         "description": "视频、播放列表或直链媒体地址"},
                "preset": {"type": "string", "enum": ["best", "audio"],
                           "default": "best"},
                "format": {"type": "string",
                           "description": "可选 yt-dlp 格式选择器，如 137+140"},
                "playlist": {"type": "boolean", "default": False,
                             "description": "是否允许下载整个播放列表"},
                "title": {"type": "string", "description": "任务标题，仅用于展示"},
            },
            "required": ["urls"],
        },
    },
    {
        "name": "job_status",
        "description": "查询下载任务的进度、错误提示与已完成文件列表。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "wait_for_job",
        "description": ("等待下载任务进入终态（done/failed/canceled/paused）。"
                        "返回最终状态与文件路径；超时返回 timed_out=true，任务仍在后台。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 60,
                                    "minimum": 1, "maximum": 600},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "cancel_job",
        "description": "取消自己提交的下载任务；不会影响用户或其他来源的任务。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "list_jobs",
        "description": "列出最近的 AI 下载任务。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20,
                                     "minimum": 1, "maximum": 100}},
        },
    },
    {
        "name": "service_status",
        "description": "查看本地下载服务是否可用、队列是否繁忙。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_service",
        "description": ("没有桌面版时，后台启动一个 headless 本地下载服务，"
                        "并写入 runtime/agent_service.json。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_service",
        "description": ("停止由本工具/CLI 启动的 headless 服务；"
                        "桌面内嵌的服务会拒绝，请关闭桌面版。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _tool_result(payload, is_error=False):
    text = json.dumps(payload, ensure_ascii=False)
    out = {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


def _call_tool(name, args, factory):
    if name == "start_service":
        data = start_local_service()
        return {"ok": True, "url": data.get("url"), "port": data.get("port"),
                "pid": data.get("pid"), "api_version": data.get("api_version")}
    if name == "stop_service":
        data = load_discovery()
        if not data:
            return {"ok": False, "error": "没有可用的 agent 服务",
                    "code": "service_not_found"}
        if data.get("embedded"):
            return {"ok": False, "error": "服务由桌面版管理，请关闭桌面版",
                    "code": "embedded"}
        return AgentClient(data["url"], data["token"]).shutdown()
    client = factory()
    if name == "download_video":
        return client.submit(args.get("urls") or [], preset=args.get("preset") or "best",
                            format=args.get("format") or "",
                            playlist=bool(args.get("playlist")),
                            title=args.get("title") or "")
    if name == "job_status":
        return client.job(args.get("job_id") or "")
    if name == "wait_for_job":
        timeout = args.get("timeout_seconds", 60)
        try:
            timeout = max(1.0, min(600.0, float(timeout)))
        except (TypeError, ValueError):
            timeout = 60.0
        return client.wait(args.get("job_id") or "", timeout=timeout, interval=1.0)
    if name == "cancel_job":
        return client.cancel(args.get("job_id") or "")
    if name == "list_jobs":
        try:
            limit = max(1, min(100, int(args.get("limit", 20))))
        except (TypeError, ValueError):
            limit = 20
        return client.jobs(limit)
    if name == "service_status":
        return client.status()
    return {"ok": False, "error": "未知工具：%s" % name}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def handle_message(msg, client_factory=None):
    """处理一条 JSON-RPC 消息；通知返回 None。测试可直接调用。"""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request")
    req_id = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    if req_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        version = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        factory = client_factory or AgentClient
        try:
            payload = _call_tool(name, args, factory)
            is_error = isinstance(payload, dict) and payload.get("ok") is False
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result(payload, is_error=is_error)}
        except AgentError as e:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result(e.payload, is_error=True)}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": _tool_result({"ok": False, "error": str(e)}, is_error=True)}
    return _error(req_id, -32601, "method not found: %s" % method)


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())