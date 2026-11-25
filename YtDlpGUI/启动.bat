@echo off
chcp 65001 >nul
setlocal
title yt-dlp GUI
cd /d "%~dp0"

rem 依次探测可用的 Python：不再写死 E:\python 这类个人机器上的路径。
rem 桌面版需要 tkinter，所以优先用带窗口的启动器（pyw/pythonw），避免多一个黑框。
set "PYW="
set "PYC="
where pyw.exe >nul 2>nul && set "PYW=pyw -3"
if not defined PYW (
    where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
)
where python.exe >nul 2>nul && set "PYC=python.exe"
if not defined PYC (
    where py.exe >nul 2>nul && set "PYC=py -3"
)

if not defined PYW if not defined PYC goto nopy

rem 先用控制台版探一次 tkinter（系统 Python 常常不带，探不到就别让用户对着闪退猜）
if defined PYC (
    %PYC% -c "import tkinter" >nul 2>nul
    if errorlevel 1 goto notk
)

if defined PYW (
    start "" %PYW% "%~dp0main.py" %*
) else (
    start "" %PYC% "%~dp0main.py" %*
)
exit /b 0

:nopy
echo.
echo [错误] 没有找到 Python。
echo        请安装 Python 3.9 或更新版本（https://www.python.org/downloads/），
echo        安装时勾选 "Add python.exe to PATH"，然后再双击本文件。
echo.
pause
exit /b 1

:notk
echo.
echo [错误] 找到的 Python 缺少 tkinter（桌面界面库），无法启动本窗口程序。
echo        常见原因：使用了精简版/嵌入式 Python，或安装时没有勾选 tcl/tk。
echo        替代方案：改用 YtDlpWeb\启动.bat（网页版，不需要 tkinter）。
echo.
pause
exit /b 1
