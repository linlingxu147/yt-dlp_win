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

## 运行目录

便携运行文件统一放在 `runtime\`（`yt-dlp.exe`、`_internal\`、`ffmpeg.exe`、`ffprobe.exe`、
`downloads\`），源码目录不放二进制；`ytdlp_core.find_tool` / `which_tool` 按
`runtime/` → 项目根（旧布局兼容）→ PATH 查找。`runtime/` 内容不入库，只保留说明文件。

## 测试

- `py -3 -m unittest discover -s tests -v`（标准库 unittest，无第三方测试依赖）
- `node tests/extension_smoke.js`（扩展冒烟 7/7）
- 测试端口避开 8765–8775（如 `--port 8799`），避免抢占正在使用的实例。

## 提交

提交信息用中文，风格沿用仓库历史；未经用户明确要求不提交、不推送。