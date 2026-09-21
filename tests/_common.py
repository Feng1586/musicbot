"""测试脚本的共用小工具。

存在的理由只有一个：**别让测试去 `import utils`**。

本项目（musicbot）的顶层包叫 `utils`，而同一个工作区里还并存着旧项目
`musicdl/`，它也有一个顶层 `utils` 包（版本号是旧机器人的号）。
`sys.path` 里只要出现过 `musicdl/` 的根目录（S1 为了让旧项目的 SDK 参与
交叉验证就会临时插进去），第一次 `import utils` 就可能解析到旧项目去。

这个坑实际踩过一次：S1 的版本断言拿到 1.2.0，看上去像"服务端版本不对"，
其实是测试自己导错了包，白查了一轮。读文件不会受 `sys.path` 影响。
"""

from __future__ import annotations

import os
import re

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

_VERSION_RE = re.compile(r"""__version__\s*=\s*['"]([^'"]+)['"]""")


def read_version(root: str | None = None) -> str:
    """从 `utils/version.py` 里读出版本号（不 import 任何项目模块）。"""
    path = os.path.join(root or PROJECT_ROOT, 'utils', 'version.py')
    try:
        with open(path, encoding='utf-8') as fp:
            match = _VERSION_RE.search(fp.read())
    except OSError:
        return '0.0.0'
    return match.group(1) if match else '0.0.0'


def drop_from_sys_path(path: str) -> int:
    """把某个目录从 `sys.path` 里彻底移掉（可能被插了多次）。

    返回移掉的次数。用在"临时借用别的项目做交叉验证"之后收尾。
    """
    import sys

    removed = 0
    while path in sys.path:
        sys.path.remove(path)
        removed += 1
    return removed
