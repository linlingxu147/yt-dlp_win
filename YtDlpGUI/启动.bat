@echo off
chcp 65001 >nul
setlocal
title 鱿鱼浏览器工具包
cd /d "%~dp0"

rem Python 探测顺序：便携虚拟环境 → py 启动器 → PATH → python.org 的常见安装目录。
rem 后两者很要紧：安装器默认不勾「Add to PATH」，只靠 PATH 会直接报「没有找到 Python」；
rem 而 Windows 应用商店那份 python.exe 是「跳商店」的占位程序，所以优先用 py 启动器绕开它。
set "PYW="
set "PYC="
rem 变量值里自带引号（路径带空格/中文时才安全），py -3 这类命令不加引号
for %%D in ("%~dp0..\.venv" "%~dp0.venv") do (
    if not defined PYC if exist "%%~D\Scripts\python.exe" set PYC="%%~D\Scripts\python.exe"
    if not defined PYW if exist "%%~D\Scripts\pythonw.exe" set PYW="%%~D\Scripts\pythonw.exe"
)
where py.exe >nul 2>nul && set "PYC=py -3"
where pyw.exe >nul 2>nul && set "PYW=pyw -3"
rem PATH 里的 python.exe 先真跑一次：占位程序会失败，失败就别用
if not defined PYC (
    python.exe -c "import sys" >nul 2>nul && set "PYC=python.exe"
)
if not defined PYW (
    where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
)
if not defined PYC (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
        if not defined PYC if exist "%%~D\python.exe" set PYC="%%~D\python.exe"
        if not defined PYW if exist "%%~D\pythonw.exe" set PYW="%%~D\pythonw.exe"
    )
)
if not defined PYW if not defined PYC goto nopy

rem 运行目录缺失时先说清楚：拷了源码但没带 runtime 是最常见的「双击没反应」
if not exist "%~dp0..\runtime\yt-dlp.exe" (
    echo [提示] 缺少 runtime\yt-dlp.exe，下载功能不可用。
    echo        便携包请完整解压（runtime\ 与源码同级）；只拿了源码请按 README「快速开始」放置运行文件。
)

rem 自动安装 / 升级 pywebview 6+（内嵌浏览器依赖 6.x 的公开事件与 WebView2 接口）
if defined PYC (
    %PYC% -c "import importlib.metadata as m; raise SystemExit(0 if int(m.version('pywebview').split('.')[0]) >= 6 else 1)" >nul 2>nul
    if errorlevel 1 (
        echo 正在安装 / 升级 pywebview 6+…
        %PYC% -m pip install -U "pywebview>=6"
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
echo [错误] pywebview 安装失败。请手动执行：py -3 -m pip install -U "pywebview>=6"
echo.
pause
exit /b 1