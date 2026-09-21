"""日志。

两个要点：

1. logger 名字用 `musicbot`，**不要用 `musicdl`**。
   旧项目踩过：logger 名与第三方 `musicdl` 包重名，经 root logger 又输出一次，
   结果同一行日志在容器里打印两遍（一遍带毫秒）。
2. 一律 `propagate = False`，避免被 root logger 再输出一次。
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

LOGGER_NAME = 'musicbot'

_FORMAT_CONSOLE = '%(asctime)s - %(levelname)s - %(message)s'
_FORMAT_FILE = '%(asctime)s,%(msecs)03d - %(name)s - %(levelname)s - %(message)s'
_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

_configured = False


def setup(level: str = 'INFO', log_file: Optional[str] = None) -> logging.Logger:
    """初始化日志（可重复调用，只生效一次）。"""
    global _configured

    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False            # 关键：别让 root logger 再打一遍

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FORMAT_CONSOLE, datefmt=_DATE_FORMAT))
    logger.addHandler(console)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(
                logging.Formatter(_FORMAT_FILE, datefmt=_DATE_FORMAT))
            logger.addHandler(file_handler)
        except OSError as e:            # 日志文件写不了不该影响服务启动
            logger.warning('无法写入日志文件 %s：%s', log_file, e)

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    """取 logger（未初始化时自动补一个默认的）。"""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        setup()
    return logger


logger = get_logger()
