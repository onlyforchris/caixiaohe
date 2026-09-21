@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 财小盒

if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\pythonw.exe" desktop.py
    exit /b 0
)
where py >nul 2>nul && (
    start "" py -3w desktop.py
    exit /b 0
)
where python >nul 2>nul && (
    start "" pythonw desktop.py
    exit /b 0
)

echo [错误] 未找到 Python，请先安装 Python 3.8 或更高版本。
echo 下载地址：https://www.python.org/downloads/windows/
pause
