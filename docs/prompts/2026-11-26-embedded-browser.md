# AI 实现提示词：桌面版内嵌浏览器

> 用法：整段复制给 AI（新会话也可，内容自包含）。最后两节是给评审用的，不必一起粘贴。
> 对应设计讨论：2026-11-26 可行性评估；页内叠加部分另见 `specs/2026-11-26-inpage-locate-design.md`。
> 术语：单窗口双模式——**同一个 WebView2** 在「应用页」与「目标网页」之间导航切换，不是分栏、不是 iframe、不新开窗口。

---

## 0. 你的角色

你是这个仓库的实现者。请按下面的约束在现有桌面版里加一个「内嵌浏览器」入口，用它直接嗅探页面资源；**原有浏览器扩展必须继续可用**，两者共用一套嗅探规则和同一条下载流水线。先给方案再动手，每个阶段做完停下来等我验收。

## 1. 项目现状

- Windows 便携工具，核心是 `yt-dlp.exe` + `ffmpeg.exe`，界面用 **pywebview 6.2.1 + WebView2**，前端是原生 ES 模块、零构建（`YtDlpGUI/ui/`）。
- 已有三层：`ytdlp_core.py`（命令构建/配置/诊断）、`YtDlpGUI/host.py`（任务流水线、事件泵、历史）、`YtDlpWeb/server.py`（纯标准库 HTTP 服务，只监听 127.0.0.1，供扩展调用）。
- 已有嗅探入口：`sniffer_extension/`（MV3 扩展，webRequest + 页面世界钩子 + DOM 扫描），条目 POST 给本机服务，回流到桌面版「扩展嗅探」页签。
- 下载是**单任务槽**：`Runner.reserve/launch` + `Host._reserve` + 扩展侧 409，桌面版与扩展共用一个槽。本特性不动这个模型。
- 测试 260 个 Python 用例（`tests/`）+ 扩展冒烟 5 个；提交信息用中文，未经我明确要求不提交。

## 2. 需求

| 编号 | 需求 | 验收方式 |
| --- | --- | --- |
| R1 | 主界面新增「浏览器」页签：输入网址后**主窗口自身**导航到该页面（同一个 WebView2），页内注入一个隔离的「返回」浮钮（closed shadow root，样式与站点隔离）回到应用页 | 能打开 https 页面；点浮钮回到应用页且页签状态不丢 |
| R2 | 窗口内的网络响应按扩展同一套规则筛选后登记为嗅探条目，`entry` 标为 `embedded` | 打开 m3u8 站点后，桌面版嗅探页出现条目，分片被折叠 |
| R3 | 页面加载完成后扫描 `<video>/<audio>/<source>/<img>`，把元素上的媒体地址补登记 | 直链 `<video src=mp4>` 的地址出现在列表，可预览试播 |
| R4 | 条目复用现有下载通路（`Host.sniff_download`），Referer/UA 取当前页面 | 点下载能落盘，Referer 正确 |
| R5 | 与扩展并存：同一 URL 不重复出现，来源可区分 | 两边同时抓同一页，列表只有一条 |
| R6 | 浏览模式下关闭主窗口也能正常退出：不能被页面的 `beforeunload` 对话框卡住，`127.0.0.1` 服务按现有逻辑停止 | 在视频页点关闭，3 秒内退出，无残留 `msedgewebview2` 进程 |
| R7 | 模式切换期间不丢状态：切走前保存 UI 状态（当前页签/滚动位置），切回后拉一次宿主快照（任务状态、最近日志、嗅探条目） | 下载进行中切到网页再切回，进度条与日志连续，不出现空白或倒退 |

## 3. 已核实的技术事实（直接采用，不要重新调研）

1. **网络事件有公共 API**。pywebview 6 的 Windows 后端在 `on_webview_ready` 里已注册 `AddWebResourceRequestedFilter('*', All)`，并把 `WebResourceResponseReceived` 转成事件：
   `window.events.response_received`，回调收到 `webview.models.Response(url, status_code, headers)`。
   参考：`webview/platforms/edgechromium.py` 的 `on_webview_ready` / `on_web_resource_response`，`webview/models.py`。
   这就是扩展 `chrome.webRequest.onHeadersReceived` 的等价物，过滤规则可以直接照搬。
2. **DOM 有公共 API**：`window.dom.get_element(s) / create_element / .style / .append(html) / .on('click')`，见 `webview/dom/`。
3. **`document_start` 注入没有公共 API**。要抢在页面脚本前注入（`injected.js` 那套 XHR/fetch/JSON.parse 钩子），只有两条路：
   `window.gui.webview.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(...)`（`window.gui` 是 `EdgeChrome` 实例，`webview` 是 WinForms 控件；**内部字段，必须包 try/except**），
   或 `webview.settings['REMOTE_DEBUGGING_PORT']`（默认 `None`，见 `webview/__init__.py`）走 CDP。
   **本特性不要求做注入**，先按「加载后挂钩子」的降级方案，把缺口写进已知限制。
4. `private_mode` 默认 `True`（`webview/__init__.py`），即 cookie/localStorage 不保留。如果要让内嵌浏览器跨启动记住登录，必须 `webview.start(private_mode=False)`——**这是全局开关**，会同时让主界面的 localStorage 落盘，需要确认不影响主题设置。
5. `ALLOW_DOWNLOADS` 默认 `False`；`ALLOW_FILE_URLS` 默认 `True`，主界面靠它加载 `file://` 的 `ui/index.html`，**不能全局关**。
6. 扩展侧规则位置：`sniffer_extension/background.js` 的 `MIME_RE` / `NON_MEDIA_RE` / `MEDIA_EXT` / `MANIFEST_RE`、`addItem()`、`onHeadersReceived`；`content.js` 的 DOM 扫描与 1.5 秒轮询；`injected.js` 是页面世界钩子（带 nonce）。
7. 服务端登记入口：`YtDlpWeb/server.py` 的 `register_sniff_items(items, page_url)`，同址去重，上限 300 条。
8. `YtDlpGUI/启动.bat` 里是未锁版本的 `pip install pywebview`。

## 4. 架构约束（硬性）

1. **单窗口双模式：同一个 WebView2 换 URL，不用 iframe、不新开窗口**。切到浏览 = `window.load_url(目标地址)`，切回 = `window.load_url(应用 index.html)`。iframe 方案直接否决：多数站点发 `X-Frame-Options: SAMEORIGIN` 或 CSP `frame-ancestors`，嵌进去白屏；即便放行，跨域 iframe 也拿不到 DOM，嗅探与定位一起废。已知代价要写进文档：切回网页是**重新加载**（视频要重新缓冲、进度归零），所以这是「分段工作流」，不是「边看边挑」。
2. **`js_api` 必须按 origin 守卫（本方案最关键的安全点）**。主窗口挂着 `Api`，一旦导航到外部站点，那个页面就能拿到 `window.pywebview.api` 调 `exit()` / `browse_cookie()` / `save_config()`；它甚至不需要页面脚本配合，自己构造 WebMessage 就能打到宿主。守卫只能做在 Python 侧：`Api` 每个入口先看 `window.get_current_url()` 的 origin，只放行 `file://` 与 `http://127.0.0.1`，其余一律返回 `{ok: false, error: "当前页面不可调用本机接口"}`。禁止只在前端判断。
3. **返回入口用注入的隔离浮钮**。WebView2 没有扩展机制；浮钮在 `window.events.loaded` 之后用 `evaluate_js` 注入，宿主包一层 `attachShadow({mode: 'closed'})` 避免被站点样式污染，点击 → `postMessage({__ytdlp: 'leave'})` → 宿主 `load_url` 回应用页。页面每次导航后 `loaded` 会重新触发，要重新注入。浮钮上带本次已抓条目数（如「← 返回 · 12」），由宿主节流推送（≥1 秒合并一次），否则用户得反复切回来看抓没抓到。**不要做常驻地址栏**——地址输入留在应用页签里。
4. **嗅探规则只能有一份**。把 MIME/后缀判定、分片识别、清单优先、URL 归一抽成 `sniff_rules.js`，扩展与内嵌共同引用（扩展侧用 `import`/`importScripts`，内嵌侧走注入或 Python 侧等价实现）。禁止复制两份正则——两边行为一旦漂移，以后没人查得清。
5. **服务端只加字段不改行为**：条目新增 `entry`（`extension`/`embedded`）、`cat`（video/image/audio/stream/other）、`mime`（原样保留）。现有 `kind` 字段语义混用（扩展上报 mime、`sniff_download` 里写 `"media"`）且截断 16 字符，请一并收敛但**保持向后兼容**（老扩展不传 `cat` 时服务端自己算）。
6. **事件回调里不做 IO，且要处理「推到空处」**。`response_received` 在 UI 线程同步触发，单条处理预算 < 1ms，只做正则过滤 + 入队。另外 `Api.push` 用的是 `evaluate_js("window.__dispatch(...)")`，切到浏览模式时页面里没有 `__dispatch`，事件会静默丢失——此时只更新宿主内的快照，切回应用页后由前端主动拉一次。
7. **导航守卫**：`NavigationStarting` 里只放行 `http/https`，拦掉 `file:` / `javascript:` / `data:`；`NewWindowRequested` 二选一（同窗口导航，或交给系统浏览器）并在代码里写明选择理由。
8. **不改** `ytdlp_core.py` 的下载命令构建、不改单任务槽语义、不改 `YtDlpWeb/server.py` 的安全校验（Host / Origin / Content-Type）。

## 5. 明确不做

- 不做多标签页、书签、历史、自己的下载管理器 UI；
- 不新开第二个窗口（备选形态见文末附录，本提示词不采用）；
- 不做 CDP 全量接管（除非第 3 节第 3 条真被判定为必须，且要同时给出关闭后的降级路径）；
- 不做页内常驻高亮框与每元素按钮（属「页内定位」特性，另见设计稿，复用同一份 overlay 模块）；
- 不尝试绕过站点对内嵌环境的登录限制（Google 账号会拒绝 WebView2，这是已知限制，写进文档而不是绕）。

## 6. 分阶段实施（每阶段停下等验收）

**P0 骨架**：应用页签里加网址输入与「打开」；主窗口 `load_url` 到目标页；`loaded` 后注入返回浮钮；导航守卫；**origin 守卫必须在 P0 落地**（一导航到外部站点就生效，不能留到后面）。
门槛：输入网址能打开；点浮钮回应用页且当前页签不丢；外部页面调 `window.pywebview.api.ping()` 被拒并留日志；`file://` 导航被拒。

**P1 网络嗅探**：`response_received` → 规则过滤 → 合批 → `register_sniff_items`；条目带 `entry=embedded` 与 `page_url`；桌面版嗅探页可见、可下载。
门槛：m3u8 站点出现清单条目且分片被折叠；同 URL 与扩展去重；1000 条响应下界面不卡。

**P2 DOM 补扫 + 预览**：页面加载后扫元素补登记；复用现有预览（缩略图/时长/试播）。
门槛：直链 `<video src>` 出现且能预览；`<img>` 的地址能出现在图片分类下。

**P3（可选）页内叠加**：接入定位模块画框与按钮。
门槛：见 `specs/2026-11-26-inpage-locate-design.md` 的验收判据。

## 7. 验收清单（整体）

- [ ] 两个入口同时工作，条目去重，来源可区分；
- [ ] 外部页面调用 `window.pywebview.api.ping()` 返回拒绝（注意：桥会注入，**不是**不存在，靠 Python 侧 origin 校验拦住），且日志里留痕；
- [ ] 下载进行中切到网页、再切回，进度与日志连续（R7）；
- [ ] 断网、证书错误、页面崩溃（`ProcessFailed`）都有可见提示，不白屏；
- [ ] 关闭窗口后 `127.0.0.1` 服务按现有逻辑停止，无孤儿进程；
- [ ] `py -3 -m unittest discover -s tests` 全绿（现有 260 + 新增），`node tests/extension_smoke.js` 5/5；
- [ ] README 增补「内嵌浏览器」一节，含与扩展的差异表与已知限制。

## 8. 测试与自测

- 过滤/合批/去重逻辑写成**纯函数**，新增 `tests/test_embedded.py` 用假事件对象驱动，不启动 GUI、不联网；
- 端口避开 8765–8775（用 `--port 8799`）；
- 扩展改动后必须到扩展页「重新加载」并刷新页面，否则跑的是缓存脚本。

## 9. 风险与降级

| 风险 | 降级 |
| --- | --- |
| pywebview 升级导致 `window.gui.webview` 失效 | 所有内部访问包 try/except + 版本探测，失败则关闭 `document_start` 注入，只留网络层嗅探 |
| `response_received` 拖慢界面 | 单条预算 < 1ms，5 秒合批，复用 300 条上限 |
| 内嵌登录态被站点拒绝 | 文档写明；提示用户改用扩展（复用系统浏览器登录态） |
| 未锁 pywebview 版本 | 在 `启动.bat` 与 `main.py` 里加最低版本检查（<6 直接提示升级） |
| origin 守卫漏掉新加的 API 方法 | 守卫收口在一个装饰器/`__getattribute__` 上，并加单测遍历 `Api` 的全部公开方法，断言非白名单 origin 下一律拒绝 |
| 用户以为切换丢了抓取结果 | 条目存在服务端，切回不丢；浮钮上的计数就是最直接的安抚 |

## 10. 交付物

1. 代码（模式切换与状态恢复、注入浮钮、origin 守卫、共享规则模块、服务端字段扩展、前端入口）；
2. 测试（新增用例 + 现有全绿）；
3. README 增补；
4. `AGENTS.md` 的「不可改动」条款需要同步放宽（本特性要改 `sniffer_extension/` 与 `YtDlpWeb/server.py`），改之前先问我。

---

## 附：备选形态——双窗口并排（本提示词不采用）

两者的嗅探、规则、登记、下载通路完全相同，只在「网页显示在哪」上分叉：

| | 单窗口双模式（本稿） | 双窗口并排（备选） |
| --- | --- | --- |
| 网页显示 | 主窗口 `load_url` 导航过去 | 第二个 pywebview 窗口 |
| 能否同时看到列表与页面 | 不能，需来回切 | 能，边看边挑 |
| 切回网页是否重新加载 | 是（视频重新缓冲、进度归零） | 否（页面状态保留） |
| 需要注入返回浮钮 | 是 | 否，关窗口即可 |
| 安全边界 | 主窗口 `js_api` 需按 origin 守卫 | 第二窗口不挂 `js_api`，天然隔离 |
| 额外工作 | 模式切换与状态恢复、浮钮、origin 守卫 | 窗口生命周期、贴靠与置顶、任务栏两个图标 |
| 相对成本 | 基准 | 约 ±1 人日，基本持平 |

选单窗口的唯一代价是「切回网页要重新加载」。如果以后发现这个代价不可接受（例如频繁在播放页与列表之间来回），再切到备选形态，两者可以复用同一套嗅探与登记代码，只有窗口层不同。

## 附：给评审看的两条判断（不必粘贴）

- 这个特性的独立价值只有三条：不装扩展也能用、干净会话、能做页内叠加。若只为「抓资源」，扩展已覆盖全部 Chromium 浏览器，投入产出比不高。
- 若要「兼容其他浏览器」，移植 Firefox 扩展比做内嵌浏览器快一个数量级；内嵌只覆盖 WebView2，不解决这个问题。
