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