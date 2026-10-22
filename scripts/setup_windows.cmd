@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo ============================================================
echo   AI电影解说工厂 - Windows 环境安装
echo ============================================================

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [警告] 未在 PATH 中找到 ffmpeg。请安装 FFmpeg 并加入 PATH，
    echo        或在程序“设置”中填写 ffmpeg.exe / ffprobe.exe 的完整路径。
) else (
    for /f "delims=" %%i in ('where ffmpeg') do (
        echo [信息] FFmpeg: %%i
        goto :ffmpeg_done
    )
)
:ffmpeg_done

if exist ".venv\Scripts\python.exe" (
    echo [信息] 已存在虚拟环境 .venv，跳过创建。
) else (
    py -3.13 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        echo [信息] 使用 Python 3.13 创建虚拟环境...
        py -3.13 -m venv .venv
    ) else (
        echo [信息] 使用默认 Python 创建虚拟环境...
        python -m venv .venv
    )
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.11 或更高版本。
        exit /b 1
    )
)

echo [信息] 安装依赖（首次安装约需数分钟）...
rem 不使用 pip HTTP 缓存：部分杀毒软件会让缓存临时文件写入长期阻塞
".venv\Scripts\python.exe" -m pip install --no-cache-dir --upgrade pip
".venv\Scripts\python.exe" -m pip install --no-cache-dir -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    exit /b 1
)

echo.
echo [完成] 安装结束。
echo   启动界面：run.cmd
echo   命令行：  run-cli.cmd --help
echo   运行测试：".venv\Scripts\python.exe" -m pytest -q
endlocal
