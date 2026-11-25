# 设计：桌面端 Web 重写（一期）

日期：2026-11-25
状态：设计已与用户逐节确认，待 spec 审阅
范围：`YtDlpGUI/`（main.py / host.py / api.py / ui/ / 启动.bat）、`tests/test_host.py`、`tests/test_api.py`、`README.md`
**不动**：`ytdlp_core.py`、`YtDlpWeb/server.py`、`sniffer_extension/`（一行不改）

---

## 1. 目标与已拍板的决策

把桌面端从 Tkinter 重写为 Web 前端，**程序形态不变**（仍是桌面窗口，不是浏览器网页版）。

| 决策点 | 结论（2026-11-25 用户确认） |
| --- | --- |
| 重写动机 | 四项全选：界面更好看、为后续功能铺路、解决 Tkinter 痛点（高 DPI/滚动/布局）、代码结构更清晰 |
| 承载方式 | pywebview 原生窗口 + WebView2（Evergreen Edge 内核）；接受引入第三方依赖（项目从此不再零依赖） |
| 架构方案 | 方案 A：本地 UI 文件 + js_api 桥；原生 ES 模块，零构建、零前端框架 |
| 功能范围 | 全量对齐现有功能 + 下载历史 + 主题切换（深浅色） |
| 多任务队列 | **二期独立项目**，不在本 spec 内（会打破 RUNNER 单槽位/扩展单任务镜像/PLAY_STATE 单份等单任务假设） |
| 旧 Tkinter 去留 | 新版验收通过后退役：先移 legacy/ 保留一个版本周期，用户确认稳定后再单独删除 |
| 否决项 | 保持零依赖（浏览器标签页形态）、PyInstaller 打包、Vue/React + 构建链、扩展 HTTP 服务 + SSE（方案 B，与网页版退役决策相反） |

## 2. 已验证的事实

- 现桌面版 `YtDlpGUI/main.py`（2011 行）：两页签（下载/扩展嗅探），8 个设置分区 + 进度 + 探测结果表（勾选下载、封面预览）+ 日志；流水线含自愈重试阶梯、自动排障、更新 yt-dlp、格式表、边下边播判定。UI 与逻辑纠缠在同一个 App 类。
- 扩展服务 `YtDlpWeb/server.py`：纯标准库 HTTP，`TASK_SINK`/`STOP_HOOK` 钩子把扩展任务交回桌面流水线；`RUNNER` 单任务闸门桌面与服务共用；`PLAY_STATE`/`SNIFF_STATE` 均为单任务模型。
- 共享核心 `ytdlp_core.py`：命令构建、进度解析、自愈阶梯、配置读写、封面代取、可播性探测、硬链接播放全在里面。**重写只换 UI 层，核心不动。**
- 项目当前零第三方依赖，靠系统 Python（3.13.5 实测）+ 启动.bat 运行；pywebview 未安装。
- 预浏览 + 边下边播已在 1.9.0 实现（见 `2026-11-25-preview-and-play-while-downloading-design.md`），本次全量保留其行为。
- Chromium 内核下 `file://` 拦截 ES 模块导入 → 前端须经 HTTP 加载（pywebview 内置 http_server）。
- 现有回归基线：`tests/test_core.py`、`test_hardening.py`、`test_server.py`、`extension_smoke.js`（5/5）、`main.py --selftest`。

## 3. 架构与代码分层

```
YtDlpGUI/
  main.py        薄入口：依赖检查 → 建 pywebview 窗口 → 装配 host/api → 内嵌扩展服务
  host.py        宿主逻辑：任务流水线、配置读写、历史记录、事件队列 + 泵
  api.py         js_api 薄壳：JS 可调的方法面，参数校验后转调 host
  ui/            前端（原生 ES 模块，零构建）
    index.html
    css/app.css  设计令牌 + light-dark() 双主题
    js/          app / bridge / download / sniff / history / theme / results / log 各模块
  legacy/        验收后旧 Tkinter main.py 移入此处（main_tk.py）
  启动.bat       首次自动 pip install pywebview；WebView2 缺失时给出安装指引
```

关键接线：

- **事件泵**：保留「工作线程 → queue → 泵」结构（现 `_pump` 的移植）。泵每 100ms 排空队列，另一端从 Tk 控件换成 `window.evaluate_js("window.__dispatch(<event>)")`。泵在 pywebview `loaded` 事件后才开推，之前事件入队不丢。
- **扩展集成不变**：`load_web_server()` → `set_config_file(桌面 config.json)` → `set_task_sink(host.submit_server_task, stop=host.stop)`，共用 `RUNNER` 单任务闸门。扩展任务进宿主流水线、`ext_emit` 镜像回服务端、`PLAY_STATE` 同步，行为与 1.9.0 完全一致。
- **前端加载**：`webview.start(http_server=True)` 由 pywebview 内置服务托管 `ui/`（本机随机端口），与扩展服务（8765-8775）互不相干。实现时第一步实测验证 ES 模块加载。
- **配置兼容**：沿用同一份 `config.json` 与全部现有键，旧配置无缝迁移。**不新增任何 config 键**——主题存 localStorage（第 6 节），历史上限为固定常量（第 7 节）。
- **窗口**：默认 1100x900，最小 920x700；标题沿用「yt-dlp 下载器 · 可视化界面」。
- **可测性**：host 的事件出口是注入的回调（默认接 evaluate_js，测试接收集器），不依赖 pywebview 即可单测。

## 4. js_api 接口面与事件协议

### 4.1 JS → Python（api.py）

所有方法返回可 JSON 序列化结果；忙/失败统一 `{ok:false, error, busy?}`（语义对齐现有 409），前端弹 toast。内部异常一律包成 `{ok:false, error}`，绝不抛进 JS。

| 分组 | 方法 | 说明 |
| --- | --- | --- |
| 配置/环境 | `get_config()` | cfg + 环境（aria2c 有无、yt-dlp 路径、扩展服务地址） |
| | `save_config(cfg)` | 核心原子写，沿用现有键 |
| 任务 | `start_download(urls, cfg, no_playlist)` | 宿主校验（sanitize_urls / unsafe_arg_in / outdir）后走现有流水线；启动前存配置 |
| | `detect(urls, cfg)` | 探测，行经 `detrow` 事件回传 |
| | `stop()` | 杀当前任务进程树（现有 kill_tree） |
| | `troubleshoot(urls, cfg)` | 四变体排障 |
| | `update_ytdlp(proxy)` | yt-dlp -U |
| | `fetch_formats(url, cfg)` | 占闸门；结果经 `formats` 事件回传 |
| | `copy_cmd(urls, cfg)` | 返回命令文本；JS 写剪贴板（navigator.clipboard 失败回退 execCommand） |
| 预览/播放 | `get_thumb(url, cfg)` | 核心 fetch_thumb 取图 → 读字节返回 base64 data URL。**不再经 ffmpeg 转 PNG**（浏览器直接解码；解码失败显示占位+原因） |
| | `play_now()` | 硬链接 + os.startfile；路径由宿主掌握，不接受客户端传路径 |
| 扩展嗅探 | `sniff_items()` / `sniff_clear()` | 读/清服务嗅探列表 |
| | `sniff_download(items)` | 走 submit_server_task 进流水线（cfg 覆盖规则与现有 _sniff_download 一致：fmt=b、play_while=True、关 embed 类） |
| | `sniff_preview(url)` | 调服务 build_sniff_preview → token 换本地路径 → 封面读字节返回 data URL；时长一并返回；片段路径宿主侧持有 |
| | `sniff_play_clip()` | os.startfile 宿主保存的片段路径；不接受客户端路径 |
| 历史 | `history_list()` / `history_clear()` / `history_redownload(i)` | 重下用记录的 URL + 当前配置 |
| 系统 | `browse_outdir()` / `browse_cookie()` | pywebview 原生文件夹/文件对话框（cookie 限 *.txt） |
| | `open_outdir()` / `open_url(url)` | os.startfile / webbrowser.open；open_url 只认 http(s) |
| | `set_ext_service(on)` | 启停内嵌扩展服务（现有 _start/_stop_ext_service 逻辑） |
| | `exit()` | 收尾：存配置、停任务、清 .preview 链接、销毁窗口 |

### 4.2 Python → JS（事件协议）

统一经 `window.__dispatch({type, ...})` 单入口分发，前端按 type 路由到各模块。

| 事件 | 载荷 | 节流 |
| --- | --- | --- |
| `logbatch` | `[{text, tag}]`，tag ∈ err/warn/ok/cmd/prog/info | 每泵 tick 批量推 |
| `progress` | pct/speed/eta/size/file/item_i/item_total/done_count | 合并取最新，≤5 次/秒 |
| `status` | `{text, tone}`（就绪/下载中/完成/出错） | 逐条 |
| `task` | `{kind, busy}`（驱动按钮禁用） | 逐条 |
| `detrow` / `detdone` | 探测行（含 thumb URL）/ `{code, count}` | 逐条 |
| `done` | code/elapsed/done_count/diag；宿主同时写历史 | 逐条 |
| `playable` | `{ok, reason}` | 逐条 |
| `preview` | `{gen, data_url, err}`（gen 代次守卫，防快速换行串图） | 逐条 |
| `formats` | `{lines, code}` | 逐条 |
| `trodone` | `{ok, task}`（task=trouble/update） | 逐条 |
| `applyfix` | 自动应用的配置项 + 需手动项（前端更新表单并存配置） | 逐条 |
| `svc` | `{text, ok}` 扩展服务状态标签 | 逐条 |
| `exit_confirm` | 忙时关窗触发，前端弹确认 dialog | 逐条 |

嗅探列表不推事件：页签可见时 JS 每 3 秒调 `sniff_items()` 轮询（沿用现有 `_sniff_tick` 节奏）。

### 4.3 安全边界

js_api 只被本机加载的自有 UI 触达，但输入仍全量校验：URL 过滤（sanitize_urls）、危险参数拦截（unsafe_arg_in）、打开路径走 path_inside、`play_now`/`sniff_play_clip` 不接受客户端路径、`open_url` 只认 http(s)。扩展服务的 Host/Origin 守卫、单任务闸门原样保留。

## 5. 前端 UI 结构

**布局**：顶栏（标题 + 环境/扩展服务状态）→ 三页签 **下载 / 扩展嗅探 / 下载历史** → 面板。下载面板沿用现有信息架构：8 个设置分组（双列 grid，窄窗口折单列）、按钮行（开始下载/探测资源/停止/自动排障/更新 yt-dlp/复制命令/清空日志）、进度区（进度条 + 边下边播按钮 + 原因文案）、探测结果表 + 预览卡、运行日志。整页自然滚动；高 DPI 由 WebView2 原生处理。

**页签**（modern-web-guidance `tabs`）：
- 初始状态渲染在 HTML：选中页 `aria-selected="true"`，其余面板 `hidden`，不等 JS。
- 键盘用经典 ARIA roving tabindex（约 30 行手写）。`focusgroup` 属 Limited availability（Chrome 150+），用户 WebView2 版本未知，不引 polyfill；选择跟随焦点。
- 选中态样式从 `aria-selected` 取；指示器用伪元素 border 下划线——不用纯颜色（WCAG 1.4.1）、不用 box-shadow（强制颜色模式会丢）。面板本身不放 padding/border。
- 隐藏面板用 `hidden`；`hidden="until-found"` 仅作渐进增强（不支持时自动退化为布尔 hidden）。

**结果表**（`responsive-table`）：语义 `<table>` + `<thead>/<th>`，表头 `position: sticky`；wrapper `tabindex="0" role="region" aria-labelledby`。勾选列用真 `<input type="checkbox">`；行点击（checkbox 外）= 切换勾选 + 选中预览，与现有桌面行为一致；全选/全不选/下载选中项/清空列表按钮保留。标题/上传者列单行省略 + `title` 兜底全文。

**对话框**（`dialog`）：格式表、aria2c 缺失确认、忙时退出确认、清空历史确认一律原生 `<dialog>` + `showModal()`。不加 `role="dialog"`/`aria-modal`/手写焦点陷阱；`aria-labelledby` 命名；破坏性确认 `autofocus` 放安全按钮；`dialog:modal { overscroll-behavior: contain }` + `::backdrop { overflow: hidden }`；关闭走 `<form method="dialog">`，`close` 监听读 `returnValue` 后重置为 `''`。Esc 关闭是原生默认，不依赖 `closedby`。

**日志**：可滚容器 + 按 tag 着色；上限 4000 行（超出裁前 800 行，沿用现有口径）；进度行（prog）只保留最新一条；自动滚到底。

**状态提示**：status 区 `aria-live="polite"`；toast 用固定位置的 `aria-live="polite"` 容器，数秒自动消失，不引依赖。

## 6. 主题切换

按 modern-web-guidance `dark-mode` / `dark-mode-toggle`：

- `<meta name="color-scheme" content="light dark">` + `:root { color-scheme: light dark }`；用 `:root:has(> head > meta[name="color-scheme"][content="dark"/"light"])` 把 meta 映射回 CSS 属性（meta 只是提示，CSS 声明会覆盖它）。
- 颜色全走 `light-dark()` 设计令牌；原生控件用 `accent-color` 跟品牌色。
- 三态控制（跟随系统/浅色/深色）放「⑧ 高级」设置区——三态适合设置面而非常驻顶栏。
- 持久化 localStorage 键 `color-scheme`（值 `light`/`dark`，缺省=跟随系统）；meta 后紧跟一行内联脚本（非 module、非 defer）读存储值防闪烁，try/catch 包裹。
- 切换目标 = 当前渲染方案的反色；目标与系统偏好一致时**删除**存储值（回到跟随系统）。**系统偏好后来变得与存储值一致时不清除存储值**（否则无法钉住主题）。
- 不依赖 JS 也能跟随系统（meta 默认 `light dark`）。

**浏览器支持政策**（本项目运行时约束）：仅 WebView2（Evergreen Edge）。Baseline Newly Available 特性（`light-dark()`、`<dialog>`、`color-scheme` 等）直接使用、不做 fallback；Limited availability 特性（`focusgroup`、`closedby`、backdrop `overscroll-behavior`）回避或按渐进增强处理。此政策同步记入 AGENTS.md。

## 7. 下载历史

- 存储：`YtDlpGUI/history.json`，原子写（tmp + replace，沿用核心写盘模式）；**上限 500 条 FIFO**，无保留期设置。
- 记录时机：下载任务结束（成功/失败/手动停止都记），含扩展提交与嗅探页发起的任务；探测/排障/更新不记。
- 字段：`ts`、`urls`、`title`（首个条目标题，无则首链接）、`outdir`、`status`（ok/failed/stopped）、`code`、`elapsed`、`done_count`、`mode`、`source`（gui/ext/sniff）。
- 历史页签：表格（时间/标题/结果/耗时/来源）+ 行操作「重新下载」（记录 URL + 当前配置）、「打开目录」；顶部「清空历史」（dialog 确认）。一期不做搜索/筛选。
- README 注明：历史含下载链接，仅存本机。

## 8. 错误处理与降级

| 场景 | 行为 |
| --- | --- |
| pywebview 未安装 | 启动.bat 首次自动 `pip install pywebview`；失败给明确手动命令，不闪退 |
| WebView2 运行时缺失 | bat 先查注册表预判；main.py 捕获异常，ctypes MessageBox（标准库）提示安装地址 |
| js_api 内部异常 | 包成 `{ok:false, error}`，不抛进 JS |
| 页面未加载完就来事件 | 泵等 `loaded` 事件后开推，之前入队不丢 |
| 忙时关窗口 | `closing` 事件拦截取消 → 推 `exit_confirm` → 前端 dialog 确认 → `api.exit()` 收尾（存配置、停任务、清 .preview 链接） |
| 配置损坏/旧键 | 核心 load_config warn 回调 → 启动后写进日志（现有行为） |
| 封面取不到/解码失败 | 占位 + 原因，元数据照常，「打开原网页」兜底 |
| 边下边播置灰 | 三条规则不变：分离源 / aria2c / probe_playable 不通过 |
| 强退残留 | 现有 6 小时清理（cookie 临时文件/封面缓存/预览素材）原样保留 |

## 9. 测试计划

TDD：先写测试再写实现。

- **现有三套单测全绿**（test_core / test_hardening / test_server）——核心与服务不动，它们是回归基线。扩展 `node --check` 三脚本 + `extension_smoke.js` 5/5。
- **新增 tests/test_host.py**（host 注入事件回调，不依赖 pywebview）：
  - 单任务闸门与扩展共槽（reserve/release、忙时拒绝）。
  - 自愈重试阶梯接线（net_hard 开关、cookie 计划优先）。
  - 历史：三种 status 写入、扩展/嗅探任务也记录、500 条 FIFO、原子写、损坏回退。
  - 配置往返（现有键不漂移）。
  - 事件泵：进度合并节流（≤5/秒）、日志批量、loaded 前入队不丢。
- **新增 tests/test_api.py**：危险参数拦截、非法 URL、忙时 busy 语义、play_now/sniff_play_clip 拒绝客户端路径、open_url 拒绝非 http(s)。
- **保留 --selftest**：无窗口输出构建命令 JSON。
- 前端无构建链，不做 DOM 单测；靠手工验收矩阵 + pywebview devtools。
- 测试端口避开 8765-8775（如 8799）。

## 10. 手工验收矩阵

- 8 分区设置存读；多链接下载；播放列表探测/勾选/下载选中项。
- 封面预览（含取不到图的降级）；边下边播：HLS 源可播 + 分离源/aria2c/mp4 渐进源三种置灰及原因。
- 10054 源自愈重试；自动排障四变体；更新 yt-dlp；格式表双击选用；复制命令；停止。
- 扩展服务开关；**扩展提交 → 桌面流水线执行 → 扩展轮询看到进度/日志/可播状态**（集成回归重点）；嗅探页折叠分片/预览/片段播放。
- 历史：gui/ext/sniff 三种来源记录、重下、打开目录、清空确认。
- 主题：三态切换、持久化、跟随系统、钉住后系统变化不丢。
- 小窗口整页可滚（日志可见）；高 DPI 清晰；键盘走页签/表格/对话框。

## 11. 旧版退役（验收后，分两步）

1. 验收通过 + 全量测试绿 → 旧 main.py 移入 `YtDlpGUI/legacy/main_tk.py` 保留一个版本周期；启动.bat 指向新入口；README 更新（pywebview 依赖、WebView2 要求、历史/主题用法、测试数量）。
2. 用户确认新版稳定后，另行确认再删除 legacy（不自动删）。

## 12. 非目标

- 多任务队列/并行下载（二期独立项目）。
- PyInstaller 打包、自带 Python 运行时。
- 前端框架与构建链（Vue/React/node）。
- 修改 ytdlp_core.py、YtDlpWeb/server.py、sniffer_extension 的任何行为。
- 浏览器网页版复活（方案 B）。
- 历史搜索/筛选、保留期设置。
- 内置播放器、网页内嵌 video 播放。

## 13. 实施顺序（概要，详细计划由 writing-plans 产出）

1. 实测探针：pywebview http_server + ES 模块 + js_api + evaluate_js 推送最小闭环。
2. host.py + test_host.py（流水线移植、历史、事件泵）。
3. api.py + test_api.py（方法面与校验）。
4. main.py 入口 + 启动.bat（依赖/WebView2 处理）。
5. ui 骨架：主题、页签、布局、设计令牌。
6. 下载页：设置区/进度/日志/结果表/预览/播放/对话框。
7. 嗅探页 + 历史页。
8. 扩展集成回归 + 手工验收矩阵。
9. README + AGENTS.md 浏览器支持政策 + 退役第 1 步。