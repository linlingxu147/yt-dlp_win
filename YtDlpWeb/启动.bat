@echo off
chcp 65001 >nul
setlocal
title yt-dlp Web UI
cd /d "%~dp0"

rem 依次探测可用的 Python：不再写死 E:\python 这类个人机器上的路径。
set "PY="
where python.exe >nul 2>nul && set "PY=python.exe"
if not defined PY (
    where py.exe >nul 2>nul && set "PY=py -3"
)

if not defined PY goto nopy

rem 网页版是控制台程序：这里不隐藏窗口，方便看「访问地址」和错误信息。
rem 额外参数（例如 --port 8770、--no-browser）会原样转发给 server.py。
%PY% server.py %*
echo.
echo Service stopped.
pause
exit /b 0

:nopy
echo.
echo [错误] 没有找到 Python。
echo        请安装 Python 3.9 或更新版本（https://www.python.org/downloads/），
echo        安装时勾选 "Add python.exe to PATH"，然后再双击本文件。
echo.
pause
exit /b 1
