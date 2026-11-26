# 多任务下载队列设计（路线图 A）

- 日期：2026-11-26
- 状态：设计已逐节确认，待用户评审本文档
- 上游：[IDM 式下载能力路线图](2026-11-26-idm-like-roadmap.md)
- 范围：仅 A（多任务队列）。B（浏览器接管）、C（浮动面板）不在本文档内。

## 1. 目标与非目标

**目标**
- 并发下载 N 个任务（默认 2，可配 1–5），超出部分 FIFO 排队
- 任务粒度：每条链接一个任务；播放列表仍是一个任务
- 暂停 / 继续 / 取消；每任务独立进度、日志、历史
- 桌面 GUI 任务与扩展提交任务进入同一队列
- 桌面版与独立扩展服务（YtDlpWeb）共用同一队列内核

**非目标**
- 跨重启持久化队列（已定：内存队列）
- 扩展端多任务列表 UI（A 仅显示排队位置，列表留待后续）
- 下载调度器 / 计划任务 / 分类管理
- 改动 `sniff_rules.js` 判定规则与既有下载命令构建行为

## 2. 已定决策

| 决策 | 结论 |
|------|------|
| 重启行为 | 内存队列，重启清空；完成任务进历史，可「继续下载」复用 `.part` |
| 并发数 | 默认 2，可配 1–5，改动即时生效 |
| 任务粒度 | 每条链接一个任务 |
| 架构 | 方案 2：共享 `ytdlp_queue.py`，桌面与独立服务都用队列 |
| 辅助操作 | 探测/格式表/排障与下载并行（彼此间单闸）；更新 yt-dlp 独占 |
| 暂停语义 | 杀进程树、保留 `.part`/`.aria2`、释放槽位；恢复插队首 |
| 取消语义 | UI 确认后杀进程并清理 `.part`/`.aria2`，历史记 canceled |
| 完成通知 | 失败逐条 toast；队列清空时汇总成功/失败数 |
| 队列上限 | 未完成任务 ≥200 拒绝新提交（409） |

## 3. 架构：共享队列内核

新增 `ytdlp_queue.py`（项目根）。host 与 server 均已把根目录加入 `sys.path`，可直接 import。

### 3.1 Task

字段：`id`（进程内自增）、`source`（gui/ext/sniff）、`urls`、`cfg`（提交时快照）、
`outdir`、`no_playlist`、`title`、`state`、`pstate`（进度状态）、
`log_ring`（deque，500 行）、`exit_code`、`proc`、`created_at/started_at/ended_at`、
`pause_requested/cancel_requested`、自愈上下文（`net_tried/net_name/diag_buf`）、
`cookie_warned`、`ext_cookie_file`。

### 3.2 QueueManager（每进程一个实例）

核心只拥有：槽位计数、FIFO 调度、状态机、进程注册、暂停/取消信号、事件发射。
执行差异由调用方提供的 executor 承担。

API（示意）：
- `submit(spec, executor) -> task_id`：spec 含 urls/cfg/outdir/no_playlist/source/title
- `pause(id)` / `resume(id)` / `cancel(id)` / `stop_all()`
- `snapshot()`：任务列表 + running/queued 计数
- `set_max(n)`：1–5；调小不杀在跑任务，调大立即补位
- `emit(event)`：事件回调（host 接 UI 事件泵，server 接镜像）

内存保留：全部活跃任务（queued/running/paused）+ 最近 50 条已结束任务；
更早的已结束任务移出内存（历史文件仍保留）。

### 3.3 统一进程泵

`run_process(task, cmd, cwd, on_line, stall_seconds)`：
`core.spawn` → 注册 `task.proc` → `iter_lines_watch`（abort 检查
pause/cancel 信号）→ 收尾 `proc.wait()` 并清空 proc。
两种 executor 都调用它，进程 IO 只有一套实现。

### 3.4 executor 协议

executor 由模式方提供，负责：构建命令（`core.build_download_cmd`）、
逐行处理（进度/日志/分类）、自愈阶梯重试（同一任务内、不释放槽位）、
完成后的历史/通知/清理。暂停/取消信号优先于自愈重试。

- host executor：富功能（Cookies 锁库降级提示、历史、open_after、扩展镜像、播放态）
- server executor：简化版（复用现有 `run_download` 阶梯逻辑改造）

### 3.5 事件

规范化事件均带 `task_id`：`added / state / progress / log / cmd / done`，
外加全局 `queue_changed`（计数变化）。host 现有事件类型保留并附加 task_id，
便于 UI 渐进迁移。

### 3.6 两种模式接线

- 桌面：host 创建 QueueManager；扩展任务经 `TASK_SINK` 入队。
  `TASK_SINK` 返回值由 bool 升级为 task_id/None（旧调用按 truthy 兼容）。
- 独立服务：server 自建 QueueManager（相同类 + 简化 executor）。
- server 新增 `QUEUE_STATUS` 钩子：桌面模式下供 `/api/status` 上报 host 队列快照。

## 4. 状态机与关键语义

```
queued → running → done / failed / canceled
            ↕
          paused →（继续）queued(队首) → running
```

- **暂停**：杀进程树，保留 `.part`/`.aria2`，释放槽位，排队任务立即补位。
- **继续**：重建命令续传；自愈阶梯从 normal 重新开始（已下载部分续传）。
  签名 URL 过期导致失败 → 明确提示「刷新页面重新获取链接」，任务置 failed。
- **取消**：UI 确认后杀进程并清理 `.part`/`.aria2`；历史记 canceled。
- **并发调整**：调小不终止在跑任务，仅暂停新启动；调大立即补位。
- **退出应用**：有活跃任务时沿用确认框，文案「N 个下载中 / M 个排队」；
  退出停止全部运行中任务（记 stopped 历史），排队中任务直接丢弃（未开始、无残留）。
- **边下边播**：PLAY_STATE 保持单份；`play_now` 指向选中任务，切换即关前一个。
- **open_after**：同一输出目录 10 秒内只打开一次，避免并发弹多个资源管理器。
- **历史**：每条链接一条记录，新增 `task_id` 与 canceled 状态；
  failed/stopped 支持「继续下载」。
- **临时 Cookie 文件**：按任务在 done/cancel 时清理；启动清理逻辑不变。

## 5. host 侧改动（YtDlpGUI/host.py）

- 新增 `self.queue`；并发数读 `queue_n`。
- `start_download`：合法 URL 逐条入队，返回 `{ok, task_ids}`；配置只保存一次。
- 单份状态（`dl_urls/dl_outdir/dl_source/pstate/proc/diag_buf/net_tried/net_name/
  user_stopped/cookie_warned/t_start`）移入任务上下文；`task_kind` 仅用于辅助操作。
- executor 按任务复用现有 `_run/_handle_line/_net_heal/on_done` 逻辑。
- 辅助操作分闸：探测/格式表/排障共用轻量 `aux` 锁，与下载并行；
  更新 yt-dlp 独占——存在下载任务（含排队）时拒绝更新，更新中拒绝新下载。
- 扩展任务入口 `submit_server_task/start_external` 改为入队并返回 task_id。
- 退出确认与 shutdown 走 `queue.stop_all()`。
- 历史写入带 task_id；新增 canceled 状态与「继续下载」入口。

## 6. server 侧与扩展接口（YtDlpWeb/server.py）

- 独立模式：自建 QueueManager；`Runner` 类移除，相关测试更新。
- 桌面模式：`TASK_SINK` 返回 task_id；新增 `QUEUE_STATUS` 钩子。
- `SNIFF_STATE` 仍镜像最近一次扩展任务（token 隔离沿用），token 对齐 task_id。

接口只增不删：

| 接口 | 新增 | 保留 |
|------|------|------|
| `POST /api/sniff` | `task_id`、`queue_position` | `ok/busy/url`；未完成任务 ≥200 时 409 |
| `GET /api/status` | `tasks[]`(id/state/pct/source)、`running`、`queued` | `busy/kind/elapsed`（取首个运行中任务） |
| `GET /api/sniff/log` | `task_id` | 其余字段不变 |
| `POST /api/stop` | 可选 `id` | 不传只停最近一次扩展任务，不误杀 GUI 任务 |

扩展（A 范围最小改动）：提交后显示「已加入队列，第 N 位」；不加新权限，
版本号小升。兼容矩阵：旧扩展+新服务 ✔；新扩展+旧服务 ✔（忽略多余字段）；
旧桌面+新扩展 ✔（服务代码随桌面一起旧）。

## 7. UI 改动（YtDlpGUI/ui，零构建原生 ES 模块）

- 下载页单进度区 → 任务列表：
  - 头部统计「N 下载中 · M 排队」+「全部停止」（带确认：停止运行中任务记 stopped，
    排队中任务直接移除）
  - 任务行：标题/链接、状态徽标、进度条、速度/ETA/大小；
    行内暂停/继续/取消、边下边播（仅符合条件的运行中任务）
  - 选中行 → 日志面板显示该任务日志；默认跟随最新运行中任务
  - 「开始下载」与探测结果「下载选中项」均逐条入队
- 设置页 ⑦ 新增「同时下载数」（1–5，默认 2）。
- 历史页：task_id 与 canceled 徽标；failed/stopped 加「继续下载」。
- 通知：失败逐条 toast；队列清空时汇总「N 成功 / M 失败」。
- 扩展面板仅加排队位置文案。
- 进度推送沿用 ≤5 次/秒节流（按任务）；空态/主题/焦点沿用现有规范。

## 8. 配置变更

- 新增 `queue_n`（int，1–5，默认 2）：`ytdlp_core.py` DEFAULTS 加默认值与范围校验，
  属新增键，不动既有行为；config schema 无需迁移（缺省补默认）。
- 其余配置键不变。

## 9. 测试与验收

**单测**
- 新增 `tests/test_queue.py`（fake executor，不起子进程）：FIFO 准入、并发上限、
  暂停释放槽位、恢复插队首、取消清理、并发调整、stop_all、200 上限、事件发射。
- host 测试：现有单任务假设用例改为按任务断言；新增更新独占、辅助并行、
  历史 canceled/继续用例。
- server 测试：新增字段兼容、task_id、停止作用域、独立模式并发。
- 扩展冒烟 `node tests/extension_smoke.js`：容忍新增字段，补排队位置断言。

**真实入口验收**（限速本地源，`open_after=False`）
1. 3 个链接并发，进度/日志各自正确
2. 暂停 ≥5 分钟后继续，`.part`/`.aria2` 续传成功
3. 扩展连提 2 个任务被接收（并发执行或排队）而非 409；扩展「停止」不影响 GUI 任务
4. 下载中更新 yt-dlp 被拒；探测/格式表可并行
5. 单任务边下边播不回归；退出确认显示任务数

## 10. 里程碑

| 里程碑 | 内容 | 验证 |
|--------|------|------|
| M1 | 队列内核（无暂停）：Task/QueueManager/run_process + host 接入 | test_queue + host 单测 |
| M2 | 暂停/继续/取消 + 续传语义 | 真实限速源暂停续传 |
| M3 | UI 任务列表/日志切换/历史/设置项 | 真实入口 UI 复测 |
| M4 | 扩展并入同一队列 + 接口兼容 | server 测试 + 扩展冒烟 + 双任务提交 |
| M5 | 自愈/边下边播/嗅探联动回归 | 全量测试 + 真实验收清单 |

## 11. 风险与对策

- **单任务假设分布广**（host/UI/server/扩展镜像/测试）：兼容字段只增不删 +
  里程碑分段验证 + 真实入口复测。
- **续传 URL 时效**：恢复时重新走提取器刷新签名；直链过期给明确提示而非静默失败。
- **并发资源**：连接预算在 B 落地；A 阶段并发上限 5 + 每任务 aria2c 参数沿用现状。
- **回滚**：队列内核独立成模块，按里程碑提交（提交需用户明确要求），可逐段回退。

## 12. 决策记录

- 2026-11-26：内存队列；默认并发 2 可调 1–5；逐链接任务；共享 queue_core（方案 2）；
  探测类并行、更新独占；暂停保留 `.part`、取消确认后清理；设计六节均获确认。