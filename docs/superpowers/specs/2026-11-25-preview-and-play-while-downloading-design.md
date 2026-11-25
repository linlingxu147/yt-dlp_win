# 设计：下载前预浏览 + 边下边播放

日期：2026-11-25
状态：已与用户逐节确认，待实现
范围：`ytdlp_core.py`、`YtDlpGUI/main.py`、`YtDlpWeb/server.py`、`YtDlpWeb/index.html`、`tests/test_core.py`、`README.md`

---

## 1. 目标

1. **预浏览**：下载前看到封面图 + 元数据，防止下错视频。
2. **边下边播放**：可选开关，下载途中用系统播放器打开正在增长的文件。**下载速度优先**——不转码、不额外拉流、不增加带宽。

浏览器嗅探扩展（`sniffer_extension/`）一行不改。

## 2. 已验证的事实

实现前用随包工具做过本地探针（ffmpeg lavfi 造测试片 → 截断到指定比例 → ffprobe 读），结论：

| 容器 | 截断到 40% | 截断到 15% | 截断到 5% |
| --- | --- | --- | --- |
| mkv | 可读、可解码 | 可读 | 不可读（头部未写完） |
| webm | 可读、可解码 | 可读 | 可读 |
| mpegts | 可读，duration 随文件增长 | 可读 | 可读 |
| mp4 | `moov atom not found`，完全打不开 | 同左 | 同左 |

解码 mkv/webm 截断文件只在尾部报 `File ended prematurely`，退出码 0。

其他已核实事实：

- 随包 yt-dlp 版本 2026.10.26，支持 `--part` / `--no-part` / `--remux-video` / `--hls-use-mpegts` / `--write-thumbnail`。
- `--hls-use-mpegts` **默认关闭**，仅直播默认开启。官方说明即「让部分播放器能边下边播」。
- 随包 ffmpeg 是 LGPL 构建，**没有 libx264**；可用编码器含 mpeg4 / libvpx / libvpx-vp9 / aac / libopus。
- `progress.filename`（`PROGRESS_TEMPLATE` 第 7 段）给的是**完整路径**，但现有代码只取 basename 并用 `FRAG_RE` 抹掉 `.f137.` 标记（`ytdlp_core.py:850`）。那是显示名，不是磁盘上的真实文件。
- `merge` 默认 `"mp4"`，哨兵值 `"不转换"`；`--merge-output-format` 仅在 `mode == "best"` 时添加（`ytdlp_core.py:733`）。
- 全项目当前没有任何缩略图或预览通路。

## 3. 关键设计决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 预浏览深度 | 封面图 + 元数据 | 不额外拉媒体流 |
| 封面获取 | 服务端代取 + 本地缓存 + token 端点 | 两端一致、绕防盗链、避免 SSRF |
| 分离源（YouTube 高清等） | 不支持，按钮置灰并说明 | 保持最高画质，不做无声播放、不自动降级 |
| 播放通路 | 同目录硬链接 + 系统默认播放器 | 零字节拷贝、零额外带宽 |
| 是否加 `--no-part` | **不加** | 保留 `.part` 标记，续传与「已下完」判定逻辑不受影响 |
| 能否播放的判定 | ffprobe 实测正在增长的文件 | 不靠猜格式；一次读取，开销可忽略 |

被否决的方案：本地 Range 端点喂网页 `<video>`（额外 IO、mp4 仍不可播）、下载途中实时 remux（抢 IO）、显式探测 VLC/PotPlayer 安装路径（维护成本高）、完成后播放（不满足需求）。

## 4. 架构

### 4.1 共享核心新增（`ytdlp_core.py`）

```python
def is_public_http_url(url) -> bool
    # scheme 必须 http/https
    # 主机名经 socket.getaddrinfo 解析，所有返回地址都必须是公网地址；解析失败即拒绝
    # 拒绝环回、私网、链路本地（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、
    # 192.168.0.0/16、169.254.0.0/16、::1、localhost）
    # 只看字面 IP 挡不住「公网域名解析到 127.0.0.1」，所以必须解析后再判

def thumb_cache_path(url, cache_dir) -> str
    # sha1(url) + 原扩展名（无扩展名则 .img）

def fetch_thumb(url, cfg, cache_dir, timeout=8, max_bytes=2*1024*1024) -> (path, err)
    # 缓存命中直接返回；否则 urllib 带 Referer/UA/Cookie 取图
    # 失败回退一次 yt-dlp --write-thumbnail --skip-download（复用现有指纹/cookie 通路）
    # 永不抛异常给调用方；err 为中文原因字符串或 None

def probe_playable(path, timeout=6) -> {"ok": bool, "reason": str}
    # 跑 ffprobe 读 streams/duration；缺失、超时、读不到流都返回 ok=False + 原因

def make_preview_link(real_path, outdir) -> (link_path, err)
    # 在 outdir/.preview/ 下建硬链接，扩展名取自 real_path 去掉 .part 后的真实容器
    # 不支持硬链接的文件系统返回 err，不复制、不降级

def clear_preview_links(outdir) -> None
    # 删除 .preview/ 内的链接；只删链接，不动被链接的数据

def fmt_is_separated(cfg) -> bool
    # 解析最终 fmt（手填优先，否则 MODE_FMT[mode]），含 "+" 即 True
```

`info_from_json` / `info_from_json_raw`：现有 6 个键（`title` `dur` `res` `size` `up` `url`）**语义与取值不变**，新增 `thumb`（取 `d["thumbnail"]`，回退 `d["thumbnails"]` 末项的 `url`，都没有则空串）。

`progress_event`：`state` 新增 `raw_path`，保存 `progress.filename` 的原始完整路径（**不做 basename、不做 FRAG_RE 替换**）。现有 `state["file"]` 显示名逻辑一个字不改。

### 4.2 配置

只新增一个键：

- `play_while`：bool，默认 `False`，加入 `_BOOL_FIELDS`。

不复用已有的 `thumb` 键——那是「下载时嵌入封面」（`--embed-thumbnail`），语义不同。

### 4.3 网页版新增端点（`YtDlpWeb/server.py`）

**`GET /api/thumb?id=<token>`**

- 探测阶段服务端把封面 URL 登记进内存表，只把 token 交给前端。表上限 512 条，FIFO 淘汰。
- token 由 `secrets.token_urlsafe(12)` 生成。
- 未知/过期 token → 404；缺 `id` → 400。
- **不接受客户端传 URL**。请求里出现 `url` 参数一律 400。
- 成功 → 200 + 图片字节 + 按扩展名推断的 `Content-Type`。
- 沿用现有 Host / Origin 守卫。

**`POST /api/play`**

- 请求体不接受任何路径或 URL 字段；出现 `path` / `url` 一律 400。服务端自己知道当前任务在下哪个文件。
- 无任务在跑 → 409（复用现有忙碌语义）。
- 分离源 / aria2c 外部下载器 / 探测不通过 / 硬链接失败 → 400 + 中文原因。
- 成功 → 200 `{"ok": true}`。

**事件流新增**

- 探测 `row` 事件增加 `thumb_id`（取不到封面 URL 时为空串）。
- 新增 `playable` 事件：`{"ok": bool, "reason": str}`，供前端启用/置灰播放按钮。
- **不向前端下发磁盘路径**。

### 4.4 两端界面

**网页版**（`index.html`）

- `#results`（`index.html:210`）下方新增预览卡，默认隐藏。
- **触发方式**：每行新增一个「预览」按钮，点它才展开预览卡。**行点击保持现有的勾选行为不变**（`renderResults` 现在把行点击绑到 `toggleCheck`，`index.html:568`），不能改成点行看预览，否则会破坏批量勾选。
- 预览卡内容：大封面（宽 320）+ 标题/时长/分辨率/大小/UP主 + 「打开原网页」按钮。
- 行内不放缩略图，避免播放列表 N 项触发 N 次请求。
- 「播放列表与容错」分组内新增「边下边播（不影响下载速度）」勾选框，紧邻现有 `net_hard`；`FIELDS`（`index.html:425`）加入 `play_while`。
- 进度区新增「播放」按钮，默认置灰；收到 `playable.ok == true` 才启用；置灰时显示原因。

**桌面版**（`YtDlpGUI/main.py`）

- `self.tree`（`main.py:412`）所在分组下方新增预览面板：封面 + 元数据 + 「打开原网页」。
- 封面经随包 ffmpeg 转 PNG（`-vf scale=320:-1 -frames:v 1`）后交给 `PhotoImage`——Tkinter 只认 PNG/GIF。
- `_build_playlist` 的容错分组内新增「边下边播」勾选框，`_collect_cfg` / `_apply_config` 同步 `play_while`。
- 下载区新增「播放」按钮，启用条件与网页版一致。

## 5. 功能 1：预浏览

**触发**：懒加载。网页版点行内「预览」按钮时取图，桌面版选中表格行时取图。探测命令一个字不改（`-j` 本来就返回 `thumbnail`），预浏览只多一次图片请求。

**通路差异**：token 表只存在于网页版（因为要跨 HTTP 边界）。桌面版直接调 `fetch_thumb`，不经 token。

**缓存**：`%TEMP%\ytdlp_thumbs\`，文件名 `sha1(url) + 原扩展名`。同一 URL 命中缓存不重复请求。复用现有 cookie 临时文件的 6 小时清理机制。

**取图**：urllib 带与下载相同的 Referer / UA / Cookie，超时 8 秒，单图上限 2 MB。失败回退一次 yt-dlp `--write-thumbnail --skip-download`。

**失败不阻塞**：取不到图时显示「封面取不到：<原因>」，元数据照常显示，「打开原网页」兜底。**探测结果永远不会因为封面失败而消失。**

**安全**：封面 URL 必须通过 `is_public_http_url`。token 机制挡住了客户端传 URL，但挡不住站点自己在 JSON 里返回指向 `127.0.0.1` 的 thumbnail——所以私网校验是必需的第二道闸。

## 6. 功能 2：边下边播

### 6.1 命令改动（仅 `play_while` 为真时）

> **2026-11-25 实测修正：本节 1、2 两条已作废，`play_while` 不改任何下载参数。**
> 用真实 H.264 HLS 素材实测发现：
> - 不加 `--hls-use-mpegts` 时，下载中的 `<name>.mp4.part` **本来就是 mpegts**，ffprobe 可读、可播；
>   下完 yt-dlp 自己跑 `[FixupM3u8] Fixing MPEG-TS in MP4 container` 修回真 mp4。
> - 加了 `--hls-use-mpegts` 会**关掉这道收尾**，把 mpegts 留在 `.mp4` 名字里。
>   `--fixup force` 救不回来；`--remux-video mp4` 按扩展名判断，直接报「already is in target format」跳过。
> - 第 2 条的「产物与平时一致」推断本身也不成立：单格式下载不触发合并，
>   `--merge-output-format` 不生效，所以 `--remux-video <merge>` 会把平时产出 mp4 的源强行变成 mkv。
>
> 结论：删掉整个命令分支，边下边播只读 `.part`。修正后实测开关开/关最终文件 sha256 相同。
> 下面两条保留作为设计过程记录。

1. ~~加 `--hls-use-mpegts`。**无条件加**——它只影响 HLS 源，对别的源无副作用，不需要先判断协议。~~
2. ~~当 `merge` 非空、不等于 `"不转换"`、且 mode 不是 `audio_*` 时，加 `--remux-video <merge>`，下完 remux 回目标容器，**最终产物与平时一致**。`merge` 为空或 `"不转换"` 时不 remux，产物为 `.ts`，界面需说明。~~
3. **不加 `--no-part`**。

其余参数、重试阶梯（`NET_HARD_PLANS`）、看门狗（`iter_lines_watch`）、进度解析全不变。

**关键回归要求**（修正后加强）：`build_download_cmd` 的输出**与 `play_while` 取值无关**——
5 种 mode × 5 种 merge 组合下，开关开/关的命令必须完全相同。

### 6.2 置灰规则（三条，任一命中即不可播）

1. `fmt_is_separated(cfg)` 为真——音视频分离源。不能只靠 ffprobe：纯视频 webm 的 `.part` 探测会通过，那就变成无声播放了，与用户选择的方案不符。
2. `cfg["aria"]` 为真——aria2c 外部下载器的文件布局不可控（可能预分配），探测结果不可信。
3. `probe_playable` 不通过——覆盖 mp4 索引在尾部、`-N` 并发分块导致的乱序等情形。

### 6.3 定位真实文件

`raw_path` 来自 `progress.filename`（完整路径、保留 `.f137.` 标记）。磁盘上优先取 `raw_path + ".part"`，不存在则取 `raw_path` 本身。

### 6.4 探测时机

拿到路径后开始等待，**「文件 ≥ 256 KB」与「已经过 3 秒」两个条件任一满足**即跑第一次 ffprobe（慢源不至于干等体积，快源不至于白等 3 秒）。不通过则隔 5 秒再试，最多 3 次；仍不通过判定「此源要下完才能播」，置灰并显示原因。**只读不转码。**

### 6.5 播放

在下载目录内建 `.preview\` 子目录（天然同卷，硬链接不会跨卷失败），`os.link` 建链接后 `os.startfile(link)` 调系统默认播放器。零字节拷贝，播放器看到正确扩展名且文件仍在增长。

**链接命名**：源文件 basename 去掉末尾 `.part`，**保留 `.f137` 这类格式标记**——例如 `xxx.f137.mp4.part` → `.preview\xxx.f137.mp4`。保留标记是为了播放列表多条目不撞名。同名链接已存在（上次残留）时先删再建。

### 6.6 清理

任务结束（成功 / 失败 / 手动停止）删除 `.preview\` 内的链接。删链接不影响正在下载的数据。异常退出的残留由 6 小时清理兜底。

### 6.7 与现有行为的交互

- 手动「停止」不触发重试——现有行为不变。
- 重试阶梯换方案时文件名不变，探测状态需重置后重探。
- 与 `open_after`（下完自动打开）互不冲突。

## 7. 错误处理

一律「降级不阻塞」：下载与探测结果不因预览/播放失败而受影响。

| 症状 | 行为 |
| --- | --- |
| 封面 403 / 超时 / 超 2 MB | 元数据照常 + 占位 + 原因 + 「打开原网页」兜底 |
| 封面 URL 非 http(s) 或指向私网 | 拒绝、不发请求，记一条 warn |
| ffmpeg 转 PNG 失败 | 「封面格式无法显示」，元数据照常 |
| ffprobe 缺失 / 超时 | 判定不可播、置灰 + 原因，下载继续 |
| 硬链接失败（exFAT/FAT32 U 盘） | 置灰 + 明说原因，不复制、不静默降级 |
| 无关联播放器 | 提示安装 VLC/PotPlayer，不静默失败 |
| token 未知/过期 | 404 + 前端占位 |
| `/api/play` 无任务在跑 | 409 |
| 客户端给 `/api/thumb` 传 url、给 `/api/play` 传 path | 400 |

## 8. 测试计划

TDD：先写测试再写实现。核心单测 102 → 约 130 例。

**核心单测**

- `info_from_json`：有 `thumbnail`、只有 `thumbnails`、两者都无；**现有 6 个键取值不变**（回归）。
- `is_public_http_url`：http/https 通过；`file:` / `ftp:` / `blob:` / `data:` 拒绝；各私网段与 `localhost` / `::1` 拒绝；**公网域名解析到私网地址时拒绝**（monkeypatch `getaddrinfo`，不联网）；解析失败时拒绝。
- `fetch_thumb`：超时、体积上限、缓存命中不重复请求、失败回退路径（monkeypatch urlopen，不联网）。
- `probe_playable`：把第 2 节的探针固化成测试——ffmpeg lavfi 造 mkv/webm/mp4/ts 再截断，断言 mkv/webm/ts 可读、mp4 不可读；无 ffmpeg 的环境用 `skipUnless` 跳过。
- `make_preview_link` / `clear_preview_links`：建立、清理、清理不影响源文件、不支持时的错误。
- `fmt_is_separated`：`bv*+ba/b` 为真，`b` / `best[acodec!=none]` 为假，手填 fmt 优先。
- `progress_event`：`raw_path` 保留完整路径与 `.f137` 标记；**显示名不变**（回归）。
- `build_download_cmd`：命令**与 `play_while` 取值无关**（5 种 mode × 5 种 merge 全覆盖）；不出现 `--hls-use-mpegts`、`--remux-video`；`mode=best` 时 `--merge-output-format` 照常生效；**任何情况下都不出现 `--no-part`**。（见 6.1 的实测修正）
- 配置：`play_while` 进 `_BOOL_FIELDS`、类型校正、原子写、`save_config` 回传警告。
- 端点守卫：`/api/thumb` 拒绝未知 token 与 url 参数；`/api/play` 拒绝 path 参数、无任务时 409；Host / Origin 校验沿用现有测试。

**手工实测矩阵**（不能只靠单测）

- 源类型：HLS/m3u8 源、单文件渐进 mp4 源、YouTube 音视频分离源，各一个。
- 播放器：VLC、PotPlayer、Windows「电影和电视」，各试一次。
- **速度对照**：同一源在开关开/关下各跑一遍，比对下载速度，验证「零额外开销」。
- **锁文件验证**：播放器打开期间 yt-dlp 能否继续写入；若某播放器独占导致写失败，在 README 写明推荐播放器。
- 硬链接失败路径：把下载目录指到 exFAT U 盘（或构造不支持的场景），确认提示明确。

**回归**

- `py -3 -m unittest discover -s tests` 全绿。
- `py -3 YtDlpGUI\main.py --selftest` 退出码 0。
- `node --check` 三个扩展脚本；`node tests\extension_smoke.js` 保持 5/5。
- 运行测试用 8765–8775 之外的端口（如 `--port 8799`），避免抢占正在使用的实例。

## 9. 文档更新

`README.md`：新增两个功能的用法、`play_while` 配置键、置灰规则与原因、推荐播放器、已知限制（分离源不支持、mp4 渐进源要下完才能播、exFAT 不支持硬链接）、测试数量。

## 10. 非目标

- 行内缩略图瀑布流 / 滚动懒加载列表图。
- 网页内嵌 `<video>` 播放、本地 Range/206 端点。
- 截开头 10~15 秒的转码预览。
- 内置播放器、播放器安装路径探测。
- 修改浏览器嗅探扩展。
- 音视频分离源的无声播放或自动降级到单文件。

## 11. 实施顺序

1. 核心：`is_public_http_url` / `thumb_cache_path` / `fetch_thumb` + 测试。
2. 核心：`info_from_json` 加 `thumb`、`progress_event` 加 `raw_path` + 回归测试。
3. 核心：`probe_playable` / `make_preview_link` / `clear_preview_links` / `fmt_is_separated` + 测试。
4. 核心：`build_download_cmd` 的 `play_while` 分支 + 逐字节回归测试。
5. 网页版：token 表、`/api/thumb`、`/api/play`、`playable` 事件 + 守卫测试。
6. 网页版前端：预览卡、勾选框、播放按钮。
7. 桌面版：预览面板（ffmpeg 转 PNG）、勾选框、播放按钮。
8. 手工实测矩阵 + 速度对照 + 锁文件验证。
9. README 更新与全量回归。