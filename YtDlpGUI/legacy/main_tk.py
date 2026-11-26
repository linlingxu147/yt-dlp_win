# 已退役的 Tkinter 版本（1.9.0），保留一个版本周期供回退；新版见上级目录 main.py。
# -*- coding: utf-8 -*-
"""
yt-dlp 图形界面（Tkinter）

界面按功能模块分区：下载链接 / 保存位置 / 内容与格式 / 播放列表 / 附加内容 / 网络与加速 / 高级
支持：分片并发 -N、外部下载器 aria2c、仅探测 --skip-download，并显示实时下载进度。
"""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(APP_DIR))  # legacy 多一层目录
CONFIG_FILE = os.path.join(os.path.dirname(APP_DIR), "config.json")

# 与网页版共用的核心（模式表、命令构建、进度解析、配置读写、自愈阶梯）在项目根目录。
sys.path.insert(0, BASE_DIR)
import ytdlp_core as core  # noqa: E402


def load_web_server():
    """把 YtDlpWeb/server.py 当模块载入（不执行它的 main）。

    返回 None 表示文件缺失——桌面版照常可用，只是扩展连不上。
    """
    import importlib.util
    path = os.path.join(BASE_DIR, "YtDlpWeb", "server.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("ytdlp_web_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ytdlp_web_server"] = mod
    spec.loader.exec_module(mod)
    return mod


# 与网页版共用的实现统一放在 ytdlp_core：这里只导入名字，避免两套逻辑各自漂移。
from ytdlp_core import (  # noqa: E402
    MODE_CHOICES, MODE_FMT, MODE_NAME, mode_key, TEMPLATES, BROWSERS,
    diagnose_pairs, find_tool, fmt_dur, fmt_size, info_from_json_raw,
    normalize_headers, sanitize_urls, unsafe_arg_in,
    build_detect_cmd, build_formats_cmd, build_probe_cmd, PROBE_VARIANTS,
    spawn, iter_lines_watch, kill_tree, command_text,
    new_progress_state, progress_event, progress_text, classify_line,
    is_net_failure, next_net_plan, next_cookie_plan,
    NET_PLAN_LABEL, COOKIE_PLAN_LABEL,
)
# 桌面版的下拉框直接显示 UA 原文（核心按「名称 → UA」存放，顺序与原来一致）
UA_PRESETS = list(core.UA_PRESETS.values())

FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")
FONT_S = ("Microsoft YaHei UI", 8)
MONO = ("Cascadia Mono", 9)

MAX_LOG_LINES = 4000

# 一次只跑一个 yt-dlp：既防任务互相踩，也让「停止」明确知道该杀谁
TASK_LABELS = {"download": "下载", "detect": "探测资源", "trouble": "自动排障",
               "update": "更新 yt-dlp", "formats": "读取格式表"}
# 桌面版比网页版宽松：慢速网络下 yt-dlp 也可能几分钟不吐行
GUI_STALL_SECONDS = 300

# 自动排障四个变体沿用桌面版原来的标题（核心里的文案更长一些）
TROUBLE_LABELS = {
    "plain": "直连（当前设置）",
    "ua": "浏览器 UA + Referer",
    "cookies": "浏览器 Cookies",
    "generic": "通用提取器兜底",
}


class App:
    def __init__(self, root, ext_service=True):
        self.root = root
        self.root.title("yt-dlp 下载器   ·   可视化界面")
        self.root.geometry("1010x1000")
        self.root.minsize(920, 800)

        self.q = queue.Queue()
        self.proc = None
        self.web_svc = None            # 内嵌的浏览器扩展服务（后台线程）
        self._want_service = ext_service
        self._web_mod = None           # 内嵌服务模块（与桌面版共用 RUNNER 闸门）
        self.ext_emit = None           # 非空=当前任务来自扩展服务，事件镜像回服务端
        self.ext_cfg = None            # 扩展任务的配置快照（不读界面控件）
        self.ext_cookie_file = None    # 扩展带来的 Netscape 临时文件，任务结束删除
        self.running = False
        self.task_kind = None     # 当前占用子进程的任务名，见 TASK_LABELS
        self.cfg = self._load_config()

        # 进度状态
        self.pct = 0.0
        self.speed = ""
        self.eta = ""
        self.size = ""
        self.item_i = 0
        self.item_total = 0
        self.cur_file = ""
        self.files_done = set()
        self.t_start = 0.0

        # 探测结果
        self.results = []
        self.checked = set()
        self.detecting = False
        self.diag_buf = ""      # 上一次任务的原始输出，用于失败时诊断

        # 扩展嗅探页
        self._sniff_items = []
        self._sniff_sig = None
        self._sniff_clip = ""

        self.has_aria2c = bool(core.which_tool("aria2c.exe"))
        self.pstate = new_progress_state()      # 进度解析状态（与网页版共用核心）

        # 失败自愈（与网页版同一套阶梯）：记录本任务的链接、已试过的招、本轮覆盖项
        self.dl_urls = []
        self.dl_no_playlist = False
        self.dl_outdir = ""
        self.net_tried = set()
        self.net_name = "normal"
        self.user_stopped = False

        self._build_ui()
        self._apply_config()
        # 配置问题（损坏、旧键改名、headers 粘连）在加载阶段已收集，这里补写进日志
        for _w in getattr(self, "_cfg_warnings", []):
            self.log_line("[配置] " + _w + "\n", "warn")
        self._refresh_progress()
        if self._want_service:
            self._start_ext_service()
        else:
            self._set_svc_label("扩展服务：未启用", ok=False)
        self._pump()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- 界面 ----------------
    def _dl_wheel(self, e):
        # 滚轮：下载页内滚整页；Text / Treeview 保留自己的滚动，不被整页抢走
        w = e.widget
        while w is not None:
            if w is self.dl_canvas:
                self.dl_canvas.yview_scroll(int(-e.delta / 120) * 3, "units")
                return
            if isinstance(w, (tk.Text, ttk.Treeview)):
                return
            w = getattr(w, "master", None)

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # 两页：下载（原有全部控件）/ 扩展嗅探（浏览器扩展上报的媒体地址）
        self.nb = ttk.Notebook(self.root)
        self.nb.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.dl_page = ttk.Frame(self.nb)
        self.sn_page = ttk.Frame(self.nb)
        self.nb.add(self.dl_page, text="  下载  ")
        self.nb.add(self.sn_page, text="  扩展嗅探  ")
        # 下载页内容约 1100px 高，小窗口装不下：整页包一层可滚动画布，
        # 任何窗口高度都能滚到日志与结果表（以前直接被裁掉看不见）
        self.dl_canvas = tk.Canvas(self.dl_page, highlightthickness=0, bd=0)
        self.dl_vsb = ttk.Scrollbar(self.dl_page, orient="vertical", command=self.dl_canvas.yview)
        self.dl_canvas.configure(yscrollcommand=self.dl_vsb.set)
        self.dl_vsb.pack(side="right", fill="y")
        self.dl_canvas.pack(side="left", fill="both", expand=True)
        self.dl_inner = ttk.Frame(self.dl_canvas)
        self.dl_win = self.dl_canvas.create_window((0, 0), window=self.dl_inner, anchor="nw")
        self.dl_inner.bind("<Configure>",
                           lambda e: self.dl_canvas.configure(scrollregion=self.dl_canvas.bbox("all")))
        self.dl_canvas.bind("<Configure>",
                            lambda e: self.dl_canvas.itemconfigure(self.dl_win, width=e.width))
        self.root.bind_all("<MouseWheel>", self._dl_wheel)
        self.dl_inner.columnconfigure(0, weight=1)
        self.dl_inner.rowconfigure(5, weight=1)

        # 顶部
        top = ttk.Frame(self.dl_inner)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        ttk.Label(top, text="yt-dlp 下载器", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        ttk.Label(top, text=f"  yt-dlp: {self.cfg.get('yt_dlp_path') or find_tool('yt-dlp.exe')}",
                  foreground="#8c959f", font=FONT_S).pack(side="left", padx=(10, 0), pady=(4, 0))
        self.lbl_env = ttk.Label(top, text="", font=FONT_S)
        self.lbl_env.pack(side="right")
        self.lbl_env.config(
            text=("ffmpeg 已就绪 · aria2c 已就绪" if self.has_aria2c else "ffmpeg 已就绪 · 未检测到 aria2c"),
            foreground=("#1a7f37" if self.has_aria2c else "#9a6700"))
        self.lbl_svc = ttk.Label(top, text="扩展服务：启动中…", font=FONT_S, foreground="#8c959f")
        self.lbl_svc.pack(side="right", padx=(0, 14))

        # 主体：左右两列分区
        body = ttk.Frame(self.dl_inner)
        body.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        lcol = ttk.Frame(body)
        lcol.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        rcol = ttk.Frame(body)
        rcol.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        lcol.columnconfigure(0, weight=1)
        rcol.columnconfigure(0, weight=1)

        self._build_source(lcol)
        self._build_dest(lcol, 1)
        self._build_format(lcol, 2)
        self._build_access(lcol, 3)

        self._build_playlist(rcol, 0)
        self._build_extras(rcol, 1)
        self._build_network(rcol, 2)
        self._build_advanced(rcol, 3)

        # 按钮
        bf = ttk.Frame(self.dl_inner)
        bf.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 2))
        self.btn_start = ttk.Button(bf, text="开始下载", command=self.start_download)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_detect = ttk.Button(bf, text="探测资源", command=self.detect)
        self.btn_detect.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(bf, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_trouble = ttk.Button(bf, text="自动排障", command=self.troubleshoot)
        self.btn_trouble.pack(side="left", padx=(0, 6))
        self.btn_update = ttk.Button(bf, text="更新 yt-dlp", command=self.update_ytdlp)
        self.btn_update.pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="复制命令", command=self.copy_cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="清空日志", command=self.clear_log).pack(side="left")
        self.lbl_status = ttk.Label(bf, text="就绪", font=FONT_B, foreground="#1a7f37")
        self.lbl_status.pack(side="right")

        self._build_progress(3)
        self._build_results(4)
        self._build_log(5)
        self._build_sniff_page(self.sn_page)

    def _build_source(self, parent):
        lf = ttk.LabelFrame(parent, text=" ① 下载链接 ")
        lf.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(0, weight=1)
        self.txt_url = tk.Text(lf, height=4, wrap="word", font=FONT, relief="solid", bd=1)
        self.txt_url.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=(6, 2))
        ttk.Label(lf, text="每行一个链接，可一次粘贴多个；也可以是播放列表地址",
                  foreground="#8c959f", font=FONT_S).grid(row=1, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Button(lf, text="剪贴板粘贴", width=10, command=self.paste_clipboard).grid(row=1, column=1, padx=4)
        ttk.Button(lf, text="清空", width=6, command=lambda: self.txt_url.delete("1.0", "end")).grid(
            row=1, column=2, padx=(0, 6))

    def _build_dest(self, parent, row):
        lf = ttk.LabelFrame(parent, text=" ② 保存位置 ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)
        ttk.Label(lf, text="输出目录", font=FONT).grid(row=0, column=0, sticky="w", padx=(6, 4), pady=(6, 3))
        self.var_outdir = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_outdir, font=FONT).grid(row=0, column=1, sticky="ew", pady=(6, 3))
        ttk.Button(lf, text="浏览…", width=7, command=self.browse_outdir).grid(row=0, column=2, padx=4)
        ttk.Button(lf, text="打开", width=6, command=self.open_outdir).grid(row=0, column=3, padx=(0, 6))

        ttk.Label(lf, text="文件名", font=FONT).grid(row=1, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_tpl = tk.StringVar()
        ttk.Combobox(lf, textvariable=self.var_tpl, values=TEMPLATES, font=FONT).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=3, padx=(0, 6))
        ttk.Label(lf, text="实际文件名 = 视频标题.扩展名（如 视频标题.mp4）。占位符：title 标题、id 编号、"
                          "uploader 作者、upload_date 日期、playlist_index 列表序号",
                  foreground="#8c959f", font=FONT_S, wraplength=470, justify="left").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

    def _build_format(self, parent, row):
        lf = ttk.LabelFrame(parent, text=" ③ 内容与格式 ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="下载模式", font=FONT).grid(row=0, column=0, sticky="w", padx=(6, 4), pady=(6, 3))
        self.var_mode = tk.StringVar()
        self.cb_mode = ttk.Combobox(lf, textvariable=self.var_mode, values=MODE_CHOICES, state="readonly", font=FONT)
        self.cb_mode.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(6, 3), padx=(0, 6))
        self.cb_mode.bind("<<ComboboxSelected>>", lambda e: self._sync_mode())

        ttk.Label(lf, text="合并容器", font=FONT).grid(row=1, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_merge = tk.StringVar()
        ttk.Combobox(lf, textvariable=self.var_merge, values=["mp4", "mkv", "webm", "不转换"],
                     state="readonly", font=FONT, width=12).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(lf, text="格式代码", font=FONT).grid(row=2, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_fmt = tk.StringVar()
        self.ent_fmt = ttk.Entry(lf, textvariable=self.var_fmt, font=FONT)
        self.ent_fmt.grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Button(lf, text="查看可用格式…", command=self.fetch_formats).grid(row=2, column=2, padx=(4, 6), pady=3)
        ttk.Label(lf, text="自定义模式下可填 137+140 这样的组合；其他模式由程序自动填好",
                  foreground="#8c959f", font=FONT_S).grid(row=3, column=0, columnspan=3, sticky="w",
                                                          padx=6, pady=(0, 6))

    def _build_access(self, parent, row):
        """访问与身份：解决「探测不到 / 403 / 需要登录」的关键设置。"""
        lf = ttk.LabelFrame(parent, text=" ④ 访问与身份（解决探测不到） ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="浏览器 Cookies", font=FONT).grid(row=0, column=0, sticky="w", padx=(6, 4), pady=(6, 3))
        self.var_browser = tk.StringVar()
        ttk.Combobox(lf, textvariable=self.var_browser, values=BROWSERS, state="readonly",
                     font=FONT, width=13).grid(row=0, column=1, sticky="w", pady=(6, 3))
        ttk.Label(lf, text="配置", font=FONT).grid(row=0, column=2, sticky="w", padx=(10, 4))
        self.var_browser_profile = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_browser_profile, font=FONT, width=14).grid(
            row=0, column=3, sticky="ew", pady=(6, 3), padx=(0, 6))

        ttk.Label(lf, text="User-Agent", font=FONT).grid(row=1, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_ua = tk.StringVar()
        ttk.Combobox(lf, textvariable=self.var_ua, values=UA_PRESETS, font=FONT).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=3, padx=(0, 6))

        ttk.Label(lf, text="Referer", font=FONT).grid(row=2, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_referer = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_referer, font=FONT).grid(
            row=2, column=1, columnspan=3, sticky="ew", pady=3, padx=(0, 6))

        ttk.Label(lf, text="请求头", font=FONT).grid(row=3, column=0, sticky="nw", padx=(6, 4), pady=3)
        self.txt_headers = tk.Text(lf, height=3, wrap="word", font=FONT, relief="solid", bd=1)
        self.txt_headers.grid(row=3, column=1, columnspan=3, sticky="ew", pady=3, padx=(0, 6))

        self.var_generic = tk.BooleanVar()
        ttk.Checkbutton(lf, text="通用提取器兜底", variable=self.var_generic).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 2))
        ttk.Label(lf, text="提取器参数", font=FONT).grid(row=5, column=0, sticky="w", padx=(6, 4), pady=2)
        self.var_extractor_args = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_extractor_args, font=FONT).grid(
            row=5, column=1, sticky="ew", pady=2)
        ttk.Label(lf, text="重试", font=FONT).grid(row=5, column=2, sticky="w", padx=(10, 4))
        self.var_retries = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_retries, font=FONT, width=6).grid(
            row=5, column=3, sticky="w", pady=2, padx=(0, 6))
        ttk.Label(lf, text="从浏览器读登录态（读取时请关闭该浏览器）；请求头每行一条 Key: Value；"
                          "提取器参数如 youtube:player_client=web",
                  foreground="#8c959f", font=FONT_S, wraplength=470, justify="left").grid(
            row=6, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

    def _build_playlist(self, parent, row):
        lf = ttk.LabelFrame(parent, text=" ⑤ 播放列表与容错 ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)
        self.var_pl = tk.BooleanVar()
        self.var_ignore = tk.BooleanVar()
        self.var_open = tk.BooleanVar()
        ttk.Checkbutton(lf, text="下载整个播放列表", variable=self.var_pl).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 2))
        ttk.Label(lf, text="范围", font=FONT).grid(row=1, column=0, sticky="w", padx=(6, 4), pady=2)
        self.var_items = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_items, font=FONT, width=16).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Checkbutton(lf, text="出错继续下一个", variable=self.var_ignore).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(lf, text="完成后打开文件夹", variable=self.var_open).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 6))
        self.var_nethard = tk.BooleanVar()
        ttk.Checkbutton(lf, text="失败后自动换姿势重试（浏览器指纹 / 分块传输）",
                        variable=self.var_nethard).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(lf, text="范围示例：1-5 或 1,3,7 或 2-", foreground="#8c959f", font=FONT_S).grid(
            row=1, column=1, sticky="e", padx=(0, 6))
        ttk.Label(lf, text="连接被掐断（10054）时会自动换一套请求方式重来；关掉后只按当前设置试一次",
                  foreground="#8c959f", font=FONT_S, wraplength=470, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))

    def _build_extras(self, parent, row):
        lf = ttk.LabelFrame(parent, text=" ⑥ 附加内容 ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)
        self.var_sub = tk.BooleanVar()
        self.var_embedsub = tk.BooleanVar()
        self.var_thumb = tk.BooleanVar()
        self.var_meta = tk.BooleanVar()
        ttk.Checkbutton(lf, text="下载字幕（含自动字幕）", variable=self.var_sub).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(6, 2))
        ttk.Label(lf, text="字幕语言", font=FONT).grid(row=1, column=0, sticky="w", padx=(6, 4), pady=2)
        self.var_sublang = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_sublang, font=FONT).grid(row=1, column=1, columnspan=2,
                                                                     sticky="ew", pady=2, padx=(0, 6))
        ttk.Checkbutton(lf, text="字幕嵌入视频", variable=self.var_embedsub).grid(
            row=2, column=0, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(lf, text="嵌入封面缩略图", variable=self.var_thumb).grid(
            row=2, column=1, sticky="w", padx=4, pady=2)
        ttk.Checkbutton(lf, text="写入元数据", variable=self.var_meta).grid(
            row=3, column=0, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(lf, text="语言：zh-Hans,zh,en（中文简体和英文）", foreground="#8c959f", font=FONT_S).grid(
            row=3, column=1, columnspan=2, sticky="w", padx=4, pady=(2, 6))

    def _build_network(self, parent, row):
        lf = ttk.LabelFrame(parent, text=" ⑦ 网络与加速 ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="限速", font=FONT).grid(row=0, column=0, sticky="w", padx=(6, 4), pady=(6, 3))
        self.var_rate = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_rate, font=FONT, width=14).grid(row=0, column=1, sticky="w", pady=(6, 3))
        ttk.Label(lf, text="如 500K / 5M，留空不限速", foreground="#8c959f", font=FONT_S).grid(
            row=0, column=2, sticky="w", padx=4)

        ttk.Label(lf, text="代理", font=FONT).grid(row=1, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_proxy = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_proxy, font=FONT).grid(row=1, column=1, columnspan=2,
                                                                   sticky="ew", pady=3, padx=(0, 6))
        ttk.Label(lf, text="如 http://127.0.0.1:7890", foreground="#8c959f", font=FONT_S).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=(0, 6))

        ttk.Label(lf, text="Cookies", font=FONT).grid(row=3, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_cookie = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_cookie, font=FONT).grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Button(lf, text="选择…", width=7, command=self.browse_cookie).grid(row=3, column=2, padx=4)

        ttk.Separator(lf).grid(row=4, column=0, columnspan=3, sticky="ew", padx=6, pady=6)

        # 分片并发 -N
        self.var_conc = tk.BooleanVar()
        self.var_conc_n = tk.StringVar()
        ttk.Checkbutton(lf, text="分片并发加速（-N）", variable=self.var_conc).grid(
            row=5, column=0, sticky="w", padx=(6, 4), pady=2)
        box = ttk.Frame(lf)
        box.grid(row=5, column=1, sticky="w", pady=2)
        ttk.Spinbox(box, from_=1, to=32, width=5, textvariable=self.var_conc_n, font=FONT).pack(side="left")
        ttk.Label(box, text="个分片同时下载（DASH/HLS 分片视频有效）",
                  foreground="#8c959f", font=FONT_S).pack(side="left", padx=6)

        # 外部下载器 aria2c
        self.var_aria = tk.BooleanVar()
        self.var_aria_args = tk.StringVar()
        ttk.Checkbutton(lf, text="外部下载器 aria2c", variable=self.var_aria).grid(
            row=6, column=0, sticky="w", padx=(6, 4), pady=2)
        self.lbl_aria = ttk.Label(lf, text="", font=FONT_S)
        self.lbl_aria.grid(row=6, column=2, sticky="w", padx=4)
        ttk.Entry(lf, textvariable=self.var_aria_args, font=FONT).grid(
            row=7, column=1, columnspan=2, sticky="ew", pady=(2, 6), padx=(0, 6))
        ttk.Label(lf, text="aria2c 参数", font=FONT).grid(row=7, column=0, sticky="w", padx=(6, 4), pady=(2, 6))
        if self.has_aria2c:
            self.lbl_aria.config(text="已就绪（多线程）", foreground="#1a7f37")
        else:
            self.lbl_aria.config(text="未检测到 aria2c.exe", foreground="#9a6700")

    def _build_advanced(self, parent, row):
        lf = ttk.LabelFrame(parent, text=" ⑧ 高级 ")
        lf.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        lf.columnconfigure(1, weight=1)
        self.var_flat = tk.BooleanVar()
        ttk.Checkbutton(lf, text="快速探测（--flat-playlist，不解析清晰度）", variable=self.var_flat).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 2))
        ttk.Label(lf, text="探测大播放列表时勾选，只列出条目标题，速度快很多",
                  foreground="#8c959f", font=FONT_S).grid(row=1, column=0, columnspan=2, sticky="w",
                                                          padx=6, pady=(0, 4))
        ttk.Label(lf, text="附加参数", font=FONT).grid(row=2, column=0, sticky="w", padx=(6, 4), pady=3)
        self.var_extra = tk.StringVar()
        ttk.Entry(lf, textvariable=self.var_extra, font=FONT).grid(row=2, column=1, sticky="ew", pady=3, padx=(0, 6))
        ttk.Label(lf, text="任意 yt-dlp 参数，如 --download-archive archive.txt",
                  foreground="#8c959f", font=FONT_S).grid(row=3, column=1, sticky="w", pady=(0, 6))
        self.var_extsvc = tk.BooleanVar()
        ttk.Checkbutton(lf, text="启用浏览器扩展支持（本机 127.0.0.1:8765，网页版/扩展共用）",
                        variable=self.var_extsvc,
                        command=self._toggle_ext_service).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 2))
        ttk.Label(lf, text="关掉后扩展就找不到服务；桌面版自身下载不受影响",
                  foreground="#8c959f", font=FONT_S).grid(row=5, column=0, columnspan=2, sticky="w",
                                                          padx=6, pady=(0, 6))

    def _build_progress(self, row):
        lf = ttk.LabelFrame(self.dl_inner, text=" 下载进度 ")
        lf.grid(row=row, column=0, sticky="ew", padx=10, pady=(4, 2))
        lf.columnconfigure(0, weight=1)
        self.lbl_file = ttk.Label(lf, text="等待开始", font=FONT, foreground="#57606a")
        self.lbl_file.grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 2))
        self.var_prog = tk.DoubleVar(value=0)
        self.bar = ttk.Progressbar(lf, variable=self.var_prog, maximum=100, mode="determinate")
        self.bar.grid(row=1, column=0, sticky="ew", padx=(8, 6), pady=2)
        self.lbl_pct = ttk.Label(lf, text="0%", font=FONT_B, width=8, anchor="e")
        self.lbl_pct.grid(row=1, column=1, padx=(0, 8), pady=2)
        self.lbl_detail = ttk.Label(lf, text="—", font=FONT_S, foreground="#57606a")
        self.lbl_detail.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

    def _build_results(self, row):
        lf = ttk.LabelFrame(self.dl_inner, text=" 探测结果（点 ✓ 列勾选要下载的条目） ")
        lf.grid(row=row, column=0, sticky="nsew", padx=10, pady=(2, 2))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        cols = ("sel", "title", "dur", "res", "size", "up")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=7, selectmode="extended")
        self.tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 2))
        sb = ttk.Scrollbar(lf, command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=(6, 2))
        self.tree.config(yscrollcommand=sb.set)
        head = {"sel": "✓", "title": "标题", "dur": "时长", "res": "清晰度", "size": "大小", "up": "上传者"}
        width = {"sel": 34, "title": 400, "dur": 62, "res": 82, "size": 80, "up": 150}
        anchor = {"sel": "center", "title": "w", "dur": "center", "res": "center", "size": "e", "up": "w"}
        for c in cols:
            self.tree.heading(c, text=head[c])
            self.tree.column(c, width=width[c], anchor=anchor[c],
                             stretch=(c in ("title", "up")))
        self.tree.tag_configure("on", foreground="#0550ae")
        self.tree.bind("<Button-1>", self._toggle_check)

        bf = ttk.Frame(lf)
        bf.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        ttk.Button(bf, text="全选", width=8, command=lambda: self._set_all(True)).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="全不选", width=8, command=lambda: self._set_all(False)).pack(side="left", padx=(0, 6))
        self.btn_dl_sel = ttk.Button(bf, text="下载选中项", command=self.download_selected, state="disabled")
        self.btn_dl_sel.pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="清空列表", width=10, command=self.clear_results).pack(side="left")
        self.lbl_count = ttk.Label(bf, text="尚未探测", font=FONT_S, foreground="#8c959f")
        self.lbl_count.pack(side="right")

        pf = ttk.Frame(lf)
        pf.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self.pv_img = ttk.Label(pf, text="选中一行看封面", font=FONT_S, foreground="#8c959f")
        self.pv_img.pack(side="left", padx=(0, 10))
        right = ttk.Frame(pf)
        right.pack(side="left", fill="both", expand=True)
        self.pv_meta = ttk.Label(right, text="", font=FONT_S, justify="left", wraplength=420)
        self.pv_meta.pack(anchor="w")
        self.btn_pv_open = ttk.Button(right, text="打开原网页", state="disabled",
                                      command=self._open_preview_url)
        self.btn_pv_open.pack(anchor="w", pady=(4, 0))
        self._preview_url = ""
        self._photo = None
        self._preview_gen = 0
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _build_log(self, row):
        lf = ttk.LabelFrame(self.dl_inner, text=" 运行日志 ")
        lf.grid(row=row, column=0, sticky="nsew", padx=10, pady=(2, 10))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log = tk.Text(lf, height=8, wrap="word", font=MONO, relief="solid", bd=1,
                           bg="#ffffff", fg="#1f2328", insertbackground="#1f2328")
        self.log.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=6)
        self.log.config(yscrollcommand=sb.set)
        for t, c in (("err", "#cf222e"), ("warn", "#9a6700"), ("ok", "#1a7f37"),
                     ("cmd", "#0550ae"), ("prog", "#57606a")):
            self.log.tag_config(t, foreground=c)
        self.log.configure(state="disabled")

    # ---------------- 配置 ----------------
    def _load_config(self):
        """读配置：交给共用核心（旧键迁移、headers 清洗、损坏回退都在里面）。

        核心通过 warn 回调上报问题，不再像以前那样静默吞掉；此时日志控件还
        没建好，先把问题收集起来，_build_ui 之后再补写进日志。
        """
        self._cfg_warnings = []
        return core.load_config(CONFIG_FILE, warn=self._cfg_warnings.append)

    def _warn(self, msg):
        """把核心上报的配置问题写进日志区。"""
        try:
            self.log_line("[配置] " + str(msg) + "\n", "warn")
        except Exception:
            pass

    def _collect_cfg(self):
        """把界面上的设置收集成 dict，交给共用核心构建命令。"""
        cfg = dict(self.cfg)
        cfg.update({
            "outdir": self.var_outdir.get().strip(),
            "mode": mode_key(self.var_mode.get()),   # 界面是中文标签，存盘是 ASCII 键
            "merge": self.var_merge.get(), "fmt": self.var_fmt.get().strip(),
            "tpl": self.var_tpl.get().strip(), "sublang": self.var_sublang.get().strip(),
            "rate": self.var_rate.get().strip(), "proxy": self.var_proxy.get().strip(),
            "items": self.var_items.get().strip(), "cookie": self.var_cookie.get().strip(),
            "extra": self.var_extra.get().strip(),
            "sub": self.var_sub.get(), "embedsub": self.var_embedsub.get(),
            "thumb": self.var_thumb.get(), "meta": self.var_meta.get(),
            "pl": self.var_pl.get(), "ignore": self.var_ignore.get(),
            "open_after": self.var_open.get(),
            "conc": self.var_conc.get(), "conc_n": self.var_conc_n.get().strip(),
            "aria": self.var_aria.get(), "aria_args": self.var_aria_args.get().strip(),
            "flat": self.var_flat.get(),
            "net_hard": self.var_nethard.get(),
            "ext_service": self.var_extsvc.get(),
            "browser": self.var_browser.get(), "browser_profile": self.var_browser_profile.get().strip(),
            "ua": self.var_ua.get().strip(), "referer": self.var_referer.get().strip(),
            "headers": normalize_headers(self.txt_headers.get("1.0", "end")),
            "generic": self.var_generic.get(),
            "extractor_args": self.var_extractor_args.get().strip(),
            "retries": self.var_retries.get().strip(),
        })
        return cfg

    def _save_config(self):
        """原子写 + 失败不静默（headers 清洗、.bak 备份由核心负责）。"""
        c = self._collect_cfg()
        c["yt_dlp_path"] = find_tool("yt-dlp.exe")
        if core.save_config(CONFIG_FILE, c, warn=self._warn):
            self.cfg = c

    def _apply_config(self):
        self.var_outdir.set(self.cfg["outdir"])
        self.var_mode.set(MODE_NAME.get(mode_key(self.cfg.get("mode")), MODE_CHOICES[0]))
        self.var_merge.set(self.cfg["merge"]); self.var_fmt.set(self.cfg["fmt"])
        self.var_tpl.set(self.cfg["tpl"]); self.var_sublang.set(self.cfg["sublang"])
        for k, v in (("rate", self.var_rate), ("proxy", self.var_proxy), ("items", self.var_items),
                     ("cookie", self.var_cookie), ("extra", self.var_extra),
                     ("aria_args", self.var_aria_args), ("browser_profile", self.var_browser_profile),
                     ("ua", self.var_ua), ("referer", self.var_referer),
                     ("extractor_args", self.var_extractor_args), ("retries", self.var_retries)):
            v.set(self.cfg[k])
        self.var_browser.set(self.cfg.get("browser") or "（不使用）")
        self.txt_headers.delete("1.0", "end")
        self.txt_headers.insert("1.0", self.cfg.get("headers") or "")
        self.var_sub.set(self.cfg["sub"]); self.var_embedsub.set(self.cfg["embedsub"])
        self.var_thumb.set(self.cfg["thumb"]); self.var_meta.set(self.cfg["meta"])
        self.var_pl.set(self.cfg["pl"]); self.var_ignore.set(self.cfg["ignore"])
        self.var_open.set(self.cfg.get("open_after", True))
        self.var_conc.set(self.cfg["conc"]); self.var_conc_n.set(self.cfg["conc_n"] or "8")
        self.var_aria.set(self.cfg["aria"]); self.var_flat.set(self.cfg["flat"])
        self.var_generic.set(self.cfg["generic"])
        self.var_nethard.set(self.cfg.get("net_hard", True))
        self.var_extsvc.set(bool(self.cfg.get("ext_service", True)))
        self._sync_mode(keep_fmt=True)

    def _sync_mode(self, keep_fmt=False):
        """切模式：自定义才放开格式框，其余模式套上该模式的默认表达式。

        载入配置时带 keep_fmt，否则用户存下来的 fmt 会被模式默认值盖掉。
        """
        key = mode_key(self.var_mode.get())
        self.ent_fmt.configure(state="normal" if key == "custom" else "disabled")
        if not keep_fmt and key in MODE_FMT:
            self.var_fmt.set(MODE_FMT[key])

    # ---------------- 小工具 ----------------
    def browse_outdir(self):
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or BASE_DIR)
        if d:
            self.var_outdir.set(d)

    def browse_cookie(self):
        p = filedialog.askopenfilename(title="选择 cookies.txt", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if p:
            self.var_cookie.set(p)

    def open_outdir(self, path=None):
        d = (path or self.var_outdir.get()).strip()
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except Exception:
            # 统一走 core.spawn，避免这里再留一处直接创建进程的代码
            try:
                spawn(["explorer", d], BASE_DIR)
            except Exception:
                pass

    def paste_clipboard(self):
        try:
            t = self.root.clipboard_get()
        except Exception:
            return
        if t:
            cur = self.txt_url.get("1.0", "end").strip()
            self.txt_url.insert("end", ("\n" if cur else "") + t.strip())

    def get_urls(self):
        raw = self.txt_url.get("1.0", "end")
        return [u.strip() for u in raw.splitlines() if u.strip() and not u.strip().startswith("#")]

    def _safe_urls(self, urls):
        """过滤非法链接（含以 "-" 开头的伪链接、裸域名补 https://），丢掉的写进日志。"""
        good, bad = sanitize_urls(urls)
        if bad:
            self.log_line("[提示] 已忽略 %d 个无法识别的链接：%s\n"
                          % (len(bad), "、".join(bad[:3])), "warn")
        return good

    def _unsafe_args(self):
        """返回 (第一个危险参数, 它来自哪个输入框)。"""
        for src, text in (("附加参数", self.var_extra.get()),
                          ("aria2c 参数", self.var_aria_args.get())):
            bad = unsafe_arg_in(text)
            if bad:
                return bad, src
        return "", ""

    def _guard_extra_args(self):
        """危险参数（--exec / --config-location 之类）一律拦下，不再直接拼进命令行。"""
        bad, src = self._unsafe_args()
        if bad:
            self.log_line("[错误]「%s」里不允许使用 %s（这类参数会执行命令或读取本机文件，已禁用）\n"
                          % (src, bad), "err")
            return False
        return True

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def log_line(self, text, tag=None):
        self.log.configure(state="normal")
        try:
            if int(self.log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
                self.log.delete("1.0", "800.0")
        except Exception:
            pass
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")
        if self.ext_emit:
            self.ext_emit({"type": "log", "text": text.rstrip("\n")})

    def set_status(self, text, color="#1f2328"):
        self.lbl_status.config(text=text, foreground=color)

    # ---------------- 进度显示 ----------------
    def _reset_progress(self):
        self.pstate = new_progress_state()
        self.pct = 0.0
        self.speed = self.eta = self.size = ""
        self.item_i = self.item_total = 0
        self.cur_file = ""
        self.files_done = set()
        self.t_start = time.time()
        self._refresh_progress()

    def _refresh_progress(self):
        self.var_prog.set(self.pct)
        self.lbl_pct.config(text=f"{self.pct:.0f}%")
        bits = []
        if self.speed:
            bits.append("速度 " + self.speed)
        if self.eta:
            bits.append("剩余 " + self.eta)
        if self.size:
            bits.append("大小 " + self.size)
        if self.item_total:
            bits.append(f"第 {max(self.item_i, 1)}/{self.item_total} 个")
        if self.files_done:
            bits.append(f"已完成 {len(self.files_done)} 个文件")
        self.lbl_detail.config(text="   ·   ".join(bits) if bits else "—")

    def _short(self, s, n=88):
        s = os.path.basename(s or "")
        return s if len(s) <= n else s[:n - 3] + "..."

    # ---------------- 命令构建 ----------------
    def build_cmd(self, urls=None, no_playlist=False, cfg=None, warn=None):
        """下载命令行：统一交给共用核心（凭据、附加参数过滤、"--" 分隔都在里面）。

        warn 传一个 list 进来，核心会往里塞「本可以做到但环境缺东西」的提示
        （比如勾了 aria2c 却没装）。
        """
        if cfg is None:
            cfg = self._collect_cfg()
        return core.build_download_cmd(
            cfg, urls if urls is not None else self.get_urls(),
            no_playlist=no_playlist, warn=warn)

    def copy_cmd(self):
        self._guard_extra_args()          # 危险参数只提示，复制出的命令已在核心里过滤
        cmd = self.build_cmd()
        show = command_text(cmd)
        self.root.clipboard_clear()
        self.root.clipboard_append(show)
        self.log_line(show + "\n", "cmd")
        self.set_status("命令已复制", "#0550ae")

    # ---------------- 运行 ----------------
    def start_download(self):
        urls = self.get_urls()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个下载链接。\n（也可以先点「探测资源」列出条目，再勾选下载）")
            return
        self._start(urls)

    def _start(self, urls, no_playlist=False):
        if self._task_blocked():
            return
        if not self._guard_extra_args():
            messagebox.showerror("错误", "「附加参数」或「aria2c 参数」里有被禁用的参数，已阻止下载。\n详见运行日志。")
            return
        outdir = self.var_outdir.get().strip()
        if not outdir:
            messagebox.showwarning("提示", "请先选择输出目录。")
            return
        urls = self._safe_urls(urls)
        if not urls:
            messagebox.showwarning("提示", "没有可用的下载链接（无法识别的行已被忽略，见日志）。")
            return
        if self.var_aria.get() and not self.has_aria2c:
            if not messagebox.askokcancel(
                    "未检测到 aria2c",
                    "程序目录和 PATH 里都没有 aria2c.exe。\n"
                    "这次会改用 yt-dlp 自带下载器继续（功能不缺，只是少了多连接分段）。\n\n"
                    "要继续吗？"):
                return
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("错误", f"无法创建输出目录：\n{e}")
            return

        warns = []
        cmd = self.build_cmd(urls, no_playlist=no_playlist, warn=warns)
        for w in warns:
            self.log_line("! " + w + "\n", "warn")
        self._save_config()
        self.dl_urls, self.dl_no_playlist, self.dl_outdir = list(urls), no_playlist, outdir
        self.net_tried, self.net_name = set(), "normal"
        self.user_stopped = False
        self.running = True
        if not self._reserve("download"):
            messagebox.showinfo("提示", "已有任务正在进行，请先点「停止」或等待结束。")
            return
        self.btn_start.config(state="disabled")
        self.btn_detect.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._reset_progress()
        self.set_status("下载中…", "#0550ae")
        self.log_line("> " + command_text(cmd) + "\n", "cmd")

        threading.Thread(target=self._run, args=(cmd, outdir), daemon=True).start()

    # ---------------- 任务登记：一次只跑一个 yt-dlp，「停止」才知道该杀谁 ----------------
    def _begin(self, proc, kind=None):
        """登记正在跑的子进程，让「停止」知道该杀谁；kind 同时占住任务闸门。"""
        self.proc = proc
        if kind:
            self.task_kind = kind

    def _end(self):
        # 只摘进程：任务闸门要等各自的完成回调（_on_done 等）再放行，
        # 否则排障每探完一轮就会出现能插进别的任务的空档。
        self.proc = None

    def _task_blocked(self):
        """开新任务前的统一闸门。"""
        if not self.task_kind:
            return False
        messagebox.showinfo("提示", "「%s」正在进行，请先点「停止」或等待结束。"
                            % TASK_LABELS.get(self.task_kind, "任务"))
        return True

    def _reserve(self, kind):
        """统一闸门：桌面版自己的任务与扩展服务共用一个槽位，不并行跑 yt-dlp。"""
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
        """当前任务的配置：扩展任务用服务带来的快照，自有任务读界面。"""
        return dict(self.ext_cfg) if self.ext_cfg is not None else self._collect_cfg()

    def submit_server_task(self, task):
        """扩展服务把下载交还桌面版的入口（HTTP 线程调用）。

        同步占住闸门立即返回；忙则返回 False，由服务端回 409。
        真正启动经队列回到 UI 线程（_pump），复用自有下载流水线。
        """
        if not self._reserve("download"):
            return False
        # 不在 HTTP 线程碰 Tcl：投进队列，由 _pump 在 UI 线程启动
        self.q.put(("exttask", task))
        return True

    def _start_external(self, task):
        cfg = dict(task.get("cfg") or {})
        urls = list(task.get("urls") or ([task["url"]] if task.get("url") else []))
        emit = task.get("emit")
        if not urls:
            self._release_runner()
            self.task_kind = None
            return
        outdir = cfg.get("outdir") or os.path.join(BASE_DIR, "downloads")
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
        self.net_tried, self.net_name = set(), "normal"
        self.user_stopped = False
        self.running = True
        self.task_kind = "download"
        self.btn_start.config(state="disabled")
        self.btn_detect.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._reset_progress()
        self.set_status("下载中…（浏览器扩展）", "#0550ae")
        warns = []
        cmd = self.build_cmd(urls, no_playlist=self.dl_no_playlist, cfg=cfg, warn=warns)
        for w in warns:
            self.log_line("! " + w + "\n", "warn")
        self.log_line("[浏览器扩展] 嗅探地址交给桌面版下载：%s\n" % self._short(urls[0]), "ok")
        self.log_line("> " + command_text(cmd) + "\n", "cmd")
        threading.Thread(target=self._run, args=(cmd, outdir), daemon=True).start()

    def _finish_task(self, code):
        """任务真正结束：放行共享槽位、把结果镜像回扩展、清掉临时 cookie。"""
        self._release_runner()
        emit, cookie = self.ext_emit, self.ext_cookie_file
        self.ext_emit = self.ext_cfg = self.ext_cookie_file = None
        if emit:
            emit({"type": "done", "code": code})
        if cookie and os.path.isfile(cookie):
            try:
                os.remove(cookie)
            except Exception:
                pass

    def _run(self, cmd, outdir):
        try:
            proc = spawn(cmd, outdir)
        except FileNotFoundError:
            self.q.put(("log", f"找不到 yt-dlp：{cmd[0]}\n", "err"))
            self.q.put(("done", -1))
            return
        except Exception as e:
            self.q.put(("log", f"启动失败：{e}\n", "err"))
            self.q.put(("done", -1))
            return
        self._begin(proc, "download")
        buf = []
        why = {}
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                          stall_seconds=GUI_STALL_SECONDS, why=why):
                buf.append(line)
                self.q.put(("line", line))
        except Exception as e:
            self.q.put(("log", f"读取输出出错：{e}\n", "err"))
        finally:
            if why.get("reason") == "stall":
                self.q.put(("log", "[长时间没有输出，已自动终止本次任务]\n", "warn"))
            self.diag_buf = "\n".join(buf[-300:])
            self.q.put(("done", proc.wait()))

    def stop(self):
        proc, kind = self.proc, self.task_kind
        if not proc or proc.poll() is not None:
            self.set_status("当前没有进行中的任务", "#57606a")
            return False
        self.user_stopped = True
        kill_tree(proc)
        self.set_status("已停止", "#cf222e")
        self.log_line("[已手动停止：%s]\n" % TASK_LABELS.get(kind, "任务"), "warn")
        return True

    def _pump(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "line":
                    self._handle_line(rest[0])
                elif kind == "log":
                    self.log_line(rest[0], rest[1] if len(rest) > 1 else None)
                elif kind == "detrow":
                    self._add_result(rest[0])
                elif kind == "detdone":
                    self._on_detect_done(rest[0])
                elif kind == "done":
                    self._on_done(rest[0])
                elif kind == "applyfix":
                    self._apply_probe_fix(rest[0])
                elif kind == "trodone":
                    self._on_trouble_done(rest[0], rest[1] if len(rest) > 1 else "trouble")
                elif kind == "preview":
                    self._on_preview(rest[0], rest[1] if len(rest) > 1 else "",
                                     rest[2] if len(rest) > 2 else None)
                elif kind == "exttask":
                    self._start_external(rest[0])
                elif kind == "sniffpv":
                    self._on_sniff_preview(rest[0])
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _handle_line(self, line):
        """进度解析交给共用核心（--progress-template 的机器可读行 + 旧正则兜底）。"""
        ev = progress_event(self.pstate, line)
        if ev:
            st = self.pstate
            self.pct = st["pct"]
            self.size = st["size"] or self.size
            self.speed = st["speed"]
            self.eta = st["eta"]
            self.cur_file = st["file"]
            self.item_i, self.item_total = st["item_i"], st["item_total"]
            self.files_done = {x for x in st["done"] if x}
            if self.ext_emit:
                self.ext_emit({"type": "progress", "pct": st["pct"], "speed": st["speed"],
                               "eta": st["eta"], "size": st["size"], "file": st["file"],
                               "item_i": st["item_i"], "item_total": st["item_total"]})
            if ev["kind"] == "file":
                self.lbl_file.config(text="正在下载：" + self._short(self.cur_file), foreground="#0550ae")
                self._refresh_progress()
                # 「Destination:」那行照旧显示原文；PROG| 机器行换成人类可读文本
                self.log_line((progress_text(st) if core.PROG_MARK in line else line) + "\n", "ok")
            elif ev["kind"] == "item":
                self._refresh_progress()
                self.log_line(line + "\n", None)
            else:
                self._refresh_progress()
                self._push_progress_log(progress_text(st))
            return

        tag = classify_line(line)
        if tag == "ok":
            stage = line[1:].split("]")[0]
            self.lbl_file.config(text=f"后处理：{stage}", foreground="#1a7f37")
        self.log_line(line + "\n", tag)

    def _push_progress_log(self, text):
        """日志里只保留最新一条进度行，避免刷屏。"""
        self.log.configure(state="normal")
        r = self.log.tag_ranges("prog")
        if r:
            self.log.delete(r[0], r[1])
        self.log.insert("end", text + "\n", "prog")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_done(self, code):
        base = self._task_cfg()
        was_ext = self.ext_cfg is not None
        self.running = False
        self.proc = None
        self.task_kind = None    # 自愈重试会由 _net_heal 重新占住闸门
        self.btn_start.config(state="normal")
        self.btn_detect.config(state="normal")
        self.btn_stop.config(state="disabled")
        elapsed = time.time() - self.t_start if self.t_start else 0
        mm, ss = divmod(int(elapsed), 60)

        if code == 0:
            self.pct = 100.0
            self.speed = self.eta = ""
            self.lbl_pct.config(text="100%")
            self.var_prog.set(100)
            tail = f"耗时 {mm:02d}:{ss:02d}"
            if self.files_done:
                tail = f"完成 {len(self.files_done)} 个文件 · " + tail
            self.lbl_detail.config(text=tail)
            self.lbl_file.config(text="全部完成", foreground="#1a7f37")
            self.set_status("完成", "#1a7f37")
            self.log_line(f"=== 完成 · {tail} ===\n", "ok")
            open_flag = base.get("open_after") if was_ext else self.var_open.get()
            self._finish_task(code)
            if open_flag:
                self.open_outdir(base.get("outdir"))
        else:
            if self._net_heal():
                return
            self.set_status(f"已结束（退出码 {code}）", "#cf222e")
            self.lbl_file.config(text="出错或中断 — 见日志中的原因与建议", foreground="#cf222e")
            self.log_line(f"=== 结束，退出码 {code} ===\n", "err")
            self._log_diag()
            self._finish_task(code)

    def _net_heal(self):
        """连接层失败后按「直连 → 浏览器指纹 → 分块传输」阶梯自动重试一次姿势。

        与网页版共用核心的 next_net_plan：403/404/站点不支持直接放弃，
        HTTP 400 先摘 Referer 再补浏览器头，其余连接错误优先换 TLS 指纹。
        """
        if self.user_stopped or not self.dl_urls:
            return False
        cfg = self._task_cfg()
        if not cfg.get("net_hard", True):
            return False
        tail = self.diag_buf or ""
        self.net_tried.add(self.net_name)
        # 浏览器 Cookies 数据库被独占锁住：先脱掉登录态重试一次（公开内容不需要它）
        nxt = next_cookie_plan(tail, self.net_tried, cfg)
        if not nxt:
            if not is_net_failure(tail):
                return False
            nxt = next_net_plan(tail, self.net_tried, cfg, url=self.dl_urls[0])
        if not nxt:
            return False
        self.net_name, extra = nxt
        self.log_line("[自动重试] 换用%s重试（已下载部分会续传）\n"
                      % (NET_PLAN_LABEL.get(self.net_name) or COOKIE_PLAN_LABEL), "warn")
        cmd = self.build_cmd(self.dl_urls, no_playlist=self.dl_no_playlist,
                             cfg=dict(cfg, **extra))
        self.log_line("> " + command_text(cmd) + "\n", "cmd")
        self.pstate = new_progress_state()
        self._reset_progress()
        self.t_start = time.time()
        self.running = True
        self.task_kind = "download"   # 先占住闸门，等工作线程把新进程登记上
        self.btn_start.config(state="disabled")
        self.btn_detect.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.set_status("下载中…（自动重试）", "#0550ae")
        threading.Thread(target=self._run, args=(cmd, self.dl_outdir), daemon=True).start()
        return True

    # ---------------- 自动排障 / 更新 ----------------
    def troubleshoot(self):
        """依次尝试多套访问配置，找出第一套能解析成功的，并自动应用到界面。"""
        if self._task_blocked():
            return
        urls = self.get_urls()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴要排查的链接。")
            return
        urls = self._safe_urls(urls)
        if not urls:
            messagebox.showwarning("提示", "没有可用的链接（无效的行已被忽略，见日志）。")
            return
        self.clear_log()
        self.log_line("=== 自动排障：依次尝试 4 套访问配置（每套最多 25 秒）===\n", "cmd")
        self.user_stopped = False
        if not self._reserve("trouble"):
            return
        self.btn_trouble.config(state="disabled")
        self.btn_detect.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.set_status("排障中…", "#0550ae")
        self.lbl_file.config(text="正在逐套尝试访问配置…", foreground="#0550ae")
        cfg = self._collect_cfg()   # Tk 变量只在 UI 线程读，避免工作线程碰 Tcl
        threading.Thread(target=self._trouble_run, args=(urls[0], cfg), daemon=True).start()

    def _trouble_run(self, url, cfg):
        collected, hit = [], None
        for key, label, fix in PROBE_VARIANTS:
            if self.user_stopped:          # 停止之后不再往下试
                self.q.put(("log", "[已取消，剩余方案未尝试]\n", "warn"))
                break
            label = TROUBLE_LABELS.get(key, label)
            cmd = build_probe_cmd(cfg, url, key)
            self.q.put(("log", "> " + command_text(cmd) + "\n", "cmd"))
            ok, msg, out = self._probe_once(cmd, timeout=25)
            self.q.put(("log", ("✓ " if ok else "✗ ") + label + "：" + msg + "\n",
                        "ok" if ok else "warn"))
            collected.append(out)
            if ok:
                hit = (label, fix, url)
                break
        if hit:
            self.q.put(("applyfix", hit))
        else:
            diag = diagnose_pairs("\n".join(collected))
            if diag:
                self.q.put(("log", "\n=== 可能的原因与建议 ===\n", "warn"))
                for title, advices in diag:
                    self.q.put(("log", "· " + title + "\n", "warn"))
                    for a in advices:
                        self.q.put(("log", "    - " + a + "\n", None))
            else:
                self.q.put(("log", "\n四套方案都失败了：请展开日志看 yt-dlp 原文，"
                                  "或先点「更新 yt-dlp」再试。\n", "warn"))
        self.q.put(("trodone", 1 if hit else 0, "trouble"))

    def _probe_once(self, cmd, timeout=25):
        """跑一条探测命令，拿到 JSON 即成功。

        超时交给 iter_lines_watch 兜底，不再另起读线程；子进程登记进 self.proc，
        这样排障途中点「停止」也能真的停下来，而不是等 25 秒。
        """
        try:
            proc = spawn(cmd, BASE_DIR)
        except Exception as e:
            return False, "启动失败：" + str(e), ""
        self._begin(proc)
        lines = []
        timed_out = []
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
            self._end()
        ok = got_json
        if ok:
            msg = "成功解析出条目"
        elif timed_out:
            msg = "超时 %ss（网络不通或站点响应极慢）" % timeout
        elif self.user_stopped:
            msg = "已取消"
        else:
            msg = lines[-1][:120] if lines else "无输出"
        return ok, msg, "\n".join(lines[-25:])

    def _apply_probe_fix(self, hit):
        label, fix, url = hit
        cfg = self._collect_cfg()
        touched, manual = core.apply_probe_fix(cfg, url, fix)
        self.var_ua.set(cfg.get("ua") or "")
        self.var_referer.set(cfg.get("referer") or "")
        self.var_browser.set(cfg.get("browser") or "（不使用）")
        self.var_generic.set(bool(cfg.get("generic")))
        self._save_config()
        parts = []
        if touched:
            parts.append("已自动应用：" + "、".join(touched))
        if manual:
            parts.append("需要你手动设置：" + "、".join(manual))
        msg = "；".join(parts) or "这个错误没有可自动应用的设置"
        self.log_line(f"=== 排障「{label}」：{msg} ===\n", "ok")

    def _on_trouble_done(self, ok, task="trouble"):
        self.task_kind = None
        self._release_runner()
        self._end()
        self.btn_trouble.config(state="normal")
        self.btn_detect.config(state="normal")
        self.btn_start.config(state="normal")
        self.btn_update.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.bar.stop()
        self.bar.config(mode="determinate")
        if task == "update":
            self.set_status("更新完成" if ok else "更新失败", "#1a7f37" if ok else "#cf222e")
            self.lbl_file.config(text="更新结束" if ok else "更新失败：多为网络/代理不通",
                                 foreground="#1a7f37" if ok else "#cf222e")
            return
        if ok:
            self.set_status("排障完成 · 已找到可用方案", "#1a7f37")
            self.lbl_file.config(text="已自动应用可用方案，点「探测资源」继续", foreground="#1a7f37")
        else:
            self.set_status("排障结束 · 四套方案都失败", "#cf222e")
            self.lbl_file.config(text="请看日志中的原因与建议", foreground="#cf222e")

    def update_ytdlp(self):
        """更新 yt-dlp 本体——站点改版后，多数「嗅探不到」靠更新就能解决。"""
        if self._task_blocked():
            return
        yt = self.cfg.get("yt_dlp_path") or find_tool("yt-dlp.exe")
        cmd = [yt, "-U"]
        proxy = self.var_proxy.get().strip()
        if proxy:
            cmd += ["--proxy", proxy]
        self.clear_log()
        self.log_line("> " + command_text(cmd) + "\n", "cmd")
        self.user_stopped = False
        if not self._reserve("update"):
            return
        self.btn_update.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.set_status("更新 yt-dlp…", "#0550ae")
        threading.Thread(target=self._update_run, args=(cmd,), daemon=True).start()

    def _update_run(self, cmd):
        try:
            proc = spawn(cmd, BASE_DIR)
        except Exception as e:
            self.q.put(("log", "启动失败：" + str(e) + "\n", "err"))
            self.q.put(("trodone", 0, "update"))
            return
        self._begin(proc)
        code = -1
        try:
            for line in iter_lines_watch(proc, abort=lambda: self.user_stopped,
                                          keep_blank=True):
                self.q.put(("log", line + "\n", None))
            code = proc.wait()
        except Exception as e:
            code = -1
            self.q.put(("log", "更新出错：" + str(e) + "\n", "err"))
        finally:
            self._end()
        self.q.put(("log", ("=== 更新完成，请重新探测 ===\n" if code == 0
                            else "=== 更新失败，通常是网络/代理不通 ===\n"),
                    "ok" if code == 0 else "err"))
        self.q.put(("trodone", 1 if code == 0 else 0, "update"))

    def detect(self):
        if self._task_blocked():
            return
        urls = self.get_urls()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴要探测的链接。")
            return
        urls = self._safe_urls(urls)
        if not urls:
            messagebox.showwarning("提示", "没有可用的链接（无效的行已被忽略，见日志）。")
            return

        # 命令构建交给共用核心；-i 保证单个链接失败不中断其余链接
        cmd = build_detect_cmd(self._collect_cfg(), urls)

        self.clear_results()
        self.detecting = True
        self.user_stopped = False
        if not self._reserve("detect"):
            return
        self.btn_detect.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.bar.config(mode="indeterminate")
        self.bar.start(12)
        self.set_status("探测中…", "#0550ae")
        self.lbl_file.config(text="正在解析链接信息…", foreground="#0550ae")
        self.log_line("> " + command_text(cmd) + "\n", "cmd")
        threading.Thread(target=self._detect_run, args=(cmd,), daemon=True).start()

    def _detect_run(self, cmd):
        try:
            proc = spawn(cmd, BASE_DIR)
        except Exception as e:
            self.q.put(("log", f"启动失败：{e}\n", "err"))
            self.q.put(("detdone", -1))
            return
        self._begin(proc, "detect")
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
                        self.q.put(("detrow", info_from_json_raw(json.loads(line))))
                        continue
                    except Exception:
                        pass
                buf.append(line)
                self.q.put(("line", line))
        except Exception as e:
            self.q.put(("log", f"读取输出出错：{e}\n", "err"))
        if why.get("reason") == "stall":
            self.q.put(("log", "[长时间没有输出，已自动停止探测]\n", "warn"))
        code = proc.wait()
        self.diag_buf = "\n".join(buf[-300:])
        self.q.put(("detdone", code))

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            i = int(sel[0])
        except ValueError:
            return
        if 0 <= i < len(self.results):
            self._show_preview(i)

    def _show_preview(self, i):
        info = self.results[i]
        self._preview_url = info.get("url") or ""
        self.btn_pv_open.config(state="normal" if self._preview_url else "disabled")
        self.pv_meta.config(text="%s\n时长 %s · 清晰度 %s · 大小 %s\n上传者 %s" % (
            info.get("title") or "", fmt_dur(info.get("dur")), info.get("res") or "-",
            fmt_size(info.get("size")), info.get("up") or "-"))
        url = info.get("thumb") or ""
        if not url:
            self._set_preview_image(None, "这个条目没有封面地址")
            return
        self._set_preview_image(None, "封面加载中…")
        cfg = self._collect_cfg()
        self._preview_gen += 1
        gen = self._preview_gen

        def work():
            # 取图与转码（ffmpeg 最坏 30s）都在这里做：旧实现把 subprocess.run 放在
            # Tk 主线程，一张慢封面就能把整个界面冻住半分钟。
            path, err = core.fetch_thumb(url, cfg)
            if not err and path:
                png = os.path.join(tempfile.gettempdir(),
                                   "ytdlp_preview_%d_%d.png" % (os.getpid(), gen))
                cmd = [find_tool("ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y",
                       "-i", path, "-vf", "scale=320:-1", "-frames:v", "1", png]
                try:
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=core.CREATE_NO_WINDOW, timeout=30)
                    path, err = png, None
                except Exception as e:
                    path, err = "", "封面格式无法显示：%s" % e
            self.q.put(("preview", gen, path, err))

        threading.Thread(target=work, daemon=True).start()

    def _set_preview_image(self, photo, text):
        self._photo = photo          # 持有引用，否则 Tkinter 会把图回收掉
        if photo is not None:
            self.pv_img.config(image=photo, text="")
        else:
            self.pv_img.config(image="", text=text or "")

    def _on_preview(self, gen, path, err):
        # 快速连点不同条目时，先发的请求可能后到：代次对不上就整条丢弃，
        # 否则用户看到的是上一条的封面。
        if gen != self._preview_gen:
            return
        if err or not path:
            self._set_preview_image(None, "封面取不到：%s" % (err or "未知原因"))
            return
        try:
            photo = tk.PhotoImage(file=path)
        except Exception as e:
            self._set_preview_image(None, "封面格式无法显示：%s" % e)
            return
        self._set_preview_image(photo, "")

    def _open_preview_url(self):
        if self._preview_url:
            webbrowser.open(self._preview_url)

    def _add_result(self, info):
        i = len(self.results)
        self.results.append(info)
        iid = str(i)
        self.checked.add(i)
        self.tree.insert("", "end", iid=iid, tags=("on",), values=(
            "☑", info["title"], fmt_dur(info["dur"]),
            info["res"], fmt_size(info["size"]), info["up"]))
        self.tree.see(iid)
        self._update_count()

    def _update_count(self):
        n, k = len(self.results), len(self.checked)
        self.lbl_count.config(text=f"共 {n} 条，已选 {k} 条" if n else "尚未探测")
        self.btn_dl_sel.config(state="normal" if n else "disabled")

    def _toggle_check(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        try:
            i = int(iid)
        except ValueError:
            return
        if i in self.checked:
            self.checked.discard(i)
        else:
            self.checked.add(i)
        on = i in self.checked
        self.tree.set(iid, "sel", "☑" if on else "☐")
        self.tree.item(iid, tags=("on",) if on else ())
        self._update_count()

    def _set_all(self, flag):
        self.checked = set(range(len(self.results))) if flag else set()
        for i in range(len(self.results)):
            self.tree.set(str(i), "sel", "☑" if flag else "☐")
            self.tree.item(str(i), tags=("on",) if flag else ())
        self._update_count()

    def download_selected(self):
        if not self.results:
            messagebox.showinfo("提示", "还没有探测结果，请先点「探测资源」。")
            return
        urls = [self.results[i]["url"] for i in sorted(self.checked) if self.results[i].get("url")]
        if not urls:
            messagebox.showwarning("提示", "请至少勾选一个条目（点“✓”列切换勾选状态）。")
            return
        self.log_line(f"[提示] 下载探测结果中已勾选的 {len(urls)} 个条目\n", "cmd")
        self._start(urls, no_playlist=True)

    def clear_results(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.results = []
        self.checked = set()
        self._update_count()

    def _on_detect_done(self, code):
        self.detecting = False
        self.proc = None
        self.task_kind = None
        self._release_runner()
        self.bar.stop()
        self.bar.config(mode="determinate")
        self.btn_detect.config(state="normal")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        n = len(self.results)
        if n:
            self.set_status(f"探测完成 · {n} 条", "#1a7f37")
            self.lbl_file.config(text=f"探测到 {n} 个条目 —— 勾选后点「下载选中项」", foreground="#1a7f37")
            self.log_line(f"=== 探测完成：{n} 条 ===\n", "ok")
        else:
            self.set_status("探测结束，没有拿到条目", "#cf222e")
            self.lbl_file.config(text="没探测到条目 — 见日志中的原因与建议", foreground="#cf222e")
            self.log_line(f"=== 探测结束，退出码 {code} ===\n", "err")
            self._log_diag()

    def _log_diag(self):
        """把上一次任务的失败原因与建议写进日志。"""
        diag = diagnose_pairs(getattr(self, "diag_buf", "") or "")
        if not diag:
            return
        self.log_line("\n=== 可能的原因与建议 ===\n", "warn")
        for title, advices in diag:
            self.log_line("· " + title + "\n", "warn")
            for a in advices:
                self.log_line("    - " + a + "\n", None)
        self.log_line("（也可以点「自动排障」，让程序逐套试出可用配置）\n", None)

    # ---------------- 格式列表 ----------------
    def fetch_formats(self):
        if self._task_blocked():
            return
        urls = self._safe_urls(self.get_urls())
        if not urls:
            messagebox.showwarning("提示", "请先填写一个链接，再查看可用格式。")
            return
        url = urls[0]
        # 占住任务闸门：格式表读取也是同一个 yt-dlp 的单任务，不能再和下载并行
        if not self._reserve("formats"):
            return
        self.user_stopped = False
        win = tk.Toplevel(self.root)
        win.title("可用格式")
        win.geometry("900x540")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)

        ttk.Label(win, text=url, font=FONT, foreground="#57606a").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        txt = tk.Text(win, wrap="none", font=MONO, relief="solid", bd=1, bg="#ffffff", fg="#1f2328")
        txt.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        sb = ttk.Scrollbar(win, command=txt.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=4)
        txt.config(yscrollcommand=sb.set)
        txt.tag_config("head", foreground="#0550ae")

        bar = ttk.Progressbar(win, mode="indeterminate")
        bar.grid(row=2, column=0, sticky="ew", padx=8, pady=2)
        bar.start(12)
        btns = ttk.Frame(win)
        btns.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 8))
        lbl = ttk.Label(btns, text="正在读取…", font=FONT, foreground="#57606a")
        lbl.pack(side="left")

        def use(value):
            self.var_mode.set(MODE_NAME["custom"])
            self._sync_mode()
            self.var_fmt.set(value)
            win.destroy()
            self.set_status(f"已选择格式：{value}", "#1a7f37")

        def on_pick(evt=None):
            try:
                idx = txt.index(f"@{evt.x},{evt.y}")
                line = txt.get(f"{idx} linestart", f"{idx} lineend")
            except Exception:
                return
            m = re.match(r"^\s*([A-Za-z0-9_\-]+)\s+(\w+)\s+", line)
            if not m:
                return
            fid = m.group(1)
            if messagebox.askyesno("选择格式", f"使用格式代码 “{fid}”？\n\n如需包含音频，建议填成 “视频ID+音频ID”。"):
                use(fid)

        txt.bind("<Double-1>", on_pick)
        ttk.Button(btns, text="最佳画质", width=10, command=lambda: use("bv*+ba/b")).pack(side="right", padx=4)
        ttk.Button(btns, text="最佳音频", width=10, command=lambda: use("ba")).pack(side="right", padx=4)
        ttk.Button(btns, text="关闭", width=8, command=win.destroy).pack(side="right", padx=(4, 0))
        ttk.Label(btns, text="（双击某一行可选中该格式）", foreground="#8c959f", font=FONT_S).pack(side="right", padx=8)

        # 以前只带 proxy/cookie：加了防盗链的站点会「下载得了却看不到格式表」
        cmd = build_formats_cmd(self._collect_cfg(), url)

        def work():
            try:
                p = spawn(cmd, BASE_DIR)
                self._begin(p)
                out = list(iter_lines_watch(p, abort=lambda: self.user_stopped,
                                            keep_blank=True, stall_seconds=120))
                code = p.wait()
            except Exception as e:
                out, code = [str(e)], -1
            # 无论成败都要放行闸门：窗口可能已经被关掉，finish 未必还会跑到
            self._end()
            if self.task_kind == "formats":
                self.task_kind = None
                self._release_runner()
            try:
                win.after(0, finish, out, code)
            except Exception:
                pass

        def finish(out, code):
            bar.stop()
            bar.grid_remove()
            txt.configure(state="normal")
            for ln in out:
                txt.insert("end", ln + "\n", "head" if ln.strip().startswith("ID") else None)
            txt.configure(state="disabled")
            lbl.config(text=f"共 {len(out)} 行" + ("（读取失败）" if code != 0 else ""))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 内嵌扩展服务 ----------------
    # ---------------- 扩展嗅探页 ----------------
    def _build_sniff_page(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        ttk.Label(bar, text="浏览器扩展嗅探到的媒体地址（开着扩展的页面会自动出现在这里）",
                  font=FONT_S, foreground="#8c959f").pack(side="left")
        self.sn_fold = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="折叠分片", variable=self.sn_fold,
                        command=self._sniff_refresh).pack(side="right")
        ttk.Button(bar, text="清空列表", command=self._sniff_clear).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="刷新", command=self._sniff_refresh).pack(side="right", padx=(0, 6))
        cols = ("title", "url", "kind", "size", "source", "page", "ts")
        self.sn_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                    selectmode="extended", height=12)
        for key, text, width in (("title", "标题", 180), ("url", "地址", 380),
                                 ("kind", "类型", 60), ("size", "大小", 80),
                                 ("source", "来源", 90), ("page", "页面", 200),
                                 ("ts", "时间", 70)):
            self.sn_tree.heading(key, text=text)
            self.sn_tree.column(key, width=width, anchor="w")
        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.sn_tree.yview)
        self.sn_tree.configure(yscrollcommand=vsb.set)
        self.sn_tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=4)
        vsb.grid(row=1, column=1, sticky="ns", pady=4)
        self.sn_tree.tag_configure("seg", foreground="#8c959f")
        bf = ttk.Frame(parent)
        bf.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 2))
        ttk.Button(bf, text="下载选中", command=self._sniff_download).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="预览选中", command=self._sniff_preview).pack(side="left", padx=(0, 6))
        self.sn_lbl = ttk.Label(bf, text="", font=FONT_S, foreground="#8c959f")
        self.sn_lbl.pack(side="right")
        pv = ttk.LabelFrame(parent, text=" 预览（缩略图 / 时长 / 短片段） ")
        pv.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10))
        self.sn_img = ttk.Label(pv)
        self.sn_img.pack(side="left", padx=8, pady=6)
        right = ttk.Frame(pv)
        right.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        self.sn_pv_lbl = ttk.Label(right, text="选中一条后点「预览选中」", font=FONT_S,
                                   foreground="#8c959f", wraplength=520, justify="left")
        self.sn_pv_lbl.pack(anchor="w")
        self.sn_play = ttk.Button(right, text="播放片段", command=self._sniff_play_clip,
                                  state="disabled")
        self.sn_play.pack(anchor="w", pady=(6, 0))
        self.root.after(3000, self._sniff_tick)

    def _sniff_tick(self):
        # 只在本页可见时刷新：别在其它页面上打扰用户的选择状态
        try:
            if self.nb.index(self.nb.select()) == 1:
                self._sniff_refresh()
        except Exception:
            pass
        self.root.after(3000, self._sniff_tick)

    def _sniff_refresh(self):
        mod = self._web_mod
        if mod is None:
            self.sn_lbl.config(text="扩展服务未启用（「高级」里勾选启用浏览器扩展支持）")
            return
        items = (mod.sniff_items_json() or {}).get("items") or []
        fold = bool(self.sn_fold.get())
        shown = [r for r in items if not (fold and r.get("seg"))]
        sig = (fold, tuple((r.get("url"), r.get("ts"), r.get("size")) for r in shown))
        self.sn_lbl.config(text="共 %d 条（折叠分片：%s）" % (len(shown), "开" if fold else "关"))
        if sig == self._sniff_sig:
            return
        self._sniff_sig = sig
        self._sniff_items = shown
        self.sn_tree.delete(*self.sn_tree.get_children())
        for i, r in enumerate(shown):
            size = r.get("size") or 0
            self.sn_tree.insert("", "end", iid=str(i), values=(
                r.get("title") or "", r.get("url") or "", r.get("kind") or "",
                fmt_size(size) if size else "", r.get("source") or "",
                r.get("page_url") or "",
                time.strftime("%H:%M:%S", time.localtime(r.get("ts") or 0))),
                tags=("seg",) if r.get("seg") else ())

    def _sniff_clear(self):
        if self._web_mod is not None:
            self._web_mod.clear_sniff_items()
        self._sniff_sig = None
        self._sniff_refresh()

    def _sniff_selected(self):
        sel = self.sn_tree.selection()
        return [self._sniff_items[int(i)] for i in sel
                if i.isdigit() and int(i) < len(self._sniff_items)]

    def _sniff_download(self):
        items = self._sniff_selected()
        if not items:
            messagebox.showinfo("扩展嗅探", "先在列表里选中要下载的条目")
            return
        cfg = dict(self.cfg)
        # 直链媒体没有封面/元数据：强行 embed 只会让 ffmpeg 报错、把成功判成失败
        cfg.update({"fmt": "b", "thumb": False, "meta": False,
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
            messagebox.showwarning("扩展嗅探", "已有任务正在进行（%s），请先停止或等待结束"
                                   % TASK_LABELS.get(self.task_kind, self.task_kind or ""))
            return
        self.nb.select(self.dl_page)

    def _sniff_preview(self):
        items = self._sniff_selected()
        if len(items) != 1:
            messagebox.showinfo("扩展嗅探", "预览一次只选一条")
            return
        url = items[0]["url"]
        self.sn_pv_lbl.config(text="正在抓预览…（%s）" % url[:90])
        self.sn_play.config(state="disabled")
        self._sniff_clip = ""
        mod = self._web_mod

        def work():
            try:
                res = mod.build_sniff_preview(url)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self.q.put(("sniffpv", res))
        threading.Thread(target=work, daemon=True).start()

    def _on_sniff_preview(self, res):
        if not res.get("ok"):
            self.sn_pv_lbl.config(text="预览失败：%s" % (res.get("error") or "未知原因"))
            return
        dur = res.get("duration")
        txt = "时长 %s" % (fmt_dur(dur) if dur else "未知")
        mod = self._web_mod
        if res.get("thumb"):
            path, _ = mod.preview_token_lookup(res["thumb"], "thumb")
            if path:
                try:
                    img = tk.PhotoImage(file=path)
                    self.sn_img.config(image=img)
                    self.sn_img.image = img      # 留住引用，否则 Tk 会把图回收
                except Exception:
                    pass
        if res.get("clip"):
            path, _ = mod.preview_token_lookup(res["clip"], "clip")
            self._sniff_clip = path or ""
        self.sn_play.config(state="normal" if self._sniff_clip else "disabled")
        self.sn_pv_lbl.config(text=txt + ("　片段已就绪，可播放" if self._sniff_clip
                                          else "　（此源没有可抓的片段）"))

    def _sniff_play_clip(self):
        if not self._sniff_clip:
            return
        try:
            os.startfile(self._sniff_clip)
        except Exception as e:
            messagebox.showwarning("扩展嗅探", "打开播放器失败：%s" % e)

    def _set_svc_label(self, text, ok=True):
        if not hasattr(self, "lbl_svc"):
            return
        self.lbl_svc.config(text=text, foreground=("#1a7f37" if ok else "#b35900"))

    def _start_ext_service(self):
        """在后台线程里拉起 HTTP 服务；失败只提示，不影响桌面版下载。"""
        if self.web_svc is not None:
            return
        try:
            mod = load_web_server()
            if mod is None:
                self._set_svc_label("扩展服务：缺少 YtDlpWeb/server.py", ok=False)
                return
            # 两边共用桌面版这份配置：扩展触发的下载和界面上的设置才是一回事
            mod.set_config_file(CONFIG_FILE)
            self._web_mod = mod
            self.web_svc = mod.start_service()
            mod.set_task_sink(self.submit_server_task, stop=self.stop)
        except Exception as e:
            self.web_svc = None
            self._web_mod = None
            self._set_svc_label("扩展服务启动失败", ok=False)
            self.log_line("[扩展服务] 启动失败：%s\n" % e, "warn")
            return
        self._set_svc_label("扩展服务：%s" % self.web_svc.url.replace("http://", ""), ok=True)
        self.log_line("[扩展服务] 已监听 %s（浏览器扩展可直连）\n" % self.web_svc.url, "info")

    def _stop_ext_service(self):
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
        self._set_svc_label("扩展服务：已停止", ok=False)

    def _toggle_ext_service(self):
        want = bool(self.var_extsvc.get())
        if want:
            self._start_ext_service()
        else:
            self._stop_ext_service()
        self._save_config()

    def on_close(self):
        if self.task_kind and not messagebox.askokcancel(
                "退出", "「%s」仍在进行，确定退出吗？" % TASK_LABELS.get(self.task_kind, "任务")):
            return
        self._save_config()
        try:
            self.stop()
        except Exception:
            pass
        self._stop_ext_service()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass
    app = App(root, ext_service=("--selftest" not in sys.argv
                                 and "--selftest-snifftab" not in sys.argv))
    if "--selftest-snifftab" in sys.argv:
        mod = load_web_server()
        app._web_mod = mod
        mod.register_sniff_items([
            {"url": "https://cdn.example.com/v/master.m3u8", "kind": "hls",
             "source": "网络请求", "title": "示例清单"},
            {"url": "https://cdn.example.com/v/seg-1.ts", "kind": "ts", "source": "网络请求"},
            {"url": "https://cdn.example.com/v/clip.mp4", "kind": "mp4", "size": 12345,
             "source": "页面请求"},
        ], page_url="https://example.com/watch")
        app._sniff_refresh()
        folded = len(app.sn_tree.get_children())
        app.sn_fold.set(False)
        app._sniff_refresh()
        total = len(app.sn_tree.get_children())
        assert folded == 2 and total == 3, (folded, total)
        # 下载页可滚性：小窗口里滚到底必须能看见完整日志（以前直接被裁掉）
        app.nb.select(app.dl_page)
        root.update_idletasks()
        root.update()
        app.dl_canvas.yview_moveto(1.0)
        root.update_idletasks()
        root.update()
        log_h = app.log.winfo_height()
        log_bottom = app.log.winfo_rooty() + log_h
        assert log_h > 80 and log_bottom <= root.winfo_rooty() + root.winfo_height() + 2, \
            (log_h, log_bottom)
        print(json.dumps({"ok": True, "folded": folded, "all": total,
                          "scroll_ok": True}, ensure_ascii=False))
        sys.stdout.flush()
        root.destroy()
        return
    if "--selftest" in sys.argv:
        print(json.dumps(app.build_cmd(["https://www.youtube.com/watch?v=TEST"]), ensure_ascii=False, indent=2))
        sys.stdout.flush()          # 退出前必须 flush，否则看不到输出（项目备忘）
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
