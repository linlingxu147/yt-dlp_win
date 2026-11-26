# 桌面 Web 重写（一期）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 YtDlpGUI 从 Tkinter 重写为 pywebview + 原生 Web 前端，全量对齐现有功能并新增下载历史与主题切换。

**Architecture:** 方案 A（js_api 桥）：host.py 承载任务流水线/配置/历史/事件泵（无 UI 依赖、可单测），api.py 是 JS 可调方法面，main.py 薄入口装配 pywebview 窗口；前端 ui/ 为零构建原生 ES 模块。ytdlp_core.py、YtDlpWeb/server.py、sniffer_extension 一行不动。

**Tech Stack:** Python 3.9+（标准库 + pywebview）、WebView2（Evergreen Edge）、原生 HTML/CSS/JS（ES 模块，无框架无构建）。

**Spec:** `docs/superpowers/specs/2026-11-25-desktop-web-rewrite-design.md`

## Global Constraints

- `ytdlp_core.py`、`YtDlpWeb/server.py`、`sniffer_extension/` 一行不改（回归基线：tests/test_core.py、test_hardening.py、test_server.py 全绿 + extension_smoke.js 5/5）。
- 单任务闸门：桌面自有任务与扩展服务共用 `RUNNER` 槽位，不并行跑 yt-dlp。
- 配置沿用同一份 `YtDlpGUI/config.json` 与全部现有键，**不新增 config 键**。
- 主题存 localStorage 键 `color-scheme`（值 `light`/`dark`，缺省=跟随系统）；历史存 `YtDlpGUI/history.json`，上限 500 条 FIFO。
- 事件泵 100ms 一拍；进度事件 ≤5 次/秒；日志上限 4000 行（超出裁前 800 行）；进度行（prog）只留最新一条。
- 窗口默认 1100x900，最小 920x700。
- 浏览器支持政策：仅 WebView2（Evergreen）；Baseline Newly Available 直接用，Limited availability（focusgroup/closedby）回避或渐进增强。
- 测试用标准库 unittest；测试端口避开 8765-8775（用 8799）。
- 安全：URL 过滤 sanitize_urls、危险参数 unsafe_arg_in、打开路径 path_inside、play_now/sniff_play_clip 不接受客户端路径、open_url 只认 http(s)。
- 提交信息用中文，风格沿用仓库历史（如「桌面 Web 重写：…」）。
- 运行测试命令：`py -3 -m unittest discover -s tests -v`（单文件：`py -3 -m unittest tests.test_host -v`）。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `YtDlpGUI/main.py` | 薄入口：依赖/WebView2 检查、建窗、装配 host/api、内嵌扩展服务、--selftest |
| `YtDlpGUI/host.py` | 宿主：事件泵、配置、历史、下载/探测/排障/更新/格式流水线、预览/播放、嗅探接线、服务生命周期 |
| `YtDlpGUI/api.py` | js_api 方法面：参数校验 + 转调 host + 事件推送（evaluate_js） |
| `YtDlpGUI/ui/index.html` | 页面骨架：meta 主题、页签、下载/嗅探/历史面板、对话框 |
| `YtDlpGUI/ui/css/app.css` | 设计令牌（light-dark）、布局、页签/表格/对话框/日志样式 |
| `YtDlpGUI/ui/js/bridge.js` | pywebview 桥：call() 封装 + __dispatch 事件路由 |
| `YtDlpGUI/ui/js/theme.js` | 三态主题（跟随系统/浅/深） |
| `YtDlpGUI/ui/js/tabs.js` | ARIA 页签 + roving tabindex |
| `YtDlpGUI/ui/js/download.js` | 设置表单收集/应用、模式联动、任务按钮 |
| `YtDlpGUI/ui/js/results.js` | 探测结果表、勾选、预览（gen 守卫） |
| `YtDlpGUI/ui/js/log.js` | 日志渲染（4000 行上限、prog 替换、自动滚底） |
| `YtDlpGUI/ui/js/dialogs.js` | 原生 dialog：格式表、确认框 |
| `YtDlpGUI/ui/js/sniff.js` | 嗅探页：轮询/折叠/选择/下载/预览/片段 |
| `YtDlpGUI/ui/js/history.js` | 历史页：列表/重下/打开目录/清空 |
| `YtDlpGUI/ui/js/app.js` | 装配：初始化各模块、订阅事件、加载配置 |
| `YtDlpGUI/启动.bat` | Python 探测、首次 pip install pywebview、WebView2 注册表预判、启动 |
| `YtDlpGUI/legacy/main_tk.py` | 验收后旧 Tkinter 版移入（Task 15） |
| `tests/test_host.py` | host 单测（注入事件回调，不依赖 pywebview） |
| `tests/test_api.py` | api 校验单测 |

host.py 从 core 导入名字到自身命名空间（`from ytdlp_core import spawn, iter_lines_watch, ...`），测试用 `mock.patch.object(host_mod, "spawn", ...)` 打桩。

---

### Task 1: pywebview 依赖 + 最小桥闭环

**Files:**
- Create: `YtDlpGUI/main.py`（本任务先写最小骨架，Task 9 扩为完整入口）
- Create: `YtDlpGUI/ui/index.html`（最小骨架，Task 10 扩为完整页面）
- Create: `YtDlpGUI/ui/js/bridge.js`

**Interfaces:**
- Consumes: 无
- Produces: `window.pywebview.api.ping()` 返回 `"pong"`；`window.__dispatch(ev)` 全局事件入口；`window.__bridgeReady` Promise（pywebviewready 后 resolve）。后续所有前端模块经 bridge.js 通信。

- [ ] **Step 1: 安装 pywebview**

Run: `py -3 -m pip install pywebview`
Expected: 安装成功（Windows 上会带 pythonnet/WebView2 依赖；若失败记录错误，检查 Python ≥3.9）。

- [ ] **Step 2: 写最小 ui/index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<title>yt-dlp 下载器</title>
</head>
<body>
<h1>桥探针</h1>
<pre id="out">等待…</pre>
<script type="module" src="js/bridge.js"></script>
<script type="module">
import { call, ready } from './js/bridge.js';
await ready;
const r = await call('ping');
document.getElementById('out').textContent = 'ping → ' + r;
window.__probeDone = true;
</script>
</body>
</html>
```

- [ ] **Step 3: 写 ui/js/bridge.js**

```js
// pywebview 桥：JS→Python 调用 + Python→JS 事件路由。
export const ready = new Promise((resolve) => {
  if (window.pywebview?.api) resolve();
  else window.addEventListener('pywebviewready', () => resolve(), { once: true });
});

export async function call(method, ...args) {
  await ready;
  return window.pywebview.api[method](...args);
}

const subscribers = new Map();
export function on(type, fn) {
  if (!subscribers.has(type)) subscribers.set(type, []);
  subscribers.get(type).push(fn);
}

window.__dispatch = (ev) => {
  for (const fn of subscribers.get(ev.type) || []) fn(ev);
};
```

- [ ] **Step 4: 写最小 main.py**

```python
# -*- coding: utf-8 -*-
import os
import sys
import webview

APP_DIR = os.path.dirname(os.path.abspath(__file__))


class Api:
    def ping(self):
        return "pong"


def main():
    api = Api()
    window = webview.create_window(
        "yt-dlp 下载器   ·   可视化界面",
        url=os.path.join(APP_DIR, "ui", "index.html"),
        js_api=api, width=1100, height=900, min_size=(920, 700))
    webview.start(http_server=True, debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 实测验证闭环**

Run: `py -3 YtDlpGUI\main.py --debug`
Expected: 窗口打开，页面显示 `ping → pong`（证明 http_server 托管下 ES 模块可加载、js_api 可调）。在 devtools console 执行 `window.__dispatch({type:"x"})` 不报错。
若 ES 模块加载失败：检查 http_server=True 是否生效、url 是否绝对路径；记录结论后再继续。

- [ ] **Step 6: 验证 evaluate_js 推送**

在 main.py 的 `webview.start` 前加临时线程：

```python
import threading, time, json
def push_probe():
    time.sleep(2)
    window.evaluate_js("window.__dispatch(%s)" % json.dumps({"type": "probe", "n": 42}))
threading.Thread(target=push_probe, daemon=True).start()
```

index.html 的 module 脚本里加 `import { on } from './js/bridge.js'; on('probe', ev => document.getElementById('out').textContent += ' / probe ' + ev.n);`
Run: `py -3 YtDlpGUI\main.py`
Expected: 2 秒后页面追加 `/ probe 42`。验证后删除临时探针代码（保留 bridge.js 与骨架）。

- [ ] **Step 7: Commit**

```bash
git add YtDlpGUI/main.py YtDlpGUI/ui/index.html YtDlpGUI/ui/js/bridge.js
git commit -m "桌面 Web 重写：pywebview 最小桥闭环（http_server + ES 模块 + js_api + 事件推送）"
```

---

### Task 2: host 事件泵 + 配置

**Files:**
- Create: `YtDlpGUI/host.py`
- Test: `tests/test_host.py`

**Interfaces:**
- Consumes: `ytdlp_core.load_config(path, warn=)`, `save_config(path, cfg, warn=)`, `which_tool`, `find_tool`, `atomic_write_text`
- Produces: `Host(config_file, emit=None)`；`host.mark_ready()`；`host.pump_once()`；`host._put(ev)`；`host.save_config(cfg) -> bool`；`host.get_env() -> dict`；`host.cfg`；事件形状 `{"type":"logbatch","items":[{"text","tag"}]}`、`{"type":"progress",...}`。后续任务往 Host 追加方法。

- [ ] **Step 1: 写失败测试 tests/test_host.py**

```python
# -*- coding: utf-8 -*-
"""host 单测：注入事件回调，不依赖 pywebview。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_host():
    spec = importlib.util.spec_from_file_location(
        "ytdlp_gui_host", os.path.join(ROOT, "YtDlpGUI", "host.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class PumpTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_ready_keeps_events_queued(self):
        self.host._put({"type": "progress", "pct": 10})
        self.host.pump_once()
        self.assertEqual(self.events, [])

    def test_progress_coalesced_to_latest(self):
        self.host.mark_ready()
        for i in range(10):
            self.host._put({"type": "progress", "pct": i})
        self.host.pump_once()
        prog = [e for e in self.events if e["type"] == "progress"]
        self.assertEqual(len(prog), 1)
        self.assertEqual(prog[0]["pct"], 9)

    def test_logs_batched(self):
        self.host.mark_ready()
        for t in ("a", "b", "c"):
            self.host._put({"type": "log", "text": t, "tag": "ok"})
        self.host.pump_once()
        batches = [e for e in self.events if e["type"] == "logbatch"]
        self.assertEqual(len(batches), 1)
        self.assertEqual([i["text"] for i in batches[0]["items"]], ["a", "b", "c"])

    def test_plain_events_pass_through(self):
        self.host.mark_ready()
        self.host._put({"type": "status", "text": "就绪", "tone": "ok"})
        self.host.pump_once()
        self.assertIn({"type": "status", "text": "就绪", "tone": "ok"}, self.events)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        self.host = self.mod.Host(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_loaded(self):
        self.assertEqual(self.host.cfg["mode"], "best")

    def test_save_roundtrip_keeps_keys(self):
        ok = self.host.save_config({"proxy": "http://127.0.0.1:7890"})
        self.assertTrue(ok)
        again = self.mod.Host(self.path)
        self.assertEqual(again.cfg["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(again.cfg["mode"], "best")

    def test_env_shape(self):
        env = self.host.get_env()
        self.assertIn("has_aria2c", env)
        self.assertIn("yt_dlp_path", env)
        self.assertIn("svc_url", env)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_host -v`
Expected: FAIL（host.py 不存在，import 报错）。

- [ ] **Step 3: 写 host.py（本任务部分）**

```python
# -*- coding: utf-8 -*-
"""桌面版宿主：事件泵 + 配置 + 历史 + 任务流水线（无 UI 依赖，可单测）。

事件出口是注入的 emit 回调（默认空操作）；main.py 把它接成 evaluate_js。
工作线程只往队列投事件，泵统一合并/节流后推出，绝不碰 UI。
"""
import json
import os
import queue
import threading
import time

import ytdlp_core as core

HISTORY_MAX = 500
PUMP_SECONDS = 0.1
PROGRESS_HZ = 5
GUI_STALL_SECONDS = 300
TASK_LABELS = {"download": "下载", "detect": "探测资源", "trouble": "自动排障",
               "update": "更新 yt-dlp", "formats": "读取格式表"}


class Host:
    def __init__(self, config_file, emit=None):
        self.config_file = config_file
        self.emit = emit or (lambda ev: None)
        self.q = queue.Queue()
        self.ready = False
        self._stop_flag = False
        self.cfg_warnings = []
        self.cfg = core.load_config(config_file, warn=self.cfg_warnings.append)
        self.history_file = os.path.join(os.path.dirname(config_file), "history.json")
        self.history = self._load_history()
        self.task_kind = None
        self.running = False
        self.proc = None
        self.user_stopped = False
        self.pstate = core.new_progress_state()
        self._progress_pending = None
        self._last_progress_ts = 0.0
        self._web_mod = None
        self.web_svc = None
        self.ext_emit = None
        self.ext_cfg = None
        self.ext_cookie_file = None
        self.dl_urls = []
        self.dl_no_playlist = False
        self.dl_outdir = ""
        self.net_tried = set()
        self.net_name = "normal"
        self.diag_buf = ""
        self.results = []
        self._play_raw = ""
        self._play_ok = False
        self._sniff_clip = ""
        self._worker = None

    # ---------------- 事件泵 ----------------
    def mark_ready(self):
        self.ready = True
        for w in self.cfg_warnings:
            self._put({"type": "log", "text": "[配置] " + str(w), "tag": "warn"})

    def _put(self, ev):
        self.q.put(ev)

    def _log(self, text, tag=None):
        self._put({"type": "log", "text": text, "tag": tag})
        if self.ext_emit:
            self.ext_emit({"type": "log", "text": text.rstrip("\n")})

    def _status(self, text, tone="info"):
        self._put({"type": "status", "text": text, "tone": tone})

    def _task_ev(self):
        self._put({"type": "task", "kind": self.task_kind, "busy": bool(self.task_kind)})

    def pump_once(self):
        if not self.ready:
            return
        now = time.time()
        if self._progress_pending and now - self._last_progress_ts >= 1.0 / PROGRESS_HZ:
            self.emit(self._progress_pending)
            self._last_progress_ts = now
            self._progress_pending = None
        logs, progress = [], None
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            t = ev.get("type")
            if t == "log":
                logs.append({"text": ev["text"], "tag": ev.get("tag")})
            elif t == "progress":
                progress = ev
            else:
                if logs:
                    self.emit({"type": "logbatch", "items": logs})
                    logs = []
                self.emit(ev)
        if logs:
            self.emit({"type": "logbatch", "items": logs})
        if progress:
            if now - self._last_progress_ts >= 1.0 / PROGRESS_HZ:
                self.emit(progress)
                self._last_progress_ts = now
            else:
                self._progress_pending = progress

    def start_pump(self):
        def loop():
            while not self._stop_flag:
                self.pump_once()
                time.sleep(PUMP_SECONDS)
        threading.Thread(target=loop, daemon=True).start()

    # ---------------- 配置 ----------------
    def get_env(self):
        return {"has_aria2c": bool(core.which_tool("aria2c.exe")),
                "yt_dlp_path": self.cfg.get("yt_dlp_path") or core.find_tool("yt-dlp.exe"),
                "svc_url": self.web_svc.url if self.web_svc else ""}

    def save_config(self, cfg):
        c = dict(self.cfg)
        c.update(cfg or {})
        c["yt_dlp_path"] = core.find_tool("yt-dlp.exe")
        warns = []
        if core.save_config(self.config_file, c, warn=warns.append):
            self.cfg = c
            for w in warns:
                self._log("[配置] " + str(w) + "\n", "warn")
            return True
        return False

    # ---------------- 历史（Task 3 补全） ----------------
    def _load_history(self):
        return []
```

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest tests.test_host -v`
Expected: PumpTest/ConfigTest 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/host.py tests/test_host.py
git commit -m "桌面 Web 重写：host 事件泵（合并/节流/就绪闸门）与配置读写"
```

---

### Task 3: host 下载历史

**Files:**
- Modify: `YtDlpGUI/host.py`（替换 `_load_history`，追加 record_history/history_list/history_clear）
- Test: `tests/test_host.py`（追加 HistoryTest）

**Interfaces:**
- Consumes: `core.atomic_write_text(path, text, backup=True)`
- Produces: `host.record_history(entry: dict)`（补默认字段、FIFO、原子写）；`host.history_list() -> list`（新→旧）；`host.history_clear()`。记录字段：ts/urls/title/outdir/status(ok|failed|stopped)/code/elapsed/done_count/mode/source(gui|ext|sniff)。

- [ ] **Step 1: 追加失败测试**

```python
class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_defaults_and_roundtrip(self):
        self.host.record_history({"urls": ["https://x/v"], "status": "ok", "code": 0})
        again = self.mod.Host(self.host.config_file)
        self.assertEqual(len(again.history), 1)
        rec = again.history[0]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["source"], "gui")
        self.assertIn("ts", rec)

    def test_fifo_cap_500(self):
        for i in range(505):
            self.host.record_history({"urls": ["u%d" % i], "status": "ok", "code": 0})
        self.assertEqual(len(self.host.history), 500)
        self.assertEqual(self.host.history[0]["urls"], ["u5"])

    def test_list_newest_first(self):
        self.host.record_history({"urls": ["a"], "status": "ok", "code": 0})
        self.host.record_history({"urls": ["b"], "status": "failed", "code": 1})
        self.assertEqual(self.host.history_list()[0]["urls"], ["b"])

    def test_corrupt_file_falls_back_empty(self):
        with open(self.host.history_file, "w", encoding="utf-8") as f:
            f.write("{bad json")
        again = self.mod.Host(self.host.config_file)
        self.assertEqual(again.history, [])

    def test_clear(self):
        self.host.record_history({"urls": ["a"], "status": "ok", "code": 0})
        self.host.history_clear()
        self.assertEqual(self.host.history_list(), [])
        again = self.mod.Host(self.host.config_file)
        self.assertEqual(again.history, [])
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_host.HistoryTest -v`
Expected: FAIL（record_history 不存在）。

- [ ] **Step 3: 实现（替换 host.py 的 `_load_history` 占位并追加）**

```python
    def _load_history(self):
        try:
            with open(self.history_file, encoding="utf-8") as f:
                data = json.load(f)
            return [r for r in data if isinstance(r, dict)][-HISTORY_MAX:]
        except Exception:
            return []

    def record_history(self, entry):
        rec = {"ts": time.time(), "urls": [], "title": "", "outdir": "",
               "status": "failed", "code": -1, "elapsed": 0, "done_count": 0,
               "mode": "", "source": "gui"}
        rec.update(entry or {})
        self.history.append(rec)
        del self.history[:-HISTORY_MAX]
        try:
            core.atomic_write_text(self.history_file,
                                   json.dumps(self.history, ensure_ascii=False, indent=1))
        except Exception:
            pass

    def history_list(self):
        return list(reversed(self.history))

    def history_clear(self):
        self.history = []
        try:
            core.atomic_write_text(self.history_file, "[]")
        except Exception:
            pass
```

- [ ] **Step 4: 运行确认通过**

Run: `py -3 -m unittest tests.test_host -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/host.py tests/test_host.py
git commit -m "桌面 Web 重写：下载历史（500 条 FIFO、原子写、损坏回退）"
```

---

### Task 4: host 下载流水线 + 自愈 + 扩展任务入口

**Files:**
- Modify: `YtDlpGUI/host.py`（追加闸门/下载/停止/自愈/扩展入口；从 core 导入流水线所需名字）
- Test: `tests/test_host.py`（追加 DownloadTest，打桩 spawn/iter_lines_watch）

**Interfaces:**
- Consumes: core 的 `spawn, iter_lines_watch, kill_tree, command_text, new_progress_state, progress_event, progress_text, classify_line, is_net_failure, next_net_plan, next_cookie_plan, NET_PLAN_LABEL, COOKIE_PLAN_LABEL, sanitize_urls, unsafe_arg_in, build_download_cmd, diagnose, clear_preview_links, real_download_path, fmt_dur, fmt_size, PROG_MARK`（导入 host 命名空间供打桩）
- Produces: `host.start_download(urls, cfg, no_playlist=False) -> {"ok":bool,"error"?:str,"busy"?:bool}`；`host.stop() -> bool`；`host.submit_server_task(task) -> bool`（扩展服务钩子）；`host._finish_task(code)`；事件 `done{code,elapsed,done_count,diag}`、`task`、`status`、`progress`、`log`。下载结束写历史（source 按 ext_emit 判 gui/ext）。

- [ ] **Step 1: 追加失败测试**

```python
from unittest import mock


class FakeProc:
    def __init__(self, code):
        self._code = code
    def poll(self):
        return self._code
    def wait(self):
        return self._code


def fake_iter(lines):
    def it(proc, abort=None, keepalive=None, **kw):
        for ln in lines:
            yield ln
    return it


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        self.cfg = dict(self.host.cfg, outdir=os.path.join(self.tmp.name, "out"))

    def tearDown(self):
        self.tmp.cleanup()

    def _run_download(self, lines, code):
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(code)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter(lines)):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()

    def test_success_emits_done_and_history(self):
        self._run_download(["[download] Destination: v.mp4"], 0)
        done = [e for e in self.events if e["type"] == "done"]
        self.assertEqual(done[0]["code"], 0)
        self.assertEqual(self.host.history[0]["status"], "ok")
        self.assertIsNone(self.host.task_kind)

    def test_unsafe_extra_arg_blocked(self):
        cfg = dict(self.cfg, extra="--exec rm")
        r = self.host.start_download(["https://x/v"], cfg)
        self.assertFalse(r["ok"])
        self.assertIn("禁用", r["error"])

    def test_busy_gate(self):
        self.host.task_kind = "detect"
        r = self.host.start_download(["https://x/v"], self.cfg)
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])

    def test_net_heal_retries_once(self):
        calls = []
        def spawn2(cmd, cwd=None):
            calls.append(cmd)
            return FakeProc(1 if len(calls) == 1 else 0)
        lines = ["ERROR: unable to download: ConnectionResetError 10054"]
        with mock.patch.object(self.mod, "spawn", side_effect=spawn2), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter(lines)):
            r = self.host.start_download(["https://x/v"], dict(self.cfg, net_hard=True))
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
            self.host.pump_once()      # 路由 done → 自愈起新 worker
            self.host._worker.join(5)
            self.host.pump_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.host.history[-1]["status"], "ok")

    def test_stop_marks_stopped_history(self):
        def stopped_iter(proc, abort=None, keepalive=None, **kw):
            self.host.user_stopped = True   # 模拟用户点了停止
            yield "ERROR: aborted"
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(1)), \
             mock.patch.object(self.mod, "iter_lines_watch", stopped_iter):
            r = self.host.start_download(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["status"], "stopped")

    def test_submit_server_task_busy(self):
        self.host.task_kind = "download"
        self.assertFalse(self.host.submit_server_task({"urls": ["https://x"]}))

    def test_external_task_mirrors_and_records_ext_source(self):
        emitted = []
        task = {"urls": ["https://x/v"], "cfg": dict(self.cfg),
                "emit": emitted.append, "no_playlist": True}
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([])):
            self.host.start_external(task)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(self.host.history[-1]["source"], "ext")
        self.assertTrue(any(e.get("type") == "done" for e in emitted))
```

（测试文件顶部补 `import time`。）

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_host.DownloadTest -v`
Expected: FAIL（start_download 不存在）。

- [ ] **Step 3: 实现——host.py 顶部导入扩充**

```python
from ytdlp_core import (
    MODE_CHOICES, MODE_FMT, MODE_NAME, mode_key, TEMPLATES, BROWSERS,
    diagnose, diagnose_pairs, find_tool, fmt_dur, fmt_size, info_from_json_raw,
    normalize_headers, sanitize_urls, unsafe_arg_in,
    build_detect_cmd, build_formats_cmd, build_probe_cmd, PROBE_VARIANTS,
    spawn, iter_lines_watch, kill_tree, command_text,
    new_progress_state, progress_event, progress_text, classify_line,
    is_net_failure, next_net_plan, next_cookie_plan,
    NET_PLAN_LABEL, COOKIE_PLAN_LABEL, PROG_MARK,
    fetch_thumb, real_download_path, probe_playable, make_preview_link,
    clear_preview_links, fmt_is_separated, path_inside,
)
```

- [ ] **Step 4: 实现——闸门与下载流水线（追加到 Host）**

```python
    # ---------------- 闸门 ----------------
    def _reserve(self, kind):
        if self.task_kind:
            return False
        if self._web_mod and not self._web_mod.RUNNER.reserve(kind):
            return False
        self.task_kind = kind
        return True

    def _release_runner(self):
        if self._web_mod:
            self._web_mod.RUNNER.finish()

    def _task_cfg(self):
        return dict(self.ext_cfg) if self.ext_cfg is not None else dict(self.cfg)

    def _busy(self):
        return {"ok": False, "busy": True,
                "error": "「%s」正在进行，请先停止或等待结束"
                         % TASK_LABELS.get(self.task_kind, "任务")}

    # ---------------- 下载 ----------------
    def start_download(self, urls, cfg, no_playlist=False):
        if self.task_kind:
            return self._busy()
        bad = unsafe_arg_in(cfg.get("extra", "")) or unsafe_arg_in(cfg.get("aria_args", ""))
        if bad:
            return {"ok": False, "error": "附加参数里有被禁用的参数 %s（会执行命令或读本机文件），已阻止" % bad}
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
        if not self._reserve("download"):
            return self._busy()
        self._begin_download(good, cfg, no_playlist, outdir, source="gui")
        return {"ok": True}

    def _begin_download(self, urls, cfg, no_playlist, outdir, source):
        self.ext_cfg = dict(cfg) if source != "gui" else None
        self.dl_urls, self.dl_no_playlist, self.dl_outdir = list(urls), no_playlist, outdir
        self.dl_source = source
        self.net_tried, self.net_name = set(), "normal"
        self.user_stopped = False
        self.running = True
        self.pstate = new_progress_state()
        self.t_start = time.time()
        self._task_ev()
        self._status("下载中…", "run")
        warns = []
        cmd = build_download_cmd(cfg, urls, no_playlist=no_playlist, warn=warns)
        for w in warns:
            self._log("! " + w + "\n", "warn")
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._run, args=(cmd, outdir), daemon=True)
        self._worker.start()

    def submit_server_task(self, task):
        if not self._reserve("download"):
            return False
        self._put({"type": "exttask", "task": task})
        return True

    def start_external(self, task):
        cfg = dict(task.get("cfg") or {})
        urls = list(task.get("urls") or ([task["url"]] if task.get("url") else []))
        emit = task.get("emit")
        if not urls:
            self._release_runner()
            self.task_kind = None
            return
        outdir = cfg.get("outdir") or os.path.join(core.BASE_DIR, "downloads")
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            if emit:
                emit({"type": "log", "level": "err", "text": "无法创建输出目录：%s" % e})
                emit({"type": "done", "code": -1})
            self._release_runner()
            self.task_kind = None
            return
        self.ext_emit, self.ext_cfg = emit, cfg
        self.ext_cookie_file = cfg.get("_sniff_cookie_file")
        self.dl_urls = urls
        self.dl_no_playlist = bool(task.get("no_playlist", True))
        self.dl_outdir = outdir
        self.dl_source = "ext"
        self.net_tried, self.net_name = set(), "normal"
        self.user_stopped = False
        self.running = True
        self.task_kind = "download"
        self.pstate = new_progress_state()
        self.t_start = time.time()
        self._task_ev()
        self._status("下载中…（浏览器扩展）", "run")
        warns = []
        cmd = build_download_cmd(cfg, urls, no_playlist=self.dl_no_playlist, warn=warns)
        for w in warns:
            self._log("! " + w + "\n", "warn")
        self._log("[浏览器扩展] 嗅探地址交给桌面版下载\n", "ok")
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._run, args=(cmd, outdir), daemon=True)
        self._worker.start()

    def _run(self, cmd, outdir):
        try:
            proc = spawn(cmd, outdir)
        except FileNotFoundError:
            self._log("找不到 yt-dlp：%s\n" % cmd[0], "err")
            self._put({"type": "done", "code": -1})
            return
        except Exception as e:
            self._log("启动失败：%s\n" % e, "err")
            self._put({"type": "done", "code": -1})
            return
        self.proc = proc
        buf = []
        why = {}
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                         stall_seconds=GUI_STALL_SECONDS, why=why):
                buf.append(line)
                self._handle_line(line)
        except Exception as e:
            self._log("读取输出出错：%s\n" % e, "err")
        finally:
            if why.get("reason") == "stall":
                self._log("[长时间没有输出，已自动终止本次任务]\n", "warn")
            self.diag_buf = "\n".join(buf[-300:])
            self.proc = None
            self._put({"type": "done", "code": proc.wait()})

    def _handle_line(self, line):
        ev = progress_event(self.pstate, line)
        if ev:
            st = self.pstate
            self._put({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                       "eta": st["eta"], "size": st["size"], "file": st["file"],
                       "item_i": st["item_i"], "item_total": st["item_total"],
                       "done_count": len(st["done"])})
            if self.ext_emit:
                self.ext_emit({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                               "eta": st["eta"], "size": st["size"], "file": st["file"],
                               "item_i": st["item_i"], "item_total": st["item_total"]})
            if ev["kind"] == "file":
                self._log((progress_text(st) if PROG_MARK in line else line) + "\n", "ok")
                self._maybe_start_play_probe()
            elif ev["kind"] == "item":
                self._log(line + "\n", None)
            else:
                self._log(progress_text(st) + "\n", "prog")
            return
        tag = classify_line(line)
        self._log(line + "\n", tag)

    def stop(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            self._status("当前没有进行中的任务", "idle")
            return False
        self.user_stopped = True
        kill_tree(proc)
        self._status("已停止", "err")
        self._log("[已手动停止：%s]\n" % TASK_LABELS.get(self.task_kind, "任务"), "warn")
        return True

    def _finish_task(self, code):
        self._release_runner()
        emit, cookie = self.ext_emit, self.ext_cookie_file
        self.ext_emit = self.ext_cfg = self.ext_cookie_file = None
        if self._web_mod:
            with self._web_mod.PLAY_LOCK:
                self._web_mod.PLAY_STATE.update({"raw_path": "", "ok": False,
                                                 "reason": "还没有开始下载"})
        if emit:
            emit({"type": "done", "code": code})
        if cookie and os.path.isfile(cookie):
            try:
                os.remove(cookie)
            except Exception:
                pass

    def on_done(self, code):
        base = self._task_cfg()
        was_ext = self.ext_cfg is not None
        clear_preview_links(base.get("outdir") or os.path.join(core.BASE_DIR, "downloads"))
        self._play_raw = ""
        self._set_playable(False, "还没有开始下载")
        self.running = False
        self.proc = None
        self.task_kind = None
        elapsed = time.time() - getattr(self, "t_start", 0) if getattr(self, "t_start", 0) else 0
        done_count = len(self.pstate.get("done") or [])
        if code == 0:
            self._status("完成", "ok")
            self._log("=== 完成 · 耗时 %d 秒 ===\n" % int(elapsed), "ok")
            self.record_history({"urls": self.dl_urls, "outdir": self.dl_outdir,
                                 "status": "ok", "code": 0, "elapsed": round(elapsed, 1),
                                 "done_count": done_count, "mode": base.get("mode", ""),
                                 "source": "ext" if was_ext else getattr(self, "dl_source", "gui")})
            self._finish_task(code)
            self._task_ev()
            if base.get("open_after"):
                self.open_outdir(base.get("outdir"))
        else:
            if self._net_heal():
                return
            status = "stopped" if self.user_stopped else "failed"
            self._status("已停止" if self.user_stopped else "已结束（退出码 %s）" % code, "err")
            self._log("=== 结束，退出码 %s ===\n" % code, "err")
            self._log_diag()
            self.record_history({"urls": self.dl_urls, "outdir": self.dl_outdir,
                                 "status": status, "code": code, "elapsed": round(elapsed, 1),
                                 "done_count": done_count, "mode": base.get("mode", ""),
                                 "source": "ext" if was_ext else getattr(self, "dl_source", "gui")})
            self._finish_task(code)
            self._task_ev()
        self._put({"type": "done", "code": code, "elapsed": round(elapsed, 1),
                   "done_count": done_count})

    def _net_heal(self):
        if self.user_stopped or not self.dl_urls:
            return False
        cfg = self._task_cfg()
        if not cfg.get("net_hard", True):
            return False
        tail = self.diag_buf or ""
        self.net_tried.add(self.net_name)
        nxt = next_cookie_plan(tail, self.net_tried, cfg)
        if not nxt:
            if not is_net_failure(tail):
                return False
            nxt = next_net_plan(tail, self.net_tried, cfg, url=self.dl_urls[0])
        if not nxt:
            return False
        self.net_name, extra = nxt
        self._log("[自动重试] 换用%s重试（已下载部分会续传）\n"
                  % (NET_PLAN_LABEL.get(self.net_name) or COOKIE_PLAN_LABEL), "warn")
        cfg2 = dict(cfg, **extra)
        self.ext_cfg = cfg2 if self.ext_cfg is not None else None
        cmd = build_download_cmd(cfg2, self.dl_urls, no_playlist=self.dl_no_playlist)
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self.pstate = new_progress_state()
        self.t_start = time.time()
        self.running = True
        self.task_kind = "download"
        self._task_ev()
        self._status("下载中…（自动重试）", "run")
        self._worker = threading.Thread(target=self._run, args=(cmd, self.dl_outdir), daemon=True)
        self._worker.start()
        return True

    def _log_diag(self):
        diag = diagnose_pairs(self.diag_buf or "")
        if not diag:
            return
        self._log("\n=== 可能的原因与建议 ===\n", "warn")
        for title, advices in diag:
            self._log("· " + title + "\n", "warn")
            for a in advices:
                self._log("    - " + a + "\n", None)

    def open_outdir(self, path=None):
        d = (path or self.cfg.get("outdir") or "").strip()
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except Exception:
            pass

    # 占位（Task 5/6 补全）
    def _maybe_start_play_probe(self):
        pass

    def _set_playable(self, ok, reason):
        self._play_ok = bool(ok)
        self._put({"type": "playable", "ok": bool(ok), "reason": reason})
```

注意：`_run` 投出的 `done` 事件由泵路由到 `on_done`——在 `pump_once` 的 else 分支前加拦截：

```python
            else:
                if logs:
                    self.emit({"type": "logbatch", "items": logs})
                    logs = []
                if t == "done" and self.running:
                    self.on_done(ev["code"])
                    continue
                if t == "exttask":
                    self.start_external(ev["task"])
                    continue
                self.emit(ev)
```

（`on_done` 内部会再投一条带 elapsed 的 `done` 给前端；此时 `self.running` 已为 False，不会二次触发。自愈重投的 `done` 同理经此路由。）

- [ ] **Step 5: 运行确认通过**

Run: `py -3 -m unittest tests.test_host -v`
Expected: 全 PASS（DownloadTest 含自愈两次 spawn、stopped 历史、ext 镜像）。

- [ ] **Step 6: 回归基线**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 原有三套 + 新增全绿。

- [ ] **Step 7: Commit**

```bash
git add YtDlpGUI/host.py tests/test_host.py
git commit -m "桌面 Web 重写：host 下载流水线（闸门/自愈/停止/扩展任务入口/历史接线）"
```

---

### Task 5: host 探测 / 排障 / 更新 / 格式表

**Files:**
- Modify: `YtDlpGUI/host.py`
- Test: `tests/test_host.py`（追加 DetectTroubleTest）

**Interfaces:**
- Consumes: `build_detect_cmd, build_probe_cmd, PROBE_VARIANTS, info_from_json_raw, json_ok, apply_probe_fix, diagnose_pairs`（core）
- Produces: `host.detect(urls, cfg) -> shape`；`host.troubleshoot(urls, cfg) -> shape`；`host.update_ytdlp(proxy) -> shape`；`host.fetch_formats(url, cfg) -> shape`；事件 `detrow{info}`、`detdone{code,count}`、`formats{lines,code}`、`trodone{ok,task}`、`applyfix{cfg,touched,manual}`。`host.results` 存探测行；`host.copy_cmd(urls, cfg) -> {"ok":True,"text":str}`。

- [ ] **Step 1: 追加失败测试**

```python
class DetectTroubleTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()
        self.cfg = dict(self.host.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_collects_rows(self):
        row = json.dumps({"id": "x", "title": "T", "duration": 60,
                          "webpage_url": "https://x/v"})
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([row])):
            r = self.host.detect(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertEqual(len(self.host.results), 1)
        self.assertEqual(self.host.results[0]["title"], "T")
        self.assertTrue(any(e["type"] == "detdone" for e in self.events))

    def test_formats_returns_lines_event(self):
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch",
                               fake_iter(["ID EXT RES", "137 mp4 1080p"])):
            r = self.host.fetch_formats("https://x/v", self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        fmt = [e for e in self.events if e["type"] == "formats"]
        self.assertEqual(fmt[0]["code"], 0)
        self.assertIn("137 mp4 1080p", fmt[0]["lines"])

    def test_copy_cmd_text(self):
        r = self.host.copy_cmd(["https://x/v"], self.cfg)
        self.assertTrue(r["ok"])
        self.assertIn("yt-dlp", r["text"])

    def test_trouble_applies_fix(self):
        row = json.dumps({"id": "x", "title": "T"})
        with mock.patch.object(self.mod, "spawn", return_value=FakeProc(0)), \
             mock.patch.object(self.mod, "iter_lines_watch", fake_iter([row])):
            r = self.host.troubleshoot(["https://x/v"], self.cfg)
            self.assertTrue(r["ok"], r)
            self.host._worker.join(5)
        self.host.pump_once()
        self.assertTrue(any(e["type"] == "trodone" for e in self.events))
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_host.DetectTroubleTest -v`
Expected: FAIL。

- [ ] **Step 3: 实现（追加到 Host；pump_once 拦截分支补 detdone/trodone/formats 路由）**

```python
    # ---------------- 探测 ----------------
    def detect(self, urls, cfg):
        if self.task_kind:
            return self._busy()
        good, _ = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的链接"}
        self.save_config(cfg)
        if not self._reserve("detect"):
            return self._busy()
        self.results = []
        self.user_stopped = False
        self._task_ev()
        self._status("探测中…", "run")
        cmd = build_detect_cmd(cfg, good)
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._detect_run, args=(cmd,), daemon=True)
        self._worker.start()
        return {"ok": True}

    def _detect_run(self, cmd):
        try:
            proc = spawn(cmd, core.BASE_DIR)
        except Exception as e:
            self._log("启动失败：%s\n" % e, "err")
            self._put({"type": "detdone", "code": -1})
            return
        self.proc = proc
        buf = []
        why = {}
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                         stall_seconds=GUI_STALL_SECONDS, why=why):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        info = info_from_json_raw(json.loads(line))
                        self.results.append(info)
                        self._put({"type": "detrow", "info": info})
                        continue
                    except Exception:
                        pass
                buf.append(line)
                self._handle_line(line)
        except Exception as e:
            self._log("读取输出出错：%s\n" % e, "err")
        if why.get("reason") == "stall":
            self._log("[长时间没有输出，已自动停止探测]\n", "warn")
        self.diag_buf = "\n".join(buf[-300:])
        self.proc = None
        self._put({"type": "detdone", "code": proc.wait()})

    def on_detect_done(self, code):
        self.task_kind = None
        self._release_runner()
        self._task_ev()
        n = len(self.results)
        if n:
            self._status("探测完成 · %d 条" % n, "ok")
            self._log("=== 探测完成：%d 条 ===\n" % n, "ok")
        else:
            self._status("探测结束，没有拿到条目", "err")
            self._log("=== 探测结束，退出码 %s ===\n" % code, "err")
            self._log_diag()
        self._put({"type": "detdone", "code": code, "count": n})

    # ---------------- 排障 ----------------
    def troubleshoot(self, urls, cfg):
        if self.task_kind:
            return self._busy()
        good, _ = sanitize_urls(urls or [])
        if not good:
            return {"ok": False, "error": "没有可用的链接"}
        self.save_config(cfg)
        if not self._reserve("trouble"):
            return self._busy()
        self.user_stopped = False
        self._task_ev()
        self._status("排障中…", "run")
        self._log("=== 自动排障：依次尝试 4 套访问配置（每套最多 25 秒）===\n", "cmd")
        self._worker = threading.Thread(target=self._trouble_run, args=(good[0], dict(cfg)),
                                        daemon=True)
        self._worker.start()
        return {"ok": True}

    def _trouble_run(self, url, cfg):
        collected, hit = [], None
        for key, label, fix in PROBE_VARIANTS:
            if self.user_stopped:
                self._log("[已取消，剩余方案未尝试]\n", "warn")
                break
            cmd = build_probe_cmd(cfg, url, key)
            self._log("> " + command_text(cmd) + "\n", "cmd")
            ok, msg, out = self._probe_once(cmd, timeout=25)
            self._log(("✓ " if ok else "✗ ") + label + "：" + msg + "\n", "ok" if ok else "warn")
            collected.append(out)
            if ok:
                hit = (label, fix, url)
                break
        if hit:
            label, fix, u = hit
            touched, manual = core.apply_probe_fix(cfg, u, fix)
            self.save_config(cfg)
            self._put({"type": "applyfix", "cfg": cfg, "touched": touched, "manual": manual})
            self._log("=== 排障「%s」：已应用 %s ===\n" % (label, "、".join(touched) or "无"), "ok")
        else:
            diag = diagnose_pairs("\n".join(collected))
            for title, advices in diag:
                self._log("· " + title + "\n", "warn")
                for a in advices:
                    self._log("    - " + a + "\n", None)
        self._put({"type": "trodone", "ok": 1 if hit else 0, "task": "trouble"})

    def _probe_once(self, cmd, timeout=25):
        try:
            proc = spawn(cmd, core.BASE_DIR)
        except Exception as e:
            return False, "启动失败：" + str(e), ""
        self.proc = proc
        lines, timed_out = [], []
        t0 = time.time()

        def expired():
            if self.user_stopped:
                return True
            if time.time() - t0 >= timeout:
                timed_out.append(True)
                return True
            return False

        got_json = False
        try:
            for line in iter_lines_watch(proc, abort=expired):
                s = line.strip()
                if not s:
                    continue
                lines.append(s)
                if s.startswith("{") and core.json_ok(s):
                    got_json = True
                    break
        finally:
            if proc.poll() is None:
                kill_tree(proc)
            proc.wait()
            self.proc = None
        if got_json:
            msg = "成功解析出条目"
        elif timed_out:
            msg = "超时 %ss" % timeout
        elif self.user_stopped:
            msg = "已取消"
        else:
            msg = lines[-1][:120] if lines else "无输出"
        return got_json, msg, "\n".join(lines[-25:])

    def on_trodone(self, ok, task):
        self.task_kind = None
        self._release_runner()
        self._task_ev()
        if task == "update":
            self._status("更新完成" if ok else "更新失败", "ok" if ok else "err")
        else:
            self._status("排障完成 · 已找到可用方案" if ok else "排障结束 · 四套方案都失败",
                         "ok" if ok else "err")
        self._put({"type": "trodone", "ok": ok, "task": task})

    # ---------------- 更新 ----------------
    def update_ytdlp(self, proxy):
        if self.task_kind:
            return self._busy()
        yt = self.cfg.get("yt_dlp_path") or find_tool("yt-dlp.exe")
        cmd = [yt, "-U"]
        if (proxy or "").strip():
            cmd += ["--proxy", proxy.strip()]
        if not self._reserve("update"):
            return self._busy()
        self.user_stopped = False
        self._task_ev()
        self._status("更新 yt-dlp…", "run")
        self._log("> " + command_text(cmd) + "\n", "cmd")
        self._worker = threading.Thread(target=self._update_run, args=(cmd,), daemon=True)
        self._worker.start()
        return {"ok": True}

    def _update_run(self, cmd):
        try:
            proc = spawn(cmd, core.BASE_DIR)
        except Exception as e:
            self._log("启动失败：%s\n" % e, "err")
            self._put({"type": "trodone", "ok": 0, "task": "update"})
            return
        self.proc = proc
        code = -1
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped, keep_blank=True):
                self._log(line + "\n", None)
            code = proc.wait()
        except Exception as e:
            self._log("更新出错：%s\n" % e, "err")
        finally:
            self.proc = None
        self._log("=== 更新完成，请重新探测 ===\n" if code == 0
                  else "=== 更新失败，通常是网络/代理不通 ===\n", "ok" if code == 0 else "err")
        self._put({"type": "trodone", "ok": 1 if code == 0 else 0, "task": "update"})

    # ---------------- 格式表 ----------------
    def fetch_formats(self, url, cfg):
        if self.task_kind:
            return self._busy()
        good, _ = sanitize_urls([url])
        if not good:
            return {"ok": False, "error": "请先填写一个有效链接"}
        if not self._reserve("formats"):
            return self._busy()
        self.user_stopped = False
        self._task_ev()
        cmd = build_formats_cmd(cfg, good[0])
        self._worker = threading.Thread(target=self._formats_run, args=(cmd,), daemon=True)
        self._worker.start()
        return {"ok": True}

    def _formats_run(self, cmd):
        try:
            proc = spawn(cmd, core.BASE_DIR)
            self.proc = proc
            out = list(iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                        keep_blank=True, stall_seconds=120))
            code = proc.wait()
        except Exception as e:
            out, code = [str(e)], -1
        self.proc = None
        if self.task_kind == "formats":
            self.task_kind = None
            self._release_runner()
            self._task_ev()
        self._put({"type": "formats", "lines": out, "code": code})

    def copy_cmd(self, urls, cfg):
        good, _ = sanitize_urls(urls or [])
        cmd = build_download_cmd(cfg, good)
        return {"ok": True, "text": command_text(cmd)}
```

pump_once 拦截分支补：

```python
                if t == "detdone" and self.task_kind == "detect":
                    self.on_detect_done(ev["code"])
                    continue
                if t == "trodone" and self.task_kind in ("trouble", "update"):
                    self.on_trodone(ev["ok"], ev.get("task", "trouble"))
                    continue
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/host.py tests/test_host.py
git commit -m "桌面 Web 重写：host 探测/排障/更新/格式表流水线"
```

### Task 6: host 封面预览 + 边下边播

**Files:**
- Modify: `YtDlpGUI/host.py`（替换 Task 4 的 `_maybe_start_play_probe`/`_set_playable` 占位，追加 get_thumb/play_now/_play_probe_loop；顶部补 `import base64`）
- Test: `tests/test_host.py`（追加 PreviewPlayTest）

**Interfaces:**
- Consumes: `fetch_thumb(url, cfg) -> (path, err)`、`is_public_http_url`、`real_download_path`、`probe_playable`、`make_preview_link`、`fmt_is_separated`（core）
- Produces: `host.get_thumb(url, cfg) -> {"ok":bool,"data_url"?:str,"err"?:str}`；`host.play_now() -> {"ok":bool,"error"?:str}`；`host._play_probe_loop(raw, cfg)`；事件 `playable{ok,reason}`（同步 ext_emit 与 PLAY_STATE）。

- [ ] **Step 1: 追加失败测试**

```python
class PreviewPlayTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()

    def tearDown(self):
        self.tmp.cleanup()

    def test_thumb_returns_data_url(self):
        png = os.path.join(self.tmp.name, "t.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        # 字面公网 IP：is_public_http_url 无需 DNS 即可判定公网（假域名会因解析失败被拒）
        with mock.patch.object(self.mod, "fetch_thumb", return_value=(png, None)):
            r = self.host.get_thumb("https://93.184.216.34/x.png", {})
        self.assertTrue(r["ok"])
        self.assertTrue(r["data_url"].startswith("data:image/png;base64,"))

    def test_thumb_private_url_rejected_without_network(self):
        r = self.host.get_thumb("https://127.0.0.1/x.png", {})
        self.assertFalse(r["ok"])

    def test_thumb_failure_degrades(self):
        with mock.patch.object(self.mod, "fetch_thumb", return_value=("", "403")):
            r = self.host.get_thumb("https://93.184.216.34/x.png", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["err"], "403")

    def test_play_disabled_when_not_playable(self):
        r = self.host.play_now()
        self.assertFalse(r["ok"])

    def test_separated_source_verdict(self):
        self.host._play_probe_loop("raw", {"fmt": "bv*+ba/b"})
        self.host.pump_once()
        ev = [e for e in self.events if e["type"] == "playable"][-1]
        self.assertFalse(ev["ok"])
        self.assertIn("分离", ev["reason"])

    def test_aria_verdict(self):
        self.host._play_probe_loop("raw", {"fmt": "b", "aria": True})
        self.host.pump_once()
        ev = [e for e in self.events if e["type"] == "playable"][-1]
        self.assertFalse(ev["ok"])
        self.assertIn("aria2c", ev["reason"])
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_host.PreviewPlayTest -v`
Expected: FAIL。

- [ ] **Step 3: 实现（替换占位并追加）**

```python
    # ---------------- 封面预览 ----------------
    def get_thumb(self, url, cfg):
        if not core.is_public_http_url(url):
            return {"ok": False, "err": "封面地址不合法（非公网 http/https）"}
        path, err = fetch_thumb(url, cfg)
        if err or not path:
            return {"ok": False, "err": err or "未知原因"}
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return {"ok": False, "err": str(e)}
        ext = os.path.splitext(path)[1].lower()
        mime = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
                ".bmp": "image/bmp"}.get(ext, "image/jpeg")
        return {"ok": True,
                "data_url": "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())}

    # ---------------- 边下边播 ----------------
    def _maybe_start_play_probe(self):
        cfg = self._task_cfg()
        raw = self.pstate.get("raw_path") or ""
        if not raw or not cfg.get("play_while") or raw == self._play_raw:
            return
        self._play_raw = raw
        if self._web_mod:
            with self._web_mod.PLAY_LOCK:
                self._web_mod.PLAY_STATE.update({"raw_path": raw, "outdir": self.dl_outdir,
                                                 "cfg": dict(cfg), "ok": False,
                                                 "reason": "正在判断能否边下边播…"})
        self._set_playable(False, "正在判断能否边下边播…")
        threading.Thread(target=self._play_probe_loop, args=(raw, dict(cfg)), daemon=True).start()

    def _play_probe_loop(self, raw, cfg):
        if fmt_is_separated(cfg):
            self._set_playable(False, "此源音视频分离，边下边播不适用")
            return
        if cfg.get("aria"):
            self._set_playable(False, "aria2c 外部下载器不支持边下边播")
            return
        reason = "文件还没写出来"
        deadline = time.time() + 3
        for _ in range(3):
            while time.time() < deadline:
                p = real_download_path(raw)
                try:
                    if p and os.path.getsize(p) >= 256 * 1024:
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            p = real_download_path(raw)
            if p:
                v = probe_playable(p)
                if v["ok"]:
                    self._set_playable(True, "")
                    return
                reason = v["reason"]
            deadline = time.time() + 5
        self._set_playable(False, "此源要下完才能播：%s" % reason)

    def _set_playable(self, ok, reason):
        self._play_ok = bool(ok)
        self._put({"type": "playable", "ok": bool(ok), "reason": reason})
        if self.ext_emit:
            self.ext_emit({"type": "playable", "ok": bool(ok), "reason": reason})
        if self._web_mod:
            with self._web_mod.PLAY_LOCK:
                self._web_mod.PLAY_STATE["ok"] = bool(ok)
                self._web_mod.PLAY_STATE["reason"] = reason

    def play_now(self):
        if not self._play_ok:
            return {"ok": False, "error": "此源不支持边下边播"}
        cfg = self._task_cfg()
        outdir = cfg.get("outdir") or os.path.join(core.BASE_DIR, "downloads")
        real = real_download_path(self._play_raw)
        if not real:
            return {"ok": False, "error": "文件还没写出来，稍等再试"}
        link, err = make_preview_link(real, outdir)
        if err:
            return {"ok": False, "error": err}
        try:
            os.startfile(link)
        except Exception as e:
            return {"ok": False, "error": "打开播放器失败：%s（建议安装 VLC 或 PotPlayer）" % e}
        return {"ok": True}
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/host.py tests/test_host.py
git commit -m "桌面 Web 重写：host 封面预览（data URL）与边下边播判定/播放"
```

---

### Task 7: host 嗅探接线 + 扩展服务生命周期

**Files:**
- Modify: `YtDlpGUI/host.py`（顶部补 `import sys`；追加服务/嗅探/shutdown 方法）
- Test: `tests/test_host.py`（追加 SniffServiceTest，用 stub mod 避免占端口）

**Interfaces:**
- Consumes: 服务模块的 `set_config_file, start_service, set_task_sink, sniff_items_json, clear_sniff_items, build_sniff_preview, preview_token_lookup, RUNNER, PLAY_LOCK, PLAY_STATE`
- Produces: `host.start_ext_service() / stop_ext_service() / set_ext_service(on) -> shape`；`host.sniff_items() -> {"ok","items","busy","svc"}`；`host.sniff_clear()`；`host.sniff_download(items) -> shape`；`host.sniff_preview(url) -> {"ok","duration","thumb_data_url","has_clip","error"?}`；`host.sniff_play_clip() -> shape`；`host.shutdown()`；事件 `svc{text,ok}`、`sniff_started`。

- [ ] **Step 1: 追加失败测试**

```python
class SniffServiceTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_host()
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.host = self.mod.Host(os.path.join(self.tmp.name, "config.json"),
                                  emit=self.events.append)
        self.host.mark_ready()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sniff_download_overrides_cfg(self):
        captured = {}
        def sink(task):
            captured.update(task)
            return True
        self.host.submit_server_task = sink
        items = [{"url": "https://cdn/x.m3u8", "referer": "https://p/", "ua": "UA"}]
        r = self.host.sniff_download(items)
        self.assertTrue(r["ok"])
        self.assertEqual(captured["cfg"]["fmt"], "b")
        self.assertTrue(captured["cfg"]["play_while"])
        self.assertFalse(captured["cfg"]["thumb"])
        self.assertEqual(captured["cfg"]["referer"], "https://p/")
        self.assertTrue(captured["no_playlist"])

    def test_sniff_download_busy(self):
        self.host.task_kind = "download"
        r = self.host.sniff_download([{"url": "https://cdn/x"}])
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])

    def test_sniff_items_without_service(self):
        d = self.host.sniff_items()
        self.assertEqual(d["items"], [])
        self.assertFalse(d["svc"])

    def test_sniff_preview_requires_service(self):
        r = self.host.sniff_preview("https://cdn/x")
        self.assertFalse(r["ok"])

    def test_sniff_play_clip_without_clip(self):
        r = self.host.sniff_play_clip()
        self.assertFalse(r["ok"])

    def test_set_ext_service_lifecycle_with_stub(self):
        class StubRunner:
            def reserve(self, kind): return True
            def finish(self): pass
        class StubMod:
            RUNNER = StubRunner()
            PLAY_LOCK = __import__("threading").Lock()
            PLAY_STATE = {}
            def set_config_file(self, p): pass
            def start_service(self):
                class Svc:
                    url = "http://127.0.0.1:8799"
                    def shutdown(self): pass
                return Svc()
            def set_task_sink(self, sink, stop=None): pass
        self.host.load_web_server = lambda: StubMod()
        r = self.host.set_ext_service(True)
        self.assertTrue(r["ok"])
        self.assertIsNotNone(self.host.web_svc)
        self.host.pump_once()
        self.assertTrue(any(e["type"] == "svc" and e["ok"] for e in self.events))
        self.host.set_ext_service(False)
        self.assertIsNone(self.host.web_svc)
        self.assertFalse(self.host.cfg["ext_service"])
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_host.SniffServiceTest -v`
Expected: FAIL。

- [ ] **Step 3: 实现（追加到 Host）**

```python
    # ---------------- 内嵌扩展服务 ----------------
    def load_web_server(self):
        import importlib.util
        path = os.path.join(core.BASE_DIR, "YtDlpWeb", "server.py")
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("ytdlp_web_server", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def start_ext_service(self):
        if self.web_svc is not None:
            return {"ok": True}
        try:
            mod = self.load_web_server()
            if mod is None:
                self._put({"type": "svc", "text": "扩展服务：缺少 YtDlpWeb/server.py", "ok": False})
                return {"ok": False, "error": "缺少 YtDlpWeb/server.py"}
            mod.set_config_file(self.config_file)
            self._web_mod = mod
            self.web_svc = mod.start_service()
            mod.set_task_sink(self.submit_server_task, stop=self.stop)
        except Exception as e:
            self.web_svc = None
            self._web_mod = None
            self._put({"type": "svc", "text": "扩展服务启动失败", "ok": False})
            return {"ok": False, "error": str(e)}
        self._put({"type": "svc", "text": "扩展服务：%s" % self.web_svc.url.replace("http://", ""),
                   "ok": True})
        self._log("[扩展服务] 已监听 %s\n" % self.web_svc.url, "info")
        return {"ok": True}

    def stop_ext_service(self):
        if self.web_svc is None:
            return
        if self._web_mod:
            self._web_mod.set_task_sink(None, stop=None)
        try:
            self.web_svc.shutdown()
        except Exception:
            pass
        self.web_svc = None
        self._web_mod = None
        self._put({"type": "svc", "text": "扩展服务：已停止", "ok": False})

    def set_ext_service(self, on):
        if on:
            r = self.start_ext_service()
        else:
            self.stop_ext_service()
            r = {"ok": True}
        self.save_config({"ext_service": bool(on)})
        return r

    # ---------------- 扩展嗅探 ----------------
    def sniff_items(self):
        if self._web_mod is None:
            return {"ok": True, "items": [], "busy": bool(self.task_kind), "svc": False}
        d = self._web_mod.sniff_items_json()
        d["svc"] = True
        return d

    def sniff_clear(self):
        if self._web_mod is not None:
            self._web_mod.clear_sniff_items()
        return {"ok": True}

    def sniff_download(self, items):
        if not items:
            return {"ok": False, "error": "先选中要下载的条目"}
        if self.task_kind:
            return self._busy()
        cfg = dict(self.cfg)
        cfg.update({"fmt": "b", "play_while": True, "thumb": False, "meta": False,
                    "sub": False, "embedsub": False})
        if len(items) == 1:
            it = items[0]
            if it.get("referer") or it.get("page_url"):
                cfg["referer"] = it.get("referer") or it.get("page_url")
            if it.get("ua"):
                cfg["ua"] = it["ua"]
        task = {"urls": [it["url"] for it in items], "kind": "media", "cfg": cfg,
                "emit": None, "no_playlist": True}
        if not self.submit_server_task(task):
            return self._busy()
        self._put({"type": "sniff_started"})
        return {"ok": True}

    def sniff_preview(self, url):
        if self._web_mod is None:
            return {"ok": False, "error": "扩展服务未启用"}
        try:
            res = self._web_mod.build_sniff_preview(url)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "未知原因"}
        out = {"ok": True, "duration": res.get("duration"),
               "thumb_data_url": "", "has_clip": False}
        if res.get("thumb"):
            path, _ = self._web_mod.preview_token_lookup(res["thumb"], "thumb")
            if path:
                try:
                    with open(path, "rb") as f:
                        out["thumb_data_url"] = ("data:image/png;base64,"
                                                 + base64.b64encode(f.read()).decode())
                except OSError:
                    pass
        if res.get("clip"):
            path, _ = self._web_mod.preview_token_lookup(res["clip"], "clip")
            self._sniff_clip = path or ""
            out["has_clip"] = bool(self._sniff_clip)
        return out

    def sniff_play_clip(self):
        if not self._sniff_clip:
            return {"ok": False, "error": "没有可播放的片段"}
        try:
            os.startfile(self._sniff_clip)
        except Exception as e:
            return {"ok": False, "error": "打开播放器失败：%s" % e}
        return {"ok": True}

    # ---------------- 收尾 ----------------
    def shutdown(self):
        self.save_config({})
        try:
            self.stop()
        except Exception:
            pass
        self.stop_ext_service()
        try:
            clear_preview_links(self.cfg.get("outdir") or os.path.join(core.BASE_DIR, "downloads"))
        except Exception:
            pass
        self._stop_flag = True
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/host.py tests/test_host.py
git commit -m "桌面 Web 重写：host 嗅探接线与扩展服务生命周期"
```

---

### Task 8: api.py 方法面 + 校验测试

**Files:**
- Create: `YtDlpGUI/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 2-7 的 Host 全部公开方法；`webview.create_file_dialog`（browse 类）
- Produces: `Api(host, window=None)`；`api.push(ev)`（evaluate_js 推送，window 为空时静默）；spec 第 4.1 节全部方法（get_config/save_config/start_download/detect/stop/troubleshoot/update_ytdlp/fetch_formats/copy_cmd/get_thumb/play_now/sniff_items/sniff_clear/sniff_download/sniff_preview/sniff_play_clip/history_list/history_clear/history_redownload/browse_outdir/browse_cookie/open_outdir/open_url/set_ext_service/exit/ping）。所有方法异常包成 `{ok:false,error}`。

- [ ] **Step 1: 写失败测试 tests/test_api.py**

```python
# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class ApiTest(unittest.TestCase):
    def setUp(self):
        host_mod = load_module("ytdlp_gui_host", "YtDlpGUI/host.py")
        api_mod = load_module("ytdlp_gui_api", "YtDlpGUI/api.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.host = host_mod.Host(os.path.join(self.tmp.name, "config.json"))
        self.api = api_mod.Api(self.host)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsafe_extra_blocked(self):
        cfg = dict(self.host.cfg, extra="--exec calc", outdir=self.tmp.name)
        r = self.api.start_download(["https://x/v"], cfg)
        self.assertFalse(r["ok"])

    def test_busy_semantics(self):
        self.host.task_kind = "download"
        r = self.api.start_download(["https://x/v"], dict(self.host.cfg))
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])

    def test_open_url_rejects_non_http(self):
        self.assertFalse(self.api.open_url("file:///C:/x")["ok"])
        self.assertFalse(self.api.open_url("javascript:alert(1)")["ok"])

    def test_play_now_no_path_param(self):
        with self.assertRaises(TypeError):
            self.api.play_now(path="C:/x")

    def test_sniff_play_clip_no_path_param(self):
        with self.assertRaises(TypeError):
            self.api.sniff_play_clip(path="C:/x")

    def test_get_config_shape(self):
        r = self.api.get_config()
        self.assertTrue(r["ok"])
        self.assertIn("cfg", r)
        self.assertIn("env", r)
        self.assertIn("modes", r)

    def test_history_redownload_uses_current_cfg(self):
        self.host.record_history({"urls": ["https://x/v"], "status": "ok", "code": 0})
        captured = {}
        def fake_start(urls, cfg, no_playlist=False):
            captured["urls"] = urls
            return {"ok": True}
        self.host.start_download = fake_start
        r = self.api.history_redownload(0)
        self.assertTrue(r["ok"])
        self.assertEqual(captured["urls"], ["https://x/v"])

    def test_push_without_window_silent(self):
        self.api.push({"type": "status", "text": "x"})  # 不抛异常即通过

    def test_internal_error_wrapped(self):
        def boom(*a, **kw):
            raise RuntimeError("炸了")
        self.host.detect = boom
        r = self.api.detect(["https://x"], {})
        self.assertFalse(r["ok"])
        self.assertIn("内部错误", r["error"])
```

- [ ] **Step 2: 运行确认失败**

Run: `py -3 -m unittest tests.test_api -v`
Expected: FAIL（api.py 不存在）。

- [ ] **Step 3: 写 api.py**

```python
# -*- coding: utf-8 -*-
"""js_api 方法面：参数校验 + 转调 host + 事件推送。

只被本机加载的自有 UI 触达，但输入仍全量校验；内部异常一律包成
{ok:false,error}，绝不把异常抛进 JS。
"""
import json
import webbrowser

import ytdlp_core as core


class Api:
    def __init__(self, host, window=None):
        self.host = host
        self.window = window
        host.emit = self.push

    def push(self, ev):
        if self.window is None:
            return
        try:
            self.window.evaluate_js(
                "window.__dispatch(%s)" % json.dumps(ev, ensure_ascii=False))
        except Exception:
            pass

    def _guard(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"ok": False, "error": "内部错误：%s" % e}

    def ping(self):
        return "pong"

    # ---------------- 配置/环境 ----------------
    def get_config(self):
        return {"ok": True, "cfg": dict(self.host.cfg), "env": self.host.get_env(),
                "modes": core.MODE_CHOICES, "mode_fmt": core.MODE_FMT,
                "mode_name": core.MODE_NAME, "templates": core.TEMPLATES,
                "browsers": core.BROWSERS, "ua_presets": list(core.UA_PRESETS.values())}

    def save_config(self, cfg):
        return {"ok": self.host.save_config(cfg)}

    # ---------------- 任务 ----------------
    def start_download(self, urls, cfg, no_playlist=False):
        return self._guard(self.host.start_download, urls, cfg, no_playlist)

    def detect(self, urls, cfg):
        return self._guard(self.host.detect, urls, cfg)

    def stop(self):
        return {"ok": self.host.stop()}

    def troubleshoot(self, urls, cfg):
        return self._guard(self.host.troubleshoot, urls, cfg)

    def update_ytdlp(self, proxy=""):
        return self._guard(self.host.update_ytdlp, proxy)

    def fetch_formats(self, url, cfg):
        return self._guard(self.host.fetch_formats, url, cfg)

    def copy_cmd(self, urls, cfg):
        return self._guard(self.host.copy_cmd, urls, cfg)

    # ---------------- 预览/播放 ----------------
    def get_thumb(self, url, cfg):
        return self._guard(self.host.get_thumb, url, cfg)

    def play_now(self):
        return self._guard(self.host.play_now)

    # ---------------- 扩展嗅探 ----------------
    def sniff_items(self):
        return self._guard(self.host.sniff_items)

    def sniff_clear(self):
        return self._guard(self.host.sniff_clear)

    def sniff_download(self, items):
        return self._guard(self.host.sniff_download, items)

    def sniff_preview(self, url):
        return self._guard(self.host.sniff_preview, url)

    def sniff_play_clip(self):
        return self._guard(self.host.sniff_play_clip)

    # ---------------- 历史 ----------------
    def history_list(self):
        return {"ok": True, "items": self.host.history_list()}

    def history_clear(self):
        self.host.history_clear()
        return {"ok": True}

    def history_redownload(self, i):
        items = self.host.history_list()
        if not (0 <= i < len(items)):
            return {"ok": False, "error": "记录不存在"}
        urls = items[i].get("urls") or []
        return self._guard(self.host.start_download, urls, dict(self.host.cfg), True)

    # ---------------- 系统 ----------------
    def browse_outdir(self):
        import webview
        r = webview.create_file_dialog(webview.FOLDER_DIALOG)
        return {"ok": True, "path": r[0] if r else ""}

    def browse_cookie(self):
        import webview
        r = webview.create_file_dialog(webview.OPEN_DIALOG,
                                       file_types=("文本文件 (*.txt)",))
        return {"ok": True, "path": r[0] if r else ""}

    def open_outdir(self, path=""):
        self.host.open_outdir(path or None)
        return {"ok": True}

    def open_url(self, url):
        u = str(url or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "只允许打开 http/https 链接"}
        webbrowser.open(u)
        return {"ok": True}

    def set_ext_service(self, on):
        return self._guard(self.host.set_ext_service, bool(on))

    def exit(self):
        self.host.shutdown()
        if self.window is not None:
            self.window.destroy()
        return {"ok": True}
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/api.py tests/test_api.py
git commit -m "桌面 Web 重写：js_api 方法面与输入校验"
```

---

### Task 9: main.py 完整入口 + 启动.bat

**Files:**
- Modify: `YtDlpGUI/main.py`（替换 Task 1 骨架）
- Modify: `YtDlpGUI/启动.bat`

**Interfaces:**
- Consumes: Host/Api；pywebview `create_window/start/events.loaded/events.closing`
- Produces: 可双击运行的桌面程序；`--selftest` 输出命令 JSON；`--debug` 开 devtools。

- [ ] **Step 1: 重写 main.py**

```python
# -*- coding: utf-8 -*-
"""yt-dlp 桌面版入口（pywebview + Web 前端）。"""
import ctypes
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(APP_DIR)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, APP_DIR)

import ytdlp_core as core  # noqa: E402
from host import Host  # noqa: E402
from api import Api  # noqa: E402

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def msgbox(text, title="yt-dlp 下载器"):
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)


def selftest():
    host = Host(CONFIG_FILE)
    cmd = core.build_download_cmd(host.cfg, ["https://www.youtube.com/watch?v=TEST"])
    print(json.dumps(cmd, ensure_ascii=False, indent=2))
    sys.stdout.flush()


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    try:
        import webview
    except ImportError:
        msgbox("缺少 pywebview。请在命令行执行：\npy -3 -m pip install pywebview")
        return
    host = Host(CONFIG_FILE)
    api = Api(host)
    try:
        window = webview.create_window(
            "yt-dlp 下载器   ·   可视化界面",
            url=os.path.join(APP_DIR, "ui", "index.html"),
            js_api=api, width=1100, height=900, min_size=(920, 700))
    except Exception as e:
        msgbox("创建窗口失败（可能缺少 WebView2 运行时）：\n%s\n\n请安装 WebView2：\n%s"
               % (e, WEBVIEW2_URL))
        return
    api.window = window

    def on_loaded():
        host.mark_ready()
        if host.cfg.get("ext_service", True):
            host.start_ext_service()
        else:
            host._put({"type": "svc", "text": "扩展服务：未启用", "ok": False})
        host.start_pump()

    def on_closing():
        if host.task_kind:
            host._put({"type": "exit_confirm"})
            return False
        host.shutdown()
        return True

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    try:
        webview.start(http_server=True, debug="--debug" in sys.argv)
    except Exception as e:
        msgbox("WebView2 启动失败：%s\n\n请安装 WebView2 运行时：\n%s" % (e, WEBVIEW2_URL))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 重写 启动.bat**

```bat
@echo off
chcp 65001 >nul
setlocal
title yt-dlp GUI
cd /d "%~dp0"

set "PYW="
set "PYC="
where pyw.exe >nul 2>nul && set "PYW=pyw -3"
if not defined PYW (
    where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
)
where python.exe >nul 2>nul && set "PYC=python.exe"
if not defined PYC (
    where py.exe >nul 2>nul && set "PYC=py -3"
)
if not defined PYW if not defined PYC goto nopy

rem 首次自动安装 pywebview
if defined PYC (
    %PYC% -c "import webview" >nul 2>nul
    if errorlevel 1 (
        echo 首次运行：正在安装 pywebview…
        %PYC% -m pip install pywebview
        if errorlevel 1 goto nopkg
    )
)

rem WebView2 运行时预判（Evergreen 安装登记在此键）
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 WebView2 运行时。若窗口无法打开，请安装：
    echo        https://developer.microsoft.com/microsoft-edge/webview2/
)

if defined PYW (
    start "" %PYW% "%~dp0main.py" %*
) else (
    start "" %PYC% "%~dp0main.py" %*
)
exit /b 0

:nopy
echo.
echo [错误] 没有找到 Python。
echo        请安装 Python 3.9 或更新版本（https://www.python.org/downloads/），
echo        安装时勾选 "Add python.exe to PATH"，然后再双击本文件。
echo.
pause
exit /b 1

:nopkg
echo.
echo [错误] pywebview 安装失败。请手动执行：py -3 -m pip install pywebview
echo.
pause
exit /b 1
```

- [ ] **Step 3: 验证 --selftest**

Run: `py -3 YtDlpGUI\main.py --selftest`
Expected: 输出命令 JSON 数组，退出码 0。

- [ ] **Step 4: 验证窗口启动**

Run: `py -3 YtDlpGUI\main.py --debug`
Expected: 窗口打开显示桥探针页（Task 1 的 ui 仍在）；扩展服务日志出现在 console（host 已接线）。关闭无任务窗口正常退出。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/main.py YtDlpGUI/启动.bat
git commit -m "桌面 Web 重写：完整入口（WebView2 降级提示、忙时关窗拦截）与启动.bat 自动装依赖"
```

---

### Task 10: 前端骨架——主题 / 页签 / 布局 / 完整 HTML

**Files:**
- Modify: `YtDlpGUI/ui/index.html`（替换探针页为完整骨架）
- Create: `YtDlpGUI/ui/css/app.css`
- Create: `YtDlpGUI/ui/js/theme.js`
- Create: `YtDlpGUI/ui/js/tabs.js`

**Interfaces:**
- Consumes: bridge.js（Task 1）
- Produces: 全部 DOM id（后续模块按 id 操作）：页签 `tab-download/tab-sniff/tab-history` + 面板 `panel-download/panel-sniff/panel-history`；主题 `#theme`；表单字段 id 与 cfg 键同名（url/outdir/tpl/mode/merge/fmt/browser/browser_profile/ua/referer/headers/generic/extractor_args/retries/pl/items/ignore/open_after/net_hard/play_while/sub/sublang/embedsub/thumb/meta/rate/proxy/cookie/conc/conc_n/aria/aria_args/flat/extra/ext_service）；工具栏 `btn-start/btn-detect/btn-stop/btn-trouble/btn-update/btn-copy/btn-clearlog` + `#status`；进度 `prog-file/prog-bar/prog-pct/prog-detail/btn-play/play-reason`；结果 `results-body/btn-all/btn-none/btn-dl-sel/btn-clear-results/result-count/pv-img/pv-meta/btn-pv-open`；日志 `#log`；嗅探 `sn-fold/btn-sn-refresh/btn-sn-clear/sniff-body/btn-sn-download/btn-sn-preview/sn-count/sn-img/sn-pv/btn-sn-play`；历史 `history-body/btn-history-clear`；对话框 `dlg-formats/formats-text/btn-fmt-best/btn-fmt-audio`、`dlg-confirm/confirm-title/confirm-text/btn-confirm-cancel/btn-confirm-ok`；`#toast`。`initTheme()`、`initTabs() -> {select, tabs}`。

- [ ] **Step 1: 写完整 index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<script>
try {
  document.querySelector('meta[name="color-scheme"]').content =
    localStorage.getItem('color-scheme') || 'light dark';
} catch (e) {}
</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt-dlp 下载器 · 可视化界面</title>
<link rel="stylesheet" href="css/app.css">
</head>
<body>
<header class="top">
  <h1>yt-dlp 下载器</h1>
  <span id="env" class="muted"></span>
  <span id="svc" class="muted">扩展服务：启动中…</span>
</header>

<div class="tabs">
  <div role="tablist" aria-label="功能页签">
    <button type="button" role="tab" id="tab-download" data-panel="panel-download"
            aria-selected="true" tabindex="0">下载</button>
    <button type="button" role="tab" id="tab-sniff" data-panel="panel-sniff"
            aria-selected="false" tabindex="-1">扩展嗅探</button>
    <button type="button" role="tab" id="tab-history" data-panel="panel-history"
            aria-selected="false" tabindex="-1">下载历史</button>
  </div>

  <section role="tabpanel" id="panel-download" aria-labelledby="tab-download" tabindex="0">
    <div class="panel-body">
      <div class="cols">
        <div class="col">
          <fieldset>
            <legend>① 下载链接</legend>
            <textarea id="url" rows="4" placeholder="每行一个链接，可一次粘贴多个；也可以是播放列表地址"></textarea>
            <div class="row">
              <button type="button" id="btn-paste">剪贴板粘贴</button>
              <button type="button" id="btn-url-clear">清空</button>
            </div>
          </fieldset>
          <fieldset>
            <legend>② 保存位置</legend>
            <label>输出目录 <input id="outdir" type="text"></label>
            <div class="row">
              <button type="button" id="btn-browse-outdir">浏览…</button>
              <button type="button" id="btn-open-outdir">打开</button>
            </div>
            <label>文件名 <select id="tpl"></select></label>
            <p class="hint">占位符：title 标题、id 编号、uploader 作者、upload_date 日期、playlist_index 列表序号</p>
          </fieldset>
          <fieldset>
            <legend>③ 内容与格式</legend>
            <label>下载模式 <select id="mode"></select></label>
            <label>合并容器
              <select id="merge">
                <option>mp4</option><option>mkv</option><option>webm</option><option>不转换</option>
              </select>
            </label>
            <label>格式代码 <input id="fmt" type="text"></label>
            <div class="row">
              <button type="button" id="btn-formats">查看可用格式…</button>
            </div>
            <p class="hint">自定义模式下可填 137+140 这样的组合；其他模式由程序自动填好</p>
          </fieldset>
          <fieldset>
            <legend>④ 访问与身份（解决探测不到）</legend>
            <label>浏览器 Cookies <select id="browser"></select></label>
            <label>配置 <input id="browser_profile" type="text"></label>
            <label>User-Agent <input id="ua" type="text" list="ua-list"></label>
            <datalist id="ua-list"></datalist>
            <label>Referer <input id="referer" type="text"></label>
            <label>请求头 <textarea id="headers" rows="3" placeholder="每行一条 Key: Value"></textarea></label>
            <label class="check"><input id="generic" type="checkbox"> 通用提取器兜底</label>
            <label>提取器参数 <input id="extractor_args" type="text" placeholder="youtube:player_client=web"></label>
            <label>重试 <input id="retries" type="text" size="6"></label>
            <p class="hint">从浏览器读登录态（读取时请关闭该浏览器）</p>
          </fieldset>
        </div>
        <div class="col">
          <fieldset>
            <legend>⑤ 播放列表与容错</legend>
            <label class="check"><input id="pl" type="checkbox"> 下载整个播放列表</label>
            <label>范围 <input id="items" type="text" placeholder="1-5 或 1,3,7 或 2-"></label>
            <label class="check"><input id="ignore" type="checkbox"> 出错继续下一个</label>
            <label class="check"><input id="open_after" type="checkbox"> 完成后打开文件夹</label>
            <label class="check"><input id="net_hard" type="checkbox"> 失败后自动换姿势重试（浏览器指纹 / 分块传输）</label>
            <label class="check"><input id="play_while" type="checkbox"> 边下边播（不影响下载速度）</label>
            <p class="hint">只对能流式播放的源有效（HLS / mkv / webm）；分离源与 aria2c 会置灰并说明原因</p>
          </fieldset>
          <fieldset>
            <legend>⑥ 附加内容</legend>
            <label class="check"><input id="sub" type="checkbox"> 下载字幕（含自动字幕）</label>
            <label>字幕语言 <input id="sublang" type="text" placeholder="zh-Hans,zh,en"></label>
            <label class="check"><input id="embedsub" type="checkbox"> 字幕嵌入视频</label>
            <label class="check"><input id="thumb" type="checkbox"> 嵌入封面缩略图</label>
            <label class="check"><input id="meta" type="checkbox"> 写入元数据</label>
          </fieldset>
          <fieldset>
            <legend>⑦ 网络与加速</legend>
            <label>限速 <input id="rate" type="text" placeholder="500K / 5M，留空不限速"></label>
            <label>代理 <input id="proxy" type="text" placeholder="http://127.0.0.1:7890"></label>
            <label>Cookies 文件 <input id="cookie" type="text"></label>
            <div class="row"><button type="button" id="btn-browse-cookie">选择…</button></div>
            <label class="check"><input id="conc" type="checkbox"> 分片并发加速（-N）</label>
            <label>分片数 <input id="conc_n" type="number" min="1" max="32" size="5"></label>
            <label class="check"><input id="aria" type="checkbox"> 外部下载器 aria2c <span id="aria-state" class="hint"></span></label>
            <label>aria2c 参数 <input id="aria_args" type="text"></label>
          </fieldset>
          <fieldset>
            <legend>⑧ 高级</legend>
            <label class="check"><input id="flat" type="checkbox"> 快速探测（--flat-playlist，不解析清晰度）</label>
            <label>附加参数 <input id="extra" type="text" placeholder="--download-archive archive.txt"></label>
            <label class="check"><input id="ext_service" type="checkbox"> 启用浏览器扩展支持（本机 127.0.0.1:8765）</label>
            <label>界面主题
              <select id="theme">
                <option value="system">跟随系统</option>
                <option value="light">浅色</option>
                <option value="dark">深色</option>
              </select>
            </label>
          </fieldset>
        </div>
      </div>

      <div class="toolbar">
        <button type="button" id="btn-start" class="primary">开始下载</button>
        <button type="button" id="btn-detect">探测资源</button>
        <button type="button" id="btn-stop" disabled>停止</button>
        <button type="button" id="btn-trouble">自动排障</button>
        <button type="button" id="btn-update">更新 yt-dlp</button>
        <button type="button" id="btn-copy">复制命令</button>
        <button type="button" id="btn-clearlog">清空日志</button>
        <span id="status" aria-live="polite">就绪</span>
      </div>

      <fieldset class="progress">
        <legend>下载进度</legend>
        <div id="prog-file" class="muted">等待开始</div>
        <div class="row">
          <progress id="prog-bar" max="100" value="0"></progress>
          <span id="prog-pct">0%</span>
        </div>
        <div id="prog-detail" class="muted">—</div>
        <div class="row">
          <button type="button" id="btn-play" disabled>边下边播</button>
          <span id="play-reason" class="hint">还没有开始下载</span>
        </div>
      </fieldset>

      <fieldset class="results">
        <legend>探测结果（点 ✓ 列勾选要下载的条目）</legend>
        <div class="table-wrapper" tabindex="0" role="region" aria-label="探测结果表">
          <table>
            <thead>
              <tr><th>✓</th><th>标题</th><th>时长</th><th>清晰度</th><th>大小</th><th>上传者</th></tr>
            </thead>
            <tbody id="results-body"></tbody>
          </table>
        </div>
        <div class="row">
          <button type="button" id="btn-all">全选</button>
          <button type="button" id="btn-none">全不选</button>
          <button type="button" id="btn-dl-sel" disabled>下载选中项</button>
          <button type="button" id="btn-clear-results">清空列表</button>
          <span id="result-count" class="hint">尚未探测</span>
        </div>
        <div class="preview row">
          <img id="pv-img" alt="" hidden>
          <span id="pv-img-text" class="hint">选中一行看封面</span>
          <div>
            <div id="pv-meta"></div>
            <button type="button" id="btn-pv-open" disabled>打开原网页</button>
          </div>
        </div>
      </fieldset>

      <fieldset class="logbox">
        <legend>运行日志</legend>
        <div id="log" tabindex="0" role="log" aria-label="运行日志"></div>
      </fieldset>
    </div>
  </section>

  <section role="tabpanel" id="panel-sniff" aria-labelledby="tab-sniff" tabindex="0" hidden>
    <div class="panel-body">
      <div class="toolbar">
        <span class="hint">浏览器扩展嗅探到的媒体地址（开着扩展的页面会自动出现在这里）</span>
        <label class="check"><input id="sn-fold" type="checkbox" checked> 折叠分片</label>
        <button type="button" id="btn-sn-refresh">刷新</button>
        <button type="button" id="btn-sn-clear">清空列表</button>
      </div>
      <div class="table-wrapper" tabindex="0" role="region" aria-label="嗅探列表">
        <table>
          <thead>
            <tr><th></th><th>标题</th><th>地址</th><th>类型</th><th>大小</th><th>来源</th><th>页面</th><th>时间</th></tr>
          </thead>
          <tbody id="sniff-body"></tbody>
        </table>
      </div>
      <div class="toolbar">
        <button type="button" id="btn-sn-download">下载选中</button>
        <button type="button" id="btn-sn-preview">预览选中</button>
        <span id="sn-count" class="hint"></span>
      </div>
      <fieldset>
        <legend>预览（缩略图 / 时长 / 短片段）</legend>
        <div class="row">
          <img id="sn-img" alt="" hidden>
          <div>
            <div id="sn-pv" class="hint">选中一条后点「预览选中」</div>
            <button type="button" id="btn-sn-play" disabled>播放片段</button>
          </div>
        </div>
      </fieldset>
    </div>
  </section>

  <section role="tabpanel" id="panel-history" aria-labelledby="tab-history" tabindex="0" hidden>
    <div class="panel-body">
      <div class="toolbar">
        <span class="hint">本机下载记录（最多 500 条，仅存本机）</span>
        <button type="button" id="btn-history-clear">清空历史</button>
      </div>
      <div class="table-wrapper" tabindex="0" role="region" aria-label="下载历史">
        <table>
          <thead>
            <tr><th>时间</th><th>标题</th><th>结果</th><th>耗时</th><th>来源</th><th>操作</th></tr>
          </thead>
          <tbody id="history-body"></tbody>
        </table>
      </div>
    </div>
  </section>
</div>

<dialog id="dlg-formats" aria-labelledby="formats-title">
  <h1 id="formats-title">可用格式</h1>
  <pre id="formats-text" class="mono"></pre>
  <p class="hint">双击某一行可选中该格式</p>
  <form method="dialog" class="row">
    <button value="best" id="btn-fmt-best">最佳画质</button>
    <button value="audio" id="btn-fmt-audio">最佳音频</button>
    <button value="close">关闭</button>
  </form>
</dialog>

<dialog id="dlg-confirm" aria-labelledby="confirm-title">
  <h1 id="confirm-title">确认</h1>
  <p id="confirm-text"></p>
  <form method="dialog" class="row">
    <button value="cancel" id="btn-confirm-cancel" autofocus>取消</button>
    <button value="ok" id="btn-confirm-ok">确定</button>
  </form>
</dialog>

<div id="toast" aria-live="polite"></div>
<script type="module" src="js/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 写 css/app.css**

```css
/* 设计令牌：light-dark() 随 color-scheme 自动切换（WebView2 Evergreen） */
:root {
  color-scheme: light dark;
  &:has(> head > meta[name="color-scheme"][content="dark"]) { color-scheme: dark; }
  &:has(> head > meta[name="color-scheme"][content="light"]) { color-scheme: light; }

  --bg: light-dark(#f6f8fa, #0d1117);
  --surface: light-dark(#ffffff, #161b22);
  --border: light-dark(#d0d7de, #30363d);
  --text: light-dark(#1f2328, #e6edf3);
  --muted: light-dark(#57606a, #8b949e);
  --accent: light-dark(#0550ae, #4493f8);
  --ok: light-dark(#1a7f37, #3fb950);
  --warn: light-dark(#9a6700, #d29922);
  --err: light-dark(#cf222e, #f85149);
  accent-color: var(--accent);
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 12px;
  background: var(--bg); color: var(--text);
  font: 14px/1.5 "Microsoft YaHei UI", system-ui, sans-serif;
}
.mono { font-family: "Cascadia Mono", Consolas, monospace; }
.muted { color: var(--muted); }
.hint { color: var(--muted); font-size: 12px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

.top { display: flex; gap: 14px; align-items: baseline; padding: 0 4px 8px; }
.top h1 { font-size: 18px; margin: 0; }
.top span { font-size: 12px; }

/* 页签：选中态从 aria-selected 取，下划线用伪元素 border（强制颜色模式可见） */
[role="tablist"] { display: flex; gap: 4px; border-block-end: 1px solid var(--border); }
[role="tab"] {
  position: relative; padding: 8px 16px; border: 0; background: none;
  color: var(--muted); font: inherit; cursor: pointer;
}
[role="tab"][aria-selected="true"] { color: var(--accent); }
[role="tab"][aria-selected="true"]::after {
  content: ""; position: absolute; inset: auto 0 0;
  border-block-end: 3px solid var(--accent);
}
[role="tabpanel"] { outline: none; }
.panel-body { padding: 12px 4px; display: grid; gap: 12px; }

.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 860px) { .cols { grid-template-columns: 1fr; } }
.col { display: grid; gap: 12px; align-content: start; }

fieldset {
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); padding: 10px 12px; margin: 0;
  display: grid; gap: 6px;
}
legend { font-weight: 600; padding: 0 6px; }
label { display: grid; gap: 2px; font-size: 13px; }
label.check { display: flex; gap: 6px; align-items: center; }
input[type="text"], input[type="number"], select, textarea {
  font: inherit; color: var(--text); background: var(--bg);
  border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px;
}
textarea { resize: vertical; }
button {
  font: inherit; padding: 5px 12px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
}
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:disabled { opacity: .5; cursor: default; }

.toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
#status { margin-inline-start: auto; font-weight: 600; }
#status[data-tone="ok"] { color: var(--ok); }
#status[data-tone="err"] { color: var(--err); }
#status[data-tone="run"] { color: var(--accent); }
#status[data-tone="warn"] { color: var(--warn); }

progress { flex: 1; block-size: 14px; }
#prog-pct { font-weight: 600; min-inline-size: 48px; text-align: end; }

/* 表格：语义 table + sticky 表头；wrapper 键盘可滚 */
.table-wrapper {
  overflow: auto; max-block-size: 320px;
  border: 1px solid var(--border); border-radius: 6px;
}
table { border-collapse: separate; border-spacing: 0; inline-size: 100%; }
thead { position: sticky; inset-block-start: 0; z-index: 1; }
th { background: var(--surface); border-block-end: 1px solid var(--border); }
th, td { padding: 4px 8px; text-align: start; font-size: 13px; }
td { border-block-end: 1px solid var(--border); }
td.ellipsis { max-inline-size: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
tr.checked td { color: var(--accent); }
tr.seg td { color: var(--muted); }

.preview img { max-inline-size: 320px; border-radius: 6px; }

#log {
  block-size: 200px; overflow: auto; padding: 8px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px;
  white-space: pre-wrap; word-break: break-all;
}
#log .err { color: var(--err); }
#log .warn { color: var(--warn); }
#log .ok { color: var(--ok); }
#log .cmd { color: var(--accent); }
#log .prog { color: var(--muted); }

/* 对话框：原生 dialog，防背后页面滚动 */
dialog {
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--surface); color: var(--text);
  padding: 16px 20px; max-inline-size: min(900px, 92vw);
}
dialog:modal { overscroll-behavior: contain; }
dialog::backdrop { background: rgb(0 0 0 / .4); overflow: hidden; overscroll-behavior: contain; }
dialog h1 { font-size: 16px; margin: 0 0 8px; }
#formats-text {
  max-block-size: 50vh; overflow: auto; font-size: 12px;
  background: var(--bg); padding: 8px; border-radius: 6px;
}

#toast {
  position: fixed; inset-block-end: 16px; inset-inline-end: 16px;
  display: grid; gap: 6px; z-index: 10;
}
#toast .item {
  background: var(--surface); border: 1px solid var(--border);
  border-inline-start: 4px solid var(--accent);
  padding: 8px 12px; border-radius: 6px; max-inline-size: 360px;
}
#toast .item.err { border-inline-start-color: var(--err); }
```

- [ ] **Step 3: 写 js/theme.js**

```js
// 三态主题：system（删存储值）/ light / dark；meta 与 CSS color-scheme 同步。
const KEY = 'color-scheme';
const meta = document.querySelector('meta[name="color-scheme"]');

function stored() {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

export function initTheme() {
  const sel = document.getElementById('theme');
  meta.content = stored() || 'light dark';
  sel.value = stored() || 'system';
  sel.addEventListener('change', () => {
    const v = sel.value;
    try {
      if (v === 'system') localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, v);
    } catch {}
    meta.content = v === 'system' ? 'light dark' : v;
  });
}
```

- [ ] **Step 4: 写 js/tabs.js**

```js
// ARIA 页签 + roving tabindex（focusgroup 支持面窄，手写零依赖）；选择跟随焦点。
export function initTabs() {
  const list = document.querySelector('[role="tablist"]');
  const tabs = [...list.querySelectorAll('[role="tab"]')];
  const panels = tabs.map(t => document.getElementById(t.dataset.panel));
  function select(tab, focus = true) {
    tabs.forEach((t, i) => {
      const on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      panels[i].hidden = !on;
    });
    if (focus) tab.focus();
    tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  list.addEventListener('click', e => {
    const t = e.target.closest('[role="tab"]');
    if (t) select(t);
  });
  list.addEventListener('keydown', e => {
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
    if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
    if (e.key === 'Home') j = 0;
    if (e.key === 'End') j = tabs.length - 1;
    if (j !== null) { e.preventDefault(); select(tabs[j]); }
  });
  return { select, tabs };
}
```

- [ ] **Step 5: 写临时 js/app.js（本任务仅初始化主题/页签，Task 11 起扩为完整装配）**

```js
import { initTheme } from './theme.js';
import { initTabs } from './tabs.js';
initTheme();
initTabs();
```

- [ ] **Step 6: 验证**

Run: `py -3 YtDlpGUI\main.py --debug`
Expected: 三页签可点、方向键切换、下划线指示器跟随；主题下拉切浅色/深色立即生效，重启后保持；「跟随系统」恢复跟随；窄窗口（<860px）设置区折单列；整页可滚。

- [ ] **Step 7: Commit**

```bash
git add YtDlpGUI/ui
git commit -m "桌面 Web 重写：前端骨架（light-dark 主题、ARIA 页签、完整页面结构与设计令牌）"
```

---

### Task 11: 下载页设置区 + 桥接装配

**Files:**
- Create: `YtDlpGUI/ui/js/download.js`
- Modify: `YtDlpGUI/ui/js/app.js`

**Interfaces:**
- Consumes: `call/on`（bridge）、`get_config` 返回的 cfg/modes/mode_fmt/mode_name/templates/browsers/ua_presets
- Produces: `initDownload({dialogs, results, tabs}) -> {applyConfig, collectCfg, getUrls}`；FIELDS 映射（id↔cfg 键）；模式联动（custom 才放开 fmt）；`btn-paste/btn-url-clear/btn-browse-outdir/btn-open-outdir/btn-browse-cookie` 接线；`ext_service` 变更调 set_ext_service；`applyfix` 事件更新表单。

- [ ] **Step 1: 写 js/download.js**

```js
import { call, on } from './bridge.js';

// id → [cfg 键, 类型]；checkbox 用 checked，其余用 value
const FIELDS = {
  outdir: ['outdir', 'text'], tpl: ['tpl', 'text'], mode: ['mode', 'mode'],
  merge: ['merge', 'text'], fmt: ['fmt', 'text'],
  browser: ['browser', 'text'], browser_profile: ['browser_profile', 'text'],
  ua: ['ua', 'text'], referer: ['referer', 'text'], headers: ['headers', 'text'],
  generic: ['generic', 'bool'], extractor_args: ['extractor_args', 'text'],
  retries: ['retries', 'text'],
  pl: ['pl', 'bool'], items: ['items', 'text'], ignore: ['ignore', 'bool'],
  open_after: ['open_after', 'bool'], net_hard: ['net_hard', 'bool'],
  play_while: ['play_while', 'bool'],
  sub: ['sub', 'bool'], sublang: ['sublang', 'text'], embedsub: ['embedsub', 'bool'],
  thumb: ['thumb', 'bool'], meta: ['meta', 'bool'],
  rate: ['rate', 'text'], proxy: ['proxy', 'text'], cookie: ['cookie', 'text'],
  conc: ['conc', 'bool'], conc_n: ['conc_n', 'text'],
  aria: ['aria', 'bool'], aria_args: ['aria_args', 'text'],
  flat: ['flat', 'bool'], extra: ['extra', 'text'], ext_service: ['ext_service', 'bool'],
};

let meta = { mode_fmt: {}, mode_name: {} };
let labelToKey = {};

export function collectCfg() {
  const cfg = {};
  for (const [id, [key, kind]] of Object.entries(FIELDS)) {
    const el = document.getElementById(id);
    if (kind === 'bool') cfg[key] = el.checked;
    else if (kind === 'mode') cfg[key] = labelToKey[el.value] || el.value;
    else cfg[key] = el.value.trim();
  }
  return cfg;
}

export function getUrls() {
  return document.getElementById('url').value.split('\n')
    .map(s => s.trim()).filter(s => s && !s.startsWith('#'));
}

function syncMode(keepFmt) {
  const key = labelToKey[document.getElementById('mode').value];
  document.getElementById('fmt').disabled = key !== 'custom';
  if (!keepFmt && meta.mode_fmt[key]) document.getElementById('fmt').value = meta.mode_fmt[key];
}

export function applyConfig(cfg, m) {
  meta = m;
  labelToKey = Object.fromEntries(Object.entries(m.mode_name).map(([k, v]) => [v, k]));
  const mode = document.getElementById('mode');
  mode.innerHTML = m.modes.map(x => `<option>${x}</option>`).join('');
  document.getElementById('tpl').innerHTML =
    m.templates.map(x => `<option>${x}</option>`).join('');
  document.getElementById('browser').innerHTML =
    m.browsers.map(x => `<option>${x}</option>`).join('');
  document.getElementById('ua-list').innerHTML =
    m.ua_presets.map(x => `<option value="${x.replace(/"/g, '&quot;')}">`).join('');
  for (const [id, [key, kind]] of Object.entries(FIELDS)) {
    const el = document.getElementById(id);
    const v = cfg[key];
    if (kind === 'bool') el.checked = !!v;
    else if (kind === 'mode') el.value = m.mode_name[v] || m.modes[0];
    else el.value = v ?? '';
  }
  syncMode(true);
  document.getElementById('aria-state').textContent =
    m.env.has_aria2c ? '已就绪' : '未检测到 aria2c.exe';
  document.getElementById('env').textContent =
    `yt-dlp: ${m.env.yt_dlp_path} · ffmpeg 已就绪 · ` +
    (m.env.has_aria2c ? 'aria2c 已就绪' : '未检测到 aria2c');
}

export function initDownload({ dialogs, results, tabs }) {
  document.getElementById('mode').addEventListener('change', () => syncMode(false));
  document.getElementById('btn-paste').addEventListener('click', async () => {
    try {
      const t = await navigator.clipboard.readText();
      const box = document.getElementById('url');
      box.value += (box.value && !box.value.endsWith('\n') ? '\n' : '') + t.trim();
    } catch { dialogs.toast('读剪贴板失败', 'err'); }
  });
  document.getElementById('btn-url-clear').addEventListener('click', () => {
    document.getElementById('url').value = '';
  });
  document.getElementById('btn-browse-outdir').addEventListener('click', async () => {
    const r = await call('browse_outdir');
    if (r.path) document.getElementById('outdir').value = r.path;
  });
  document.getElementById('btn-open-outdir').addEventListener('click', () =>
    call('open_outdir', document.getElementById('outdir').value));
  document.getElementById('btn-browse-cookie').addEventListener('click', async () => {
    const r = await call('browse_cookie');
    if (r.path) document.getElementById('cookie').value = r.path;
  });
  document.getElementById('ext_service').addEventListener('change', async (e) => {
    const r = await call('set_ext_service', e.target.checked);
    if (!r.ok) dialogs.toast(r.error || '扩展服务切换失败', 'err');
  });
  on('applyfix', (ev) => {
    for (const k of ['ua', 'referer', 'browser', 'generic']) {
      const el = document.getElementById(k);
      if (el.type === 'checkbox') el.checked = !!ev.cfg[k];
      else el.value = ev.cfg[k] ?? '';
    }
    const parts = [];
    if (ev.touched?.length) parts.push('已自动应用：' + ev.touched.join('、'));
    if (ev.manual?.length) parts.push('需手动设置：' + ev.manual.join('、'));
    dialogs.toast(parts.join('；') || '排障完成');
  });
  on('svc', (ev) => {
    const el = document.getElementById('svc');
    el.textContent = ev.text;
    el.style.color = ev.ok ? 'var(--ok)' : 'var(--warn)';
  });
  on('sniff_started', () => tabs.select(document.getElementById('tab-download')));
  return { applyConfig, collectCfg, getUrls };
}
```

- [ ] **Step 2: 扩 app.js（本任务阶段）**

```js
import { ready, call } from './bridge.js';
import { initTheme } from './theme.js';
import { initTabs } from './tabs.js';
import { initDownload } from './download.js';

await ready;
initTheme();
const tabs = initTabs();
const dialogs = { toast: (t, k) => console.log('[toast]', t, k) }; // Task 13 换真实实现
const results = {};                                              // Task 12 换真实实现
const download = initDownload({ dialogs, results, tabs });
const r = await call('get_config');
download.applyConfig(r.cfg, r);
```

- [ ] **Step 3: 验证**

Run: `py -3 YtDlpGUI\main.py --debug`
Expected: 表单按 config.json 填好；切「自定义」模式 fmt 可编辑、切回最佳画质自动填 bv*+ba/b；浏览目录/cookies 弹原生对话框；ext_service 勾选切换后顶栏服务状态变化；改代理等设置后点下载（Task 13 接）前可先在 console 执行 `window.pywebview.api.save_config({...})` 验证存盘。

- [ ] **Step 4: Commit**

```bash
git add YtDlpGUI/ui/js/download.js YtDlpGUI/ui/js/app.js
git commit -m "桌面 Web 重写：下载页设置区（配置收集/应用、模式联动、原生对话框接线）"
```

---

### Task 12: 进度 / 日志 / 结果表 / 预览 / 播放

**Files:**
- Create: `YtDlpGUI/ui/js/log.js`
- Create: `YtDlpGUI/ui/js/results.js`
- Modify: `YtDlpGUI/ui/js/app.js`

**Interfaces:**
- Consumes: 事件 `logbatch/progress/status/task/detrow/detdone/done/playable/preview`；`call('get_thumb'|'play_now'|'start_download'|'open_url')`
- Produces: `initLog() -> {clear}`；`initResults({download, dialogs}) -> {clear, addRow, selectedUrls}`；进度/播放接线在 app.js。日志上限 4000 行裁前 800；prog 行只留最新；结果行点击=勾选+预览，gen 守卫防串图。

> 对 spec 的一处实现细化：spec 4.2 的 `preview` 推送事件改为「`get_thumb` 请求/响应 + 前端 gen 代次守卫」，防串图效果等价，少一条事件通路；其余事件不变。

- [ ] **Step 1: 写 js/log.js**

```js
import { on } from './bridge.js';

const MAX = 4000;
let progEl = null;

export function initLog() {
  const box = document.getElementById('log');
  function append(text, tag) {
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    if (tag === 'prog') {
      if (!progEl) {
        progEl = document.createElement('div');
        progEl.className = 'prog';
        box.appendChild(progEl);
      }
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
  on('logbatch', ev => { for (const it of ev.items) append(it.text, it.tag); });
  document.getElementById('btn-clearlog').addEventListener('click', () => {
    box.textContent = '';
    progEl = null;
  });
  return { clear: () => { box.textContent = ''; progEl = null; } };
}
```

- [ ] **Step 2: 写 js/results.js**

```js
import { call, on } from './bridge.js';

let rows = [];
const checked = new Set();
let gen = 0;
let previewUrl = '';

function fmtDur(sec) {
  if (!sec && sec !== 0) return '-';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
function fmtSize(b) {
  if (!b) return '-';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + u[i];
}

function updateCount() {
  document.getElementById('result-count').textContent =
    rows.length ? `共 ${rows.length} 条，已选 ${checked.size} 条` : '尚未探测';
  document.getElementById('btn-dl-sel').disabled = !rows.length;
}

function renderRow(i) {
  const info = rows[i];
  const tr = document.createElement('tr');
  tr.dataset.i = i;
  if (checked.has(i)) tr.classList.add('checked');
  const tdCheck = document.createElement('td');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = checked.has(i);
  cb.setAttribute('aria-label', '勾选 ' + (info.title || ''));
  cb.addEventListener('click', e => e.stopPropagation());
  cb.addEventListener('change', () => toggle(i));
  tdCheck.appendChild(cb);
  tr.appendChild(tdCheck);
  for (const [key, val, cls] of [
    ['title', info.title || '', 'ellipsis'], ['dur', fmtDur(info.dur), ''],
    ['res', info.res || '-', ''], ['size', fmtSize(info.size), ''],
    ['up', info.up || '-', 'ellipsis'],
  ]) {
    const td = document.createElement('td');
    td.textContent = val;
    if (cls) td.className = cls;
    if (key === 'title' || key === 'up') td.title = val;
    tr.appendChild(td);
  }
  tr.addEventListener('click', () => { toggle(i); showPreview(i); });
  return tr;
}

function toggle(i) {
  if (checked.has(i)) checked.delete(i); else checked.add(i);
  const tr = document.querySelector(`#results-body tr[data-i="${i}"]`);
  tr.classList.toggle('checked', checked.has(i));
  tr.querySelector('input').checked = checked.has(i);
  updateCount();
}

async function showPreview(i) {
  const info = rows[i];
  previewUrl = info.url || '';
  document.getElementById('btn-pv-open').disabled = !previewUrl;
  document.getElementById('pv-meta').textContent =
    `${info.title || ''}\n时长 ${fmtDur(info.dur)} · 清晰度 ${info.res || '-'} · ` +
    `大小 ${fmtSize(info.size)}\n上传者 ${info.up || '-'}`;
  const img = document.getElementById('pv-img');
  const txt = document.getElementById('pv-img-text');
  if (!info.thumb) {
    img.hidden = true; txt.hidden = false; txt.textContent = '这个条目没有封面地址';
    return;
  }
  img.hidden = true; txt.hidden = false; txt.textContent = '封面加载中…';
  const myGen = ++gen;
  const r = await call('get_thumb', info.thumb, window.__collectCfg());
  if (myGen !== gen) return;
  if (r.ok) {
    img.src = r.data_url;
    img.hidden = false; txt.hidden = true;
    img.onerror = () => {
      img.hidden = true; txt.hidden = false; txt.textContent = '封面格式无法显示';
    };
  } else {
    txt.textContent = '封面取不到：' + (r.err || '未知原因');
  }
}

export function initResults({ download, dialogs }) {
  const body = document.getElementById('results-body');
  on('detrow', ev => {
    const i = rows.length;
    rows.push(ev.info);
    checked.add(i);
    body.appendChild(renderRow(i));
    updateCount();
  });
  on('detdone', () => updateCount());
  document.getElementById('btn-all').addEventListener('click', () => {
    rows.forEach((_, i) => checked.add(i));
    body.querySelectorAll('tr').forEach(tr => {
      tr.classList.add('checked');
      tr.querySelector('input').checked = true;
    });
    updateCount();
  });
  document.getElementById('btn-none').addEventListener('click', () => {
    checked.clear();
    body.querySelectorAll('tr').forEach(tr => {
      tr.classList.remove('checked');
      tr.querySelector('input').checked = false;
    });
    updateCount();
  });
  document.getElementById('btn-clear-results').addEventListener('click', () => {
    rows = []; checked.clear(); body.textContent = ''; updateCount();
  });
  document.getElementById('btn-dl-sel').addEventListener('click', async () => {
    const urls = [...checked].sort((a, b) => a - b)
      .map(i => rows[i].url).filter(Boolean);
    if (!urls.length) { dialogs.toast('请至少勾选一个条目', 'err'); return; }
    const r = await call('start_download', urls, download.collectCfg(), true);
    if (!r.ok) dialogs.toast(r.error || '无法开始下载', 'err');
  });
  document.getElementById('btn-pv-open').addEventListener('click', () =>
    call('open_url', previewUrl));
  return {
    clear: () => { rows = []; checked.clear(); body.textContent = ''; updateCount(); },
    selectedUrls: () => [...checked].sort((a, b) => a - b).map(i => rows[i].url).filter(Boolean),
  };
}
```

- [ ] **Step 3: 扩 app.js——进度/播放/状态接线 + 暴露 collectCfg**

```js
import { ready, call, on } from './bridge.js';
import { initTheme } from './theme.js';
import { initTabs } from './tabs.js';
import { initDownload } from './download.js';
import { initLog } from './log.js';
import { initResults } from './results.js';

await ready;
initTheme();
const tabs = initTabs();
const dialogs = { toast: (t, k) => console.log('[toast]', t, k) }; // Task 13 换真实实现
const download = initDownload({ dialogs, results: {}, tabs });
window.__collectCfg = download.collectCfg;
const results = initResults({ download, dialogs });
initLog();

const r = await call('get_config');
download.applyConfig(r.cfg, r);

on('progress', ev => {
  document.getElementById('prog-bar').value = ev.pct;
  document.getElementById('prog-pct').textContent = Math.round(ev.pct) + '%';
  document.getElementById('prog-file').textContent = '正在下载：' + (ev.file || '');
  const bits = [];
  if (ev.speed) bits.push('速度 ' + ev.speed);
  if (ev.eta) bits.push('剩余 ' + ev.eta);
  if (ev.size) bits.push('大小 ' + ev.size);
  if (ev.item_total) bits.push(`第 ${Math.max(ev.item_i, 1)}/${ev.item_total} 个`);
  if (ev.done_count) bits.push(`已完成 ${ev.done_count} 个文件`);
  document.getElementById('prog-detail').textContent = bits.join('   ·   ') || '—';
});
on('status', ev => {
  const el = document.getElementById('status');
  el.textContent = ev.text;
  el.dataset.tone = ev.tone;
});
on('task', ev => {
  document.getElementById('btn-start').disabled = ev.busy;
  document.getElementById('btn-detect').disabled = ev.busy;
  document.getElementById('btn-trouble').disabled = ev.busy;
  document.getElementById('btn-update').disabled = ev.busy;
  document.getElementById('btn-formats').disabled = ev.busy;
  document.getElementById('btn-stop').disabled = !ev.busy;
});
on('done', ev => {
  if (ev.code === 0) {
    document.getElementById('prog-bar').value = 100;
    document.getElementById('prog-pct').textContent = '100%';
    document.getElementById('prog-file').textContent = '全部完成';
  } else {
    document.getElementById('prog-file').textContent = '出错或中断 — 见日志中的原因与建议';
  }
});
on('playable', ev => {
  document.getElementById('btn-play').disabled = !ev.ok;
  document.getElementById('play-reason').textContent =
    ev.reason || (ev.ok ? '可以边下边播' : '此源不支持边下边播');
});
document.getElementById('btn-play').addEventListener('click', async () => {
  const r = await call('play_now');
  if (!r.ok) dialogs.toast(r.error || '无法播放', 'err');
});
```

- [ ] **Step 4: 验证**

Run: `py -3 YtDlpGUI\main.py --debug`，粘贴一个真实视频链接：
Expected: 探测资源（Task 13 接按钮前可在 console 调 `window.pywebview.api.detect([...], window.__collectCfg())`）→ 结果表逐行出现、默认勾选；点行切换勾选并显示封面+元数据；快速连点不同行封面不串；下载选中项 → 进度条/速度/剩余更新、日志滚动且进度行不刷屏；HLS 源边下边播按钮亮起并可播放；分离源置灰带原因。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/ui/js/log.js YtDlpGUI/ui/js/results.js YtDlpGUI/ui/js/app.js
git commit -m "桌面 Web 重写：进度/日志/结果表/封面预览/边下边播前端"
```

---

### Task 13: 工具栏动作 + 对话框 + toast

**Files:**
- Create: `YtDlpGUI/ui/js/dialogs.js`
- Modify: `YtDlpGUI/ui/js/app.js`（替换占位 dialogs，接工具栏按钮）

**Interfaces:**
- Consumes: `call('start_download'|'detect'|'stop'|'troubleshoot'|'update_ytdlp'|'fetch_formats'|'copy_cmd')`；事件 `formats/exit_confirm`
- Produces: `initDialogs() -> {toast, confirm, openFormats}`；confirm 返回 Promise<boolean>（原生 dialog returnValue）；格式表双击选行 → 切自定义模式填 fmt；工具栏全部按钮接线；忙时关窗弹确认。

- [ ] **Step 1: 写 js/dialogs.js**

```js
import { on } from './bridge.js';

export function initDialogs() {
  const toastBox = document.getElementById('toast');
  function toast(text, kind) {
    const div = document.createElement('div');
    div.className = 'item' + (kind === 'err' ? ' err' : '');
    div.textContent = text;
    toastBox.appendChild(div);
    setTimeout(() => div.remove(), 5000);
  }

  const dlg = document.getElementById('dlg-confirm');
  function confirm(title, text, okLabel = '确定') {
    return new Promise(resolve => {
      document.getElementById('confirm-title').textContent = title;
      document.getElementById('confirm-text').textContent = text;
      document.getElementById('btn-confirm-ok').textContent = okLabel;
      dlg.returnValue = '';
      dlg.showModal();
      dlg.addEventListener('close', () => {
        const v = dlg.returnValue;
        dlg.returnValue = '';
        resolve(v === 'ok');
      }, { once: true });
    });
  }

  const fd = document.getElementById('dlg-formats');
  const ftext = document.getElementById('formats-text');
  let pickHandler = null;
  function openFormats(onPick) {
    ftext.textContent = '正在读取…';
    pickHandler = onPick;
    fd.returnValue = '';
    fd.showModal();
  }
  // 每行一个 div：双击直接命中行元素，避免文本偏移定位的歧义
  ftext.addEventListener('dblclick', e => {
    const d = e.target.closest('div');
    if (!d || !pickHandler) return;
    const m = d.textContent.match(/^\s*([A-Za-z0-9_\-]+)\s+(\w+)\s+/);
    if (m) { pickHandler(m[1]); fd.close(); }
  });
  fd.addEventListener('close', () => {
    const v = fd.returnValue;
    fd.returnValue = '';
    if (v === 'best' && pickHandler) pickHandler('bv*+ba/b');
    if (v === 'audio' && pickHandler) pickHandler('ba');
  });
  on('formats', ev => {
    ftext.textContent = '';
    for (const ln of ev.lines) {
      const d = document.createElement('div');
      d.textContent = ln;
      ftext.appendChild(d);
    }
    const tail = document.createElement('div');
    tail.className = 'hint';
    tail.textContent = ev.code !== 0 ? '（读取失败）' : `共 ${ev.lines.length} 行`;
    ftext.appendChild(tail);
  });
  on('exit_confirm', async () => {
    const ok = await confirm('退出', '仍有任务在进行，确定退出吗？', '退出');
    if (ok) window.pywebview.api.exit();
  });
  return { toast, confirm, openFormats };
}
```

- [ ] **Step 2: 扩 app.js——工具栏接线（替换占位 dialogs 行，追加按钮逻辑）**

```js
import { initDialogs } from './dialogs.js';
// 将 const dialogs = { toast: ... } 占位行替换为：
const dialogs = initDialogs();
```

```js
function urlsOrToast() {
  const urls = download.getUrls();
  if (!urls.length) { dialogs.toast('请先粘贴至少一个链接', 'err'); return null; }
  return urls;
}
document.getElementById('btn-start').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  const cfg = download.collectCfg();
  if (cfg.aria && !r.env.has_aria2c) {
    const ok = await dialogs.confirm('未检测到 aria2c',
      '程序目录和 PATH 里都没有 aria2c.exe。这次会改用 yt-dlp 自带下载器继续。要继续吗？');
    if (!ok) return;
  }
  const res = await call('start_download', urls, cfg);
  if (!res.ok) dialogs.toast(res.error || '无法开始下载', 'err');
});
document.getElementById('btn-detect').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  results.clear();
  const res = await call('detect', urls, download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法探测', 'err');
});
document.getElementById('btn-stop').addEventListener('click', () => call('stop'));
document.getElementById('btn-trouble').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  const res = await call('troubleshoot', urls, download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法排障', 'err');
});
document.getElementById('btn-update').addEventListener('click', async () => {
  const res = await call('update_ytdlp', download.collectCfg().proxy);
  if (!res.ok) dialogs.toast(res.error || '无法更新', 'err');
});
document.getElementById('btn-copy').addEventListener('click', async () => {
  const res = await call('copy_cmd', download.getUrls(), download.collectCfg());
  if (!res.ok) { dialogs.toast(res.error || '复制失败', 'err'); return; }
  try {
    await navigator.clipboard.writeText(res.text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = res.text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  dialogs.toast('命令已复制');
});
document.getElementById('btn-formats').addEventListener('click', async () => {
  const urls = download.getUrls();
  if (!urls.length) { dialogs.toast('请先填写一个链接', 'err'); return; }
  dialogs.openFormats(fid => {
    document.getElementById('mode').value = r.mode_name['custom'];
    document.getElementById('mode').dispatchEvent(new Event('change'));
    document.getElementById('fmt').value = fid;
    dialogs.toast('已选择格式：' + fid);
  });
  const res = await call('fetch_formats', urls[0], download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法读取格式', 'err');
});
```

- [ ] **Step 3: 验证**

Run: `py -3 YtDlpGUI\main.py --debug`
Expected: 开始下载/探测/停止/排障/更新/复制命令全部可用；忙时按钮禁用、停止可用；格式表对话框双击选行填入 fmt；Esc 关对话框；忙时点窗口关闭弹确认，取消则窗口保留；toast 5 秒消失。

- [ ] **Step 4: Commit**

```bash
git add YtDlpGUI/ui/js/dialogs.js YtDlpGUI/ui/js/app.js
git commit -m "桌面 Web 重写：工具栏动作、原生对话框（格式表/确认/退出）与 toast"
```

---

### Task 14: 嗅探页 + 历史页

**Files:**
- Create: `YtDlpGUI/ui/js/sniff.js`
- Create: `YtDlpGUI/ui/js/history.js`
- Modify: `YtDlpGUI/ui/js/app.js`

**Interfaces:**
- Consumes: `call('sniff_items'|'sniff_clear'|'sniff_download'|'sniff_preview'|'sniff_play_clip'|'history_list'|'history_clear'|'history_redownload'|'open_outdir')`；事件 `done`（刷新历史）
- Produces: `initSniff({dialogs, tabs})`（页签可见时 3 秒轮询、折叠分片、多选、下载/预览/片段）；`initHistory({dialogs})`（列表/重下/打开目录/清空确认）。

- [ ] **Step 1: 写 js/sniff.js**

```js
import { call, on } from './bridge.js';

let items = [];
let shown = [];
let sig = '';
const selected = new Set();

function fmtSize(b) {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + u[i];
}

function render() {
  const body = document.getElementById('sniff-body');
  body.textContent = '';
  shown.forEach((r, i) => {
    const tr = document.createElement('tr');
    if (r.seg) tr.classList.add('seg');
    const tdSel = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = selected.has(r.url);
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(r.url); else selected.delete(r.url);
    });
    tdSel.appendChild(cb);
    tr.appendChild(tdSel);
    for (const v of [r.title || '', r.url || '', r.kind || '', fmtSize(r.size),
                     r.source || '', r.page_url || '',
                     new Date((r.ts || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false })]) {
      const td = document.createElement('td');
      td.textContent = v;
      td.className = 'ellipsis';
      td.title = v;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  });
  document.getElementById('sn-count').textContent =
    `共 ${shown.length} 条（折叠分片：${document.getElementById('sn-fold').checked ? '开' : '关'}）`;
}

async function refresh() {
  const d = await call('sniff_items');
  if (!d.svc) {
    document.getElementById('sn-count').textContent = '扩展服务未启用（「高级」里勾选启用浏览器扩展支持）';
    return;
  }
  const fold = document.getElementById('sn-fold').checked;
  shown = (d.items || []).filter(r => !(fold && r.seg));
  const s = JSON.stringify([fold, shown.map(r => [r.url, r.ts, r.size])]);
  if (s === sig) return;
  sig = s;
  render();
}

export function initSniff({ dialogs, tabs }) {
  document.getElementById('btn-sn-refresh').addEventListener('click', refresh);
  document.getElementById('sn-fold').addEventListener('change', () => { sig = ''; refresh(); });
  document.getElementById('btn-sn-clear').addEventListener('click', async () => {
    await call('sniff_clear');
    selected.clear();
    sig = '';
    refresh();
  });
  document.getElementById('btn-sn-download').addEventListener('click', async () => {
    const its = shown.filter(r => selected.has(r.url));
    if (!its.length) { dialogs.toast('先选中要下载的条目', 'err'); return; }
    const r = await call('sniff_download', its);
    if (!r.ok) dialogs.toast(r.error || '无法下载', 'err');
    else tabs.select(document.getElementById('tab-download'));
  });
  document.getElementById('btn-sn-preview').addEventListener('click', async () => {
    const its = shown.filter(r => selected.has(r.url));
    if (its.length !== 1) { dialogs.toast('预览一次只选一条', 'err'); return; }
    document.getElementById('sn-pv').textContent = '正在抓预览…';
    document.getElementById('btn-sn-play').disabled = true;
    const r = await call('sniff_preview', its[0].url);
    const img = document.getElementById('sn-img');
    if (!r.ok) {
      document.getElementById('sn-pv').textContent = '预览失败：' + (r.error || '未知原因');
      return;
    }
    if (r.thumb_data_url) { img.src = r.thumb_data_url; img.hidden = false; }
    const dur = r.duration ? `${Math.floor(r.duration / 60)} 分 ${Math.round(r.duration % 60)} 秒` : '未知';
    document.getElementById('sn-pv').textContent =
      `时长 ${dur}` + (r.has_clip ? '　片段已就绪，可播放' : '　（此源没有可抓的片段）');
    document.getElementById('btn-sn-play').disabled = !r.has_clip;
  });
  document.getElementById('btn-sn-play').addEventListener('click', async () => {
    const r = await call('sniff_play_clip');
    if (!r.ok) dialogs.toast(r.error || '播放失败', 'err');
  });
  // 只在嗅探页可见时轮询（沿用旧版 3 秒节奏）
  setInterval(() => {
    if (!document.getElementById('panel-sniff').hidden) refresh();
  }, 3000);
}
```

- [ ] **Step 2: 写 js/history.js**

```js
import { call, on } from './bridge.js';

const STATUS = { ok: '成功', failed: '失败', stopped: '已停止' };
const SOURCE = { gui: '桌面', ext: '扩展', sniff: '嗅探页' };

async function refresh() {
  const r = await call('history_list');
  const body = document.getElementById('history-body');
  body.textContent = '';
  (r.items || []).forEach((rec, i) => {
    const tr = document.createElement('tr');
    const cells = [
      new Date(rec.ts * 1000).toLocaleString('zh-CN', { hour12: false }),
      rec.title || (rec.urls || [])[0] || '',
      STATUS[rec.status] || rec.status,
      rec.elapsed ? Math.round(rec.elapsed) + ' 秒' : '-',
      SOURCE[rec.source] || rec.source,
    ];
    for (const v of cells) {
      const td = document.createElement('td');
      td.textContent = v;
      td.className = 'ellipsis';
      td.title = v;
      tr.appendChild(td);
    }
    const tdOp = document.createElement('td');
    const re = document.createElement('button');
    re.textContent = '重新下载';
    re.addEventListener('click', async () => {
      const res = await call('history_redownload', i);
      if (!res.ok) window.__toast(res.error || '无法重下', 'err');
    });
    const open = document.createElement('button');
    open.textContent = '打开目录';
    open.addEventListener('click', () => call('open_outdir', rec.outdir));
    tdOp.append(re, open);
    tr.appendChild(tdOp);
    body.appendChild(tr);
  });
}

export function initHistory({ dialogs }) {
  window.__toast = dialogs.toast;
  document.getElementById('btn-history-clear').addEventListener('click', async () => {
    const ok = await dialogs.confirm('清空历史', '确定清空全部下载历史？此操作不可恢复。', '清空');
    if (!ok) return;
    await call('history_clear');
    refresh();
  });
  on('done', refresh);
  document.getElementById('tab-history').addEventListener('click', refresh);
  refresh();
}
```

- [ ] **Step 3: 扩 app.js（追加初始化）**

```js
import { initSniff } from './sniff.js';
import { initHistory } from './history.js';
initSniff({ dialogs, tabs });
initHistory({ dialogs });
```

- [ ] **Step 4: 验证**

Run: `py -3 YtDlpGUI\main.py --debug`，开着嗅探扩展浏览一个视频页：
Expected: 嗅探页 3 秒内出现条目、分片默认折叠、勾选后下载选中跳回下载页并走流水线；预览选中出缩略图/时长/片段播放；历史页出现 gui/ext/sniff 来源记录，重下/打开目录可用，清空有确认。

- [ ] **Step 5: Commit**

```bash
git add YtDlpGUI/ui/js/sniff.js YtDlpGUI/ui/js/history.js YtDlpGUI/ui/js/app.js
git commit -m "桌面 Web 重写：扩展嗅探页与下载历史页"
```

---

### Task 15: 全量回归 + 手工验收 + 文档 + 旧版退役第 1 步

**Files:**
- Modify: `README.md`
- Create: `AGENTS.md`（浏览器支持政策；若已存在则追加）
- Move: `YtDlpGUI/main.py`（旧 Tkinter 版，git mv 自当前 main.py 的历史版本——见步骤）→ `YtDlpGUI/legacy/main_tk.py`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 可交付的一期版本；旧版进入 legacy 保留一个版本周期。

- [ ] **Step 1: 全量自动回归**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 原有三套 + test_host + test_api 全绿。
Run: `node --check sniffer_extension/background.js && node --check sniffer_extension/content.js && node --check sniffer_extension/injected.js && node tests/extension_smoke.js`
Expected: 语法通过 + 5/5。
Run: `py -3 YtDlpGUI\main.py --selftest`
Expected: 命令 JSON，退出码 0。

- [ ] **Step 2: 手工验收矩阵（逐项勾选，记录结果）**

按 spec 第 10 节逐项实测：8 分区存读 · 多链接下载 · 播放列表探测/勾选/下载选中 · 封面预览（含失败降级）· 边下边播（HLS 可播 + 分离源/aria2c/mp4 三种置灰）· 10054 自愈重试 · 排障四变体 · 更新 yt-dlp · 格式表选用 · 复制命令 · 停止 · 扩展服务开关 · **扩展提交→桌面流水线→扩展轮询进度/日志/可播状态** · 嗅探折叠/预览/片段 · 历史三来源/重下/清空 · 主题三态/持久化/跟随系统/钉住不丢 · 小窗口整页可滚 · 高 DPI 清晰 · 键盘走页签/表格/对话框。
任何一项失败：修复后重跑该项 + 自动回归，再继续。

- [ ] **Step 3: 旧版退役第 1 步**

旧 Tkinter 版已在 Task 9 被新 main.py 覆盖，从 git 历史取回存入 legacy：

```bash
mkdir YtDlpGUI\legacy
git show 0a51ed2:YtDlpGUI/main.py > YtDlpGUI/legacy/main_tk.py
```

（0a51ed2 是重写前的 main 提交；若届时 HEAD 历史不同，用 `git log --oneline -- YtDlpGUI/main.py` 找到重写前最后一个提交。）
在 legacy/main_tk.py 顶部加一行注释：`# 已退役的 Tkinter 版本（1.9.0），保留一个版本周期供回退；新版见上级目录 main.py。`
验证回退可用：`py -3 YtDlpGUI\legacy\main_tk.py --selftest` 退出码 0。

- [ ] **Step 4: 更新 README.md**

新增/修改：桌面版现为 pywebview + WebView2（首次启动.bat 自动装 pywebview；WebView2 缺失时的安装地址）；下载历史（history.json、500 条、仅本机）；主题三态；旧 Tkinter 版在 legacy/ 保留一个版本周期；测试数量更新；其余功能说明沿用。

- [ ] **Step 5: 写 AGENTS.md 浏览器支持政策**

```markdown
# 项目约定

## 浏览器支持政策（桌面 Web 前端）
运行时仅 WebView2（Evergreen Edge）。Baseline Newly Available 特性（light-dark()、<dialog>、color-scheme 等）直接使用，不做 fallback；Limited availability 特性（focusgroup、closedby 等）回避或按渐进增强处理。前端零构建、零框架依赖（原生 ES 模块）。

## 不可改动
ytdlp_core.py、YtDlpWeb/server.py、sniffer_extension/ 的行为不在 UI 重写范围内改动；扩展集成经 TASK_SINK/RUNNER 单任务闸门。
```

- [ ] **Step 6: 最终回归 + Commit**

Run: `py -3 -m unittest discover -s tests -v`
Expected: 全绿。

```bash
git add YtDlpGUI/legacy/main_tk.py README.md AGENTS.md
git commit -m "桌面 Web 重写一期完成：全量验收通过，旧 Tkinter 版退役至 legacy"
```

（不改 YtDlpWeb/server.py 的 APP_VERSION——「server.py 一行不改」是硬约束；版本号升级另行决定。）