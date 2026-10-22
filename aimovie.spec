# -*- mode: python ; coding: utf-8 -*-
"""Windows 免安装目录构建配置（PyInstaller onedir）。

构建：
    .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean aimovie.spec

产物：dist/AIMovieFactory/AIMovieFactory.exe
FFmpeg/ffprobe 不随包分发，需系统 PATH 或设置中配置。
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve()

datas = []
binaries = []
hiddenimports = []

for package in ("faster_whisper", "ctranslate2", "edge_tts", "scenedetect", "piper"):
    try:
        datas += collect_data_files(package)
        binaries += collect_dynamic_libs(package)
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

hiddenimports += [
    "av",
    "tokenizers",
    "huggingface_hub",
    "onnxruntime",
    "numpy",
    "cv2",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

analysis = Analysis(
    [str(ROOT / "scripts" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "matplotlib", "PySide6.QtWebEngineCore"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="AIMovieFactory",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="AIMovieFactory",
)
