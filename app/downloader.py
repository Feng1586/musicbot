"""下载队列：单 worker、FIFO、入队即返回。

三条不变量（旧项目实测踩出来的，别改坏）：
1. **先发回执、再入队** —— worker 一拿到任务就立刻推「开始下载」，
   先入队会让用户先看到「开始下载」再看到「已加入下载队列」，顺序反了。
   （回执在 commands.py 里发，它算好数字后再 submit。）
2. **入队时快照** —— 任务里存的是 SongInfo 对象引用与目标路径，
   不是「记个编号、下载时再回查搜索结果」。否则用户入队后紧接着再搜一次
   （这正是要支持的行为），编号表被覆盖，排队的歌就找不到了。
3. **`pending()` 要算上正在下载的那条** —— `queue.qsize()` 在 worker `get()`
   之后即归零，只用它会让「下载进行中」看起来像「队列已空」。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app import notices
from app.config import settings
from app.sources import MusicEngine, MusicEngineError, SOURCE_META
from app.wecom import send_text
from utils.logger import logger

WORKER_COUNT = 1


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


@dataclass
class DownloadJob:
    song: Any                     # musicdl 的 SongInfo 对象
    title: str                    # 「歌名-歌手」，用于消息
    target_path: str              # 期望落盘路径
    from_user: str                # 回消息给谁
    source_id: str = ''
    queued_at: float = 0.0
    extra: dict = field(default_factory=dict)


class DownloadQueue:
    def __init__(self, engine: MusicEngine, worker_count: int = WORKER_COUNT) -> None:
        self._engine = engine
        self._queue: queue.Queue[DownloadJob] = queue.Queue()
        self._worker_count = max(int(worker_count), 1)
        self._lock = threading.Lock()
        self._started = False
        self._inflight: list[str] = []      # 正在下载的（含标题，供 /queue 显示）

    # -- 对外接口 ---------------------------------------------------------

    def submit(self, job: DownloadJob) -> None:
        self._ensure_started()
        self._queue.put(job)
        logger.info('已入队：%s（当前排队 %d 条）', job.title, self.pending())

    def pending(self) -> int:
        """还没处理完的任务数（含正在下载的）。"""
        with self._lock:
            return self._queue.qsize() + len(self._inflight)

    def snapshot(self) -> tuple[str, list[str]]:
        """返回 (正在下载的标题, 排队中的标题列表)。"""
        with self._lock:
            inflight = self._inflight[0] if self._inflight else ''
        waiting: list[str] = []
        with self._queue.mutex:
            for job in list(self._queue.queue):
                waiting.append(job.title)
        return inflight, waiting

    def cancel_pending(self) -> int:
        """清空**还没开始**的任务，返回清掉的数量。正在下载的不受影响。"""
        removed = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            removed += 1
            logger.info('已取消排队中的任务：%s', job.title)
        return removed

    # -- 内部 -------------------------------------------------------------

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            for index in range(self._worker_count):
                threading.Thread(target=self._worker, daemon=True,
                                 name=f'musicbot-download-{index + 1}').start()
            self._started = True
            logger.info('下载队列已启动，worker 数 %d', self._worker_count)

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            with self._lock:
                self._inflight.append(job.title)
            try:
                self._run_job(job)
            except Exception as e:
                # _run_job 内部已处理常规异常，走到这里说明是意料之外的问题，
                # 但绝不能让 worker 线程死掉，否则后面所有任务都没人处理。
                logger.error('下载任务异常：%s', e, exc_info=True)
            finally:
                with self._lock:
                    if job.title in self._inflight:
                        self._inflight.remove(job.title)
                self._queue.task_done()

    def _run_job(self, job: DownloadJob) -> None:
        send_text(notices.start_download_text(job.title), job.from_user)

        try:
            path, size = self._engine.download(job.song, job.target_path)
        except MusicEngineError as e:
            logger.error('下载失败 %s：%s', job.title, e)
            send_text(notices.download_failed_text(job.title, str(e)), job.from_user)
            return
        except Exception as e:
            logger.error('下载异常 %s：%s', job.title, e, exc_info=True)
            send_text(notices.download_failed_text(job.title, f'未预期的错误：{e}'),
                      job.from_user)
            return

        send_text(notices.download_done_text(os.path.basename(path), format_size(size)),
                  job.from_user)


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


# 全局单例：queue 需要引擎，引擎由 sources 持有
download_queue: Optional[DownloadQueue] = None


def init_queue() -> tuple[MusicEngine, DownloadQueue]:
    """启动时装配：初始化引擎 + 建队列。"""
    global download_queue
    from app.sources import init_engine
    music_engine = init_engine('启动初始化')
    download_queue = DownloadQueue(music_engine)
    return music_engine, download_queue


def get_queue() -> DownloadQueue:
    if download_queue is None:
        raise RuntimeError('下载队列尚未初始化')
    return download_queue
