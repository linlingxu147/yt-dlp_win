@echo off
chcp 65001 >nul
setlocal
title 鱿鱼浏览器工具包 · 扩展服务
cd /d "%~dp0"

rem 依次探测可用的 Python：不再写死 E:\python 这类个人机器上的路径。
set "PY="
where python.exe >nul 2>nul && set "PY=python.exe"
if not defined PY (
    where py.exe >nul 2>nul && set "PY=py -3"
)

if not defined PY goto nopy

rem 本服务无网页界面：只给浏览器扩展用。日常请开桌面版 YtDlpGUI\启动.bat（自带本服务）。
rem 这里不隐藏窗口，方便看「服务地址」和错误信息。额外参数（例如 --port 8770）原样转发。
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
