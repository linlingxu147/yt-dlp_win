# -*- coding: utf-8 -*-
"""内嵌浏览器：主窗口内的浏览器视图（单窗口）。

形态（见 docs/prompts/2026-11-26-embedded-browser.md 的单窗口变体）：
* 不创建第二个 pywebview 窗口，而是在主窗体的同一个 WinForms 里放两个视图：
  pywebview 的主界面 WebView2（切换时只隐藏，状态不丢）与这里自建的浏览器视图；
* 浏览器视图 = Panel（原生 ToolStrip + 自建 WebView2），第一次打开时懒创建；
* 自建 WebView2 不经过 pywebview 注入，页面里不存在 window.pywebview；
* 顶层导航只放行 http/https（外加初始 about:blank），拦掉 file:/javascript:/data:；
* 站点请求新窗口时在当前视图里就地导航，系统浏览器只由地址栏右键菜单显式打开；
* 页面直接触发的下载一律取消（下载走嗅探列表的直链，或工具栏/页面浮条的
  「下载本页」整页解析）；
* 工具栏与起始页跟随应用传入的浅色/深色主题（应用主题可能与系统主题不同）；
* 快捷键只经 UI 线程消息过滤器拦截（浏览器视图可见时安装），不改网页自己的按键行为；
* 所有 CoreWebView2 访问都必须在 UI 线程上做，事件回调里不做 IO。

pywebview 没有公开「在窗口里再放一个 WebView2」的 API，所以这里直接使用
WebView2 的 WinForms 控件；pywebview 升级后若内部类型不可用，打开时给出错误提示，
不退回第二窗口。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import webbrowser
from urllib.parse import urlsplit

try:
    import sniff_rules as _rules
except Exception:            # 没有 sniffer_extension 目录时浏览器仍可用，只是不参与网络嗅探
    _rules = None

WINDOW_TITLE = "内嵌浏览器 · 嗅探"
HOME_STATUS = "输入网址回车打开"
_PROFILE_PREFIX = "ytdlp_embedded_"

# 与应用 app.css 同一套令牌；工具栏/起始页都取这里，避免两处主题漂移。
_THEMES = {
    "light": {
        "bg": "#f6f8fa",
        "field": "#ffffff",
        "text": "#1f2328",
        "muted": "#57606a",
        "border": "#d0d7de",
        "hover": "#eaeef2",
        "press": "#d0d7de",
        "accent": "#0550ae",
        "err": "#cf222e",
    },
    "dark": {
        "bg": "#161b22",
        "field": "#0d1117",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "border": "#30363d",
        "hover": "#21262d",
        "press": "#30363d",
        "accent": "#4493f8",
        "err": "#f85149",
    },
}

_START_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="__SCHEME__">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>内嵌浏览器</title>
<style>
:root {
  color-scheme: __SCHEME__;
  --bg: light-dark(#f6f8fa, #0d1117);
  --surface: light-dark(#ffffff, #161b22);
  --border: light-dark(#d0d7de, #30363d);
  --text: light-dark(#1f2328, #e6edf3);
  --muted: light-dark(#57606a, #8b949e);
  --accent: light-dark(#0550ae, #4493f8);
  --err: light-dark(#cf222e, #f85149);
  accent-color: var(--accent);
}
* { box-sizing: border-box; }
body {
  margin: 0; min-block-size: 100vh;
  display: grid; place-items: center;
  padding: 40px 24px;
  background: var(--bg); color: var(--text);
  font: 14px/1.6 "Microsoft YaHei UI", system-ui, sans-serif;
}
main { inline-size: min(640px, 100%); display: grid; gap: 28px; }
h1 { font-size: 26px; margin: 0 0 6px; }
h2 { font-size: 13px; color: var(--muted); font-weight: 600; margin: 0 0 8px; }
header p { margin: 0; color: var(--muted); }
form { display: grid; gap: 8px; }
label { font-size: 13px; font-weight: 600; }
.open-row { display: flex; gap: 8px; }
input[type="text"] {
  flex: 1; min-inline-size: 0;
  font: inherit; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
}
input[type="text"]::placeholder { color: var(--muted); }
input[type="text"]:focus-visible,
a:focus-visible,
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button {
  font: inherit; font-weight: 600; color: light-dark(#ffffff, #0d1117);
  background: var(--accent); border: 1px solid var(--accent);
  border-radius: 8px; padding: 10px 18px;
  cursor: pointer; transition: filter 150ms ease-out, scale 150ms ease-out;
}
button:hover { filter: brightness(1.08); }
button:active { scale: .97; }
.error { margin: 0; color: var(--err); font-size: 13px; }
ul { list-style: none; margin: 0; padding: 0; }
.links { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.links a {
  display: grid; gap: 2px; padding: 12px 14px;
  color: inherit; text-decoration: none;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  transition: border-color 150ms ease-out, box-shadow 150ms ease-out, scale 150ms ease-out;
}
.links a:hover { border-color: var(--accent); box-shadow: 0 2px 10px rgb(0 0 0 / .08); }
.links a:active { scale: .98; }
.links strong { font-size: 14px; }
.links span { color: var(--muted); font-size: 12px; }
.tips { display: grid; gap: 6px; }
.tips li { padding-inline-start: 1.1em; position: relative; color: var(--muted); }
.tips li::before { content: "·"; position: absolute; inset-inline-start: .2em; color: var(--accent); font-weight: 700; }
kbd {
  font: 12px/1 "Cascadia Mono", Consolas, monospace;
  border: 1px solid var(--border); border-block-end-width: 2px;
  border-radius: 4px; padding: 1px 5px; color: var(--text); background: var(--surface);
}
@media (prefers-reduced-motion: reduce) {
  button, .links a { transition: none; }
}
</style>
</head>
<body>
<main>
  <header>
    <h1>内嵌浏览器</h1>
    <p>在同一个窗口里打开网页；页面里的媒体请求会自动出现在「扩展嗅探」页签。</p>
  </header>
  <form id="open-form" aria-labelledby="open-label">
    <label for="url-input" id="open-label">输入网址</label>
    <div class="open-row">
      <input id="url-input" name="url" type="text" inputmode="url"
             autocomplete="off" autocorrect="off" autocapitalize="none"
             spellcheck="false" enterkeyhint="go"
             placeholder="www.bilibili.com 或完整 https 地址">
      <button type="submit">打开</button>
    </div>
    <p id="url-error" class="error" role="status" aria-live="polite" hidden></p>
  </form>
  <nav aria-labelledby="links-title">
    <h2 id="links-title">常用站点</h2>
    <ul class="links">
      <li><a href="https://www.bilibili.com/">
        <strong>哔哩哔哩</strong><span>视频、番剧与直播</span></a></li>
      <li><a href="https://www.douyin.com/">
        <strong>抖音</strong><span>短视频与直播</span></a></li>
      <li><a href="https://www.youtube.com/">
        <strong>YouTube</strong><span>登录可能被 WebView2 拒绝</span></a></li>
    </ul>
  </nav>
  <section aria-labelledby="tips-title">
    <h2 id="tips-title">使用提示</h2>
    <ul class="tips">
      <li>工具栏左侧「← 返回下载器」切回主界面，浏览器页面会保留。</li>
      <li><kbd>Ctrl</kbd>+<kbd>L</kbd> 定位地址栏，<kbd>F5</kbd> 刷新，<kbd>Esc</kbd> 停止加载，<kbd>Alt</kbd>+<kbd>←</kbd>/<kbd>→</kbd> 前进后退。</li>
      <li>工具栏「下载本页」把当前页交给 yt-dlp 解析：B 站这类 DASH 页面会合并音视频，整段下载。</li>
      <li>页面直接触发的下载会被取消；单个直链从嗅探列表里选，整段视频点「下载本页」。</li>
    </ul>
  </section>
</main>
<script>
(() => {
  const form = document.getElementById('open-form');
  const input = document.getElementById('url-input');
  const error = document.getElementById('url-error');

  function allowed(text) {
    try {
      const url = new URL(text);
      return (url.protocol === 'http:' || url.protocol === 'https:') && !!url.hostname
        ? url.href : '';
    } catch {
      return '';
    }
  }

  function normalize(text) {
    const s = text.trim();
    if (!s) return '';
    if (!s.includes('://')) {
      if (/^[a-z][a-z0-9+.-]*:/i.test(s)) return '';
      const first = s.split('/')[0];
      if (!first.includes('.') && !first.includes(':')) return '';
      return allowed('https://' + s);
    }
    return allowed(s);
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const url = normalize(input.value);
    if (!url) {
      error.textContent = '只支持 http(s) 网址，例如 www.bilibili.com';
      error.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    location.href = url;
  });

  input.addEventListener('input', () => {
    if (error.hidden) return;
    error.hidden = true;
    error.textContent = '';
    input.removeAttribute('aria-invalid');
  });
})();
</script>
</body>
</html>"""


def normalize_theme(value):
    v = str(value or "").strip().lower()
    return v if v in ("light", "dark") else ""


def palette_for(theme):
    """主题 → 颜色表（十六进制字符串）；未知值按浅色，返回副本。"""
    return dict(_THEMES["dark" if normalize_theme(theme) == "dark" else "light"])


def start_page_html(theme="light"):
    """起始页 HTML；主题在这里定型，不依赖 WebView2 的系统配色偏好。"""
    scheme = "dark" if normalize_theme(theme) == "dark" else "light"
    return _START_PAGE_TEMPLATE.replace("__SCHEME__", scheme)

_BLANK_URLS = ("about:blank",)
# 普通 Esc 不属于 WebView2 的“加速键”（只含 Ctrl/Alt/功能键），用页面脚本兜底。
_ESCAPE_SCRIPT = """
(() => {
  if (window.__ytdlpEscHooked) return;
  window.__ytdlpEscHooked = true;
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    try { window.chrome.webview.postMessage({ __ytdlp: 'escape' }); } catch (err) {}
  }, true);
})();
"""
# DOM 补扫：扫描器与规则都在 sniffer_extension（与扩展共用同一份），缺文件就整段关掉。
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOM_MAX_ITEMS = 300          # 单条消息最多收这么多条
_DOM_SEEN_MAX = 5000          # 每页已登记地址的去重集合上限
_MIN_IMAGE_SIDE = 200         # 与 dom_scan.js 的 MIN_IMAGE_SIDE 保持一致
_HOOK_MAX_ITEMS = 60          # 正文钩子单条消息最多收这么多条
_HOOK_SEEN_MAX = 2000         # 每页钩子去重集合上限
_NET_SEEN_MAX = 4000          # 网络侧实锤地址表上限（只用于钩子复核）
_BODY_MAX_BYTES = 2 * 1024 * 1024   # 超过这个体积的响应不读正文
_BODY_TIMEOUT = 3.0           # 读正文的总等待时间（deferral 不能一直挂着）
_BODY_CONCURRENCY = 2         # 同时读正文的响应数上限，防止拖住请求
_BODY_MAX_ITEMS = 8           # 一条正文里最多取这么多地址
# 值得读正文的 MIME：清单/接口声明常被伪装成这些类型
_BODY_TEXT_MIME_RE = re.compile(
    r"(json|javascript|xml|mpegurl|dash\+xml|text/|octet-stream|x-www-form-urlencoded)", re.I)
# 明确不读的类型（读完也认不出清单，纯浪费）
_BODY_SKIP_MIME_RE = re.compile(r"^(image/|font/|video/|audio/|multipart/)", re.I)
# 正文里的绝对媒体地址；JSON 里的转义斜杠先还原再匹配
_BODY_MEDIA_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\)]+?\.(?:m3u8|mpd|mp4|flv|m4s|f4v|m2ts|webm|mkv|mov|ts|mp3|m4a|"
    r"aac|flac|ogg|opus)(?:\?[^\s\"'<>\\)]*)?", re.I)


def _load_js(name):
    try:
        with open(os.path.join(_BASE_DIR, "sniffer_extension", name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


_SNIFF_RULES_JS = _load_js("sniff_rules.js")
_DOM_SCAN_JS = _load_js("dom_scan.js")
_DOM_DRIVER_JS = """
(() => {
  if (!window.SniffDom || window.__ytdlpDomHooked) return;
  window.__ytdlpDomHooked = true;
  let timer = 0;
  const scan = () => {
    timer = 0;
    try {
      const items = window.SniffDom.scan(document);
      if (items.length && window.chrome && window.chrome.webview) {
        window.chrome.webview.postMessage({ __ytdlp: 'dom', items: items,
                                            frame: String(location.href || '') });
      }
    } catch (err) {}
  };
  // 页面一直在长：DOM 变化后等 800ms 静下来再扫，别每个节点都全页扫一遍
  const schedule = () => {
    if (timer) return;
    timer = setTimeout(scan, 800);
  };
  let observer = null;
  const watch = () => {
    if (observer || !document.documentElement) return;
    try {
      observer = new MutationObserver(schedule);
      observer.observe(document.documentElement, { childList: true, subtree: true });
    } catch (err) {
      observer = null;
    }
  };
  document.addEventListener('DOMContentLoaded', () => { watch(); schedule(); });
  window.addEventListener('load', schedule);
  watch();
})();
"""
if not (_SNIFF_RULES_JS and _DOM_SCAN_JS):
    _DOM_DRIVER_JS = ""            # 缺规则/扫描器文件：DOM 补扫整段关闭

# 页面世界钩子：与扩展共用 injected.js（XHR / fetch / JSON.parse 挖正文）。
# WebView2 只能用页面世界注入，钩子事件本身不可信；Python 侧要求同一 URL
# 在 WebResourceResponseReceived 里出现过（网络侧实锤）才收，和扩展的
# 「必须同时被后台嗅探看到」是同一套校验。这里只加一层很薄的转发。
_HOOK_JS = _load_js("injected.js")
_HOOK_RELAY_JS = """
(() => {
  if (window.__ytdlpHookRelay) return;
  window.__ytdlpHookRelay = true;
  let buf = [], timer = 0;
  const flush = () => {
    timer = 0;
    if (!buf.length) return;
    const items = buf;
    buf = [];
    try {
      window.chrome.webview.postMessage({ __ytdlp: 'hook', items: items,
                                          frame: String(location.href || '') });
    } catch (err) {}
  };
  // 钩子一次可能抛好几条（JSON 里有多个地址），合并成一条消息，别把 UI 线程刷爆。
  window.addEventListener('ytdlp-sniff', (event) => {
    const d = event && event.detail;
    if (!d || !d.url) return;
    buf.push({ url: String(d.url), kind: String(d.kind || '') });
    if (buf.length >= 16) { flush(); return; }
    if (!timer) timer = setTimeout(flush, 150);
  });
})();
"""
if not _HOOK_JS:
    _HOOK_RELAY_JS = ""            # 缺注入脚本：正文钩子整段关闭

# 页面内嗅探浮条：顶层框架右下角的「▶ 嗅探 N」，点开是本页媒体列表。
# 只做展示和提需求；下载 URL 回给 Python 后还要比对 _ui_items（可信侧）才入队，
# 所以页面世界改 DOM 顶多骗自己，改不出一个没嗅到过的下载。
_SNIFF_UI_JS = r"""
(() => {
  if (window.top !== window) return;          // 浮条只在顶层框架出现
  if (window.__ytdlpSniffUI) return;
  window.__ytdlpSniffUI = true;

  const HOST_ID = "__ytdlp_sniff_ui";
  const MAX_ROWS = 60;
  let host = null, root = null, pill = null, badge = null;
  let panel = null, listEl = null, statusEl = null, foldCb = null;
  let items = [], lastVisible = [];

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; font: 12px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
    #wrap { position: relative; }
    #pill { display: inline-flex; align-items: center; gap: 6px; background: #1f2328;
            color: #fff; padding: 6px 12px; border-radius: 18px; cursor: pointer;
            user-select: none; box-shadow: 0 2px 10px rgba(0,0,0,.35); white-space: nowrap; }
    #badge { background: #2da44e; color: #fff; border-radius: 9px; padding: 1px 7px;
             font-size: 11px; min-width: 16px; text-align: center; }
    #panel { display: none; position: absolute; right: 0; bottom: 40px; width: 370px;
             max-height: 330px; overflow: auto; background: #fff; color: #1f2328;
             border: 1px solid #d0d7de; border-radius: 10px;
             box-shadow: 0 4px 18px rgba(0,0,0,.25); padding: 8px; }
    #head { display: flex; align-items: center; gap: 6px; justify-content: space-between;
            margin: 2px 4px 6px; }
    #head b { font-size: 12px; font-weight: 600; }
    #fold { display: flex; align-items: center; gap: 3px; color: #57606a;
            font-size: 11px; cursor: pointer; white-space: nowrap; }
    #all { border: 1px solid #d0d7de; background: #f6f8fa; border-radius: 6px;
           padding: 3px 8px; cursor: pointer; font-size: 12px; color: #1f2328; white-space: nowrap; }
    #page { border: 1px solid #1f883d; background: #2da44e; color: #fff; border-radius: 6px;
            padding: 3px 8px; cursor: pointer; font-size: 12px; white-space: nowrap; }
    .row { display: flex; align-items: center; gap: 8px; padding: 5px;
           border-bottom: 1px solid #f0f0f0; }
    .tag { flex: none; background: #ddf4ff; color: #0969da; border-radius: 4px;
           padding: 0 6px; font-size: 11px; min-width: 36px; text-align: center; }
    .tag.audio { background: #fff8c5; color: #9a6700; }
    .tag.image { background: #ffeff7; color: #bf3989; }
    .label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #1f2328; }
    .meta { flex: none; color: #57606a; font-size: 11px; }
    .dl { flex: none; border: 1px solid #1f883d; background: #2da44e; color: #fff;
          border-radius: 6px; padding: 2px 9px; cursor: pointer; font-size: 12px; }
    #empty { color: #57606a; padding: 12px; text-align: center; }
    #more { color: #8b949e; padding: 6px; text-align: center; font-size: 11px; }
    #status { margin: 6px 4px 2px; color: #57606a; font-size: 11px; min-height: 15px; }
  `;

  function fmtSize(n) {
    n = Number(n) || 0;
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + u[i];
  }

  function shortUrl(u) {
    const s = String(u || "").replace(/^https?:\/\//, "");
    return s.length > 68 ? s.slice(0, 66) + "…" : s;
  }

  function typeOf(it) {
    if (it.image) return ["图片", "image"];
    if (it.audio) return ["音频", "audio"];
    if (it.seg) return ["分片", ""];
    const k = String(it.kind || it.mime || "media").replace(/^.*\//, "").toUpperCase();
    return [k.slice(0, 6) || "媒体", ""];
  }

  function setStatus(text, color) {
    if (statusEl) { statusEl.textContent = text; statusEl.style.color = color || "#57606a"; }
  }

  function build() {
    host = document.createElement("div");
    host.id = HOST_ID;
    host.setAttribute("style",
      "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647;display:block;");
    root = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = CSS;
    root.appendChild(style);

    const wrap = document.createElement("div");
    wrap.id = "wrap";

    pill = document.createElement("div");
    pill.id = "pill";
    pill.textContent = "▶ 嗅探";
    badge = document.createElement("span");
    badge.id = "badge";
    badge.textContent = "0";
    pill.appendChild(badge);
    pill.addEventListener("click", () => togglePanel());
    wrap.appendChild(pill);

    panel = document.createElement("div");
    panel.id = "panel";

    const head = document.createElement("div");
    head.id = "head";
    const title = document.createElement("b");
    title.textContent = "本页嗅探到的媒体";
    head.appendChild(title);

    const fold = document.createElement("label");
    fold.id = "fold";
    fold.title = "默认折叠 .ts/.m4s 等分片；取消勾选可查看";
    foldCb = document.createElement("input");
    foldCb.type = "checkbox";
    foldCb.checked = true;
    foldCb.addEventListener("change", render);
    const foldText = document.createElement("span");
    foldText.textContent = "折叠分片";
    fold.appendChild(foldCb);
    fold.appendChild(foldText);
    head.appendChild(fold);

    const allBtn = document.createElement("button");
    allBtn.id = "all";
    allBtn.textContent = "下载全部";
    allBtn.addEventListener("click", () => {
      const urls = lastVisible.slice(0, 20).map((it) => it.url).filter(Boolean);
      if (urls.length) send(urls);
      else setStatus("没有可下载的条目", "#cf222e");
    });
    head.appendChild(allBtn);

    const pageBtn = document.createElement("button");
    pageBtn.id = "page";
    pageBtn.textContent = "下载本页";
    pageBtn.title = "把当前页面交给 yt-dlp 整段下载；B 站会合并视频和音频";
    pageBtn.addEventListener("click", () => sendPage());
    head.appendChild(pageBtn);
    panel.appendChild(head);

    listEl = document.createElement("div");
    listEl.id = "list";
    panel.appendChild(listEl);

    statusEl = document.createElement("div");
    statusEl.id = "status";
    panel.appendChild(statusEl);

    wrap.appendChild(panel);
    root.appendChild(wrap);
    setStatus("播放一下视频，嗅探到的媒体会出现在这里");
  }

  function ensure() {
    try {
      if (!host) build();
      if (host && !host.isConnected && document.documentElement) {
        document.documentElement.appendChild(host);
      }
    } catch (e) { /* 页面 DOM 被重写时下一轮再来 */ }
  }

  function togglePanel() {
    ensure();
    if (!panel) return;
    const open = panel.style.display !== "block";
    panel.style.display = open ? "block" : "none";
    if (open) render();
  }

  function send(urls) {
    try {
      window.chrome.webview.postMessage({ __ytdlp: "dl", urls: urls });
      setStatus("已提交，等待加入下载队列…", "#0969da");
    } catch (e) {
      setStatus("提交失败：" + e, "#cf222e");
    }
  }

  function sendPage() {
    try {
      window.chrome.webview.postMessage({ __ytdlp: "dlpage" });
      setStatus("已提交整页下载，等待加入队列…", "#0969da");
    } catch (e) {
      setStatus("提交失败：" + e, "#cf222e");
    }
  }

  function render() {
    if (!badge || !listEl) return;
    const fold = !!(foldCb && foldCb.checked);
    badge.textContent = String(items.length);
    const sorted = items.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    lastVisible = sorted.filter((it) => !(fold && it.seg));
    listEl.textContent = "";
    if (!lastVisible.length) {
      const empty = document.createElement("div");
      empty.id = "empty";
      empty.textContent = fold && items.length
        ? "只发现分片（已折叠）；取消勾选「折叠分片」可查看"
        : "暂未发现媒体——播放一下视频试试";
      listEl.appendChild(empty);
      return;
    }
    for (const it of lastVisible.slice(0, MAX_ROWS)) {
      const row = document.createElement("div");
      row.className = "row";

      const tag = document.createElement("span");
      tag.className = "tag";
      const [label, cls] = typeOf(it);
      if (cls) tag.classList.add(cls);
      tag.textContent = label;
      row.appendChild(tag);

      const text = document.createElement("span");
      text.className = "label";
      const name = it.title || shortUrl(it.url);
      text.textContent = name + (it.size ? "  (" + fmtSize(it.size) + ")" : "");
      text.title = String(it.url || "") + (it.source ? "\n来源：" + it.source : "");
      row.appendChild(text);

      const dl = document.createElement("button");
      dl.className = "dl";
      dl.textContent = "下载";
      dl.addEventListener("click", () => send([it.url]));
      row.appendChild(dl);
      listEl.appendChild(row);
    }
    if (lastVisible.length > MAX_ROWS) {
      const more = document.createElement("div");
      more.id = "more";
      more.textContent = "还有 " + (lastVisible.length - MAX_ROWS) +
        " 条未显示；可回下载器到「扩展嗅探」页处理";
      listEl.appendChild(more);
    }
  }

  function boot() {
    try { ensure(); render(); } catch (e) { /* ignore */ }
  }

  try {
    window.chrome.webview.addEventListener("message", (ev) => {
      const d = (ev && ev.data) || {};
      if (d.__ytdlp === "sniff_ui") {
        items = Array.isArray(d.items) ? d.items : [];
        ensure();
        render();
      } else if (d.__ytdlp === "sniff_ui_result") {
        setStatus(d.text || (d.ok ? "已加入下载队列" : "下载失败：" + (d.error || "未知原因")),
                  d.ok ? "#1a7f37" : "#cf222e");
      }
    });
  } catch (e) { /* 没有 webview 桥时只当展示脚本 */ }

  // 通知宿主补发当前已收条目：脚本可能晚于首批嗅探结果才就绪
  try { window.chrome.webview.postMessage({ __ytdlp: "ui_ready" }); } catch (e) { /* ignore */ }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
  setInterval(() => { try { ensure(); } catch (e) { /* ignore */ } }, 2500);
})();
"""

_ALLOWED_SCHEMES = ("http", "https")
# 这些「没有 //」的伪协议不能被补成 https://javascript:...
_BLOCKED_PREFIXES = (
    "javascript", "data", "file", "blob", "about", "chrome", "chrome-extension",
    "edge", "ftp", "ftps", "mailto", "vbscript", "ws", "wss",
)

_NAV_ERRORS = {
    "HostNameNotResolved": "打不开：域名解析失败（断网或网址写错）",
    "ServerUnreachable": "打不开：连不上服务器（断网 / 需要代理）",
    "Timeout": "打不开：连接超时（断网 / 需要代理）",
    "ConnectionAborted": "连接被中断，稍后重试",
    "ConnectionReset": "连接被重置，稍后重试",
    "Disconnected": "网络已断开",
    "OperationCanceled": "",
}


def is_allowed_navigation(url):
    """顶层导航白名单：http/https，以及初始的 about:blank。"""
    s = str(url or "").strip()
    if not s:
        return False
    if s.lower() in _BLANK_URLS:
        return True
    try:
        parts = urlsplit(s)
    except ValueError:
        return False
    return parts.scheme.lower() in _ALLOWED_SCHEMES and bool(parts.netloc)


def normalize_address(text):
    """地址栏输入 → 可导航 URL；不合法返回空串。"""
    s = str(text or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in _BLANK_URLS:
        return "about:blank"
    if "://" not in s:
        prefix = low.split(":", 1)[0].strip() if ":" in low else ""
        if prefix in _BLOCKED_PREFIXES:
            return ""
        # 没写协议时只给「像域名」的输入补 https，避免把一段普通文字当网址
        first = s.split("/", 1)[0]
        if "." not in first and ":" not in first:
            return ""
        s = "https://" + s
    return s if is_allowed_navigation(s) else ""


def describe_nav_error(status):
    """WebErrorStatus 枚举名 → 中文提示；用户主动取消返回空串。"""
    name = str(status or "").strip()
    if not name:
        return ""
    if name in _NAV_ERRORS:
        return _NAV_ERRORS[name]
    if name.lower().startswith("certificate"):
        return "打不开：站点证书有问题（证书过期或不受信任）"
    return "加载失败（%s）" % name


def _short(text, limit=80):
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _side(value):
    """DOM 尺寸 → 像素数；取不到或非正数按 0（图片过滤会因此拒收）。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _as_int(value, default=0):
    """字符串/None → 整数；拿不到就按 default（Content-Length 常是空串）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def page_for_frame(frame_url, fallback=""):
    """帧地址 → 条目的归属页/Referer；不是 http(s) 就退回顶层页。

    跨域播放器 iframe 里发的媒体，Referer 应该是帧自己的地址；WebView2 的
    网络事件不提供帧信息，所以 DOM / 钩子消息由页面侧把 location.href 带上来。
    """
    f = str(frame_url or "").strip()[:500]
    if f and _rules is not None:
        f = _rules.normalize_url(f)
    if f.lower().startswith(("http://", "https://")):
        return f
    return str(fallback or "")


def response_item(url, status, mime, size, page_url="", ua="", referer="", ranged=False):
    """网络响应 → 待登记条目；纯函数，便于用假事件对象单测。

    referer 优先用请求头里的 Referer：跨域 iframe 播放时它才是真实来源页，
    没有就退回调用方给的顶层页地址。ranged 表示响应带 Content-Range，
    供 accept() 判断「无后缀 + octet-stream」的签名直链。
    """
    if _rules is None:
        return None
    try:
        status = int(status)
    except (TypeError, ValueError):
        return None
    if status < 200 or status >= 400:
        return None
    url = _rules.normalize_url(url)
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        size = max(0, int(size or 0))
    except (TypeError, ValueError):
        size = 0
    if not _rules.accept(url, mime, size, {"range": bool(ranged)}):
        return None
    page_url = str(page_url or "")
    ref = str(referer or "").strip()[:500]
    if not ref.lower().startswith(("http://", "https://")):
        ref = page_url
    return {
        "url": url,
        "kind": str(mime or ""),
        "mime": str(mime or ""),
        "cat": _rules.classify(url, mime),
        "entry": "embedded",
        "size": size,
        "source": "内嵌网络",
        "page_url": ref,
        "referer": ref,
        "ua": str(ua or ""),
        "title": "",
    }


def dedupe_items(items):
    """同一批里按 URL 去重（后到的覆盖先到的），供合批线程使用。"""
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("url"):
            out[it["url"]] = it
    return list(out.values())


def readable_body(url, mime, size):
    """这个响应值不值得读正文。

    只读「文本类 + 体积已知且不大」的响应：清单和接口返回都在这个范围内；
    二进制大文件读完也认不出清单，还会把 deferral 挂很久（页面请求跟着等）。
    体积未知（分块传输、没有 content-length）时不读，避免读不完卡住请求。
    """
    if _rules is None:
        return False
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0 or n > _BODY_MAX_BYTES:
        return False
    m = str(mime or "").split(";")[0].strip()
    if _BODY_SKIP_MIME_RE.search(m):
        return False
    return bool(_BODY_TEXT_MIME_RE.search(m))


def sniff_body(text, url=""):
    """响应正文 → [(url, kind)]。

    清单正文按它自己的地址收（这才是「伪装成 text/plain 的 m3u8」的正解）；
    接口体里则挖绝对媒体直链，交给规则判种类。
    """
    if _rules is None or not text:
        return []
    head = str(text[:64]).lstrip("\ufeff").strip().upper()
    if head.startswith("#EXTM3U"):
        return [(str(url), "m3u8")] if url else []
    if "<MPD" in str(text[:1024]).upper():
        return [(str(url), "mpd")] if url else []
    out = []
    seen = set()
    body = str(text).replace("\\/", "/")      # JSON 里常见转义斜杠
    for m in _BODY_MEDIA_URL_RE.finditer(body):
        u = _rules.normalize_url(m.group(0))
        if not u or u in seen:
            continue
        seen.add(u)
        out.append((u, _rules.guess_kind(u)))
        if len(out) >= _BODY_MAX_ITEMS:
            break
    return out


def sniff_record(url, kind, source, page_url="", ua="", referer="", mime=""):
    """正文/钩子类条目 → 待登记记录；字段与 _accept_hook_items 造的一致。"""
    if _rules is None:
        return None
    u = _rules.normalize_url(url)
    if not u.lower().startswith(("http://", "https://")):
        return None
    k = str(kind or "").strip().lower()[:12]
    if k not in ("m3u8", "mpd", "audio", "video"):
        k = _rules.guess_kind(u)
    cat = "stream" if k in ("m3u8", "mpd") else _rules.classify(u, "")
    page = page_for_frame(referer, page_url)
    return {
        "url": u,
        "kind": k,
        "mime": str(mime or ""),
        "cat": cat,
        "entry": "embedded",
        "size": 0,
        "source": source,
        "page_url": page,
        "referer": page,
        "ua": str(ua or ""),
        "title": "",
    }


def task_text(task, timeout=_BODY_TIMEOUT, limit=_BODY_MAX_BYTES):
    """WebView2 的 Task<Stream> → 正文字符串；超时或失败返回空串。

    只能在后台线程调用：Task 的完成回调由 UI 线程派发，而 UI 线程此时已经
    从 WebResourceRequested 返回、空闲着，所以阻塞在后台线程不会死锁。
    """
    if task is None:
        return ""
    deadline = time.time() + max(0.5, float(timeout or 0))
    while time.time() < deadline:
        try:
            if task.IsCompleted:
                break
        except Exception:
            return ""
        time.sleep(0.02)
    else:
        return ""
    try:
        stream = task.Result
    except Exception:
        return ""
    if stream is None:
        return ""
    try:
        from System.IO import StreamReader
        reader = StreamReader(stream)
        try:
            return str(reader.ReadToEnd())[:limit]
        finally:
            try:
                reader.Dispose()
            except Exception:
                pass
    except Exception:
        return ""


def cookies_from_webview2(cookies):
    """CoreWebView2Cookie 列表 → 服务端 write_cookie_jar 认的字典列表。

    只搬 yt-dlp 真正用得到的字段；会话 cookie 的 Expires 为负/0，统一按 0
    （服务端会补一年）。任何一条取字段失败都跳过，不影响整罐。
    """
    out = []
    for c in cookies or []:
        try:
            name = str(getattr(c, "Name", "") or "")
            if not name:
                continue
            try:
                expires = float(getattr(c, "Expires", 0) or 0)
            except (TypeError, ValueError):
                expires = 0
            if bool(getattr(c, "IsSession", False)) or expires <= 0:
                expires = 0
            out.append({
                "name": name,
                "value": str(getattr(c, "Value", "") or ""),
                "domain": str(getattr(c, "Domain", "") or ""),
                "path": str(getattr(c, "Path", "") or "/"),
                "secure": bool(getattr(c, "IsSecure", False)),
                "httpOnly": bool(getattr(c, "IsHttpOnly", False)),
                "expirationDate": expires,
            })
        except Exception:
            continue
    return out


class EmbeddedBrowser:
    """主窗口里的浏览器视图控制器；本身不依赖 GUI，视图在第一次 open() 时创建。"""

    # pywebview 会把 js_api 对象的公开属性递归展开成可调用方法表；主窗口的 api.host
    # 下面挂着本对象，不挡住的话它会一路反射进 WinForms/WebView2 对象图（日志刷屏）。
    _serializable = False

    def __init__(self, sniff_sink=None, download_sink=None, page_sink=None):
        self._sniff_sink = sniff_sink
        self._download_sink = download_sink      # 页面浮条 → 宿主下载队列（单条/批量）
        self._page_sink = page_sink              # 页面浮条「下载本页」→ yt-dlp 整段
        self._sniff_q = None
        self._sniff_thread = None
        self._sniff_stop = False
        self._sniff_warned = False
        self._page_url = ""
        self._ua = ""
        self._manifest_pages = set()
        self._dom_seen = set()
        self._hook_seen = set()
        self._net_seen = {}
        self._ui_items = {}          # 当前顶层页的浮条条目（页面内提示用）
        self._ui_lock = threading.Lock()
        self._window = None
        self._form = None
        self._app_webview = None
        self._app_title = ""
        self._panel = None
        self._webv = None
        self._strip = None
        self._addr = None
        self._status = None
        self._btn_back = None
        self._btn_forward = None
        self._btn_return = None
        self._btn_reload = None
        self._btn_dlpage = None
        self._wf = None
        self._colors = None
        self._renderer = None
        self._menu = None
        self._mi_undo = None
        self._mi_cut = None
        self._mi_copy = None
        self._mi_paste = None
        self._mi_select_all = None
        self._mi_copy_url = None
        self._mi_open_ext = None
        self._mi_paste_open = None
        self._controller = None
        self._accel_hooked = False
        self._theme = "light"
        self._palette = palette_for("light")
        self._loading = False
        self._stopped = False
        self._status_tone = "info"
        self._core_ready = False
        self._init_started = False
        self._cancelled = False
        self._pending_nav = ""
        self._page_title = ""
        self._start_page_shown = False
        self._profile_dir = ""
        self.last_status = ""
        self._body_busy = 0                 # 正在读正文的响应数（WebResourceRequested）
        self._body_lock = threading.Lock()

    # ---------------- 打开 / 关闭 ----------------
    def open(self, window=None, theme=""):
        """在主窗口里显示浏览器视图；window 是 pywebview Window（第一次必传）。

        theme 由应用前端解析后传入（system 已解析成 light/dark）；视图已存在时
        每次打开都重新套用，停在起始页时按新主题重写。
        """
        requested = normalize_theme(theme)
        changed = bool(requested and requested != self._theme)
        if requested:
            self._theme = requested
        if self._panel is not None and self._form is not None:
            def show():
                self._apply_theme(self._theme)
                self._show_browser()
                if changed:
                    self._maybe_rewrite_start_page()
            self._ui(show)
            self._start_sniff()
            return {"ok": True}
        if window is None:
            return {"ok": False, "error": "缺少主窗口引用，无法打开内嵌浏览器"}
        form = getattr(window, "native", None)
        if form is None:
            return {"ok": False, "error": "主窗口尚未就绪，请稍后再试"}
        self._window, self._form = window, form
        result = {}

        def build():
            try:
                self._create_view()
                self._apply_theme(self._theme)
                self._show_browser()
                result["ok"] = True
            except Exception as e:
                result["ok"] = False
                result["error"] = "创建内嵌浏览器视图失败：%s" % e
        self._ui(build)
        if result.get("ok"):
            self._start_sniff()
            return {"ok": True}
        return {"ok": False,
                "error": result.get("error") or "界面线程未响应，无法创建浏览器视图"}

    def close(self):
        """销毁浏览器视图（主窗口退出时调用）；无视图时是空操作。"""
        if self._form is None:
            self._reset_refs()
            return

        def dispose():
            form = self._form
            profile = self._profile_dir
            self._show_app()
            try:
                self._detach_accelerators()
                if self._webv is not None:
                    try:
                        self._webv.Dispose()
                    except Exception:
                        pass
                if self._panel is not None and form is not None:
                    try:
                        form.Controls.Remove(self._panel)
                    except Exception:
                        pass
                    try:
                        self._panel.Dispose()
                    except Exception:
                        pass
                if self._menu is not None:
                    try:
                        self._menu.Dispose()
                    except Exception:
                        pass
            finally:
                if form is not None:
                    try:
                        form.Resize -= self._on_form_resize
                    except Exception:
                        pass
                self._reset_refs()
            if profile:
                shutil.rmtree(profile, ignore_errors=True)
        self._ui(dispose)

    def _reset_refs(self):
        self._sniff_stop = True
        self._sniff_q = None
        self._sniff_thread = None
        self._sniff_warned = False
        self._page_url = ""
        self._ua = ""
        self._manifest_pages = set()
        self._dom_seen = set()
        self._hook_seen = set()
        self._net_seen = {}
        self._ui_items = {}
        self._window = None
        self._form = None
        self._app_webview = None
        self._app_title = ""
        self._panel = None
        self._webv = None
        self._strip = None
        self._addr = None
        self._status = None
        self._btn_back = None
        self._btn_forward = None
        self._btn_return = None
        self._btn_reload = None
        self._btn_dlpage = None
        self._colors = None
        self._renderer = None
        self._menu = None
        self._mi_undo = None
        self._mi_cut = None
        self._mi_copy = None
        self._mi_paste = None
        self._mi_select_all = None
        self._mi_copy_url = None
        self._mi_open_ext = None
        self._mi_paste_open = None
        self._controller = None
        self._accel_hooked = False
        self._loading = False
        self._stopped = False
        self._status_tone = "info"
        self._core_ready = False
        self._init_started = False
        self._cancelled = False
        self._pending_nav = ""
        self._page_title = ""
        self._start_page_shown = False
        self._profile_dir = ""

    # ---------------- 视图创建 ----------------
    def _create_view(self):
        """UI 线程：创建 Panel + 工具栏 + 自建 WebView2，先隐藏等 open 时切换。"""
        if self._panel is not None or self._form is None:
            return
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        try:
            from webview.util import interop_dll_path
            clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
            clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
        except Exception:
            pass
        import System.Windows.Forms as WinForms
        from System.Drawing import ContentAlignment, ColorTranslator
        from Microsoft.Web.WebView2.WinForms import WebView2, CoreWebView2CreationProperties

        form = self._form
        self._wf = WinForms
        self._palette = palette_for(self._theme)
        self._colors = {k: ColorTranslator.FromHtml(v) for k, v in self._palette.items()}
        self._app_webview = getattr(form, "webview", None)
        self._app_title = str(form.Text or "")

        self._purge_profiles()
        self._profile_dir = tempfile.mkdtemp(prefix=_PROFILE_PREFIX)

        panel = None
        try:
            panel = WinForms.Panel()
            panel.Dock = WinForms.DockStyle.Fill
            panel.Visible = False
            webv = WebView2()
            webv.Dock = WinForms.DockStyle.Fill
            try:
                webv.DefaultBackgroundColor = self._colors["bg"]
            except Exception:
                pass
            try:
                props = CoreWebView2CreationProperties()
                props.UserDataFolder = self._profile_dir
                try:
                    props.set_IsInPrivateModeEnabled(True)
                except Exception:
                    pass
                webv.CreationProperties = props
            except Exception:
                pass
            webv.CoreWebView2InitializationCompleted += self._on_core_init

            strip = self._build_strip(form)
            panel.Controls.Add(webv)
            panel.Controls.Add(strip)
            form.Controls.Add(panel)
            form.Resize += self._on_form_resize
            self._panel, self._webv, self._strip = panel, webv, strip
            self._addr.Width = self._addr_width()
        except Exception:
            try:
                if panel is not None:
                    try:
                        form.Controls.Remove(panel)
                    except Exception:
                        pass
                    panel.Dispose()
            except Exception:
                pass
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = ""
            raise

    def _build_strip(self, form):
        WinForms = self._wf
        from System.Drawing import ContentAlignment
        owner = self

        class _BrowserStrip(WinForms.ToolStrip):
            """地址栏聚焦时 Esc / Alt+方向键属于对话框键，先于 KeyDown 被 ToolStrip
            消费；在这里交给宿主处理，其余仍走默认行为。"""

            def ProcessDialogKey(self, keyData):
                try:
                    data = int(keyData)
                    vk = data & 0xFFFF
                    ctrl = bool(data & int(WinForms.Keys.Control))
                    alt = bool(data & int(WinForms.Keys.Alt))
                    if vk == 0x1B or (alt and vk in (0x25, 0x27)):
                        if owner._handle_shortcut(vk, ctrl, alt):
                            return True
                except Exception:
                    pass
                return super().ProcessDialogKey(keyData)

        def mk(text, tip, fn):
            b = WinForms.ToolStripButton(text)
            b.DisplayStyle = WinForms.ToolStripItemDisplayStyle.Text
            b.ToolTipText = tip
            b.Click += lambda s, e: fn()
            try:
                b.Margin = WinForms.Padding(3, 0, 3, 0)
            except Exception:
                pass
            # 导航按钮永远留在工具栏上；溢出后 GetPreferredSize 会让地址栏宽度计算失败
            b.Overflow = WinForms.ToolStripItemOverflow.Never
            return b

        strip = _BrowserStrip()
        strip.GripStyle = WinForms.ToolStripGripStyle.Hidden
        strip.Dock = WinForms.DockStyle.Top
        strip.AutoSize = True
        try:
            strip.Padding = WinForms.Padding(8, 3, 8, 3)
        except Exception:
            pass
        try:
            strip.Font = form.Font
        except Exception:
            pass
        self._renderer = self._make_renderer()
        strip.Renderer = self._renderer

        self._btn_return = mk("← 返回下载器", "回到下载器界面（浏览器页面保留，可再切回）",
                              self.return_to_app)
        self._btn_back = mk("后退", "后退（Alt+←）", self.go_back)
        self._btn_forward = mk("前进", "前进（Alt+→）", self.go_forward)
        self._btn_back.Enabled = False
        self._btn_forward.Enabled = False
        self._btn_reload = mk("刷新", "重新加载当前页（F5 / Ctrl+R）", self.go_reload)
        self._btn_dlpage = mk(
            "下载本页",
            "把当前页交给 yt-dlp 解析并下载；B 站这类 DASH 页面会合并音视频，"
            "不会只落下一条分片",
            self.download_page)
        self._addr = WinForms.ToolStripTextBox()
        self._addr.AutoSize = False
        self._addr.Width = 480
        self._addr.ToolTipText = "输入网址后按回车（只支持 http/https）"
        self._addr.Overflow = WinForms.ToolStripItemOverflow.Never
        self._addr.KeyDown += self._on_addr_key
        try:
            self._addr.TextBox.Enter += self._on_addr_enter
            self._addr.TextBox.MouseUp += self._on_addr_mouseup
        except Exception:
            pass
        self._status = WinForms.ToolStripLabel("")
        # 状态栏固定宽度：长错误只裁显示，全文放 tooltip，避免把工具栏挤进溢出菜单
        self._status.AutoSize = False
        try:
            scale = float(getattr(form, "_scale", 0) or 0)
        except Exception:
            scale = 0.0
        if scale <= 0:
            try:
                scale = float(getattr(form, "DeviceDpi", 96) or 96) / 96.0
            except Exception:
                scale = 1.0
        self._status.Width = int(240 * max(1.0, scale))
        self._status.TextAlign = ContentAlignment.MiddleLeft
        self._status.Overflow = WinForms.ToolStripItemOverflow.Never

        strip.Items.Add(self._btn_return)
        strip.Items.Add(WinForms.ToolStripSeparator())
        for it in (self._btn_back, self._btn_forward, self._btn_reload):
            strip.Items.Add(it)
        strip.Items.Add(WinForms.ToolStripSeparator())
        for it in (self._btn_dlpage, self._addr, self._status):
            strip.Items.Add(it)
        self._build_context_menu()
        return strip

    # ---------------- 自绘主题 ----------------
    def _make_renderer(self):
        WinForms = self._wf
        from System.Drawing import Pen, Rectangle, SolidBrush
        owner = self

        class _ThemedRenderer(WinForms.ToolStripProfessionalRenderer):
            """平铺配色；不使用默认 Office 风格渐变，避免深色主题下露出亮色底。"""

            def OnRenderToolStripBackground(self, e):
                try:
                    e.Graphics.Clear(owner._colors["bg"])
                except Exception:
                    pass

            def OnRenderToolStripBorder(self, e):
                try:
                    ts = e.ToolStrip
                    if ts is None:
                        return
                    pen = Pen(owner._colors["border"])
                    try:
                        if isinstance(ts, WinForms.ContextMenuStrip):
                            e.Graphics.DrawRectangle(
                                pen, 0, 0, max(0, ts.Width - 1), max(0, ts.Height - 1))
                        else:
                            e.Graphics.DrawLine(pen, 0, ts.Height - 1, ts.Width, ts.Height - 1)
                    finally:
                        pen.Dispose()
                except Exception:
                    pass

            def OnRenderButtonBackground(self, e):
                try:
                    item = e.Item
                    if item is None or not item.Enabled:
                        return
                    if item.Pressed:
                        color = owner._colors["press"]
                    elif item.Selected:
                        color = owner._colors["hover"]
                    else:
                        return
                    rect = Rectangle(0, 0, item.Width, item.Height)
                    rect.Inflate(-1, -1)
                    brush = SolidBrush(color)
                    try:
                        e.Graphics.FillRectangle(brush, rect)
                    finally:
                        brush.Dispose()
                except Exception:
                    pass

            def OnRenderMenuItemBackground(self, e):
                try:
                    item = e.Item
                    if item is None or not item.Enabled:
                        return
                    if not (item.Selected or item.Pressed):
                        return
                    color = owner._colors["press"] if item.Pressed else owner._colors["hover"]
                    brush = SolidBrush(color)
                    try:
                        e.Graphics.FillRectangle(brush, 0, 0, item.Width, item.Height)
                    finally:
                        brush.Dispose()
                except Exception:
                    pass

            def OnRenderSeparator(self, e):
                try:
                    item = e.Item
                    if item is None:
                        return
                    pen = Pen(owner._colors["border"])
                    try:
                        if item.Height > item.Width:
                            x = item.Width // 2
                            e.Graphics.DrawLine(pen, x, 3, x, max(4, item.Height - 4))
                        else:
                            y = item.Height // 2
                            e.Graphics.DrawLine(pen, 6, y, max(7, item.Width - 7), y)
                    finally:
                        pen.Dispose()
                except Exception:
                    pass

            def OnRenderItemText(self, e):
                try:
                    text = str(e.Text or "")
                    if e.Item is None or not text:
                        return
                    color = e.Item.ForeColor if e.Item.Enabled else owner._colors["muted"]
                    flags = (WinForms.TextFormatFlags.NoPrefix
                             | WinForms.TextFormatFlags.VerticalCenter)
                    align = e.Item.TextAlign
                    if align in (WinForms.ContentAlignment.MiddleLeft,
                                 WinForms.ContentAlignment.TopLeft,
                                 WinForms.ContentAlignment.BottomLeft):
                        flags = flags | WinForms.TextFormatFlags.Left
                    elif align in (WinForms.ContentAlignment.MiddleRight,
                                   WinForms.ContentAlignment.TopRight,
                                   WinForms.ContentAlignment.BottomRight):
                        flags = flags | WinForms.TextFormatFlags.Right
                    else:
                        flags = flags | WinForms.TextFormatFlags.HorizontalCenter
                    font = e.TextFont or e.Item.Font
                    WinForms.TextRenderer.DrawText(
                        e.Graphics, text, font, e.TextRectangle, color, flags)
                except Exception:
                    pass

        return _ThemedRenderer()

    def _build_context_menu(self):
        WinForms = self._wf

        def add(text, fn):
            item = WinForms.ToolStripMenuItem(text)
            item.Click += lambda s, e: fn()
            menu.Items.Add(item)
            return item

        menu = WinForms.ContextMenuStrip()
        menu.ShowImageMargin = False
        menu.Renderer = self._renderer
        menu.Opening += self._on_menu_opening
        self._menu = menu
        self._mi_undo = add("撤销", self._menu_undo)
        self._mi_cut = add("剪切", self._menu_cut)
        self._mi_copy = add("复制", self._menu_copy)
        self._mi_paste = add("粘贴", self._menu_paste)
        self._mi_select_all = add("全选", self._menu_select_all)
        menu.Items.Add(WinForms.ToolStripSeparator())
        self._mi_copy_url = add("复制页面地址", self._menu_copy_url)
        self._mi_open_ext = add("在系统浏览器中打开", self._menu_open_ext)
        self._mi_paste_open = add("粘贴并打开", self._menu_paste_open)
        try:
            self._addr.TextBox.ContextMenuStrip = menu
        except Exception:
            pass

    def _box(self):
        try:
            return self._addr.TextBox
        except Exception:
            return None

    def _menu_undo(self):
        tb = self._box()
        if tb is not None:
            try:
                tb.Undo()
            except Exception:
                pass

    def _menu_cut(self):
        tb = self._box()
        if tb is not None:
            try:
                tb.Cut()
            except Exception:
                pass

    def _menu_copy(self):
        tb = self._box()
        if tb is not None:
            try:
                tb.Copy()
            except Exception:
                pass

    def _menu_paste(self):
        tb = self._box()
        if tb is not None:
            try:
                tb.Paste()
            except Exception:
                pass

    def _menu_select_all(self):
        tb = self._box()
        if tb is not None:
            try:
                tb.SelectAll()
            except Exception:
                pass

    def _current_url(self):
        src = ""
        core = self._core()
        if core is not None:
            try:
                src = str(core.Source or "")
            except Exception:
                src = ""
        if not src and self._addr is not None:
            try:
                src = str(self._addr.Text or "").strip()
            except Exception:
                src = ""
        if src.lower() in _BLANK_URLS:
            return ""
        return src if is_allowed_navigation(src) else ""

    def _clip_text(self):
        try:
            if not self._wf.Clipboard.ContainsText():
                return ""
            return str(self._wf.Clipboard.GetText() or "")
        except Exception:
            return ""

    def _menu_copy_url(self):
        url = self._current_url()
        if not url:
            self._set_status("当前页不是 http(s) 地址", bad=True)
            return
        try:
            self._wf.Clipboard.SetText(url)
            self._set_status("地址已复制")
        except Exception as e:
            self._set_status("复制失败：%s" % e, bad=True)

    def _menu_open_ext(self):
        url = self._current_url()
        if not url:
            self._set_status("当前页不是 http(s) 地址", bad=True)
            return
        try:
            webbrowser.open(url)
            self._set_status("已交给系统浏览器打开")
        except Exception as e:
            self._set_status("打不开系统浏览器：%s" % e, bad=True)

    def _menu_paste_open(self):
        target = normalize_address(self._clip_text())
        if not target:
            self._set_status("剪贴板里没有可打开的 http(s) 网址", bad=True)
            return
        self.navigate(target)

    def _on_menu_opening(self, sender, args):
        tb = self._box()
        selected = 0
        if tb is not None:
            try:
                selected = int(tb.SelectionLength)
            except Exception:
                selected = 0
        try:
            self._mi_undo.Enabled = bool(tb is not None and tb.CanUndo)
        except Exception:
            self._mi_undo.Enabled = False
        self._mi_cut.Enabled = selected > 0
        self._mi_copy.Enabled = selected > 0
        try:
            self._mi_select_all.Enabled = bool(tb is not None and str(tb.Text or ""))
        except Exception:
            self._mi_select_all.Enabled = False
        has_text = bool(self._clip_text())
        self._mi_paste.Enabled = has_text
        url = self._current_url()
        self._mi_copy_url.Enabled = bool(url)
        self._mi_open_ext.Enabled = bool(url)
        self._mi_paste_open.Enabled = bool(has_text and normalize_address(self._clip_text()))

    # ---------------- 快捷键（WebView2 加速键 + 地址栏） ----------------
    def _attach_accelerators(self):
        """pythonnet 无法实现 IMessageFilter 接口，改用 WebView2 自带的
        AcceleratorKeyPressed；控制器只暴露内部字段，取不到就静默降级。"""
        if self._accel_hooked or self._webv is None:
            return
        try:
            import System.Reflection as Reflection
            flags = (Reflection.BindingFlags.NonPublic
                     | Reflection.BindingFlags.Instance)
            field = self._webv.GetType().GetField("_coreWebView2Controller", flags)
            ctrl = field.GetValue(self._webv) if field is not None else None
            if ctrl is None:
                return
            ctrl.AcceleratorKeyPressed += self._on_accelerator_key
            self._controller = ctrl
            self._accel_hooked = True
        except Exception:
            self._controller = None
            self._accel_hooked = False

    def _detach_accelerators(self):
        if not self._accel_hooked or self._controller is None:
            self._accel_hooked = False
            return
        try:
            self._controller.AcceleratorKeyPressed -= self._on_accelerator_key
        except Exception:
            pass
        self._controller = None
        self._accel_hooked = False

    def _on_accelerator_key(self, sender, args):
        """页面有焦点时的浏览器快捷键；只处理明确支持的组合，其余交还页面。"""
        try:
            kind = str(getattr(args, "KeyEventKind", "") or "")
            if "KeyDown" not in kind:
                return
            vk = int(getattr(args, "VirtualKey", 0) or 0)
            Keys = self._wf.Keys
            mods = self._wf.Control.ModifierKeys
            if self._handle_shortcut(vk, bool(mods & Keys.Control), bool(mods & Keys.Alt)):
                try:
                    args.Handled = True
                except Exception:
                    pass
        except Exception:
            pass

    def _handle_shortcut(self, vk, ctrl, alt):
        """vk 是 Win32 虚拟键码；返回 True 表示该按键由宿主消费。"""
        if ctrl and vk == 0x4C:            # Ctrl+L
            self._focus_address()
            return True
        if ctrl and vk == 0x52:            # Ctrl+R
            self.go_reload()
            return True
        if vk == 0x74:                     # F5
            self.go_reload()
            return True
        if vk == 0x1B:                     # Esc
            if self._loading:
                self.go_stop()
                return True
            tb = self._box()
            try:
                if tb is not None and tb.Focused:
                    self._sync_addr()
                    self._focus_page()
                    return True
            except Exception:
                pass
            return False
        if alt and vk == 0x25:             # Alt+Left
            if self._can_go(True):
                self.go_back()
                return True
            return False
        if alt and vk == 0x27:             # Alt+Right
            if self._can_go(False):
                self.go_forward()
                return True
            return False
        return False

    # ---------------- Form/工具栏上的快捷键（页面内走 WebView2 加速键） ----------------
    def _can_go(self, back):
        core = self._core()
        if core is None:
            return False
        try:
            return bool(core.CanGoBack if back else core.CanGoForward)
        except Exception:
            return False

    def _focus_address(self):
        """延后到下一轮消息循环：在 WebView2 的加速键回调里直接抢焦点会被它拿回去。"""
        form = self._form
        if form is None:
            return
        try:
            from System import Action
            form.BeginInvoke(Action(self._focus_address_now))
        except Exception:
            self._focus_address_now()

    def _focus_address_now(self):
        try:
            self._addr.TextBox.Focus()
            self._addr.TextBox.SelectAll()
        except Exception:
            pass

    def _focus_page(self):
        def run():
            try:
                self._webv.Focus()
            except Exception:
                pass
        self._ui(run)

    # ---------------- 主题应用 ----------------
    def _apply_theme(self, theme):
        self._theme = normalize_theme(theme) or "light"
        self._palette = palette_for(self._theme)
        if self._strip is None:
            return
        try:
            from System.Drawing import ColorTranslator
            self._colors = {k: ColorTranslator.FromHtml(v) for k, v in self._palette.items()}
        except Exception:
            return
        colors = self._colors
        try:
            self._strip.BackColor = colors["bg"]
            self._strip.ForeColor = colors["text"]
            for item in self._strip.Items:
                try:
                    item.ForeColor = colors["accent"] if item == self._btn_return else colors["text"]
                except Exception:
                    pass
            self._addr.TextBox.BackColor = colors["field"]
            self._addr.TextBox.ForeColor = colors["text"]
            if self._panel is not None:
                self._panel.BackColor = colors["bg"]
            try:
                self._webv.DefaultBackgroundColor = colors["bg"]
            except Exception:
                pass
            if self._menu is not None:
                self._menu.BackColor = colors["bg"]
                self._menu.ForeColor = colors["text"]
                for item in self._menu.Items:
                    try:
                        item.ForeColor = colors["text"]
                    except Exception:
                        pass
            self._apply_status_color()
            self._strip.Invalidate()
            if self._menu is not None:
                self._menu.Invalidate()
        except Exception:
            pass

    def _apply_status_color(self):
        if self._status is None or self._colors is None:
            return
        key = {"bad": "err", "busy": "accent"}.get(self._status_tone, "muted")
        try:
            self._status.ForeColor = self._colors[key]
        except Exception:
            pass

    def _maybe_rewrite_start_page(self):
        """主题变了且仍停在起始页时重写一次，避免旧配色残留。"""
        if not self._start_page_shown:
            return
        core = self._core()
        if core is None:
            return
        try:
            src = str(core.Source or "")
        except Exception:
            src = ""
        if src.lower() in ("", "about:blank"):
            self._write_start_page(core)

    def _addr_width(self):
        """地址栏占满工具栏剩余宽度（按钮/状态栏按实际 DPI 计算）。"""
        try:
            form = self._form
            if form is None or self._strip is None or self._addr is None:
                return 480
            from System.Drawing import Size
            fixed = 0
            for it in self._strip.Items:
                if it == self._addr:
                    continue
                if it == self._status:
                    fixed += int(self._status.Width)
                    continue
                try:
                    width = int(it.GetPreferredSize(Size.Empty).Width)
                except Exception:
                    width = 0
                fixed += width if width > 0 else 64
            return max(180, int(form.ClientSize.Width - fixed - 56))
        except Exception:
            return 480

    def _on_form_resize(self, *_):
        if self._addr is None:
            return
        try:
            self._addr.Width = self._addr_width()
        except Exception:
            pass

    def _purge_profiles(self):
        """清掉异常退出留下的旧 profile（24 小时前的）。"""
        cutoff = time.time() - 24 * 3600
        tmp = tempfile.gettempdir()
        try:
            for name in os.listdir(tmp):
                if not name.startswith(_PROFILE_PREFIX):
                    continue
                path = os.path.join(tmp, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass

    # ---------------- 视图切换 ----------------
    def _show_browser(self):
        if self._panel is None:
            return
        if self._app_webview is not None:
            try:
                self._app_webview.Visible = False
            except Exception:
                pass
        try:
            self._panel.Visible = True
            self._panel.BringToFront()
        except Exception:
            pass
        self._apply_title()
        if not self._core_ready and not self._init_started:
            self._init_started = True
            self._set_status("正在启动浏览器内核…")
            try:
                self._webv.EnsureCoreWebView2Async(None)
            except Exception as e:
                self._init_started = False
                self._set_status("浏览器内核启动失败：%s" % e, bad=True)
        # 空白页时把焦点给地址栏（用户可以直接输入）；已有页面时焦点给网页。
        core = self._core()
        source = ""
        if core is not None:
            try:
                source = str(core.Source or "")
            except Exception:
                source = ""
        if not source or source.lower() == "about:blank":
            try:
                self._addr.TextBox.Focus()
            except Exception:
                try:
                    self._webv.Focus()
                except Exception:
                    pass
        else:
            try:
                self._webv.Focus()
            except Exception:
                pass
        self._schedule_relayout()

    def _schedule_relayout(self):
        """首次显示时 ToolStrip 还没完成测量，按钮的 PreferredSize 会偏小；
        等一轮消息循环后按真实宽度重算地址栏，避免把按钮挤进溢出菜单。"""
        form = self._form
        if form is None:
            return
        try:
            from System import Action
            form.BeginInvoke(Action(self._on_form_resize))
        except Exception:
            pass

    def _show_app(self):
        if self._panel is not None:
            try:
                self._panel.Visible = False
            except Exception:
                pass
        if self._app_webview is not None:
            try:
                self._app_webview.Visible = True
                self._app_webview.BringToFront()
                self._app_webview.Focus()
            except Exception:
                pass
        if self._form is not None:
            try:
                self._form.Text = self._app_title or WINDOW_TITLE
            except Exception:
                pass

    def return_to_app(self):
        self._ui(self._show_app)

    def _apply_title(self):
        if self._form is None:
            return
        text = WINDOW_TITLE
        if self._page_title:
            text = self._page_title + " — " + WINDOW_TITLE
        try:
            self._form.Text = text
        except Exception:
            pass

    # ---------------- 导航 ----------------
    def _core(self):
        """只在 UI 线程调用：CoreWebView2 的 getter 从别的线程读会和布局互锁。"""
        if self._webv is None:
            return None
        try:
            return self._webv.CoreWebView2
        except Exception:
            return None

    def _navigate_from_box(self):
        target = normalize_address(self._addr.Text if self._addr is not None else "")
        if not target:
            self._set_status("地址不合法：只支持 http(s) 网址", bad=True)
            return
        self.navigate(target)

    def navigate(self, target):
        def run():
            core = self._core()
            if core is None:
                self._pending_nav = target
                if self._addr is not None:
                    self._addr.Text = target
                self._set_status("浏览器内核启动中，已记下地址…")
                return
            if self._addr is not None:
                self._addr.Text = target
            try:
                core.Navigate(target)
            except Exception as e:
                self._set_status("打开失败：%s" % e, bad=True)
        self._ui(run)
        return True

    def go_back(self):
        def run():
            core = self._core()
            try:
                if core is not None and core.CanGoBack:
                    core.GoBack()
            except Exception:
                pass
        self._ui(run)

    def go_forward(self):
        def run():
            core = self._core()
            try:
                if core is not None and core.CanGoForward:
                    core.GoForward()
            except Exception:
                pass
        self._ui(run)

    def go_reload(self):
        def run():
            core = self._core()
            if core is None:
                return
            try:
                if self._start_page_shown and str(core.Source or "").lower() in ("", "about:blank"):
                    if self._write_start_page(core):
                        self._set_status(HOME_STATUS)
                        return
                core.Reload()
            except Exception:
                pass
        self._ui(run)

    def _write_start_page(self, core):
        """把起始页写进当前 about:blank；不走导航，避免被 data: 守卫拦掉。"""
        script = ("document.open();document.write(%s);document.close();"
                  % json.dumps(start_page_html(self._theme)))
        try:
            core.ExecuteScriptAsync(script)
            return True
        except Exception:
            return False

    def go_stop(self):
        def run():
            core = self._core()
            try:
                if core is not None:
                    core.Stop()
            except Exception:
                pass
            self._loading = False
            self._stopped = True
            self._update_buttons()
            self._set_status("已停止")
        self._ui(run)

    def _on_addr_key(self, sender, args):
        try:
            if args.KeyCode == self._wf.Keys.Enter:
                args.SuppressKeyPress = True
                self._navigate_from_box()
        except Exception:
            pass

    def _on_addr_enter(self, sender, args):
        """聚焦地址栏时全选，直接输入即可替换当前网址。"""
        try:
            self._addr.TextBox.SelectAll()
        except Exception:
            pass

    def _on_addr_mouseup(self, sender, args):
        try:
            if args.Button == self._wf.MouseButtons.Left and int(
                    self._addr.TextBox.SelectionLength) == 0:
                self._addr.TextBox.SelectAll()
        except Exception:
            pass

    # ---------------- WebView2 事件 ----------------
    def _on_core_init(self, sender, args):
        if self._webv is None:
            return
        if not getattr(args, "IsSuccess", False):
            self._init_started = False
            exc = getattr(args, "InitializationException", None)
            self._set_status("浏览器内核初始化失败：%s" % (exc or "未知原因"), bad=True)
            return
        try:
            core = self._webv.CoreWebView2
        except Exception:
            core = None
        if core is None:
            self._init_started = False
            self._set_status("浏览器内核初始化失败：拿不到 CoreWebView2", bad=True)
            return
        self._attach_core(core)
        self._core_ready = True
        self._attach_accelerators()
        self._sync_addr()
        self._update_buttons()
        if self._pending_nav:
            target, self._pending_nav = self._pending_nav, ""
            self.navigate(target)
        elif not self._start_page_shown:
            self._start_page_shown = True
            # 不能用 NavigateToString：它内部是 data: 导航，会被本类的守卫拦掉。
            self._write_start_page(core)
            try:
                self._addr.TextBox.Focus()
            except Exception:
                pass
            self._set_status(HOME_STATUS)
        else:
            self._set_status(HOME_STATUS)

    def _attach_core(self, core):
        """只在 UI 线程调用。"""
        if self._core_ready:
            return
        core.NavigationStarting += self._on_nav_starting
        core.NavigationCompleted += self._on_nav_completed
        core.SourceChanged += self._on_source_changed
        core.DocumentTitleChanged += self._on_title_changed
        core.ProcessFailed += self._on_process_failed
        core.NewWindowRequested += self._on_new_window
        core.DownloadStarting += self._on_download_starting
        core.WebResourceResponseReceived += self._on_web_resource_response
        core.WebMessageReceived += self._on_web_message
        # 正文嗅探：WebResourceRequested 才能拿 GetContentAsync（WebView2 版的
        # 「读响应体」）。只给正文有意义的资源类型挂过滤器，图片/字体/脚本不进回调。
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext as _Ctx
        except Exception:
            _Ctx = None
        if _Ctx is not None:
            try:
                core.WebResourceRequested += self._on_web_resource_requested
            except Exception:
                pass
            for _name in ("XmlHttpRequest", "Fetch", "Media", "Other"):
                try:
                    core.AddWebResourceRequestedFilter("*", getattr(_Ctx, _name))
                except Exception:
                    pass
        try:
            core.AddScriptToExecuteOnDocumentCreatedAsync(_ESCAPE_SCRIPT)
        except Exception:
            pass
        # DOM 补扫脚本：先规则、再扫描器、最后驱动（驱动依赖前两者）
        if _DOM_DRIVER_JS:
            try:
                for js in (_SNIFF_RULES_JS, _DOM_SCAN_JS, _DOM_DRIVER_JS):
                    core.AddScriptToExecuteOnDocumentCreatedAsync(js)
            except Exception:
                pass
        # 正文钩子：先转发器、再共用 injected.js（转发器必须先就位）
        if _HOOK_RELAY_JS:
            try:
                for js in (_HOOK_RELAY_JS, _HOOK_JS):
                    core.AddScriptToExecuteOnDocumentCreatedAsync(js)
            except Exception:
                pass
        # 页面内浮条：只负责展示和发下载请求，URL 真实性由 Python 侧复核
        if _SNIFF_UI_JS:
            try:
                core.AddScriptToExecuteOnDocumentCreatedAsync(_SNIFF_UI_JS)
            except Exception:
                pass
        try:
            self._ua = str(core.Settings.UserAgent or "")
        except Exception:
            self._ua = ""

    def _on_web_message(self, sender, args):
        """页面脚本发的消息：Esc（停止加载）、DOM 扫描、正文钩子、浮条下载请求。

        浮条下载只带 URL；真正的条目由 Python 侧的 _ui_items 复核，页面伪造
        一个没嗅到过的地址不会进队列。
        """
        try:
            msg = json.loads(str(getattr(args, "WebMessageAsJson", "") or "{}"))
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        try:
            tag = msg.get("__ytdlp")
            # args.Source 是宿主给的发送方文档地址（页面改不了），优先用它；
            # 拿不到时才退回消息体里的 frame（后者只能当线索）。
            frame = str(getattr(args, "Source", "") or "") or msg.get("frame")
            if tag == "escape" and self._loading:
                self.go_stop()
            elif tag == "dom":
                self._accept_dom_items(msg.get("items"), frame)
            elif tag == "hook":
                self._accept_hook_items(msg.get("items"), frame)
            elif tag == "dl":
                self._download_from_ui(msg.get("urls"))
            elif tag == "dlpage":
                self._download_page_from_ui()
            elif tag == "ui_ready":
                self._sync_ui()          # 页面脚本晚于首批结果就绪时补发快照
        except Exception:
            pass

    # ---------------- 页面内浮条：展示 + 下载 ----------------
    def _post_ui(self, payload):
        """宿主 → 页面浮条；只能从 UI 线程调 PostWebMessageAsJson。"""
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return

        def run():
            core = self._core()
            if core is None:
                return
            try:
                core.PostWebMessageAsJson(text)
            except Exception:
                pass
        self._ui(run)

    def _sync_ui(self):
        """把当前已收条目整份发给页面浮条（晚挂载/换页后补发也走这里）。"""
        with self._ui_lock:
            snapshot = list(self._ui_items.values())[-80:]
        self._post_ui({"__ytdlp": "sniff_ui", "items": snapshot})

    def _push_ui(self, items):
        """一批嗅探结果 → 当前顶层页的浮条条目。

        条目按 URL 去重、只保留最近 200 条；带 top_url 的条目还要属于当前页，
        避免换页后队列里迟到的旧响应跑到新页面的浮条上。
        """
        if not items:
            return
        with self._ui_lock:
            for it in items:
                if not isinstance(it, dict):
                    continue
                url = str(it.get("url") or "")
                if not url:
                    continue
                top = str(it.get("top_url") or self._page_url or "")
                if top and top != self._page_url:
                    continue
                cat = str(it.get("cat") or "").lower()
                try:
                    size = int(it.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                self._ui_items[url] = {
                    "url": url,
                    "title": str(it.get("title") or "")[:120],
                    "kind": str(it.get("kind") or "")[:24],
                    "mime": str(it.get("mime") or "")[:40],
                    "cat": cat[:16],
                    "size": size,
                    "source": str(it.get("source") or "")[:24],
                    "seg": bool(_rules.is_segment(url)) if _rules else False,
                    "audio": bool(_rules.is_audio(url)) if _rules else False,
                    "image": cat == "image",
                    "ts": time.time(),
                }
            if len(self._ui_items) > 200:
                for key in list(self._ui_items)[:len(self._ui_items) - 200]:
                    self._ui_items.pop(key, None)
        self._sync_ui()

    def _download_from_ui(self, urls):
        """浮条点「下载」→ 后台提交；cookie 导出不能在 UI 线程做。"""
        wanted = []
        with self._ui_lock:
            for u in urls or []:
                u = str(u or "").strip()
                if u and u in self._ui_items and u not in wanted:
                    wanted.append(u)
        if not wanted:
            self._post_ui({"__ytdlp": "sniff_ui_result", "ok": False,
                           "text": "这些地址不在本页嗅探结果里"})
            return
        if self._download_sink is None:
            self._post_ui({"__ytdlp": "sniff_ui_result", "ok": False,
                           "text": "下载通道未接通"})
            return
        wanted = wanted[:20]
        page = self._page_url

        def work():
            try:
                res = self._download_sink(wanted, page) or {}
            except Exception as e:
                res = {"ok": False, "error": "提交失败：%s" % e}
            if res.get("ok"):
                ids = res.get("task_ids") or []
                text = "已加入下载队列：%d 个" % (len(ids) or len(wanted))
            else:
                text = "下载失败：%s" % (res.get("error") or "未知原因")
            self._post_ui({"__ytdlp": "sniff_ui_result",
                           "ok": bool(res.get("ok")),
                           "error": str(res.get("error") or ""),
                           "text": text})

        threading.Thread(target=work, daemon=True).start()

    def _submit_page_download(self, page, report):
        """整页下载：后台把页面 URL 交给 _page_sink，report(text, ok, error) 回报。

        cookie 导出与队列提交都不能占 UI 线程，所以固定开后台线程；回报走调用方
        自己的通道（浮条消息 / 工具栏状态栏）。
        """
        if self._page_sink is None:
            report("页面下载通道未接通", False)
            return

        def work():
            try:
                res = self._page_sink(page) or {}
            except Exception as e:
                res = {"ok": False, "error": "提交失败：%s" % e}
            if res.get("ok"):
                ids = res.get("task_ids") or []
                report("已加入下载队列：%d 个" % (len(ids) or 1), True)
            else:
                report("下载失败：%s" % (res.get("error") or "未知原因"), False,
                       str(res.get("error") or ""))

        threading.Thread(target=work, daemon=True).start()

    def _post_page_result(self, text, ok, error=""):
        """整页下载结果 → 页面浮条状态行。"""
        self._post_ui({"__ytdlp": "sniff_ui_result", "ok": bool(ok),
                       "error": str(error or ""), "text": text})

    def download_page(self):
        """工具栏「下载本页」→ 当前页交给 yt-dlp 做站点解析并合并音视频。

        嗅探到的常常只是 B 站 DASH 的分片（`-1-30011.m4s` 之类，视频与音频各一条），
        拼不成能播的整段；这里用页面 URL 让 yt-dlp 自己解析格式再合。地址以
        WebView2 当前的 Source 为准（SPA 只改 pushState 也拿得到），取不到再退到
        最近一次顶层导航记录的 _page_url；页面世界传来的地址一律不当下载目标。
        """
        page = self._current_url() or str(self._page_url or "").strip()
        if not page.lower().startswith(("http://", "https://")):
            self._set_status("当前页不是 http(s) 地址，无法整页下载", bad=True)
            return
        self._set_status("正在解析当前页：%s" % _short(page, 60), busy=True)
        self._submit_page_download(
            page, lambda text, ok, error="": self._set_status(text, bad=not ok))

    def _download_page_from_ui(self):
        """浮条点「下载本页」→ 当前顶层页 URL 交给 yt-dlp 做页面级解析。

        单条嗅探 URL 只是 B 站 DASH 的视频/音频分片，拼不成整段；页面级下载
        由 yt-dlp 解析出所有格式并合并。只认 Python 侧的 _page_url，页面
        世界传来的地址不能直接当下载目标。
        """
        page = str(self._page_url or "").strip()
        if not page.lower().startswith(("http://", "https://")):
            self._post_ui({"__ytdlp": "sniff_ui_result", "ok": False,
                           "text": "当前页面不是 http(s) 地址，无法整页下载"})
            return
        self._submit_page_download(page, self._post_page_result)

    # ---------------- 网络嗅探：过滤入队 → 后台合批登记 ----------------
    def _start_sniff(self):
        if _rules is None or self._sniff_sink is None:
            return
        if self._sniff_thread is not None and self._sniff_thread.is_alive():
            return
        self._sniff_stop = False
        self._sniff_q = queue.Queue(maxsize=4000)
        self._sniff_thread = threading.Thread(
            target=self._sniff_loop, args=(self._sniff_q,), daemon=True)
        self._sniff_thread.start()

    def _sniff_loop(self, q):
        while not self._sniff_stop:
            try:
                first = q.get(timeout=5)
            except queue.Empty:
                continue
            # 播放器一开就是几十个分片请求：等一小会儿合成一批，别一条一条打服务
            time.sleep(0.8)
            batch = [first]
            while len(batch) < 300:
                try:
                    batch.append(q.get_nowait())
                except queue.Empty:
                    break
            items = dedupe_items(batch)
            if not items:
                continue
            page = items[-1].get("page_url") or self._page_url
            try:
                ok = self._sniff_sink(items, page)
            except Exception:
                ok = False
            if ok is False and not self._sniff_warned:
                self._sniff_warned = True
                self._set_status("嗅探结果未登记（YtDlpWeb/server.py 不可用）", bad=True)
            elif ok is not False:
                # 页面内浮条只显示真正登记成功的条目，否则点了下载也会被服务端拒绝
                self._push_ui(items)

    def _on_web_resource_response(self, sender, args):
        """UI 线程回调：只做正则过滤 + 入队，IO 全部交给合批线程。"""
        q = self._sniff_q
        if q is None or self._sniff_sink is None or _rules is None:
            return
        try:
            resp = args.Response
            status = int(resp.StatusCode)
            if status < 200 or status >= 400:
                return
            url = _rules.normalize_url(str(args.Request.Uri or ""))
            if not url.lower().startswith(("http://", "https://")):
                return
            # 先记实锤：正文钩子上报的 URL 必须在这张表里出现过才收。
            # 这里连被 accept() 判掉的响应也记（伪装成 text/plain 的清单
            # 只有钩子能认出来，网络层仍然算「真的请求过」）。
            self._remember_net(url)
            mime = self._header(resp.Headers, "Content-Type")
            size = self._header(resp.Headers, "Content-Length")
            page = self._page_url
            # 请求头里的 Referer 才是跨域 iframe 的真实来源页
            ref = self._header(getattr(args.Request, "Headers", None), "Referer")
            ranged = bool(self._header(resp.Headers, "Content-Range"))
            item = response_item(url, status, mime, size, page, self._ua, ref, ranged)
            if item is None:
                return
            item["top_url"] = page
            # 同一页已经看到清单后，后续分片不再登记（列表只留能下整片的那条）
            if _rules.is_segment(item["url"]) and page in self._manifest_pages:
                return
            is_manifest = _rules.is_manifest(item["url"]) or bool(
                _rules.MANIFEST_MIME_RE.search(item["mime"]))
            if is_manifest and page:
                self._manifest_pages.add(page)
                if len(self._manifest_pages) > 200:
                    self._manifest_pages.clear()
            item["title"] = str(self._page_title or "")
            try:
                q.put_nowait(item)
            except queue.Full:
                pass
        except Exception:
            pass

    def _remember_net(self, url):
        """记录本页「网络层真的见过」的地址；钩子复核只认这张表。"""
        if not url:
            return
        seen = self._net_seen
        seen[url] = True
        if len(seen) > _NET_SEEN_MAX:
            # 一次丢最早的一批，别每来一条都弹一次
            for key in list(seen)[:len(seen) - _NET_SEEN_MAX]:
                seen.pop(key, None)

    # ---------------- 正文嗅探（WebResourceRequested + GetContentAsync） ----------------
    def _on_web_resource_requested(self, sender, args):
        """UI 线程：只判断「要不要读正文」，读的动作交给后台线程。

        正文只有在 WebResourceRequested 里拿得到（GetContentAsync）；它是异步流，
        所以先取 deferral 兜住这次请求，读完 / 超时 / 出错都必须 Complete，
        不然页面那个请求会一直挂着。不满足条件的响应根本不取 deferral，
        代价只是一次头读取。
        """
        q = self._sniff_q
        if q is None or self._sniff_sink is None or _rules is None:
            return
        counted = False
        deferral = None
        try:
            resp = args.Response
            if resp is None:
                return
            status = _as_int(resp.StatusCode)
            if status < 200 or status >= 400:
                return
            url = _rules.normalize_url(str(args.Request.Uri or ""))
            if not url.lower().startswith(("http://", "https://")):
                return
            mime = self._header(resp.Headers, "Content-Type")
            size = _as_int(self._header(resp.Headers, "Content-Length"))
            # 清单/接口体很小且体积已知才读；大块二进制读完也认不出清单
            if not readable_body(url, mime, size):
                return
            with self._body_lock:
                if self._body_busy >= _BODY_CONCURRENCY:
                    return
                self._body_busy += 1
                counted = True
            deferral = args.GetDeferral()
            task = resp.GetContentAsync()
            if deferral is None or task is None:
                raise RuntimeError("拿不到 deferral / content")
            page = self._page_url
            ref = self._header(getattr(args.Request, "Headers", None), "Referer")
            threading.Thread(target=self._read_body,
                             args=(url, mime, page, ref, task, deferral),
                             daemon=True).start()
            counted = False                  # 交棒给读线程，由它收尾
        except Exception:
            if counted:
                with self._body_lock:
                    self._body_busy = max(0, self._body_busy - 1)
            if deferral is not None:
                try:
                    deferral.Complete()
                except Exception:
                    pass

    def _read_body(self, url, mime, page, referer, task, deferral):
        """后台线程：等 GetContentAsync 完成 → 认清单/挖直链 → 入队。"""
        try:
            text = task_text(task)
            if not text:
                return
            for hit_url, kind in sniff_body(text, url):
                rec = sniff_record(hit_url, kind, "响应正文", page, self._ua, referer, mime)
                if rec is None:
                    continue
                rec["top_url"] = page
                if _rules.is_segment(rec["url"]) and page in self._manifest_pages:
                    continue
                if rec["kind"] in ("m3u8", "mpd") and page:
                    self._manifest_pages.add(page)
                    if len(self._manifest_pages) > 200:
                        self._manifest_pages.clear()
                rec["title"] = str(self._page_title or "")[:200]
                try:
                    self._sniff_q.put_nowait(rec)
                except queue.Full:
                    break
        except Exception:
            pass
        finally:
            self._release_body(deferral)

    def _release_body(self, deferral):
        """读完必须解除挂起：先还配额，再回 UI 线程 Complete。"""
        with self._body_lock:
            self._body_busy = max(0, self._body_busy - 1)

        def done():
            try:
                deferral.Complete()
            except Exception:
                pass
        if self._form is None:
            done()
        else:
            self._ui(done)

    @staticmethod
    def _header(headers, name):
        try:
            if headers is None or not headers.Contains(name):
                return ""
            return str(headers.GetHeader(name) or "")
        except Exception:
            return ""

    def _accept_dom_items(self, items, frame_url=""):
        """DOM 扫描结果 → 待登记条目；返回本次收下的列表（便于单测）。

        扫描器与扩展共用一份（dom_scan.js），这里再复核：只收 http(s)、图片要求
        宽高都 ≥ 200、同页地址只登记一次（换页清空），收下的塞进合批队列。
        frame_url 是发消息那一帧的地址：跨域 iframe 里播放时用它当 Referer。
        """
        out = []
        if _rules is None or not isinstance(items, list):
            return out
        page = page_for_frame(frame_url, self._page_url)
        for it in items:
            if len(out) >= _DOM_MAX_ITEMS:
                break
            if not isinstance(it, dict):
                continue
            url = _rules.normalize_url(it.get("url"))
            if not url.lower().startswith(("http://", "https://")):
                continue
            kind = str(it.get("kind") or "").strip().lower()[:16]
            if kind == "image":
                # 图片过滤在扫描器里做过一次，这里再复核（页面脚本可以被伪造）
                if _side(it.get("w")) < _MIN_IMAGE_SIDE or _side(it.get("h")) < _MIN_IMAGE_SIDE:
                    continue
            elif kind not in ("video", "audio"):
                kind = _rules.guess_kind(url)
            if url in self._dom_seen:
                continue
            if len(self._dom_seen) >= _DOM_SEEN_MAX:
                self._dom_seen.clear()
            rec = {
                "url": url,
                "kind": kind,
                "mime": "",
                "cat": _rules.classify(url, ""),
                "entry": "embedded",
                "size": 0,
                "source": "页面元素",
                "page_url": page,
                "referer": page,
                "top_url": self._page_url,
                "ua": str(self._ua or ""),
                "title": str(self._page_title or "")[:200],
            }
            q = self._sniff_q
            if q is not None:
                try:
                    q.put_nowait(rec)
                except queue.Full:
                    continue             # 合批队列满：本条直接丢弃
            self._dom_seen.add(url)
            out.append(rec)
        return out

    def _accept_hook_items(self, items, frame_url=""):
        """正文钩子候选 → 待登记条目。

        与扩展同一套信任模型：钩子跑在页面世界，页面能伪造事件，所以只认
        「网络层也真的见过」的 URL（_net_seen）。另外清单到手后不再收分片。
        """
        out = []
        if _rules is None or not isinstance(items, list):
            return out
        page = page_for_frame(frame_url, self._page_url)
        for it in items:
            if len(out) >= _HOOK_MAX_ITEMS:
                break
            if not isinstance(it, dict):
                continue
            url = _rules.normalize_url(it.get("url"))
            if not url.lower().startswith(("http://", "https://")):
                continue
            if url not in self._net_seen:      # 没有实锤：一律不收
                continue
            if url in self._hook_seen:
                continue
            if _rules.is_segment(url) and self._page_url in self._manifest_pages:
                continue
            kind = str(it.get("kind") or "").strip().lower()[:12]
            if kind not in ("m3u8", "mpd", "audio", "video"):
                kind = _rules.guess_kind(url)
            # 钩子认的是正文（#EXTM3U/<MPD），地址可能不带后缀，classify 认不出来
            cat = "stream" if kind in ("m3u8", "mpd") else _rules.classify(url, "")
            rec = {
                "url": url,
                "kind": kind,
                "mime": "",
                "cat": cat,
                "entry": "embedded",
                "size": 0,
                "source": "页面请求",
                "page_url": page,
                "referer": page,
                "top_url": self._page_url,
                "ua": str(self._ua or ""),
                "title": str(self._page_title or "")[:200],
            }
            q = self._sniff_q
            if q is not None:
                try:
                    q.put_nowait(rec)
                except queue.Full:
                    continue             # 合批队列满：本条直接丢弃
            if len(self._hook_seen) >= _HOOK_SEEN_MAX:
                self._hook_seen.clear()
            self._hook_seen.add(url)
            out.append(rec)
        return out

    def export_cookies(self, url, timeout=2.0):
        """从 WebView2 的 CookieManager 取该 URL 会带的 cookie（含 httpOnly）。

        必须在非 UI 线程调用：先 Invoke 到 UI 线程发出异步请求，再在调用线程
        等结果（不阻塞 UI）。视图没开、内核没就绪或超时都返回空列表。
        """
        u = str(url or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            return []
        if self._form is None or self._webv is None or not self._core_ready:
            return []
        try:
            if not self._form.InvokeRequired:      # 已经在 UI 线程：不能等结果
                return []
        except Exception:
            return []
        box = {}

        def kick():
            core = self._core()
            if core is None:
                return
            try:
                box["task"] = core.CookieManager.GetCookiesAsync(u)
            except Exception:
                pass

        self._ui(kick)
        task = box.get("task")
        if task is None:
            return []
        deadline = time.time() + max(0.5, float(timeout or 2.0))
        while time.time() < deadline:
            try:
                if task.IsCompleted:
                    break
            except Exception:
                return []
            time.sleep(0.02)
        else:
            return []
        try:
            return cookies_from_webview2(task.Result)
        except Exception:
            return []

    def _update_page_url(self, source):
        u = str(source or "")
        self._page_url = u if u.lower().startswith(("http://", "https://")) else ""

    def _on_nav_starting(self, sender, args):
        uri = str(getattr(args, "Uri", "") or "")
        if not is_allowed_navigation(uri):
            self._cancelled = True
            try:
                args.Cancel = True
            except Exception:
                pass
            self._set_status("已拦截非 http(s) 地址：%s" % _short(uri), bad=True)
            return
        self._cancelled = False
        self._stopped = False
        prev = self._page_url
        self._page_url = uri if uri.lower().startswith(("http://", "https://")) else ""
        # 换页/重载：浮条只保留当前顶层页的条目，旧页的迟到响应由 top_url 再拦一次
        with self._ui_lock:
            self._ui_items.clear()
        if self._page_url != prev:
            self._dom_seen.clear()       # 换页后同一条媒体可以重新登记
            self._hook_seen.clear()
            self._net_seen.clear()       # 实锤只在当前页有效
        self._loading = True
        self._update_buttons()
        self._set_status("正在加载…", busy=True)

    def _on_nav_completed(self, sender, args):
        ok = bool(getattr(args, "IsSuccess", True))
        self._loading = False
        if ok:
            self._set_status("已打开")
        elif self._stopped:
            pass                     # 用户主动停止，保留 go_stop 的「已停止」
        elif not self._cancelled:
            hint = describe_nav_error(getattr(args, "WebErrorStatus", ""))
            if hint:
                self._set_status(hint, bad=True)
        self._cancelled = False
        try:
            src = str(getattr(sender, "Source", "") or "")
            self._update_page_url(src)
            self._sync_addr(src)
        except Exception:
            pass
        self._update_buttons()

    def _on_source_changed(self, sender, args):
        try:
            src = str(getattr(sender, "Source", "") or "")
            self._update_page_url(src)
            self._sync_addr(src)
        except Exception:
            pass
        self._update_buttons()

    def _on_title_changed(self, sender, args):
        try:
            self._page_title = str(getattr(sender, "DocumentTitle", "") or "").strip()
        except Exception:
            self._page_title = ""
        if self._panel is not None and self._panel.Visible:
            self._apply_title()

    def _on_process_failed(self, sender, args):
        kind = str(getattr(args, "ProcessFailedKind", "") or "")
        if "BrowserProcessExited" in kind:
            self._set_status("浏览器内核进程异常退出：请返回下载器后重新打开", bad=True)
            return
        self._set_status("页面进程异常（%s），正在重载…" % kind, bad=True)

        def run():
            try:
                sender.Reload()
            except Exception:
                pass
        self._ui(run)

    def _on_new_window(self, sender, args):
        """站点请求新窗口：在当前视图里打开（单窗口模型，不弹系统浏览器）。

        以前这里交给系统浏览器，点 target=_blank 的链接会直接被踢出程序。
        现在按单标签浏览器处理：http(s) 就地导航；空白窗（window.open('about:blank')
        后再写入内容）无法接住，直接忽略，别把当前页清空。
        """
        try:
            args.Handled = True
        except Exception:
            pass
        uri = str(getattr(args, "Uri", "") or "")
        if uri.lower() in _BLANK_URLS:
            return
        if not is_allowed_navigation(uri):
            self._set_status("已拦截新窗口请求：%s" % _short(uri), bad=True)
            return
        self.navigate(uri)

    def _on_download_starting(self, sender, args):
        """页面直接触发的下载一律取消：整段走「下载本页」，单条走嗅探列表。"""
        try:
            args.Cancel = True
        except Exception:
            pass
        self._set_status("已取消页面触发的下载；整段视频点「下载本页」")

    # ---------------- UI 小工具 ----------------
    def _ui(self, fn):
        form = self._form
        if form is None:
            return
        try:
            if form.InvokeRequired:
                from System import Action
                form.Invoke(Action(fn))
            else:
                fn()
        except Exception:
            pass

    def _set_status(self, text, bad=False, busy=False):
        self.last_status = text
        self._status_tone = "bad" if bad else ("busy" if busy else "info")

        def run():
            if self._status is None:
                return
            try:
                self._status.Text = text
                self._status.ToolTipText = text
                self._apply_status_color()
            except Exception:
                pass
        self._ui(run)

    def _sync_addr(self, url=""):
        u = str(url or "")

        def run():
            if self._addr is None:
                return
            text = u
            if not text:
                core = self._core()
                if core is not None:
                    try:
                        text = str(core.Source or "")
                    except Exception:
                        text = ""
            if text.lower() == "about:blank":
                text = ""
            try:
                if str(self._addr.Text) != text:
                    self._addr.Text = text
            except Exception:
                pass
        self._ui(run)

    def _update_buttons(self):
        def run():
            core = self._core()
            back = forward = False
            if core is not None:
                try:
                    back, forward = bool(core.CanGoBack), bool(core.CanGoForward)
                except Exception:
                    pass
            try:
                if self._btn_back is not None:
                    self._btn_back.Enabled = back
                if self._btn_forward is not None:
                    self._btn_forward.Enabled = forward
                if self._btn_reload is not None:
                    if self._loading:
                        self._btn_reload.Text = "停止"
                        self._btn_reload.ToolTipText = "停止加载（Esc）"
                    else:
                        self._btn_reload.Text = "刷新"
                        self._btn_reload.ToolTipText = "重新加载当前页（F5 / Ctrl+R）"
                if self._btn_dlpage is not None:
                    # 起始页 / 加载失败时没有 http(s) 当前页，整页下载无从下手
                    self._btn_dlpage.Enabled = bool(self._current_url()
                                                    or self._page_url)
                if self._addr is not None:
                    self._addr.Width = self._addr_width()
                    self._schedule_relayout()
            except Exception:
                pass
        self._ui(run)