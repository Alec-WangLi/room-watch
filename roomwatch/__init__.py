"""room-watch —— 房间进入抓拍与录像。

对外只暴露版本号；具体功能从各子模块导入，避免 `import roomwatch` 时
把 cv2 / ultralytics 一起拉起来（`rw status` 这类轻命令不需要它们）。

用法（推荐统一走 `rw` 入口，等价写法 `python -m roomwatch`）：

    rw start          启动监控
    rw status         看是否在线
    rw gallery        打开本地相册
    rw test           跑全部测试
    rw doctor         环境自检
"""

from __future__ import annotations

from .paths import PACKAGE_DIR

try:
    __version__ = (PACKAGE_DIR.parent / "VERSION").read_text(encoding="utf-8").strip()
except OSError:  # 打包成 exe 或源码不完整时退化，不阻塞导入
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
