# runtime 运行目录

便携运行文件统一放在这里，**均不进仓库**（`.gitignore` 已排除）：

- `yt-dlp.exe` + `_internal/`：yt-dlp 上游发行版（onedir 打包，二者必须放在一起）
- `ffmpeg.exe` / `ffprobe.exe`：合并、转码、截帧、时长探测
- `downloads/`：默认下载目录

源码目录只保留代码、测试与文档。启动方式不变：

- 桌面版：`YtDlpGUI\启动.bat`
- 独立扩展服务：`YtDlpWeb\启动.bat`

查找顺序：`runtime/` → 项目根目录（兼容旧布局）→ PATH。