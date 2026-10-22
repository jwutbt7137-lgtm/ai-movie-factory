@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
set "PYTHONUTF8=1"
if exist "%ROOT%.venv\Scripts\python.exe" (
    "%ROOT%.venv\Scripts\python.exe" -m aimovie.cli %*
) else (
    echo [错误] 未找到 .venv，请先运行 scripts\setup_windows.cmd 安装依赖。
    exit /b 1
)
endlocal
