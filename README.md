# yt-dlp 便携版工具箱（Windows）

一个开箱即用的视频下载工具包：内置 `yt-dlp.exe` 与 `ffmpeg.exe`，并提供三种用法——
**桌面窗口版**（主入口，自带扩展服务）、**浏览器嗅探扩展**、**本地网页版**（可选）。

同一套核心逻辑放在 `ytdlp_core.py` 里，两个前端都只是薄薄的界面层。

---

## 快速开始（选一种即可）

### 1. 桌面版（Tkinter，单窗口 · 主入口）

双击 `YtDlpGUI\启动.bat`。

需要本机装有 Python 3.9+（脚本会自动探测 `pyw -3` / `pythonw` / `python` / `py -3`，
并检查 tkinter；缺 tkinter 时会给出中文提示而不是直接闪退）。

**桌面版会顺带在自己窗口里跑起浏览器扩展所需的本地服务**（默认开，右上角显示
「扩展服务：127.0.0.1:8765」）。也就是说：开着桌面版，扩展就能直接用，不必再开一个
黑框窗口。高级设置里取消勾选「启用浏览器扩展支持」即可关掉；关掉窗口时服务随之停止。

### 2. 网页版（浏览器界面，可选）

> 阶段说明：桌面版已经内嵌服务，网页版在功能上被桌面版 + 扩展覆盖，后续版本会退役。
> 仍保留它是因为「40+ 配置项、批量粘链接、格式表、排障阶梯、完整日志」这些低频操作
> 目前只有网页界面顺手。

双击 `YtDlpWeb\启动.bat`。控制台会打印：

```
  访问地址：  http://127.0.0.1:8765
```

浏览器打开该地址即可使用（支持批量探测、勾选下载、诊断与一键修复）。

- 端口在 **8765–8775** 之间自动避让占用；也可以用 `--port 8770` 指定，`--no-browser` 阻止自动开浏览器。
- 只监听 `127.0.0.1`，**局域网/手机访问不到**（这是刻意的安全设计）。

### 3. 浏览器嗅探扩展（Chrome / Edge，MV3）

1. 先让本机服务跑起来：**开着桌面版即可**（推荐），或者启动网页版（`YtDlpWeb\启动.bat`）。
2. 打开 `chrome://extensions`（Edge 为 `edge://extensions`），开启「开发者模式」。
3. 「加载已解压的扩展程序」→ 选择本目录下的 `sniffer_extension`。
4. 打开要下载的网页并刷新一次，右下角会出现「▶ 嗅探 (N)」浮条。
5. 点浮条展开列表 → 点某一项「下载」→ 文件落到 `downloads\`。

扩展会自动带上当前页面的 **Referer / UA / Cookie（含 httpOnly）**，
cookie 会写成临时文件传给 yt-dlp，任务结束即删除。

每条结果现在带**缩略图、时长、试播**：直链没有封面元数据，缩略图和时长是让本机
ffmpeg/ffprobe 现场截一帧、读一次头拿到的（mp4 等浏览器原生能解的容器直接在面板里
播放原地址；m3u8/mpd 先抓 6 秒片段再播）。截图与片段走本机服务缓存，同一条第二次几乎
瞬时。**下载途中**列表上方还会出现「边下边播」，用系统播放器打开正在增长的文件。

> 为什么缩略图不是页面直接 `<img src="http://127.0.0.1:8765/...">`：https 页面里那是
> 混合内容，会被浏览器拦掉；而 content script 直接 fetch 又受页面 CORS 限制。所以统一
> 由扩展后台取回字节、转成 data URL 再交给页面。网页版自己是同源 http 页面，没这个问题。

HLS/DASH 的流是几秒一个分片（`.ts` / `.m4s`）拼起来的，播放器会一个一个请求，
webRequest 全都能看见——以前它们会挤满列表，几千个条目谁也挑不出来。现在**同一页面里
一旦嗅到 `.m3u8` / `.mpd` 清单，就不再收后续分片**（清单才是能下整片的入口），网页版
遇到分片也会默认折叠，右上角「显示分片 (N)」可以展开看。

嗅探结果还会**回流到网页版**：扩展把条目 POST 给本机服务，网页版新增的「浏览器扩展嗅探结果」
卡片里能刷新看到、试播、勾选下载——网页版界面更适合批量挑选，扩展适合随手抓。

> 扩展改过代码后必须到扩展页点「重新加载」并刷新页面，否则跑的还是缓存里的旧脚本。

页面世界钩子（`injected.js`）与页面共享 JS 环境，页面脚本理论上也能派发同名事件。所以扩展只收
「带本次加载随机 nonce」且「后台 `chrome.webRequest` 真的看到过这个地址」的条目：伪造者既没有 nonce，
也写不进网络层记录。

---

## 预浏览与边下边播

**预浏览**：探测出条目后，网页版点行尾「预览」、桌面版选中表格行，即可看到封面图 +
标题/时长/清晰度/大小/上传者，并可一键打开原网页。封面按需取（不批量拉图），
缓存在 `%TEMP%\ytdlp_thumbs\`，6 小时后自动清理。取不到封面不影响探测结果。

**边下边播**（设置里的「边下边播（不影响下载速度）」，默认关）：下载途中点「边下边播」，
程序把正在增长的 `.part` 文件硬链接成一个扩展名正确的名字，交给系统默认播放器。
零字节拷贝、不转码、不额外拉流。以下情况按钮会置灰并说明原因：

- 音视频分离的源（YouTube 高清等，`bv*+ba/b`）——两个 `.part` 都不是完整片子；
- 开了 aria2c 外部下载器——文件布局不可控；
- ffprobe 读不到流——例如渐进式 mp4 的索引在文件末尾，要下完才能播。

这个开关**不改任何下载参数**：HLS 的 `.part` 本来就是 mpegts（下载途中就能播），
收尾仍由 yt-dlp 自己的 `FixupM3u8` 修回 mp4。实测开关开/关的最终文件 sha256 相同。
**推荐 VLC 或 PotPlayer**；`.preview\` 子目录里的链接在任务结束时删除。

---

## 目录结构

```
yt-dlp_win\
├─ yt-dlp.exe                  # 上游发行版（onedir 打包），升级见下文
├─ _internal\                  # yt-dlp.exe 的依赖目录（勿删）
├─ ffmpeg.exe                  # 合并音视频 / 转码用
├─ ffprobe.exe                 # 元数据探测（yt-dlp 部分功能会调用）
├─ ytdlp_core.py               # ★ 共享核心：命令构建、配置读写、诊断规则、进度解析
├─ YtDlpGUI\                   # 桌面版
│   ├─ main.py
│   ├─ config.json             # 桌面版配置（schema_version=3；内嵌服务也读写这份）
│   └─ 启动.bat
├─ YtDlpWeb\                   # 网页版
│   ├─ server.py               # 纯标准库 HTTP 服务（也能被桌面版当模块内嵌）
│   ├─ index.html              # 单文件前端（无构建依赖）
│   ├─ config.json             # 网页版配置（与桌面版键名一致）
│   └─ 启动.bat
├─ sniffer_extension\          # Chrome/Edge MV3 扩展
│   ├─ manifest.json
│   ├─ background.js           # webRequest 嗅探 + 与本地服务通信
│   ├─ injected.js             # 页面主世界钩子（XHR / fetch / JSON.parse）
│   ├─ content.js              # 浮条 UI、轮询进度、上报元素里的媒体地址
│   └─ README.md               # 扩展单独的说明与版本历史
├─ tests\                       # 回归测试
│   ├─ test_core.py            # 核心逻辑单元测试（unittest）
│   ├─ test_hardening.py       # 安全/健壮性回归
│   ├─ test_server.py          # 服务端接口与并发闸门
│   └─ extension_smoke.js      # 扩展后台冒烟（Node + 假 chrome API）
└─ downloads\                  # 默认下载目录
```

---

## 配置

桌面版与网页版各自默认有一份 `config.json`，**键名完全一致**（老版本的 `open` 会自动迁移成
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
`net_hard` 是自动重试阶梯的总开关，桌面版和网页版都有勾选框，默认开着。
`play_while` 是边下边播的总开关，两端都有勾选框，默认关着。

> `extra`（附加参数）和 `aria_args` 都经过危险参数黑名单过滤，
> 形如 `--exec` / `--config-location` / `--downloader` / `--cookies-from-browser` 等的会被直接拒绝。

---

## 安全设计

- 服务只绑定 `127.0.0.1`，并校验 `Host`（防 DNS rebinding）、`Origin`（含 `chrome-extension://`、
  `moz-extension://` 白名单）、`Content-Type: application/json`（防跨站提交）。
- 页面带 CSP（`default-src 'self'`、`frame-ancestors 'none'`）与 `X-Frame-Options: DENY`。
- `/api/open` 只允许打开下载目录内的路径（拒绝 UNC、`..` 逃逸）。
- 请求体上限 1 MB（含分块传输的请求体，避免长连接下的请求走私）；cookie 临时文件在任务结束与下次启动时清理，
  超过 6 小时的也会被扫掉。
- 封面代理只认服务端自己登记的 token（不接受客户端指定 URL），且每一跳重定向都重新校验目标地址；
  指向本机/内网/保留地址（含 `::ffff:127.0.0.1`、`100.64.0.0/10` 这类非常见写法）一律拒绝。
- 请求封面时只带 UA 与 Referer，不带 Cookie 和自定义认证头——封面地址是站点元数据给的，不该拿到登录凭据。
- 网页版所有会起任务的接口（探测/下载/格式表/排障/更新/嗅探）在忙碌时统一返回 **409**，且只允许一个任务占位。
- 配置文件写入是原子的（临时文件 + `os.replace`，并保留 `.bak`），跨进程加建议锁，残留的 `.tmp` 会在下次写入时清掉。
- 下载链接必须是 `http(s)://`；参数列表里 URL 前统一加 `--`，避免「以 `-` 开头的伪链接」被当成命令行选项。
- 「附加参数 / aria2c 参数」在两个界面里都会过滤危险项（`--exec`、`--config-location`、
  `--downloader`、`--cookies-from-browser`、`--update`、`--postprocessor-args` 等）；
  桌面版命中即拒绝开始下载，网页版返回 400。这是纵深防御而不是硬边界（yt-dlp 允许参数前缀缩写）。
- 网页版 `/api/config` 对 `outdir`/`tpl`/`cookie` 做结构校验：拒绝 UNC、盘符/绝对路径模板与 `..` 逃逸。
- 配置文件损坏（非法 JSON、BOM、`²` 这类 `isdigit()` 为真的非 ASCII 数字）时回退默认值并在日志里说明，不会崩。

---

## 自愈与诊断

内置两套阶梯式重试，失败会自动换方案，不用手改配置（桌面版与网页版共用同一份实现，
都在 `ytdlp_core.py`；桌面版的阶梯在 `_on_done → _net_heal`，网页版在 `run_download`）：

| 症状 | 阶梯 |
| --- | --- |
| `WinError 10054` 连接被重置 | 直连 → `--impersonate Chrome` + IPv4 → Edge 指纹 + `--http-chunk-size 10M` |
| HTTP 400（带 Referer 被拒） | 去 Referer → 补 Origin/Accept → 指纹 + IPv4 → Edge 指纹 + 10M 分块 |
| HTTP 403 / 需要登录 | 浏览器 cookies / UA / Referer / 通用提取器兜底（诊断面板可「一键应用修复」） |
| 浏览器 Cookies 读不出来（`Could not copy Chrome cookie database`） | 脱掉浏览器登录态重试一次（公开内容照常能下）；登录态内容请关掉浏览器，或用扩展面板点下载 |

重试会自动续传；手动点「停止」不会再触发重试；403/404/不支持的站点直接放弃，不浪费等待时间。
「失败后自动换姿势重试」（配置项 `net_hard`，两边界面都有勾选框）可以整开关掉这套阶梯。

进度解析走机器可读通道（`--progress-template`），不再靠正则猜 `[download] 45.3% of ...`。

---

## 自测

```powershell
# 核心逻辑单元测试（223 个用例：命令构建、进度解析、诊断规则、
# 重试阶梯、配置读写与类型校正、路径校验、SSE 分帧、Cookie 罐、看门狗、
# 封面取图与公网校验、可播性探测、硬链接预览、边下边播不改命令、
# 直链预览（ffmpeg 截帧/抓片段/时长）、嗅探条目登记与预览 token、
# 分片识别、配置迁移到 v3、aria2c 缺失降级、浏览器 Cookies 被锁时的降级、端口占用探测）
py -3 -m unittest discover -s tests

# 桌面版命令构建自检（不打开窗口，打印将执行的命令）
py -3 YtDlpGUI\main.py --selftest

# 扩展脚本语法检查
node --check sniffer_extension\background.js
node --check sniffer_extension\content.js
node --check sniffer_extension\injected.js

# 扩展后台冒烟（假 chrome API：MV3 回收后数据不丢、端口发现、旧版服务兼容）
node tests\extension_smoke.js
```

---

## 升级 yt-dlp

- 桌面版：窗口里有「更新 yt-dlp」按钮；网页版：右上角「更新」按钮（等价于 `yt-dlp -U`）。
- 手动方式：把新的 `yt-dlp.exe`（onedir 发行包需连同 `_internal\`）覆盖进来即可。

---

## 已知限制

- `blob:` / `data:` 地址无法下载（浏览器内部对象，不落地）。
- DRM 加密内容不支持（也不应绕过）。
- 网页版同一时间只跑一个任务（忙碌时返回 409），避免 CPU/带宽抢占。
- 部分站点（如部分苹果CMS站点）真实 `m3u8` 只在播放时通过接口下发且加密，
  需要扩展的 `JSON.parse` 钩子才能抓到——所以该钩子不能简单移除。
- 边下边播不支持音视频分离的源与 aria2c；渐进式 mp4 要下完才能播。
- 下载目录在 exFAT/FAT32 U 盘上时建不了硬链接，边下边播会置灰并说明原因。
- 个别播放器以独占方式打开文件时可能挡住续写，遇到就换 VLC / PotPlayer。
- 浏览器开着的时候，Chromium 系（Edge / Chrome）会**独占锁住自己的 Cookies 数据库**，
  yt-dlp 复制不出来就会报 `Could not copy Chrome cookie database`（
  [yt-dlp#7271](https://github.com/yt-dlp/yt-dlp/issues/7271)）。这不是本项目的 bug，
  两条路：完全退出浏览器，或者改用扩展面板里的「下载」——扩展直接把本页 cookie 交给程序，
  根本不碰那个数据库。程序也会自动降级重试一次，只是那一次不带登录态。
- 播放器还占着预览链接时，Windows 会拒绝删除，`.preview\` 会留到下一个任务结束才清掉
  （只是空目录里的硬链接，不占额外空间，也不影响下载产物）。

## 目录维护记录

- 已移出发布目录（保留在 `..\yt-dlp_removed_20261104\`）：`ffplay.exe`、`ffmpeg-master-latest-win64-lgpl\`
  （上游文档目录，本项目代码零引用）。
- 开发备忘与 Playwright 测试脚手架已移到 `..\yt-dlp_win_dev_notes\`（`2026-11-01` / `2026-11-04` 备忘、nettest）。
- 运行测试请使用 **8765–8775 之外**的端口（例如 `--port 8799`），避免抢占用正在使用的实例。

---

## 第三方组件与许可

| 组件 | 说明 |
| --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense；本目录为官方 Windows onedir 发行包 |
| [FFmpeg](https://ffmpeg.org/) | LGPL/GPL 视具体构建；随包分发请遵守其许可 |
| `curl_cffi` / `websockets` / `pycryptodome` / `certifi` | 见 `_internal\THIRD_PARTY_LICENSES.txt` |

本仓库自身的界面与嗅探代码仅为个人自用工具，请遵守目标站点的服务条款与版权规定。
