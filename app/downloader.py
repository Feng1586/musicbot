"""下载执行与落盘路径。

任务队列（搜索 + 下载的调度）已经搬到 `app/pipeline.py`；这里只剩两件事：
1. 把**一首**歌下下来并把结果告诉用户（`download_and_report`）；
2. 拼落盘路径（`build_target_path`）。

为什么要单独一个函数而不是塞进 worker：worker 需要「一条任务完整走完、
绝不让异常冒出去」的保证（见 pipeline 的不变量 4），所以下载的异常在
这里就地消化、转成给用户的消息。
"""

from __future__ import annotations

import os
import time
from typing import Any

from app import notices
from app.config import settings
from app.sources import SOURCE_META, MusicEngineError, engine
from app.wecom import send_text
from utils.logger import logger


def format_size(size: int) -> str:
    """人类可读的文件大小（与旧项目一致，用 1024 进制）。"""
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            if unit == 'B':
                return f'{int(value)} B'
            return f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} GB'


def download_and_report(song: Any, title: str, target_path: str, user: str, *,
                        silent: bool = False, batch: str = '') -> None:
    """下载一首歌并回报用户。

    **失败也回消息、绝不抛异常** —— 队列 worker 靠这个保证流水线不会停住。

    `silent=True`（批量自动下载）时**成功不再逐条发消息**，只登记到批次里，
    由批次汇总统一回报；失败仍然立即上报 —— 那是要看的东西，不该被静音。
    """
    try:
        path, size = engine().download(song, target_path)
    except MusicEngineError as e:
        logger.error('下载失败 %s：%s', title, e)
        _record_result(batch, title, False, str(e))
        send_text(notices.download_failed_text(title, str(e)), user)
        return
    except Exception as e:
        logger.error('下载异常 %s：%s', title, e, exc_info=True)
        _record_result(batch, title, False, f'未预期的错误：{e}')
        send_text(notices.download_failed_text(title, f'未预期的错误：{e}'), user)
        return

    _record_result(batch, title, True)
    if silent:
        return
    send_text(notices.download_done_text(os.path.basename(path), format_size(size)), user)


def _record_result(batch: str, title: str, ok: bool, reason: str = '') -> None:
    """把一首歌的结果登记到所属批次（没批次号就什么都不做）。"""
    if not batch:
        return
    try:
        # 惰性导入：newsong 会 import pipeline，模块级互相 import 会成环
        from app import newsong
        newsong.record_result(batch, title, ok, reason)
    except Exception as e:      # noqa: BLE001 - 登记失败不该影响下载主流程
        logger.debug('登记批量下载结果失败：%s', e)


def build_target_path(source_id: str, keyword: str, song_name: str,
                      singers: str, ext: str) -> str:
    """落盘路径：downloads/{源客户端}/{时间戳 关键词}/{歌名} - {歌手}.{ext}

    沿用旧项目在客户磁盘上已经形成的目录习惯，方便他们按批次翻找。
    """
    meta = SOURCE_META.get(source_id) or {'client': source_id}
    batch = f'{time.strftime("%Y-%m-%d-%H-%M-%S")} {keyword}'.strip()
    directory = os.path.join(settings.download_dir, meta['client'], _safe(batch))
    filename = f'{_safe(song_name)} - {_safe(singers)}.{ext or "mp3"}'
    return os.path.join(directory, filename)


def _safe(name: str) -> str:
    """去掉 Windows/Linux 都不接受的文件名字符，并限长。"""
    text = (name or '未知').strip()
    for ch in '\\/:*?"<>|\r\n\t':
        text = text.replace(ch, '_')
    text = text.strip().rstrip('.')
    return text[:80] or '未知'
