"""启动界面、自动截图并退出，用于验收第一阶段 UI。

默认使用 Windows 原生平台（能看到真实中文字体），窗口只显示约 2 秒。
如无桌面环境，可设置 QT_QPA_PLATFORM=offscreen（中文字体可能缺失）。

用法：
    .venv\\Scripts\\python.exe scripts\\capture_ui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aimovie.config import load_config  # noqa: E402
from aimovie.db import ProjectDB  # noqa: E402
from aimovie.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    config = load_config()
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    window = MainWindow(config, db)
    window.resize(1320, 840)
    window.show()
    window.refresh_projects()

    output_dir = ROOT / "docs"
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [
        (0, output_dir / "screenshot_phase1.png"),
        (1, output_dir / "screenshot_dialogues.png"),
        (2, output_dir / "screenshot_story.png"),
        (3, output_dir / "screenshot_script.png"),
        (4, output_dir / "screenshot_tts.png"),
        (5, output_dir / "screenshot_matching.png"),
        (6, output_dir / "screenshot_export.png"),
        (8, output_dir / "screenshot_cost.png"),
    ]

    def capture() -> None:
        for index, path in targets:
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(str(path))
            print(f"saved {path} ({path.stat().st_size} bytes)")
        window.close()
        app.quit()

    QTimer.singleShot(2000, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
