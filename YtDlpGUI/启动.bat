@echo off
chcp 65001 >nul
setlocal
title yt-dlp GUI
cd /d "%~dp0"

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

rem 首次自动安装 pywebview
if defined PYC (
    %PYC% -c "import webview" >nul 2>nul
    if errorlevel 1 (
        echo 首次运行：正在安装 pywebview…
        %PYC% -m pip install pywebview
        if errorlevel 1 goto nopkg
    )
)

rem WebView2 运行时预判（Evergreen 安装登记在此键）
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 WebView2 运行时。若窗口无法打开，请安装：
    echo        https://developer.microsoft.com/microsoft-edge/webview2/
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

:nopkg
echo.
echo [错误] pywebview 安装失败。请手动执行：py -3 -m pip install pywebview
echo.
pause
exit /b 1