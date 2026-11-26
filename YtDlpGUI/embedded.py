# -*- coding: utf-8 -*-
"""内嵌浏览器：主窗口内的浏览器视图（单窗口）。

形态（见 docs/prompts/2026-11-26-embedded-browser.md 的单窗口变体）：
* 不创建第二个 pywebview 窗口，而是在主窗体的同一个 WinForms 里放两个视图：
  pywebview 的主界面 WebView2（切换时只隐藏，状态不丢）与这里自建的浏览器视图；
* 浏览器视图 = Panel（原生 ToolStrip + 自建 WebView2），第一次打开时懒创建；
* 自建 WebView2 不经过 pywebview 注入，页面里不存在 window.pywebview；
* 顶层导航只放行 http/https（外加初始 about:blank），拦掉 file:/javascript:/data:；
* 站点请求新窗口时交给系统浏览器；页面直接触发的下载一律取消（下载走嗅探列表）；
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
      <li>页面直接触发的下载会被取消；请从嗅探列表里选择资源下载。</li>
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


def response_item(url, status, mime, size, page_url="", ua=""):
    """网络响应 → 待登记条目；纯函数，便于用假事件对象单测。"""
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
    if not _rules.accept(url, mime, size):
        return None
    page_url = str(page_url or "")
    return {
        "url": url,
        "kind": str(mime or ""),
        "mime": str(mime or ""),
        "cat": _rules.classify(url, mime),
        "entry": "embedded",
        "size": size,
        "source": "内嵌网络",
        "page_url": page_url,
        "referer": page_url,
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


class EmbeddedBrowser:
    """主窗口里的浏览器视图控制器；本身不依赖 GUI，视图在第一次 open() 时创建。"""

    # pywebview 会把 js_api 对象的公开属性递归展开成可调用方法表；主窗口的 api.host
    # 下面挂着本对象，不挡住的话它会一路反射进 WinForms/WebView2 对象图（日志刷屏）。
    _serializable = False

    def __init__(self, sniff_sink=None):
        self._sniff_sink = sniff_sink
        self._sniff_q = None
        self._sniff_thread = None
        self._sniff_stop = False
        self._sniff_warned = False
        self._page_url = ""
        self._ua = ""
        self._manifest_pages = set()
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
        for it in (self._btn_back, self._btn_forward, self._btn_reload,
                   self._addr, self._status):
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
        try:
            core.AddScriptToExecuteOnDocumentCreatedAsync(_ESCAPE_SCRIPT)
        except Exception:
            pass
        try:
            self._ua = str(core.Settings.UserAgent or "")
        except Exception:
            self._ua = ""

    def _on_web_message(self, sender, args):
        """页面脚本只发 Esc 消息；用于停止加载（普通按键不走加速键）。"""
        try:
            msg = json.loads(str(getattr(args, "WebMessageAsJson", "") or "{}"))
        except Exception:
            return
        try:
            if isinstance(msg, dict) and msg.get("__ytdlp") == "escape" and self._loading:
                self.go_stop()
        except Exception:
            pass

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
                self._set_status("扩展服务未启用，嗅探结果未登记", bad=True)

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
            url = str(args.Request.Uri or "")
            if not url.lower().startswith(("http://", "https://")):
                return
            mime = self._header(resp.Headers, "Content-Type")
            size = self._header(resp.Headers, "Content-Length")
            page = self._page_url
            item = response_item(url, status, mime, size, page, self._ua)
            if item is None:
                return
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

    @staticmethod
    def _header(headers, name):
        try:
            if headers is None or not headers.Contains(name):
                return ""
            return str(headers.GetHeader(name) or "")
        except Exception:
            return ""

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
        self._page_url = uri if uri.lower().startswith(("http://", "https://")) else ""
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
        """站点请求新窗口：交给系统浏览器（单窗口模型，不做标签页）。"""
        try:
            args.Handled = True
        except Exception:
            pass
        uri = str(getattr(args, "Uri", "") or "")
        if is_allowed_navigation(uri):
            try:
                webbrowser.open(uri)
                self._set_status("新窗口链接已交给系统浏览器")
                return
            except Exception:
                pass
        self._set_status("已拦截新窗口请求：%s" % _short(uri), bad=True)

    def _on_download_starting(self, sender, args):
        """页面直接触发的下载一律取消：下载统一从嗅探列表进入。"""
        try:
            args.Cancel = True
        except Exception:
            pass
        self._set_status("已取消页面触发的下载；请从嗅探列表里下载")

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
                if self._addr is not None:
                    self._addr.Width = self._addr_width()
                    self._schedule_relayout()
            except Exception:
                pass
        self._ui(run)