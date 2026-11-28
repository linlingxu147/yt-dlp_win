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
WebView2 的 COM 对象不能跨线程碰：`CookieManager`、`CoreWebView2Cookie` 的字段只在 UI 线程访问
（后台线程直读会抛 `E_NOINTERFACE`，`cookies_from_webview2` 里的逐条 `except` 会把整罐悄悄丢空，
偶发还把界面卡死）。导出 cookie 用 `_begin_ui`（`BeginInvoke` 投递、不等待）分两段做：worker 只轮询
`box` 里的 .NET 属性，字段转换回 UI 线程完成；`cookies_from_webview2` / `_cookie_expires` 只许在
UI 线程调用（`Expires` 在部分 SDK 上是 `DateTime`，`_cookie_expires` 兼容两种）。
页面级下载失败后由 `Host._page_fallback` 兜一次（`PAGE_FALLBACK_SOURCES = embedded-page / gui / ext`，
直链任务 `sniff` / `embedded-link` 不兜）：内嵌浏览器「下载本页」可以用本页、找不到就用同站点
嗅探到的整段媒体再下一次（抖音把视频放在信息流弹层里，条目登记的页面是站点首页），桌面粘贴的
链接与扩展提交的页面任务只认**同一个页面**的条目；候选由 `_page_media_items` 选——必须是整段、
**必须有已知体积**，再取体积最大的，只兜一次。
页面直接触发的下载（`DownloadStarting`）浏览器那份一律 `args.Cancel = True`，但地址不再丢掉：
只认 `DownloadOperation.Uri` 且必须是 `http(s)`，交给 `Host.download_embedded_link`
（`_link_sink`，spec `source="embedded-link"`）走 `sniff_download` 的同一条直链通道，历史里显示
「页面下载」；不要改成让 WebView2 自己落盘或弹 `SaveFileDialog`（会和队列重复管理同一份文件）。
扩展 / HTTP API 送来的 `kind=page` 任务自己没带 `cookie` / `cookie_header` 时，`Host._ext_spec`
经 `_webview_cookie_file` 借当前内嵌浏览器的 cookie 罐（`browser` 置 `"（不使用）"` 跳过
`--cookies-from-browser`）——调用方不必先在内嵌浏览器里手动点一次「下载本页」。
右上角下载入口（`_QUEUE_UI_JS`）与蓝色上浮提示同样只负责展示：小条 + 展开的列表数据来自
`Host.queue_summary`（`_queue_loop` 每秒取一次，签名没变就不发；`tasks` 最多 8 条、新的在上，
只有 `id/label/state/pct/speed/eta/size/file` 这些显示字段），面板里「打开下载器」只回一条
`queue_open` → `Host.open_embedded_queue`（`return_to_app()` + `open_tab` 事件切到「下载」页签）；
行内按钮只回一条 `queue_action`（`act` 白名单 `cancel/pause/resume/remove` + 任务编号），
`EmbeddedBrowser._queue_action_from_ui` 只做粗筛，真正的合法性由 `Host.embedded_queue_action`
判定——任务必须在队列里、状态得允许这个动作，取消 / 移除与桌面端同义（取消清部分文件，
移除只清列表）。页面从头到尾拿不到任何下载参数；成功加入队列的三条路径（浮条单条、整页下载、
页面触发下载）在成功分支各补一条 `toast`。
页面地址入队前一律经 `core.normalize_site_url` 归一（抖音精选 `/jingxuan?modal_id=<id>` ⇄
`/video/<id>`；规则在 `ytdlp_core` 里，别改行为），地址栏粘贴、扩展 / HTTP API、工具栏与浮条
「下载本页」四条入口都不能漏；`Host._ext_spec` 也补一次，兜住 MCP / 旧扩展。`same_page` 与
`_page_media_items` 认这两种写法是同一个页面——弹窗页里登记的整段媒体要算「本页」，不能退化成「同站点」。

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