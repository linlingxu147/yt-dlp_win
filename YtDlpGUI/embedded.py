# -*- coding: utf-8 -*-
"""内嵌浏览器：主窗口内的浏览器视图（单窗口）。

形态（见 docs/prompts/2026-11-26-embedded-browser.md 的单窗口变体）：
* 不创建第二个 pywebview 窗口，而是在主窗体的同一个 WinForms 里放两个视图：
  pywebview 的主界面 WebView2（切换时只隐藏，状态不丢）与这里自建的浏览器视图；
* 浏览器视图 = Panel（原生 ToolStrip + 自建 WebView2），第一次打开时懒创建；
* 自建 WebView2 不经过 pywebview 注入，页面里不存在 window.pywebview；
* 顶层导航只放行 http/https（外加初始 about:blank），拦掉 file:/javascript:/data:；
* 站点请求新窗口时交给系统浏览器；页面直接触发的下载一律取消（下载走嗅探列表）；
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
_START_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<style>
:root { color-scheme: light dark; }
body { margin: 0; padding: 48px; font: 15px/1.7 "Microsoft YaHei UI", system-ui, sans-serif;
       background: light-dark(#f6f8fa, #0d1117); color: light-dark(#1f2328, #e6edf3); }
h1 { font-size: 22px; margin: 0 0 10px; }
p { margin: 6px 0; }
.hint { color: light-dark(#57606a, #8b949e); }
</style></head><body>
<h1>内嵌浏览器</h1>
<p>在上方地址栏输入网址，按回车打开。</p>
<p class="hint">点「← 返回下载器」回到主界面；这个页面会保留，可再次切回。</p>
</body></html>"""

_BLANK_URLS = ("about:blank",)
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
        self._wf = None
        self._normal_color = None
        self._err_color = None
        self._core_ready = False
        self._init_started = False
        self._cancelled = False
        self._pending_nav = ""
        self._page_title = ""
        self._start_page_shown = False
        self._profile_dir = ""
        self.last_status = ""

    # ---------------- 打开 / 关闭 ----------------
    def open(self, window=None):
        """在主窗口里显示浏览器视图；window 是 pywebview Window（第一次必传）。"""
        if self._panel is not None and self._form is not None:
            self._ui(self._show_browser)
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
        from System.Drawing import Color, ContentAlignment, SystemColors
        from Microsoft.Web.WebView2.WinForms import WebView2, CoreWebView2CreationProperties

        form = self._form
        self._wf = WinForms
        self._normal_color = SystemColors.ControlText
        self._err_color = Color.Firebrick
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
                webv.DefaultBackgroundColor = Color.White
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

        def mk(text, tip, fn):
            b = WinForms.ToolStripButton(text)
            b.DisplayStyle = WinForms.ToolStripItemDisplayStyle.Text
            b.ToolTipText = tip
            b.Click += lambda s, e: fn()
            return b

        strip = WinForms.ToolStrip()
        strip.GripStyle = WinForms.ToolStripGripStyle.Hidden
        strip.Dock = WinForms.DockStyle.Top
        strip.AutoSize = True
        try:
            strip.Font = form.Font
        except Exception:
            pass

        self._btn_return = mk("← 返回下载器", "回到下载器界面（浏览器页面保留，可再切回）",
                              self.return_to_app)
        self._btn_back = mk("后退", "后退", self.go_back)
        self._btn_forward = mk("前进", "前进", self.go_forward)
        self._btn_back.Enabled = False
        self._btn_forward.Enabled = False
        self._addr = WinForms.ToolStripTextBox()
        self._addr.AutoSize = False
        self._addr.Width = 480
        self._addr.ToolTipText = "输入网址后按回车（只支持 http/https）"
        self._addr.Overflow = WinForms.ToolStripItemOverflow.Never
        self._addr.KeyDown += self._on_addr_key
        self._status = WinForms.ToolStripLabel("")
        self._status.ForeColor = self._normal_color
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
        self._status.Width = int(210 * max(1.0, scale))
        self._status.TextAlign = ContentAlignment.MiddleLeft
        self._status.Overflow = WinForms.ToolStripItemOverflow.Never

        strip.Items.Add(self._btn_return)
        strip.Items.Add(WinForms.ToolStripSeparator())
        for it in (self._btn_back, self._btn_forward,
                   mk("刷新", "重新加载当前页", self.go_reload),
                   mk("停止", "停止加载", self.go_stop),
                   self._addr, self._status):
            strip.Items.Add(it)
        return strip

    def _addr_width(self):
        """地址栏占满工具栏剩余宽度（按钮/状态栏按实际 DPI 计算）。"""
        try:
            form = self._form
            if form is None or self._strip is None or self._addr is None:
                return 480
            from System.Drawing import Size
            fixed = 0
            for it in self._strip.Items:
                if it is self._addr:
                    continue
                if it is self._status:
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
                  % json.dumps(_START_PAGE))
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
        self._ui(run)

    def _on_addr_key(self, sender, args):
        try:
            if args.KeyCode == self._wf.Keys.Enter:
                args.SuppressKeyPress = True
                self._navigate_from_box()
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
        try:
            self._ua = str(core.Settings.UserAgent or "")
        except Exception:
            self._ua = ""

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
        self._page_url = uri if uri.lower().startswith(("http://", "https://")) else ""
        self._set_status("正在加载…")

    def _on_nav_completed(self, sender, args):
        ok = bool(getattr(args, "IsSuccess", True))
        if ok:
            self._set_status("已打开")
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

    def _set_status(self, text, bad=False):
        self.last_status = text

        def run():
            if self._status is None:
                return
            try:
                self._status.Text = text
                self._status.ToolTipText = text
                self._status.ForeColor = self._err_color if bad else self._normal_color
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
            except Exception:
                pass
        self._ui(run)