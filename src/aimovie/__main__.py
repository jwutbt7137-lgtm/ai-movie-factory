"""``python -m aimovie``：默认启动界面；带 CLI 子命令时转交命令行工具。"""

from __future__ import annotations

import sys

CLI_COMMANDS = {
    "info",
    "analyze",
    "projects",
    "export",
    "story",
    "script",
    "scripts",
    "costs",
}


def main() -> int:
    argv = sys.argv[1:]
    if any(argument in CLI_COMMANDS for argument in argv[:2]):
        from .cli import main as cli_main

        return cli_main(argv)
    from .app import main as gui_main

    return gui_main(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
