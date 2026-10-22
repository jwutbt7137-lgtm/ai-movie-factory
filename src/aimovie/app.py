"""图形界面入口。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .config import ensure_data_dirs, load_config
from .db import ProjectDB
from .logging_setup import setup_logging

LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """启动 PySide6 桌面应用。"""
    arguments = list(sys.argv if argv is None else argv)
    config = load_config()
    ensure_data_dirs(config)
    log_path = setup_logging(config.logs_dir(), config.log_level, console=True)
    LOGGER.info("启动 %s v%s，数据目录：%s", APP_NAME, __version__, config.resolved_data_dir())

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:
        print("未安装 PySide6，无法启动界面。请运行 scripts\\setup_windows.cmd 安装依赖。", file=sys.stderr)
        print(f"详细信息：{exc}", file=sys.stderr)
        return 1

    app = QApplication.instance() or QApplication(arguments)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("AIMovieFactory")
    app.setStyle("Fusion")
    try:
        from .ui.theme import DARK_STYLESHEET

        app.setStyleSheet(DARK_STYLESHEET)
    except Exception:  # noqa: BLE001
        LOGGER.exception("加载界面主题失败，使用默认样式")

    try:
        db = ProjectDB(config.resolved_data_dir() / "app.db")
        from .ui.main_window import MainWindow

        window = MainWindow(config, db, log_path=log_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("启动主窗口失败")
        QMessageBox.critical(None, "启动失败", f"无法启动应用：\n{exc}\n\n日志：{log_path}")
        return 1
    if "--smoke-test" in arguments:
        for module in ("faster_whisper", "cv2", "scenedetect", "av", "ctranslate2", "edge_tts"):
            __import__(module)
        LOGGER.info("打包/启动自检通过（GUI + Whisper/OpenCV/SceneDetect/AV/EdgeTTS）")
        window.close()
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
