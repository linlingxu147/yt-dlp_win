# -*- coding: utf-8 -*-
"""鱿鱼浏览器工具包的 AI/脚本入口（无 GUI，纯 JSON）。

依赖本地 agent 服务：桌面版启动后自动开启，或运行
`python tools/ytdlp_agent.py service start` 起一个独立 headless 服务。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "YtDlpWeb")):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent_client  # noqa: E402
from agent_client import AgentClient, AgentError, load_discovery, start_local_service  # noqa: E402


def emit(obj, pretty=False):
    print(json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None))


def fail(err, pretty=False):
    payload = dict(getattr(err, "payload", {}) or {})
    payload.setdefault("ok", False)
    payload.setdefault("error", str(err))
    if getattr(err, "status", 0):
        payload.setdefault("http_status", err.status)
    emit(payload, pretty)
    return 2


def make_client(args, ensure=False):
    if args.url or args.token:
        if not (args.url and args.token):
            raise AgentError(0, {"error": "--url 和 --token 必须同时提供",
                                 "code": "bad_args"})
        return AgentClient(args.url, args.token)
    if ensure:
        d = start_local_service()
        return AgentClient(d["url"], d["token"])
    return AgentClient()


def _progress_line(job):
    bits = [job.get("state") or "?"]
    if job.get("pct") is not None:
        bits.append("%.1f%%" % float(job["pct"]))
    if job.get("speed"):
        bits.append(str(job["speed"]))
    if job.get("eta"):
        bits.append("剩余 " + str(job["eta"]))
    if job.get("queue_position"):
        bits.append("排队第 %s 位" % job["queue_position"])
    return " · ".join(bits)


def cmd_download(args):
    client = make_client(args, ensure=args.ensure_service)
    sub = client.submit(args.urls, preset=args.preset, format=args.format,
                        playlist=args.playlist, title=args.title)
    if not args.wait:
        emit(sub, args.pretty)
        return 0
    job_id = sub.get("job_id")
    deadline = args.timeout or 0
    last = client.wait(job_id, timeout=deadline, interval=1.0)
    emit(last, args.pretty)
    if last.get("timed_out"):
        print("等待超时，任务仍在后台运行；用 job %s 继续查询" % job_id, file=sys.stderr)
        return 3
    return 0 if last.get("state") == "done" else 1


def cmd_wait(args):
    client = make_client(args)
    last = client.wait(args.job_id, timeout=args.timeout, interval=1.0)
    emit(last, args.pretty)
    if last.get("timed_out"):
        print("等待超时，任务仍在后台运行", file=sys.stderr)
        return 3
    return 0 if last.get("state") == "done" else 1


def cmd_job(args):
    emit(make_client(args).job(args.job_id), args.pretty)
    return 0


def cmd_jobs(args):
    emit(make_client(args).jobs(args.limit), args.pretty)
    return 0


def cmd_cancel(args):
    emit(make_client(args).cancel(args.job_id), args.pretty)
    return 0


def cmd_status(args):
    emit(make_client(args).status(), args.pretty)
    return 0


def cmd_service(args):
    if args.action == "path":
        emit({"ok": True, "path": agent_client.DISCOVERY_FILE,
              "exists": bool(load_discovery())}, args.pretty)
        return 0
    if args.action == "start":
        d = start_local_service()
        emit({"ok": True, "url": d.get("url"), "port": d.get("port"),
              "pid": d.get("pid"), "discovery_file": agent_client.DISCOVERY_FILE},
             args.pretty)
        return 0
    if args.action == "stop":
        d = load_discovery()
        if not d:
            raise AgentError(0, {"error": "没有可用的 agent 服务", "code": "service_not_found"})
        if d.get("embedded"):
            raise AgentError(409, {"error": "服务由桌面版管理，请关闭桌面版",
                                   "code": "embedded"})
        emit(make_client(args).shutdown(), args.pretty)
        return 0
    client = make_client(args)
    data = client.status()
    data.pop("discovery_file", None)
    emit(data, args.pretty)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="ytdlp-agent",
        description="鱿鱼浏览器工具包的无界面 JSON 入口（复用桌面版队列与自愈）")
    p.add_argument("--url", default="", help="覆盖发现文件的服务地址（高级）")
    p.add_argument("--token", default="", help="覆盖发现文件的 token（高级）")
    p.add_argument("--pretty", action="store_true", help="缩进输出，便于人看")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="提交下载；返回 job_id")
    d.add_argument("urls", nargs="+", help="视频/播放列表地址")
    d.add_argument("--preset", default="best", choices=("best", "audio"))
    d.add_argument("--format", default="", help="yt-dlp 格式选择器，如 137+140")
    d.add_argument("--playlist", action="store_true", help="允许下载整个播放列表")
    d.add_argument("--title", default="", help="任务标题（仅展示）")
    d.add_argument("--wait", action="store_true", help="等待任务结束再返回")
    d.add_argument("--timeout", type=float, default=0.0,
                   help="等待秒数；0 表示一直等到结束")
    d.add_argument("--ensure-service", action="store_true",
                   help="没有可用服务时自动后台启动独立服务")
    d.set_defaults(func=cmd_download)

    w = sub.add_parser("wait", help="等待已有任务结束")
    w.add_argument("job_id")
    w.add_argument("--timeout", type=float, default=0.0)
    w.set_defaults(func=cmd_wait)

    j = sub.add_parser("job", help="查询单个任务")
    j.add_argument("job_id")
    j.set_defaults(func=cmd_job)

    js = sub.add_parser("jobs", help="最近任务列表")
    js.add_argument("--limit", type=int, default=20)
    js.set_defaults(func=cmd_jobs)

    c = sub.add_parser("cancel", help="取消自己的任务")
    c.add_argument("job_id")
    c.set_defaults(func=cmd_cancel)

    s = sub.add_parser("status", help="服务与队列状态")
    s.set_defaults(func=cmd_status)

    svc = sub.add_parser("service", help="本地服务")
    svc.add_argument("action", choices=("start", "stop", "status", "path"))
    svc.set_defaults(func=cmd_service)

    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AgentError as e:
        return fail(e, args.pretty)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        return fail(AgentError(0, {"error": "内部错误：%s" % e}), args.pretty)


if __name__ == "__main__":
    sys.exit(main())