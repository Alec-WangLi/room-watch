"""PyInstaller 的入口脚本（不是给人直接运行的）。

为什么不直接把 roomwatch/gui.py 交给 PyInstaller：
gui.py 用的是包内相对导入（`from . import __version__`），被当成
顶层脚本执行时会报 "attempted relative import with no known parent package"。
所以用一个两行的启动器，让 PyInstaller 以「包」的方式分析依赖。

打包命令见 scripts/build-exe.ps1。
"""

from __future__ import annotations

import sys

from roomwatch.gui import main

if __name__ == "__main__":
    sys.exit(main())
