"""统一任务队列：搜索与下载走**同一条**流水线，单 worker 串行。

为什么把「搜索」也放进队列（2026-09-22 定案）
------------------------------------------------------------------
以前搜索是在回调线程里**同步**跑的，而 `sources.py` 那把 RLock 同时罩住了
`search()` 与 `download()`。于是用户连发 N 首歌名时：

* N 条消息确实并发起来了（每条一个后台任务），但它们全在**等同一把锁**，
  而「等锁 + 本次搜索」这一整段时间**一条反馈都发不出去** → 用户以为卡死了；
* 谁先抢到锁是不确定的 → **下载顺序 ≠ 发送顺序**。

现在入口只做两件事：**立刻回执 + 入队**；worker 一条任务完整走完再取下一个
（盲下 = 搜索 + 下载一次做完），所以顺序 = 发送顺序，而且全程可见。

为什么不拆成「待搜索队列 + 待下载队列」两个队列
------------------------------------------------------------------
两个队列必然要配两个 worker，而两个 worker 会**重新去抢同一把引擎锁**，
顺序又变随机 —— 等于白做。要么统一成一个 worker，要么拆锁；而
`sources.py` 写得清楚「musicdl 的客户端不是为并发设计的」，锁不能拆。
所以「一个队列、一个 worker」是唯一自洽的选择。

四条不变量（前三条沿用旧版已验证的结论，别改坏）
------------------------------------------------------------------
1. **入队时快照** —— 任务里存的是源、关键词、SongInfo 对象引用与目标路径。
   否则用户排队期间换源、或紧接着再搜一次，排队的任务会拿错东西
   （这正是这条队列要支持的行为）。**最容易踩的就是这个。**
2. **回执必须在任务对 worker 可见之前发出** —— 否则 worker 会先喊
   「开始下载」，用户看到的顺序就反了。见 `reserve_position()`。
3. **`pending()` 要算上正在处理的那条** —— `queue.qsize()` 在 worker `get()`
   之后即归零，只用它会让「正在处理」看起来像「队列已空」。
4. **worker 绝不能死** —— 任何异常都要吞掉并继续，否则后面所有任务都没人处理。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app import notices
from app.config import settings
from app.sources import MusicEngine
from app.wecom import send_text
from utils.logger import logger

KIND_SEARCH = 'search'        # 普通搜索：搜完回编号列表，等用户回数字
KIND_BLIND = 'blind'          # 盲下：搜完取第 1 首直接下载
KIND_DOWNLOAD = 'download'    # 用户已选定某首，只下载

KIND_LABELS = {KIND_SEARCH: '搜索', KIND_BLIND: '盲下', KIND_DOWNLOAD: '下载'}

# /queue 里最多列几条排队中的任务
MAX_WAITING_SHOWN = 15


@dataclass
class Task:
    kind: str
    user: str
    title: str                    # 回执与队列展示用（搜索=关键词，下载=歌名-歌手）
    source: str = ''              # ← 入队时快照
    keyword: str = ''
    song: Any = None              # musicdl 的 SongInfo 对象引用
    target_path: str = ''
    queued_at: float = 0.0
    seq: int = 0

    @property
    def label(self) -> str:
        return f'[{KIND_LABELS.get(self.kind, self.kind)}] {self.title}'


class TaskQueue:
    def __init__(self, engine: MusicEngine,
                 executor: Optional[Callable[[Task], None]] = None) -> None:
        self._engine = engine
        self._executor = executor          # 由 commands 提供；不传则惰性解析
        self._queue: queue.Queue[Task] = queue.Queue()
        self._lock = threading.RLock()
        self._started = False
        self._inflight: Optional[Task] = None
        self._reserved = 0                 # 已发回执、还没入队的位次预留数
        self._seq = 0
        self._timeouts = 0
        self._done = 0

    # -- 对外接口 ---------------------------------------------------------

    def reserve_position(self) -> int:
        """占一个位次，供「先发回执、后入队」使用（不变量 2）。

        发消息是网络 I/O，不能抓着队列锁做；但位次又必须准（连发的几条消息
        是并发进来的，各自算一次 `pending()` 会算出同一个数字）。所以先把
        位次占住，等回执发完再 `submit(task, position=...)`。
        """
        with self._lock:
            self._reserved += 1
            return (self._queue.qsize()
                    + (1 if self._inflight is not None else 0)
                    + self._reserved)

    def submit(self, task: Task, *, position: int = 0) -> int:
        """入队，返回位次（1 = 下一个就轮到它）。"""
        self._ensure_started()
        with self._lock:
            self._seq += 1
            task.seq = self._seq
            task.queued_at = time.time()
            if position > 0:
                self._reserved = max(0, self._reserved - 1)
            else:
                position = (self._queue.qsize()
                            + (1 if self._inflight is not None else 0)
                            + self._reserved + 1)
            self._queue.put(task)
            depth = self._queue.qsize() + (1 if self._inflight is not None else 0)
        logger.info('已入队[%s] 第 %d 位：%s（待处理 %d 条）',
                    KIND_LABELS.get(task.kind, task.kind), position, task.title, depth)
        return position

    def pending(self) -> int:
        """还没处理完的任务数（含正在处理的那条，不变量 3）。"""
        with self._lock:
            return self._queue.qsize() + (1 if self._inflight is not None else 0)

    def snapshot(self) -> tuple[str, list[str]]:
        """返回 (正在处理的标签, 排队中的标签列表)。"""
        with self._lock:
            inflight = self._inflight.label if self._inflight is not None else ''
        waiting: list[str] = []
        with self._queue.mutex:
            for task in list(self._queue.queue):
                waiting.append(task.label)
        return inflight, waiting

    def cancel_pending(self) -> int:
        """清空**还没开始**的任务，返回清掉的数量。正在处理的不受影响。"""
        removed = 0
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            removed += 1
            logger.info('已取消排队中的任务：%s', task.label)
        return removed

    def has_pending_search(self, user: str) -> bool:
        """该用户是否还有没跑完的搜索（含盲下，它们也要先搜）。

        用来回答「用户回数字、但结果还没搜出来」这种情况 —— 此时应该提示
        「还在排队搜索中」，而不是误导性的「请先发送歌曲名称搜索」。
        """
        kinds = (KIND_SEARCH, KIND_BLIND)
        with self._lock:
            current = self._inflight
            if current is not None and current.user == user and current.kind in kinds:
                return True
        with self._queue.mutex:
            for task in self._queue.queue:
                if task.user == user and task.kind in kinds:
                    return True
        return False

    def stats(self) -> str:
        with self._lock:
            return (f'待处理 {self._queue.qsize()} 条 / 已完成 {self._done} 条'
                    f' / 超时放弃 {self._timeouts} 次')

    # -- 内部 -------------------------------------------------------------

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            threading.Thread(target=self._worker, daemon=True,
                             name='musicbot-task-worker').start()
            self._started = True
            logger.info('任务队列已启动（单 worker，串行处理）')

    def _resolve_executor(self) -> Callable[[Task], None]:
        if self._executor is None:
            # 惰性导入：commands 会 import 本模块，模块级互相 import 会成环
            from app import commands
            self._executor = commands.execute_task
        return self._executor

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            with self._lock:
                self._inflight = task
            try:
                self._run_once(task)
            except Exception as e:          # 不变量 4：绝不让 worker 死掉
                logger.error('任务处理异常：%s', e, exc_info=True)
            finally:
                with self._lock:
                    self._inflight = None
                    self._done += 1
                self._queue.task_done()
                self._sleep_interval()

    def _run_once(self, task: Task) -> None:
        """执行一条任务，并施加单任务超时（看门狗）。

        超时计量的是**整条任务**（盲下 = 搜索 + 下载一起算），不是分段计时。
        之所以不做分段：真卡住的时候你分不清是卡在搜索还是卡在下载，而且
        分段会让实现复杂一倍、收益很小。

        ⚠️ Python 没法强杀一个线程，所以超时是**软**的：到点后我们不再等它、
        直接处理下一个任务并告知用户。被放弃的那条若仍在跑，可能还占着引擎锁，
        因此下一个任务仍可能要等它收尾 —— 真卡死时的兜底手段是 `/restart`。
        """
        timeout = _timeout_seconds()
        done = threading.Event()
        box: dict[str, BaseException] = {}

        def _body() -> None:
            try:
                self._resolve_executor()(task)
            except BaseException as e:      # noqa: BLE001 —— 兜底，别让线程静默死掉
                box['error'] = e
            finally:
                done.set()

        started = time.time()
        threading.Thread(target=_body, daemon=True,
                         name=f'musicbot-task-{task.seq}').start()

        if not done.wait(timeout):
            self._timeouts += 1
            logger.error('任务超时（超过 %d 分钟），已放弃等待并处理下一个：%s',
                         settings.task_timeout_minutes, task.label)
            send_text(notices.task_timeout_text(
                task.title, int(settings.task_timeout_minutes)), task.user)
            return

        error = box.get('error')
        if error is not None:
            logger.error('任务异常：%s', error, exc_info=error)
            send_text(notices.task_failed_text(task.title, f'未预期的错误：{error}'),
                      task.user)
            return

        logger.info('任务完成[%s]：%s（耗时 %.2fs）',
                    KIND_LABELS.get(task.kind, task.kind), task.title,
                    time.time() - started)

    def _sleep_interval(self) -> None:
        """两条任务之间的间隔（用户可用 /interval 调，立即采用新值）。"""
        target = _current_interval()
        if target <= 0:
            return
        deadline = time.time() + target
        while time.time() < deadline:
            time.sleep(0.2)
            if _current_interval() != target:
                return          # 用户改了设置，不必等完剩下的时间


def _current_interval() -> int:
    """实时读取间隔（不用构造时缓存的值，否则 /interval 要等下一轮才生效）。"""
    try:
        return max(0, int(settings.task_interval_seconds))
    except (TypeError, ValueError):
        return 0


def _timeout_seconds() -> float:
    """实时读取单任务超时（秒）。单独成函数是为了让测试能把它调小。"""
    try:
        return max(1, int(settings.task_timeout_minutes)) * 60
    except (TypeError, ValueError):
        return 300


# --- 全局单例 ---------------------------------------------------------------
# 队列需要引擎；引擎也由 sources 持有。由 main.py 在启动时装配。

task_queue: Optional[TaskQueue] = None


def init_queue(executor: Optional[Callable[[Task], None]] = None
               ) -> tuple[MusicEngine, TaskQueue]:
    global task_queue
    from app.sources import init_engine
    music_engine = init_engine('启动初始化')
    task_queue = TaskQueue(music_engine, executor)
    return music_engine, task_queue


def get_queue() -> TaskQueue:
    if task_queue is None:
        raise RuntimeError('任务队列尚未初始化')
    return task_queue
