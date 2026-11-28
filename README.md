# 鱿鱼浏览器工具包（Windows）

一个基于 yt-dlp 的开箱即用视频下载工具包：便携运行文件（`yt-dlp.exe`、`ffmpeg.exe` 等）统一放在 `runtime\`，
并提供两种用法——**桌面窗口版**（主入口，自带扩展服务与「扩展嗅探」页签）、**浏览器嗅探扩展**。

同一套核心逻辑放在 `ytdlp_core.py` 里，桌面窗口与扩展面板都只是薄薄的界面层。

---

## 快速开始（选一种即可）

### 1. 桌面版（pywebview + Web 界面，单窗口 · 主入口）

双击 `YtDlpGUI\启动.bat`。

下载/合并所需的运行文件在 `runtime\`（源码仓库不含 exe；目录结构见下文）。
需要本机装有 Python 3.9+（脚本会自动探测 `pyw -3` / `pythonw` / `python` / `py -3`）。
界面用 **pywebview** 承载原生 HTML/CSS/JS（WebView2 / Evergreen Edge 内核）：
首次启动脚本会自动 `pip install pywebview`；若窗口打不开并提示缺少 WebView2，
按提示安装 [WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)（Win10/11 一般自带）。
高 DPI 清晰渲染、整页自然滚动，不再有旧 Tkinter 版的模糊与裁切问题。

**桌面版会顺带在自己窗口里跑起浏览器扩展所需的本地服务**（默认开，顶栏显示
「扩展服务：127.0.0.1:8765」）。也就是说：开着桌面版，扩展就能直接用，不必再开一个
黑框窗口。高级设置里取消勾选「启用浏览器扩展支持」即可关掉；关掉窗口时服务随之停止。
**扩展提交的下载由桌面版自己执行**（1.8.0 起）：进度、日志、停止按钮都在桌面窗口里，
内嵌服务只负责接请求、把状态镜像回扩展面板；桌面版自己发起的任务与扩展提交的任务共用一个任务槽。

**窗口分四个页签：「下载」「设置」「扩展嗅探」「下载历史」。** 「下载」页只放链接、保存位置、
操作按钮、进度、探测结果与日志；③ 内容与格式、④ 访问与身份、⑤ 播放列表与容错、⑥ 附加内容、
⑦ 网络与加速、⑧ 高级 六个分区都在「设置」页里调整（随任务启动保存）。
Referer 留空时会按站点自动（抖音 / B站 / YouTube 等），跨站设置不会串用。
队列里每条任务按状态给操作：下载中可「暂停」、已暂停可「继续」、还没结束可「取消」（连部分文件一起清）；
**完成 / 失败 / 已取消的任务可以「删除」**从列表里移除，顶栏「清空已结束」一次清完——
只清列表，磁盘上的文件与「下载历史」里那条记录（失败任务要靠它续传 `.part`）都不动。
扩展抓到的条目会回流到「扩展嗅探」页：
每 3 秒自动刷新（只在该页可见时）、默认折叠分片、可预览试播、勾选后「下载选中」直接进桌面版流水线。
「下载历史」页记录每次下载（成功/失败/停止，含扩展与嗅探页发起的任务，最多 500 条，
存本机 `YtDlpGUI\history.json`，不进仓库），可一键重新下载或打开目录。
「设置」的「高级」里有界面主题三态开关（跟随系统/浅色/深色），选择存本机 localStorage，即时生效。
旧 Tkinter 界面已退役，保留在 `YtDlpGUI\legacy\main_tk.py` 一个版本周期供回退。

### 2. 独立扩展服务（无网页界面，可选）

> 1.9.0 起 `YtDlpWeb\server.py` 只保留扩展需要的接口（状态、嗅探条目登记/列表、
> 预览字节流、下载提交、进度、停止），**不再 serve 任何页面**。

不开桌面版时，可以双击 `YtDlpWeb\启动.bat`（或 `py -3 YtDlpWeb\server.py`）单独跑服务：
控制台只打印监听地址供扩展连接，没有浏览器界面。日常推荐直接开桌面版。

- 端口在 **8765–8775** 之间自动避让占用；也可以用 `--port 8770` 指定。
- 只监听 `127.0.0.1`，**局域网/手机访问不到**（这是刻意的安全设计）。

### 3. 浏览器嗅探扩展（Chrome / Edge，MV3）

1. 先让本机服务跑起来：**开着桌面版即可**（推荐），或者启动独立扩展服务（`YtDlpWeb\启动.bat`，无界面）。
2. 打开 `chrome://extensions`（Edge 为 `edge://extensions`），开启「开发者模式」。
3. 「加载已解压的扩展程序」→ 选择本目录下的 `sniffer_extension`。
4. 打开要下载的网页并刷新一次，右下角会出现「▶ 嗅探 (N)」浮条。
5. 点浮条展开列表 → 点某一项「下载」→ 文件落到 `runtime\downloads\`。

扩展会自动带上当前页面的 **Referer / UA / Cookie（含 httpOnly）**；「下载本页」也会带当前页
cookies（抖音等需要 fresh cookies 的站点）。cookie 会写成临时文件传给 yt-dlp，任务结束/提交被拒时删除。
抖音 `?modal_id=<id>` 的精选/推荐页会自动归一成 `/video/<id>`；feed 首页 `?recommend=1`
没有视频 ID，仍然不能直接下载。

每条结果现在带**缩略图、时长、试播**：直链没有封面元数据，缩略图和时长是让本机
ffmpeg 现场截一帧、读一次头拿到的（mp4 等浏览器原生能解的容器直接在面板里
播放原地址；m3u8/mpd 先抓 6 秒片段再播）。截图与片段走本机服务缓存，同一条第二次几乎
瞬时。

> 为什么缩略图不是页面直接 `<img src="http://127.0.0.1:8765/...">`：https 页面里那是
> 混合内容，会被浏览器拦掉；而 content script 直接 fetch 又受页面 CORS 限制。所以统一
> 由扩展后台取回字节、转成 data URL 再交给页面。桌面版「扩展嗅探」页签直接读进程内缓存，没这个问题。

HLS/DASH 的流是几秒一个分片（`.ts` / `.m4s`）拼起来的，播放器会一个一个请求，
webRequest 全都能看见——以前它们会挤满列表，几千个条目谁也挑不出来。现在**同一页面里
一旦嗅到 `.m3u8` / `.mpd` 清单，就不再收后续分片**（清单才是能下整片的入口），桌面版
「扩展嗅探」页签默认勾选「折叠分片」，取消勾选可以展开看。

B 站这类 DASH 站点会把音频单独做成一条 `.m4s`（流 ID 302xx）。桌面嗅探页和扩展浮条
都默认**隐藏音频单轨**，避免误点后只下到音轨；桌面页勾选「显示音频流」、扩展面板勾选
「显示音频 (N)」即可展开并单独下载，条目会标注「音频」。

嗅探结果还会**回流到桌面版**：扩展把条目 POST 给本机服务，桌面版「扩展嗅探」页签里能刷新看到、
预览试播、勾选下载——桌面版适合批量挑选，扩展适合随手抓。

> 扩展改过代码后必须到扩展页点「重新加载」并刷新页面，否则跑的还是缓存里的旧脚本。

页面世界钩子（`injected.js`）与页面共享 JS 环境，页面脚本理论上也能派发同名事件。所以扩展只收
「带本次加载随机 nonce」且「后台 `chrome.webRequest` 真的看到过这个地址」的条目：伪造者既没有 nonce，
也写不进网络层记录。

---

## 预浏览与试播

**预浏览**：探测出条目后，桌面版选中表格行（或「扩展嗅探」页点「预览选中」），即可看到封面图 +
标题/时长/清晰度/大小/上传者，并可一键打开原网页。封面按需取（不批量拉图），
缓存在 `%TEMP%\ytdlp_thumbs\`，6 小时后自动清理。取不到封面不影响探测结果。

**试播**：扩展嗅探页的「播放片段」会为 m3u8/mpd 这类浏览器不能直放的流生成 6 秒片段
（本机 ffmpeg `-c copy`，不转码整片），直链容器则直接在面板里播放原地址。

---

## 内嵌浏览器（阶段 P1，单窗口）

主界面顶栏的「内嵌浏览器」**不另开窗口**：同一个窗口整体切到浏览器视图，带原生地址栏、
后退 / 前进 / 刷新（加载中变为「停止」）与「下载本页」，工具栏左侧的「返回下载器」切回主界面。
下载器界面只是被隐藏，
DOM、日志、任务状态都保留，返回即恢复；浏览器页面也保留，可再次切回继续用。

- **外观**：工具栏、地址栏与起始页跟随应用当前的浅色/深色主题（应用设置了深色而系统是浅色时也一致）；
  状态文字用色调区分信息、加载与错误。
- **起始页**：打开后是自带网址输入与常用站点（B 站 / 抖音 / YouTube）的新标签页；
  网址回车走原生表单提交，中文输入法确认候选时不会误跳转；非法输入会在行内提示。
- **快捷键**（仅浏览器视图可见时生效）：`Ctrl+L` 定位地址栏，`F5` / `Ctrl+R` 刷新，
  `Esc` 停止加载，`Alt+←` / `Alt+→` 后退 / 前进。地址栏右键菜单可复制页面地址、
  交给系统浏览器打开，或「粘贴并打开」。
- **实现形态**：主窗体的 WinForms 里有两个 WebView2——pywebview 的主界面与一个自建浏览器视图；
  浏览器视图第一次打开时才创建，切回下载器时只隐藏、不销毁。
- **安全边界**：自建浏览器不经过 pywebview，页面里不存在 `window.pywebview`（任何 Python 方法都拿不到）；
  `http/https` 之外的顶层导航（`file:` / `javascript:` / `data:` 等）一律拦截并提示；
  站点请求新窗口时在当前视图里打开，不再跳到系统浏览器（需要外部打开时用地址栏右键菜单）；
  页面直接触发的下载不再一律丢弃：浏览器自己那份取消，地址交给下载队列接管
  （历史里记成「页面下载」）；`blob:` / `file:` 之类非 `http(s)` 地址仍然拒绝。
- **会话**：浏览器使用独立的临时 profile（`%TEMP%\ytdlp_embedded_*`，私有模式），退出时清理。
- **网络嗅探（P1）**：浏览器视图里的网络响应按 `sniffer_extension/sniff_rules.js` 的共享规则过滤
  （MIME / 后缀 / 清单 / 分片），后台合批登记到「扩展嗅探」页；条目带 `entry=embedded`，
  同一页出现 `.m3u8` / `.mpd` 清单后分片不再登记；与扩展抓到同一 URL 时列表只保留一条
  （入口列显示首次抓到的来源）。条目可直接预览/下载，Referer 优先取请求头里的 `Referer`
  （跨域 iframe 播放时才是真实来源页），没有则用当前页面地址。
  无后缀的签名直链（`application/octet-stream`）在体积超过 1 MB 或带 `Content-Range` 时按兜底收下；
  但 `.js/.css/字体` 等静态资源不会被这条兜底误收，`text/plain` 日志里的 `?url=...mp4` 也不算媒体。
- **响应正文（P2）**：清单/接口类响应另走 `WebResourceRequested` + `GetContentAsync` 读正文，
  认 `#EXTM3U` / `<MPD` 和接口 JSON 里的直链——伪装成 `text/plain` 的清单、以及靠钩子绕不过的
  站点也能拿到。只读「文本类 + 体积已知且 ≤ 2 MB」的响应，同时并行数封顶，拿到就解除挂起。
- **页面元素 + 正文钩子（P2）**：DOM 补扫复用 `dom_scan.js`；正文钩子复用扩展的 `injected.js`
  （XHR / fetch / JSON.parse，另含 `responseType=json` 的 XHR、WebSocket 下发、`performance`
  资源时序表补扫），但**只收网络层实锤过的 URL**——同一地址必须在
  `WebResourceResponseReceived` 里真的出现过。页面世界能伪造消息，这条校验和扩展后台
  「必须同时被 webRequest 看到」是同一套信任模型。
- **整页下载（工具栏「下载本页」）**：把当前页 URL 交给 yt-dlp 做站点解析——B 站这类 DASH
  页面会解析出格式并合并视频和音频。地址取 WebView2 当前的 `Source`（站点用 `pushState`
  换页也算），取不到才退回最近一次顶层导航记录；起始页等非 http(s) 页面按钮置灰。
  嗅探列表里的条目常常只是分片（`-1-30011.m4s` 之类，视频与音频各一条），要整段用这个入口。
  解析失败的站点会自动兜底：改用本页（或同站点）嗅探到的整段媒体再下一次，只兜一次；
  候选必须有已知体积、取体积最大的一条——抖音页自己的静态素材 `uuu_265.mp4` 也是整段 MP4，
  没有体积证据宁可不兜底，也不给个几百 KB 的占位文件。桌面端粘贴的页面链接、扩展 / HTTP API 的
  页面任务失败时同样会兜底，但只认**同一个页面**的嗅探条目（浏览器里开过那一页），同站点其它页面的
  条目不顶替；直链任务不兜底。
  抖音精选弹窗页（`/jingxuan?modal_id=<id>`）与标准视频页（`/video/<id>`）是同一个作品：
  地址栏粘贴、扩展 / HTTP API、工具栏与浮条「下载本页」四条入口入队前都会先归一成 `/video/<id>`
  （yt-dlp 完全不认 `jingxuan` 形式），队列、历史与回退匹配看到的也都是同一种写法。
- **页面触发的下载（接管）**：网页自己发起的下载（点站点下载按钮、`Content-Disposition` 之类的
  响应）浏览器那份会被取消，地址立刻交给下载队列接管——进度、暂停、续传、历史都在队列里，
  与嗅探条目用同一条直链通道（`fmt=b` + 当前页 Referer + WebView2 cookie 罐）。
  非 `http(s)` 的地址（`blob:` / `data:` / `file:`）一律拒绝并提示，不进队列。
- **页面内浮条**：顶层页面右下角显示「▶ 嗅探 N」，点开是本页媒体列表（默认折叠分片，
  支持单条或批量下载）；「下载本页」和工具栏按钮走同一条通道，把当前页 URL 交给 yt-dlp 做
  站点解析，B 站这类 DASH 页面会合并视频和音频，不会只落下一条分片。浮条只做展示；下载请求
  回到 Python 后必须命中本轮页面已登记的条目才进队列，页面世界改 UI 也变不出没嗅到过的地址。
- **右上角下载入口（可展开）+ 成功提示**：顶层页面右上角常驻一个小条，显示队列概况（运行中的
  百分比与速度、排队条数、失败/完成条数），点它**就地展开一个下载列表**（最多 8 条，新的在上）：
  每行是标题、进度条和状态（下载中 / 排队中 / 已暂停 / 已完成 / 失败 / 已取消），运行中可
  「暂停 / 取消」、暂停中可「继续 / 取消」、已结束的可「移除」；面板顶部的「打开下载器」才切回
  下载器并打开「下载」页签看完整进度。进度由 `Host.queue_summary` 每秒一次推给页面，变了才发。
  成功加入下载队列时页面中央下方会出现一条蓝色的「已加入下载队列：N 个」，向上飘并淡出
  （`prefers-reduced-motion` 时改为原地淡出）。和浮条一样，页面只做展示与提需求：行内按钮只回
  一条 `queue_action`（动作 + 任务编号），由 `Host.embedded_queue_action` 按白名单校验后执行
  （取消 / 移除与桌面端同义，取消清部分文件、移除只清列表），页面拿不到任何下载参数。
- **无需安装扩展**：内嵌浏览器的登记表、预览与 cookie 罐直接调用 `YtDlpWeb/server.py` 的模块函数
  （宿主缓存同一个模块对象），不开「启用浏览器扩展支持」也能嗅探、预览、下载；之后再启用服务，
  扩展上报与内嵌条目共用同一份登记表，列表不会分裂成两份。
- **凭据**：内嵌浏览器是私有临时 profile，与 `--cookies-from-browser` 读的浏览器库不是一份。
  下载内嵌条目时用 `CoreWebView2CookieManager` 按条目域名导出 cookie（含 httpOnly），
  写成临时罐交给 yt-dlp，任务结束/取消即删；取不到 cookie 时才回退原来的浏览器库路径。
  导出分两段投递：worker 线程只发起 `GetCookiesAsync`，cookie 字段一律回 UI 线程读——
  WebView2 的 COM 对象不能跨线程访问（后台直读会抛 `E_NOINTERFACE`，逐条的兜底 `except`
  会把整罐静默丢空，偶尔还会把界面卡死）。
  扩展 / HTTP API 送来的「下载本页」（`kind=page`）自己没带凭据时，桌面版也会借用这份
  WebView2 cookie 罐（`_webview_cookie_file`），不必先在界面里点一次「下载本页」。
- **当前进度**：P0′ + P1 + P2 已完成；不做多标签页与下载管理器。
- **卡死排查**：界面连续 8 秒不处理消息时，程序会把 Python 线程栈追加到
  `%TEMP%\ytdlp_gui_hang.log`；正常使用不会生成这个文件。
- **已知限制**：浏览器视图会暂时盖住下载器界面（不是页签）；切回下载器后网页继续运行，
  但可能被 Chromium 对隐藏内容做节流，HLS 后续分片可能变慢；Google 等站点会拒绝 WebView2 登录
  （请改用系统浏览器 + 扩展）；正文钩子只收「页面真的请求过」的地址，只在接口 JSON 里出现、
  从未请求的地址仍收不到（与扩展一致）；没有 `Content-Length`（分块传输）的响应不读正文，
  这类清单只能靠页面钩子认；抖音的页面级解析会被抖音自己的接口 403 挡住（yt-dlp 提取器
  拿不到 web detail JSON，`Fresh cookies ... are needed`），这种失败会自动回退到嗅探到的
  整段 MP4（带音轨）——但回退要有条目可选：视频得在页面里真的播放过（媒体流被嗅探到），
  也可以直接点嗅探列表里那条 `video/mp4`；DRM 内容不支持。

---

## AI / 脚本调用（Agent）

桌面版启动后会自动开启本机 agent API（复用扩展服务端口）；桌面版没开时，也可以起一个无界面的独立服务：

```bat
py -3 tools\ytdlp_agent.py service start
py -3 tools\ytdlp_agent.py status
py -3 tools\ytdlp_agent.py service stop
```

- 服务只监听 `127.0.0.1`；端口和临时 token 写在 `runtime\agent_service.json`（不入库）。桌面内嵌的服务拒绝 `service stop`，要停请关闭桌面版。
- AI 任务进入**与桌面版/扩展相同的下载队列**，并发数沿用 `queue_n`；进度、日志、历史（来源 `agent`）都能在桌面版看到。
- 输出目录固定为 `<下载目录>\agent\<job_id>\`，任务只返回这些文件；不写配置、不打开资源管理器。
- 任务记录在服务进程内存里，服务重启后查询不到（输出文件保留）。桌面版关闭会中断内嵌服务上的 AI 任务；需要跨桌面运行，用 `service start` 的独立服务。
- 桌面版关闭了「启用浏览器扩展支持」时，内嵌的 agent 服务也不可用，请用 `service start`。

### CLI（纯 JSON）

```bat
py -3 tools\ytdlp_agent.py download "https://www.bilibili.com/video/BV..." --preset best --wait
py -3 tools\ytdlp_agent.py job <job_id>
py -3 tools\ytdlp_agent.py jobs --limit 10
py -3 tools\ytdlp_agent.py cancel <job_id>
```

- `--preset best|audio`；`--format 137+140` 可覆盖格式；`--playlist` 才允许整列表；`--wait` 等到结束（可用 `--timeout` 秒）。
- 输出任务 JSON：`state`、`pct`、`speed`、`eta`、`files[].path`、`logs`、`error`、`hint`；退出码 0=完成，1=失败/取消，2=调用错误，3=等待超时。
- 没有服务时可用 `--ensure-service` 自动后台启动独立服务；也可用 `--url/--token` 覆盖发现文件。

### MCP（支持 MCP 的 AI 客户端）

在客户端注册 stdio 服务（把路径换成你的实际目录）：

```json
{
  "mcpServers": {
    "yt-dlp": {
      "command": "py",
      "args": ["-3", "E:\\path\\to\\yt-dlp_win\\tools\\ytdlp_mcp.py"]
    }
  }
}
```

工具：`download_video`（立即返回 `job_id`）、`job_status`、`wait_for_job`、`cancel_job`、`list_jobs`、`service_status`、`start_service`、`stop_service`。长任务不要同步等待，提交后用 `wait_for_job`/`job_status`；桌面版没开时先调 `start_service`。

安全：agent 路由需要每进程随机 token，只监听本机；扩展的 Origin 守卫不变；不需要视觉、不弹窗；AI 只能取消自己提交的任务。

## 目录结构

```
yt-dlp_win\
├─ runtime\                    # 便携运行文件（不入库，仅 README 入库）
│   ├─ yt-dlp.exe              # 上游发行版（onedir 打包），升级见下文
│   ├─ _internal\              # yt-dlp.exe 的依赖目录（勿删）
│   ├─ ffmpeg.exe              # 自编译裁剪版：合并 / 转码 / 截帧 / mp3 提取
│   ├─ ffprobe.exe             # 自编译裁剪版：yt-dlp 的元数据/音频探测（核心代码不调用）
│   ├─ downloads\              # 默认下载目录
│   └─ README.md               # 运行目录说明（入库）
├─ ytdlp_core.py               # ★ 共享核心：命令构建、配置读写、诊断规则、进度解析
├─ sniff_rules.py              # Python 侧嗅探判定：读 sniffer_extension/sniff_rules.js 的同一份规则表
├─ YtDlpGUI\                   # 桌面版（pywebview + Web 前端）
│   ├─ main.py                 # 薄入口：建窗、装配 host/api、内嵌扩展服务
│   ├─ host.py                 # 宿主：任务流水线、配置、历史、事件泵（可单测）
│   ├─ api.py                  # js_api 方法面（JS↔Python 桥）
│   ├─ embedded.py             # 内嵌浏览器：主窗口内视图 + 原生工具栏 + 导航守卫
│   ├─ updater.py              # yt-dlp 镜像更新：下载/校验/替换 runtime/回滚
│   ├─ ui\                     # 前端：原生 ES 模块，零构建（index.html / css / js）
│   ├─ legacy\main_tk.py       # 已退役的 Tkinter 版（保留一个版本周期）
│   ├─ config.json             # 桌面版配置（schema_version=3；内嵌服务也读写这份）
│   ├─ history.json            # 本机下载历史（最多 500 条，不进仓库）
│   └─ 启动.bat
├─ YtDlpWeb\                   # 扩展服务（无网页界面）
│   ├─ server.py               # 纯标准库 HTTP 服务（也能被桌面版当模块内嵌）
│   ├─ agent_client.py         # AI/脚本客户端：发现端口/token、提交/查询任务
│   ├─ config.json             # 独立模式配置（与桌面版键名一致）
│   └─ 启动.bat
├─ tools\                      # 独立脚本入口
│   ├─ ytdlp_agent.py          # AI/脚本 CLI：下载、状态、等待、取消、服务启停
│   ├─ ytdlp_mcp.py            # MCP stdio 工具层（给支持 MCP 的 AI 客户端）
│   ├─ build_ffmpeg_min.sh     # 最小 ffmpeg 构建脚本
│   └─ build_release.py        # 发布打包：桌面端 + 扩展 zip
├─ installer\                   # 安装版构建：冻结入口 / 路径解析 / Inno 模板
├─ sniffer_extension\          # Chrome/Edge MV3 扩展
│   ├─ manifest.json
│   ├─ sniff_rules.js          # ★ 嗅探判定唯一真源（MIME/后缀/分片/清单/音频），桌面内嵌共用
│   ├─ background.js           # webRequest 嗅探 + 与本地服务通信
│   ├─ injected.js             # 页面主世界钩子（XHR / fetch / JSON.parse）
│   ├─ audio_stream.js         # 音频单轨识别（调用 sniff_rules.isAudio）
│   ├─ content.js              # 浮条 UI、轮询进度、上报元素里的媒体地址
│   └─ README.md               # 扩展单独的说明与版本历史
├─ docs\                       # 设计、计划与需求提示词（入库）
│   ├─ README.md               # 文档索引
│   ├─ prompts\                # 功能需求提示词
│   └─ superpowers\            # 设计稿与实施计划
├─ tests\                       # 回归测试
│   ├─ test_core.py            # 核心逻辑单元测试（unittest）
│   ├─ test_hardening.py       # 安全/健壮性回归
│   ├─ test_server.py          # 服务端接口、条目字段与并发闸门
│   ├─ test_host.py            # 桌面宿主：事件泵/历史/流水线/预览播放/嗅探
│   ├─ test_api.py             # js_api 方法面与输入校验
│   ├─ test_agent.py           # agent API/CLI/MCP：token、任务、取消、发现文件
│   ├─ test_embedded.py        # 内嵌浏览器/共享嗅探规则/合批与生命周期
│   ├─ test_updater.py         # 镜像更新：URL/校验/替换回滚/下载
│   ├─ test_portable.py        # 便携性：@{APP} 标记往返、换机后的失效路径提示
│   ├─ fixtures\               # 探针测试用的 3 秒小样片（mkv/webm/ts/mp4）
│   └─ extension_smoke.js      # 扩展后台冒烟（Node + 假 chrome API）
└─ _local\                     # 本机杂物（不入库：vc140.pdb、待完成.txt 等）
```

> `runtime/` 里的 exe、`_internal/`、`downloads/`，以及 `_local/`、`.workbuddy/`、`config.json`、
> `history.json`、`__pycache__/` 都是本机文件，已在 `.gitignore` 中排除，不随仓库发布。

---

## 配置

桌面版与独立扩展服务各自默认有一份 `config.json`，**键名完全一致**（老版本的 `open` 会自动迁移成
`open_after`）。桌面版内嵌服务时会让服务直接读写桌面版这份，所以扩展触发的下载与界面设置
是同一套配置。读写都由 `ytdlp_core.py` 负责：

- 写入是**原子**的（临时文件 + `os.replace`，并留一份 `.bak`），断电不会写坏；
- 加载时会做类型/取值校验并给出警告，坏值回退默认值而不是崩掉整个界面；
- `headers` 字段会被规范化：拆分粘连的 `abcOrigin:`、去掉零宽字符/多余空白、剔除非法行、同名头去重；
- 落在程序目录内的路径以 `@{APP}/…` 标记落盘（见下节「可移植性」），换盘换机器后自动指向新位置。

常用键：`outdir` `mode` `fmt` `merge` `tpl` `sublang` `thumb` `meta` `pl` `open_after`
`browser` `browser_profile` `ua` `referer` `headers` `cookie`
`extractor_args` `retries` `proxy` `rate` `conc` `conc_n` `aria` `aria_args` `extra`
以及进阶网络键 `impersonate` `ipv4` `chunk` `proxy_off` `socket_timeout`（仅手改配置）。
`ext_service` 是桌面版的「启用浏览器扩展支持」开关（默认 `true`）。

`conc` / `conc_n` 是分片并发（yt-dlp `-N`）：**默认开、并发 4**。本机对同一段流实测
`-N 1` 4.88 MiB/s、`-N 4` 6.36、`-N 8` 6.09、`-N 32` 5.54——甜点在 4，再往上多半只是
给自己添 403。它只对 HLS/DASH 这类分片流有效，**单个文件的直链（.mp4 一条）加多少并发
都没用**，那种情况得靠 aria2c 多连接分段。
`aria` 勾选后不再「一刀切」：只有**直链音视频**（嗅探条目里 `cat=video/audio`、不是
`.m3u8`/`.mpd` 清单、也不是分片、体积 ≥ 8MB 或未知）才交给 aria2c 多连接分段；清单/分片
照旧走内置下载器（那里靠 `-N`），小文件也不折腾 16 连接。命令固定写成
`--external-downloader "http,https:aria2c"`，把多连接限定在直链协议上，就算判断失误，
m3u8/dash 也不会落到外部下载器。aria2c 的控制台读数（`[#xxxx 8.4MiB/40MiB(21%) CN:1
DL:4.0MiB ETA:7s]`）会被解析成进度，面板不再全程 0%。程序目录和 PATH 里都找不到
`aria2c.exe` 时不会拼一个必然失败的参数，而是自动退回 yt-dlp 自带下载器并在日志里提示
（桌面版开始前会再问一次）。
`net_hard` 是自动重试阶梯的总开关，桌面版有勾选框，默认开着。

> `extra`（附加参数）和 `aria_args` 都经过危险参数黑名单过滤，
> 形如 `--exec` / `--config-location` / `--downloader` / `--cookies-from-browser` 等的会被直接拒绝。

### 可移植性（换盘 / 换机器）

便携包的全部状态都按**当前程序目录**推导，不看盘符、不看用户名，所以整个文件夹可以随意搬家：

- **配置里的应用内路径写标记**：`outdir`、`yt_dlp_path`、`cookie` 只要落在程序目录内，
  落盘时压成 `@{APP}/runtime/downloads` 这类标记，加载时再换回当前目录的绝对路径。
  拷到 `D:\工具\yt-dlp_win` 或 `C:\Users\别人\yt-dlp_win` 都照样能用，不用改配置；
  程序目录以外的路径（例如 `D:\视频`）原样保留，那本来就是用户自己选的位置。
- **路径失效不静默**：`yt_dlp_path` 指向的文件不存在时回退到 `runtime/` 或 PATH 里那份并记一条日志；
  `outdir` 所在磁盘不存在时只提示、不擅自改（可能只是移动硬盘没插）。
- **启动脚本不写死解释器**：`启动.bat` 依次找 便携虚拟环境（`.venv`）→ `py` 启动器 → PATH 里的
  `python.exe`（会先真跑一次，挡掉应用商店的占位程序）→ `%LOCALAPPDATA%\Programs\Python\Python3*`
  / `%ProgramFiles%\Python3*`；覆盖了「装 Python 时没勾 Add to PATH」这种最常见的开箱失败。
  路径含空格/中文也安全。
- **运行文件查找**：`runtime/` → 项目根（旧布局）→ PATH；换机器只补 `runtime\` 即可。
- **构建机无关**：`tools/build_installer.py` 找 Inno Setup 时会看 `SQUIDKIT_ISCC` 环境变量、
  `%LOCALAPPDATA%\Programs\Inno Setup 6|7` 和 PATH，不再只认 `C:\Program Files`。

跨机器**不**保证的部分：`YtDlpGUI\history.json` 里的历史下载目录、以及浏览器 Profile 的名字。
前者打不开时「打开目录」会退回当前下载目录并提示；后者换机器后需要重新选一次。

---

## 安全设计

- 服务只绑定 `127.0.0.1`，并校验 `Host`（防 DNS rebinding）、`Origin`（含 `chrome-extension://`、
  `moz-extension://` 白名单）、`Content-Type: application/json`（防跨站提交）。
- 1.9.0 起服务不再 serve 任何网页，浏览器侧攻击面（XSS / 点击劫持 / 混合内容）整体移除；
  接口与媒体字节流仍带 `X-Content-Type-Options: nosniff`。
- 请求体上限 1 MB（含分块传输的请求体，避免长连接下的请求走私）；cookie 临时文件在任务结束与下次启动时清理，
  超过 6 小时的也会被扫掉。
- 预览字节流只认服务端自己登记的 token（`/api/sniff/media?id=`，不接受客户端指定 URL）；
  取封面/片段时每一跳重定向都重新校验目标地址，指向本机/内网/保留地址
  （含 `::ffff:127.0.0.1`、`100.64.0.0/10` 这类非常见写法）一律拒绝。
- 请求封面时只带 UA 与 Referer，不带 Cookie 和自定义认证头——封面地址是站点元数据给的，不该拿到登录凭据。
- 会起任务的接口（嗅探下载提交）在忙碌时统一返回 **409**，且只允许一个任务占位；
  桌面版是宿主时任务槽由桌面版与服务共用——桌面版正在下载时扩展提交同样会被 409 挡回。
- 配置文件写入是原子的（临时文件 + `os.replace`，并保留 `.bak`），跨进程加建议锁，残留的 `.tmp` 会在下次写入时清掉。
- 下载链接必须是 `http(s)://`；参数列表里 URL 前统一加 `--`，避免「以 `-` 开头的伪链接」被当成命令行选项。
- 「附加参数 / aria2c 参数」在桌面版会过滤危险项（`--exec`、`--config-location`、
  `--downloader`、`--cookies-from-browser`、`--update`、`--postprocessor-args` 等），
  命中即拒绝开始下载。这是纵深防御而不是硬边界（yt-dlp 允许参数前缀缩写）。
- 配置文件损坏（非法 JSON、BOM、`²` 这类 `isdigit()` 为真的非 ASCII 数字）时回退默认值并在日志里说明，不会崩。

---

## 自愈与诊断

内置两套阶梯式重试，失败会自动换方案，不用手改配置（实现都在 `ytdlp_core.py`；
桌面版的阶梯在 `_on_done → _net_heal`，独立服务的嗅探下载在 `run_download`）：

| 症状 | 阶梯 |
| --- | --- |
| `WinError 10054` 连接被重置 | 直连 → `--impersonate Chrome` + IPv4 → Edge 指纹 + `--http-chunk-size 10M` |
| HTTP 400（带 Referer 被拒） | 去 Referer → 补 Origin/Accept → 指纹 + IPv4 → Edge 指纹 + 10M 分块 |
| HTTP 403 / 需要登录 | 浏览器 cookies / UA / Referer / 通用提取器兜底（诊断面板可「一键应用修复」） |
| 浏览器 Cookies 读不出来（`Could not copy Chrome cookie database`） | 脱掉浏览器登录态重试一次（公开内容照常能下）；登录态内容请关掉浏览器，或用扩展面板点下载 |

重试会自动续传；手动点「停止」不会再触发重试；403/404/不支持的站点直接放弃，不浪费等待时间。
「失败后自动换姿势重试」（配置项 `net_hard`，桌面版有勾选框）可以整开关掉这套阶梯。

进度解析走机器可读通道（`--progress-template`），不再靠正则猜 `[download] 45.3% of ...`。

---

## 自测

```powershell
# 核心逻辑单元测试：命令构建、进度解析、诊断规则、重试阶梯、配置读写与类型校正、
# 路径校验、Cookie 罐、看门狗、封面取图与公网校验、可播性探测、硬链接预览、
# 直链预览（ffmpeg 截帧/抓片段/时长）、嗅探条目登记/清空与预览 token、分片识别、
# 共享嗅探规则（JS/Python 同源）、内嵌浏览器响应过滤与合批、服务端 entry/cat/mime 字段、
# 配置迁移到 v3、aria2c 缺失降级、浏览器 Cookies 被锁时的降级、端口占用探测、
# 任务委托与 409 闸门、网页 UI 路由退役回归
py -3 -m unittest discover -s tests

# 桌面版命令构建自检（不打开窗口，打印将执行的命令）
py -3 YtDlpGUI\main.py --selftest

# 桌面版「扩展嗅探」页签自检（不联网：登记假条目 → 折叠/刷新断言）
py -3 YtDlpGUI\main.py --selftest-snifftab

# 扩展脚本语法检查
node --check sniffer_extension\sniff_rules.js
node --check sniffer_extension\background.js
node --check sniffer_extension\audio_stream.js
node --check sniffer_extension\content.js
node --check sniffer_extension\injected.js

# 扩展后台冒烟（假 chrome API：MV3 回收后数据不丢、端口发现、旧版服务兼容）
node tests\extension_smoke.js
```

---

## 升级 yt-dlp

- 桌面版：窗口里有「更新 yt-dlp」按钮，默认执行官方的 `yt-dlp -U`。
- **官网不通时用镜像**：在「设置 → 网络与加速 → 更新镜像」填 GitHub 代理前缀
  （如 `https://ghproxy.net` 或 `https://gh-proxy.com`，留空关闭）。官方 `-U` 失败后，
  程序会从镜像下载官方 onedir 包，校验 `SHA2-256SUMS` 后替换
  `runtime\yt-dlp.exe` + `runtime\_internal\`；失败自动回滚，旧版备份在
  `runtime\.update_backup_*`，可手动恢复。
- **信任提示**：镜像属于第三方，校验文件与更新包同源，不能抵御恶意镜像；默认关闭，
  开启前请确认镜像可信。镜像下载默认直连，直连失败才回退到「代理」设置。
- 手动方式：把新的 `yt-dlp.exe`（onedir 发行包需连同 `_internal\`）放进/覆盖 `runtime\`
  （即 `runtime\yt-dlp.exe` 与 `runtime\_internal\`）。

---

## 最小 ffmpeg / ffprobe（自编译）

随包的 `ffmpeg.exe`、`ffprobe.exe` 裁剪自 **FFmpeg 8.1.3**（静态单文件，LGPL；两者合计
约 19 MB，原全量构建是 272 MB）。只开启本项目与 yt-dlp 实际用到的能力：合并/封装
（mp4/mkv/webm/flv/mpegts/HLS/DASH）、截帧、6 秒试播片段、mp3 提取、`ffmpeg -i` 读时长。
核心代码已不直接调用 ffprobe——时长与可播性探测都改用 `ffmpeg -i`；保留 ffprobe 只是
为了让 yt-dlp 的元数据/音频处理路径保持完整。

在 MSYS2 MINGW64 里重建（工具链可装在任何位置，例如 `E:\envir\msys`）：

```bash
pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-nasm mingw-w64-x86_64-lame \
         mingw-w64-x86_64-libxml2 mingw-w64-x86_64-libmpg123 \
         mingw-w64-x86_64-zlib mingw-w64-x86_64-pkgconf pkgconf make
FFMPEG_BUILD_DIR=/e/envir/build/ffmpeg-min ./tools/build_ffmpeg_min.sh
```

脚本固定了源码版本、SHA-256 与 configure 参数，产物直接写入 `runtime\`
（`runtime\ffmpeg.exe` / `runtime\ffprobe.exe`）。需要新增能力（例如某个站点的特殊编码）时，往脚本的
`--enable-decoder` / `--enable-demuxer` 列表里加一项再重建即可。

---

## 已知限制

- `blob:` / `data:` 地址无法下载（浏览器内部对象，不落地）。
- DRM 加密内容不支持（也不应绕过）。
- 同一时间只跑一个任务（忙碌时返回 409），避免 CPU/带宽抢占；桌面版与扩展提交共用这一个槽位。
- 部分站点（如部分苹果CMS站点）真实 `m3u8` 只在播放时通过接口下发且加密，
  需要扩展的 `JSON.parse` 钩子才能抓到——所以该钩子不能简单移除。
- 浏览器开着的时候，Chromium 系（Edge / Chrome）会**独占锁住自己的 Cookies 数据库**，
  yt-dlp 复制不出来就会报 `Could not copy Chrome cookie database`（
  [yt-dlp#7271](https://github.com/yt-dlp/yt-dlp/issues/7271)）。这不是本项目的 bug，
  两条路：完全退出浏览器，或者改用扩展面板里的「下载」——扩展直接把本页 cookie 交给程序，
  根本不碰那个数据库。程序也会自动降级重试一次（探测 / 格式表 / 下载都适用），只是那一次
  不带登录态；日志里会以黄色提示说明「已忽略 Cookies 继续」，不再按红色错误展示。

## 目录维护记录

- `2026-11-26`：运行文件分离到 `runtime\`（`yt-dlp.exe`、`_internal\`、`ffmpeg.exe`、`ffprobe.exe`、
  `downloads\`），源码目录只保留代码/测试/文档；旧布局的根目录 exe 仍兼容查找。
- `2026-11-26`：`ffmpeg.exe` / `ffprobe.exe` 换为同源自编译裁剪版（静态，合计约 19 MB）；
  原全量构建移到 `..\yt-dlp_removed_20261126\`。
- 已移出发布目录（保留在 `..\yt-dlp_removed_20261104\`）：`ffplay.exe`、`ffmpeg-master-latest-win64-lgpl\`
  （上游文档目录，本项目代码零引用）。
- 开发备忘与 Playwright 测试脚手架已移到 `..\yt-dlp_win_dev_notes\`（`2026-11-01` / `2026-11-04` 备忘、nettest）。
- 运行测试请使用 **8765–8775 之外**的端口（例如 `--port 8799`），避免抢占用正在使用的实例。

---

## 第三方组件与许可

| 组件 | 说明 |
| --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense；`runtime/` 为官方 Windows onedir 发行包 |
| [FFmpeg](https://ffmpeg.org/) | LGPL；本项目自编译裁剪版（静态链接 LAME / libxml2 / zlib） |
| [pywebview](https://pywebview.flowrl.com/) | BSD-3；桌面窗口承载 Web 界面（首次启动自动安装，依赖系统 WebView2） |
| `curl_cffi` / `websockets` / `pycryptodome` / `certifi` | 见 `runtime\_internal\THIRD_PARTY_LICENSES.txt` |

本仓库自身的界面与嗅探代码仅为个人自用工具，请遵守目标站点的服务条款与版权规定。

---

## 安装版（Windows 安装包）

除 zip 便携包外，还可以构建成常规 Windows 应用：双击 `squidkit-<版本>-setup.exe` 安装，
出现在「已安装的应用」里，有开始菜单 / 桌面快捷方式、卸载入口、独立图标与版本信息，
任务栏不再显示成 Python 进程。

### 与便携包的区别

| | 便携包（zip） | 安装版（setup.exe） |
| --- | --- | --- |
| 启动 | `YtDlpGUI\启动.bat`，需本机 Python 3.9+ | `squidkit.exe`，自带 Python 运行时，不要求装 Python |
| 身份 | 进程是 `pythonw.exe`，任务栏沿用 Python 图标 | 独立 exe，设了 `AppUserModelID`，任务栏 / Alt-Tab 认得本应用 |
| 配置与历史 | 写在 `YtDlpGUI\` | `%APPDATA%\SquidKit\`（首次启动会从旧位置迁移一次） |
| 默认下载目录 | `runtime\downloads\` | 「下载」`\SquidKit\` |
| 系统登记 | 无 | HKCU 卸载项、注册表安装路径、可选开机自启 |
| 升级 | 覆盖解压 | 重新运行同版本或新版本安装包（AppId 相同，自动升级并保留用户数据） |

安装目录默认是 `%LOCALAPPDATA%\Programs\SquidKit`，`PrivilegesRequired=lowest`，不需要管理员权限；
`runtime\` 随程序放在该可写目录，因此内置的 yt-dlp 自动更新照常可用。

**WebView2**：窗口依赖 Evergreen WebView2 运行时。Win11 与正常 Win10 自带；安装器会查
HKLM（32/64 位视图）与 HKCU 三处登记，缺失时用内置的微软官方引导器
（`MicrosoftEdgeWebview2Setup.exe /silent /install`，需联网）静默补装，补装失败则首次启动的弹窗
会给出下载地址。引导器在构建时自动下载到 `build\tools\webview2\`（不入库，也不随安装包常驻磁盘：
装到 `{tmp}` 用完即删）。

安装包**未做代码签名**，来自网络的副本会触发 SmartScreen「未知发布者」，需要用户点
「更多信息 → 仍要运行」；企业 AppLocker / 只允许签名程序的环境会直接拦截。要消除提示需自备
代码签名证书并在 ISCC 里加 `SignTool`。

### 构建

```bat
py -3 -m pip install --target build\tools\pyinstaller "pyinstaller==6.22.3"
:: Inno Setup 7 的 ISCC.exe 放到 build\tools\inno7\，或直接用系统安装版 / PATH 中的 ISCC
py -3 tools\build_installer.py
```

产物在 `dist\squidkit-<版本>-setup.exe`。脚本依次完成：生成多尺寸图标与 exe 版本资源 →
PyInstaller 冻结 `squidkit.exe` → 组装 `{app}\src`（源码 + `runtime\`）→ 冻结产物无界面冒烟
（校验 `BASE_DIR` 指向安装目录、yt-dlp / ffmpeg 能被找到）→ Inno Setup 编译安装包。

调试用参数：`--skip-freeze`（复用上次冻结结果）、`--skip-selftest`、`--skip-inno`
（只产出 `build\installer\payload\squidkit\` 与渲染好的 `squidkit.iss`）、
`--version X.Y.Z`、`--out DIR`。

构建工具与中间产物都在 `build\`（不入库）。源码 `installer\` 下的
`squidkit_paths.py`（路径 / 身份 / 旧配置迁移）、`squidkit_launcher.py`（冻结入口）、
`make_icon.py`（图标）、`squidkit.iss.tpl`（安装包模板）可单独单测，见 `tests\test_installer.py`。

应用本体的更新靠重新运行安装包；yt-dlp 内核仍用界面里内置的更新功能（替换 `runtime\yt-dlp.exe`）。
卸载时会询问是否一并删除 `%APPDATA%\SquidKit` 的用户数据，默认保留；下载文件不在安装目录内，
卸载不会动它们。
