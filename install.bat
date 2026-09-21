@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" install.py %*
    pause
    exit /b 0
)
where py >nul 2>nul && (
    py -3 install.py %*
    pause
    exit /b 0
)
where python >nul 2>nul && (
    python install.py %*
    pause
    exit /b 0
)

echo [错误] 未找到 Python，请先安装 Python 3.8 或更高版本。
echo 下载地址：https://www.python.org/downloads/windows/
echo 安装时请勾选 "Add python.exe to PATH"
pause
