# 项目约定

## 浏览器支持政策（桌面 Web 前端）

运行时仅 WebView2（Evergreen Edge）。Baseline Newly Available 特性（`light-dark()`、
`<dialog>`、`color-scheme` 等）直接使用，不做 fallback；Limited availability 特性
（`focusgroup`、`closedby` 等）回避或按渐进增强处理。前端零构建、零框架依赖（原生 ES 模块）。

## 不可改动

`ytdlp_core.py` 的行为不在 UI 重写范围内改动；`sniffer_extension/` 与 `YtDlpWeb/server.py`
只在「内嵌浏览器 / 共享嗅探规则 / 扩展提交凭据」范围内改动：嗅探判定以
`sniffer_extension/sniff_rules.js` 为唯一真源（桌面 `sniff_rules.py` 读同一份规则表），
服务端只加字段并保持向后兼容；扩展的直链与「下载本页」都携带当前页 cookies。
扩展集成经 `TASK_SINK`/`RUNNER` 单任务闸门，桌面与扩展共用一个任务槽。

## 内嵌浏览器嗅探

桌面内嵌浏览器复用 `sniffer_extension/` 的 `sniff_rules.js`、`dom_scan.js`、`injected.js`。
WebView2 没有隔离世界注入，页面世界发来的 DOM / 正文钩子消息一律不可信：Python 侧只认
`EmbeddedBrowser._net_seen`（`WebResourceResponseReceived`）实锤过的 URL。下载凭据用
`CoreWebView2CookieManager` 导出临时罐（`_sniff_cookie_file`）并按任务清理；内嵌 profile
是私有临时目录，与 `--cookies-from-browser` 读的不是同一份 cookie。
嗅探登记/预览/凭据罐走 `YtDlpWeb/server.py` 的模块级函数（`Host.load_web_server` 缓存同一模块对象），
不启用 8765 服务也能工作；扩展服务启用后与扩展上报共用同一份登记表。
站点请求新窗口（`NewWindowRequested`）在当前视图内就地导航，空白窗忽略；系统浏览器只由
地址栏右键菜单显式打开，不要再把普通新窗口请求交给 `webbrowser.open`。
页面内浮条（`_SNIFF_UI_JS`）只负责展示；`dl` / `ui_ready` 消息回到 `_on_web_message` 后，
下载 URL 必须命中 `_ui_items`（本轮顶层页已登记条目）才提交，页面世界传来的字段不能直接
当下载参数用。`dlpage` 走 `Host.download_embedded_page`，只认 Python 侧 `_page_url`，
强制 `bv*+ba/b` 让 DASH 站点合并音视频；工具栏「下载本页」（`download_page()`）走同一条
通道，地址取 WebView2 当前 `Source`（`_current_url()`，站点 `pushState` 换页也拿得准），
取不到回退 `_page_url`，两条入口共用 `_submit_page_download`。

## 运行目录

便携运行文件统一放在 `runtime\`（`yt-dlp.exe`、`_internal\`、`ffmpeg.exe`、`ffprobe.exe`、
`downloads\`），源码目录不放二进制；`ytdlp_core.find_tool` / `which_tool` 按
`runtime/` → 项目根（旧布局兼容）→ PATH 查找。`runtime/` 内容不入库，只保留说明文件。

可移植性约定：配置里落在程序目录内的路径一律以 `@{APP}/…` 标记落盘
（`ytdlp_core.portable_encode` / `portable_decode`，键清单见 `PATH_KEYS`），加载时还原成当前目录，
新增「路径语义」的配置键要同步加进 `PATH_KEYS`；换盘/换机器后失效的路径只提示、不擅自改
（`yt_dlp_path` 例外，缺文件时回退到 `runtime/` 或 PATH）。`启动.bat` 探测解释器顺序为
便携 venv → `py` 启动器 → PATH（试用一次再采纳）→ python.org 常见安装目录，别再写死任何个人机器路径。

## 测试

- `py -3 -m unittest discover -s tests -v`（标准库 unittest，无第三方测试依赖）
- `node tests/extension_smoke.js`（扩展冒烟 9/9）
- 测试端口避开 8765–8775（如 `--port 8799`），避免抢占正在使用的实例。

## 提交

提交信息用中文，风格沿用仓库历史；未经用户明确要求不提交、不推送。