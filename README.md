# yt-dlp 便携版工具箱（Windows）

一个开箱即用的视频下载工具包：便携运行文件（`yt-dlp.exe`、`ffmpeg.exe` 等）统一放在 `runtime\`，
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
后退 / 前进 / 刷新（加载中变为「停止」），工具栏左侧的「返回下载器」切回主界面。下载器界面只是被隐藏，
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
  站点请求新窗口时交给系统浏览器；页面直接触发的下载会被取消（下载统一走嗅探列表）。
- **会话**：浏览器使用独立的临时 profile（`%TEMP%\ytdlp_embedded_*`，私有模式），退出时清理。
- **网络嗅探（P1）**：浏览器视图里的网络响应按 `sniffer_extension/sniff_rules.js` 的共享规则过滤
  （MIME / 后缀 / 清单 / 分片），后台合批登记到「扩展嗅探」页；条目带 `entry=embedded`，
  同一页出现 `.m3u8` / `.mpd` 清单后分片不再登记；与扩展抓到同一 URL 时列表只保留一条
  （入口列显示首次抓到的来源）。条目可直接预览/下载，Referer/UA 取当前页面。
- **当前进度**：P0′ + P1 已完成；P2（页面元素补扫、`<img>` 图片分类）尚未接入。
- **卡死排查**：界面连续 8 秒不处理消息时，程序会把 Python 线程栈追加到
  `%TEMP%\ytdlp_gui_hang.log`；正常使用不会生成这个文件。
- **已知限制**：浏览器视图会暂时盖住下载器界面（不是页签）；切回下载器后网页继续运行，
  但可能被 Chromium 对隐藏内容做节流，HLS 后续分片可能变慢；Google 等站点会拒绝 WebView2 登录
  （请改用系统浏览器 + 扩展）；不做多标签页、书签与下载管理器；P2 之前页面里只有 `<video>` 元素、
  没有网络请求的直链不会自动登记。

---

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
│   ├─ config.json             # 独立模式配置（与桌面版键名一致）
│   └─ 启动.bat
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
│   ├─ test_embedded.py        # 内嵌浏览器/共享嗅探规则/合批与生命周期
│   ├─ test_updater.py         # 镜像更新：URL/校验/替换回滚/下载
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
- `headers` 字段会被规范化：拆分粘连的 `abcOrigin:`、去掉零宽字符/多余空白、剔除非法行、同名头去重。

常用键：`outdir` `mode` `fmt` `merge` `tpl` `sublang` `thumb` `meta` `pl` `open_after`
`browser` `browser_profile` `ua` `referer` `headers` `cookie`
`extractor_args` `retries` `proxy` `rate` `conc` `conc_n` `aria` `aria_args` `extra`
以及进阶网络键 `impersonate` `ipv4` `chunk` `proxy_off` `socket_timeout`（仅手改配置）。
`ext_service` 是桌面版的「启用浏览器扩展支持」开关（默认 `true`）。

`conc` / `conc_n` 是分片并发（yt-dlp `-N`）：**默认开、并发 4**。本机对同一段流实测
`-N 1` 4.88 MiB/s、`-N 4` 6.36、`-N 8` 6.09、`-N 32` 5.54——甜点在 4，再往上多半只是
给自己添 403。它只对 HLS/DASH 这类分片流有效，**单个文件的直链（.mp4 一条）加多少并发
都没用**，那种情况得靠 aria2c 多连接分段。
`aria` 勾选会用 aria2c 下载；程序目录和 PATH 里都找不到 `aria2c.exe` 时不会拼一个必然
失败的参数，而是自动退回 yt-dlp 自带下载器并在日志里提示（桌面版开始前会再问一次）。
`net_hard` 是自动重试阶梯的总开关，桌面版有勾选框，默认开着。

> `extra`（附加参数）和 `aria_args` 都经过危险参数黑名单过滤，
> 形如 `--exec` / `--config-location` / `--downloader` / `--cookies-from-browser` 等的会被直接拒绝。

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
