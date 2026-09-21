@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" cli.py %*
    exit /b %errorlevel%
)
where py >nul 2>nul && (
    py -3 cli.py %*
    exit /b %errorlevel%
)
where python >nul 2>nul && (
    python cli.py %*
    exit /b %errorlevel%
)

echo [错误] 未找到 Python，请先安装 Python 3.8 或更高版本。
pause
