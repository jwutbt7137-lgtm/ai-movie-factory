# 构建 Windows 免安装目录：dist/AIMovieFactory
# 前置：已运行 scripts\setup_windows.cmd，且 ffmpeg/ffprobe 在 PATH。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到 $python，请先运行 scripts\setup_windows.cmd"
}
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --no-cache-dir pyinstaller
}
& $python -m PyInstaller --noconfirm --clean aimovie.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$dist = Join-Path $root "dist\AIMovieFactory"
$launcher = @"
@echo off
set AIMOVIE_DATA_DIR=%~dp0data
start "" "%~dp0AIMovieFactory.exe"
"@
Set-Content -LiteralPath (Join-Path $dist "run.cmd") -Value $launcher -Encoding ASCII
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $dist -Force
$docs = Join-Path $root "docs"
if (Test-Path -LiteralPath $docs) {
    Copy-Item -LiteralPath $docs -Destination $dist -Recurse -Force
}
Write-Host "构建完成：$dist"
Write-Host "启动：$dist\run.cmd"
Write-Host "FFmpeg/ffprobe 需在 PATH，或在设置中指定路径。"
