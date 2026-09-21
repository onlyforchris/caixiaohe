@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" install.py --uninstall
    pause
    exit /b 0
)
where py >nul 2>nul && (
    py -3 install.py --uninstall
    pause
    exit /b 0
)
where python >nul 2>nul && (
    python install.py --uninstall
    pause
    exit /b 0
)

echo [错误] 未找到 Python，无法执行卸载。
pause
