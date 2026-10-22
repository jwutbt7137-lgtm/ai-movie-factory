"""PyInstaller 入口：只负责调用 GUI main，便于打包。"""

from aimovie.app import main

if __name__ == "__main__":
    raise SystemExit(main())
