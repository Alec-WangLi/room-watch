"""`python -m roomwatch` 的入口，等价于 `rw`。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
