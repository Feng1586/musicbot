"""新歌首发：每天查新 + 自动严格匹配下载。

工作方式
--------------------------------
1. 每 TICK 秒醒一次，若「当前整点 == 用户设定的小时」且「今天还没跑过」就跑一轮；
2. 一轮 = 取各已启用栏目的新歌列表 → **跨栏目按 mid 去重** → 与本地缓存的
   「已见过的歌」做差集 → 差集就是新歌；
3. 新歌按用户设定的数量（0 = 全部）生成**严格匹配**下载任务，交给统一任务队列。

为什么必须缓存历史
--------------------------------
各栏目歌曲数量不同、更新节奏也不固定（可能一首一首来，也可能一批一批来），
只有把「见过的」记下来做差集，才能判断哪些是**新增**的。

首次运行只建基线（用户确认过）
--------------------------------
缓存为空时，当前所有歌都是「没见过」，若直接当成新歌会一次性排队上百首 ——
而它们并不是新歌。所以首次只把当前歌曲**登记为已见过**（基线），不下载；
从第二次开始才下真正的增量。想立刻验证链路可以用 `/new test [N]`。

状态文件 `data/newsong.json`
--------------------------------
    {
      "settings": {"enabled": ..., "tabs": [...], "count": 0, "hour": 8,
                   "owner": "<企微 userid>", "last_date": "YYYY-MM-DD",
                   "last_run": <时间戳>},
      "seen": ["<mid>", ...]
    }
设置放在这里而不是 `data/settings.json`：这一块是自成一体的状态（设置 + 缓存），
单独一个文件读写更简单，也避免和 config 的覆盖项读写互相抢。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app import notices, qq_web
from app.config import settings
from app.pipeline import KIND_STRICT_BLIND, Task, get_queue
from app.wecom import send_text
from utils.logger import logger

STATE_FILENAME = 'newsong.json'
# 巡检线程的唤醒间隔。设为 30 秒是为了「到点就跑」的误差不超过 30 秒，
# 开销可以忽略（只是比较一个整数）。
TICK_SECONDS = 30
# 缓存里最多保留多少条 mid（按插入顺序裁剪最旧的）
MAX_SEEN = 20000

_lock = threading.RLock()


# --- 状态读写 -----------------------------------------------------------------

def _state_path() -> str:
    return os.path.join(settings.data_dir, STATE_FILENAME)


def _default_settings() -> dict[str, Any]:
    return {
        'enabled': False,
        # 默认 6 个栏目全开：总开关一开就有内容，不想要的栏目再单独关掉。
        'tabs': list(qq_web.DEBUT_TAB_NAMES),
        'count': 0,          # 0 = 全部
        'hour': 8,           # 每天几点跑（24 小时制整点）
        'owner': '',         # 谁开启的（通知与下载的接收人）
        'last_date': '',     # 上次跑过的日期，保证一天只跑一次
        'last_run': 0.0,
    }


def _load() -> dict[str, Any]:
    """读取状态文件，缺项用默认值补齐。"""
    try:
        with open(_state_path(), encoding='utf-8') as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged_settings = _default_settings()
    stored = data.get('settings')
    if isinstance(stored, dict):
        merged_settings.update({k: v for k, v in stored.items() if k in merged_settings})
    # 栏目只保留已知的，防止配置里塞进陌生栏目名
    merged_settings['tabs'] = [t for t in merged_settings.get('tabs') or []
                               if t in qq_web.DEBUT_TAB_NAMES]
    seen = data.get('seen')
    return {'settings': merged_settings,
            'seen': [str(x) for x in seen if x] if isinstance(seen, list) else []}


def _save(state: dict[str, Any]) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    path = _state_path()
    temp = f'{path}.tmp'
    with open(temp, 'w', encoding='utf-8') as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def get_settings() -> dict[str, Any]:
    with _lock:
        return dict(_load()['settings'])


def update_settings(**changes: Any) -> dict[str, Any]:
    with _lock:
        state = _load()
        state['settings'].update(changes)
        _save(state)
        return dict(state['settings'])


def set_tab(name: str, enabled: bool) -> list[str]:
    """打开/关闭一个栏目，返回更新后的栏目列表。"""
    with _lock:
        state = _load()
        tabs = list(state['settings'].get('tabs') or [])
        if enabled and name not in tabs:
            tabs.append(name)
        elif not enabled and name in tabs:
            tabs.remove(name)
        # 保持固定的栏目顺序，便于展示
        tabs = [t for t in qq_web.DEBUT_TAB_NAMES if t in tabs]
        state['settings']['tabs'] = tabs
        _save(state)
        return tabs


def cache_size() -> int:
    with _lock:
        return len(_load()['seen'])


def reset_cache() -> int:
    """清空历史（下次扫描会重新建立基线）。返回清掉的条数。"""
    with _lock:
        state = _load()
        removed = len(state['seen'])
        state['seen'] = []
        state['settings']['last_date'] = ''
        _save(state)
        return removed


# --- 扫描 ---------------------------------------------------------------------

@dataclass
class ScanReport:
    """一轮扫描的结果，供日志与命令回执使用。"""

    tabs: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    fetched: int = 0
    new_songs: list[Any] = field(default_factory=list)
    baseline: bool = False
    queued: int = 0
    note: str = ''

    @property
    def tab_text(self) -> str:
        return '、'.join(f'{name} {count}' for name, count in self.tabs.items()) or '无'


def scan(*, download: bool = True) -> ScanReport:
    """跑一轮查新。`download=False` 时只更新缓存、不生成下载任务。"""
    report = ScanReport()
    state = get_settings()
    tabs = [t for t in state.get('tabs') or [] if t in qq_web.DEBUT_TAB_NAMES]
    if not tabs:
        report.note = '没有启用任何栏目'
        logger.info('新歌首发：未启用任何栏目，跳过')
        return report

    counts, songs, errors = qq_web.fetch_all_debut_songs(tabs)
    report.tabs, report.errors, report.fetched = counts, errors, len(songs)
    if not songs:
        report.note = '所有栏目都没取到歌' if errors else '各栏目当前均无数据'
        logger.warning('新歌首发：本轮没取到任何歌（%s）', report.note)
        return report

    with _lock:
        snapshot = _load()
        seen = set(snapshot['seen'])
        report.baseline = not seen          # 缓存为空 → 这是第一次，只建基线
        new_songs = [song for song in songs if song.key not in seen]
        report.new_songs = new_songs

        # 登记本轮见到的全部歌曲（含新歌），并裁剪最旧的
        merged = list(snapshot['seen']) + [s.key for s in songs if s.key not in seen]
        snapshot['seen'] = merged[-MAX_SEEN:]
        snapshot['settings']['last_run'] = time.time()
        _save(snapshot)

    logger.info('新歌首发扫描完成：%s；去重后 %d 首，新歌 %d 首%s',
                report.tab_text, report.fetched, len(new_songs),
                '（首次，仅建立基线）' if report.baseline else '')

    if not download or report.baseline:
        return report

    owner = state.get('owner') or ''
    if not new_songs:
        return report
    if not owner:
        report.note = '没有设置接收人，无法自动下载'
        logger.warning('新歌首发：没有 owner，跳过自动下载')
        return report

    limit = int(state.get('count') or 0)
    targets = new_songs if limit <= 0 else new_songs[:limit]
    report.queued = enqueue_strict_downloads(
        targets, owner, notices.newsong_found_text(len(targets), len(new_songs),
                                                   report.tab_text),
        label='新歌首发')
    return report


def test_download(count: int, owner: str) -> tuple[int, list[str], str]:
    """忽略缓存，把当前各栏目的前 N 首直接走一遍严格匹配下载（**不写缓存**）。

    用途：让用户立刻验证「取歌 → 严格匹配搜索 → 下载」这条链路是否通。
    返回 (入队数量, 错误列表, 各栏目条数说明)。
    """
    tabs = [t for t in (get_settings().get('tabs') or []) if t in qq_web.DEBUT_TAB_NAMES]
    if not tabs:
        tabs = list(qq_web.DEBUT_TAB_NAMES)
    counts, songs, errors = qq_web.fetch_all_debut_songs(tabs)
    tab_text = '、'.join(f'{name} {n}' for name, n in counts.items()) or '无'
    targets = songs[:max(1, count)]
    queued = enqueue_strict_downloads(
        targets, owner, notices.newsong_test_text(len(targets), tab_text),
        label='新歌首发试下')
    return queued, errors, tab_text


def enqueue_strict_downloads(songs: list[Any], owner: str, receipt: str, *,
                             label: str = '自动下载') -> int:
    """把若干首歌按**严格匹配**入队下载。

    严格匹配是「锁定」的：一律用 `KIND_STRICT_BLIND`（只认第一个三段
    完全一致的结果，不匹配就报下载失败），不提供开关。

    ⚠️ 回执必须在任务对 worker 可见之前发出（pipeline 的不变量 2）：
    先 `reserve_position()` → 发消息 → 再 `submit(position=...)`。

    这些任务带 `silent=True`：逐首成功消息被静音，结束时由批次汇总统一交代
    （一次下几十首时否则会刷出上百条消息）。失败仍然逐条立即上报。
    """
    if not songs or not owner:
        return 0
    try:
        queue = get_queue()
    except RuntimeError:
        logger.error('新歌首发：任务队列尚未初始化，放弃本次入队')
        return 0

    batch_id = _start_batch(owner, label, len(songs))
    position = queue.reserve_position()
    send_text(receipt, owner)
    for song in songs:
        # 关键词只用「歌名 歌手」：实测把专辑拼进去反而略降召回
        # （见 songinfo.SongFields.search_keyword）。是否这首歌仍由完整三段判定。
        queue.submit(Task(kind=KIND_STRICT_BLIND, user=owner,
                          title=song.fields.display(), source='qq',
                          keyword=song.fields.search_keyword(), strict=True,
                          fields=song.fields, silent=True, batch=batch_id),
                     position=position)
        position = 0        # 只有第一条用预留位次，其余顺位递增
    logger.info('新歌首发：已入队 %d 首（严格匹配，批次 %s）', len(songs), batch_id)
    return len(songs)


# --- 批次登记（用于「静默 + 汇总」）-------------------------------------------
# 每首任务跑完（无论成败）都会 report 一次；收齐 total 次后由看护线程发汇总。
# 用「收到的结果数」而不是「队列是否为空」来判定结束，更准（用户中途可能又发别的歌）。

# 等一批跑完的最长时间。超过就先汇总（避免个别任务超时把汇总无限期拖住）。
BATCH_MAX_WAIT_SECONDS = 1800
# 汇总线程的轮询间隔
BATCH_POLL_SECONDS = 5

_batches: dict[str, dict[str, Any]] = {}
_batch_lock = threading.RLock()


def _start_batch(owner: str, label: str, total: int) -> str:
    """登记一个批次并起一个看护线程，返回批次号（失败时返回空串）。"""
    if total <= 0:
        return ''
    batch_id = uuid.uuid4().hex[:10]
    with _batch_lock:
        _batches[batch_id] = {'owner': owner, 'label': label, 'total': total,
                              'done': 0, 'failed': []}
    threading.Thread(target=_watch_batch, args=(batch_id,), daemon=True,
                     name=f'musicbot-batch-{batch_id}').start()
    return batch_id


def record_result(batch_id: str, title: str, ok: bool, reason: str = '') -> None:
    """登记一首歌的结果（由 downloader 与 commands 在跑完一首后调用）。"""
    if not batch_id:
        return
    with _batch_lock:
        info = _batches.get(batch_id)
        if info is None:
            return
        info['done'] += 1
        if not ok:
            info['failed'].append(title)
            logger.info('批量下载失败：%s（%s）', title, reason or '未说明')


def _watch_batch(batch_id: str) -> None:
    """等这一批收齐结果，然后发一条汇总。"""
    deadline = time.time() + BATCH_MAX_WAIT_SECONDS
    while time.time() < deadline:
        with _batch_lock:
            info = _batches.get(batch_id)
            if info is None:
                return
            if info['done'] >= info['total']:
                break
        time.sleep(BATCH_POLL_SECONDS)

    with _batch_lock:
        info = _batches.pop(batch_id, None)
    if info is None:
        return
    total = int(info['total'])
    failed = list(info['failed'])
    ok_count = total - len(failed)
    if info['done'] < total:
        logger.warning('批次 %s 只收到 %d/%d 个结果（可能有个别任务超时），先汇总',
                       batch_id, info['done'], total)
    try:
        send_text(notices.batch_summary_text(str(info['label']), total, ok_count, failed),
                  str(info['owner']))
    except Exception as error:      # noqa: BLE001
        logger.warning('发送批量下载汇总失败：%s', error)


# --- 每日调度 -----------------------------------------------------------------

def watch_loop(stop_event: threading.Event) -> None:
    """后台巡检线程：到点跑一轮。异常一律吞掉，绝不让线程死掉。"""
    logger.info('新歌首发巡检已启动（设定整点 %d 点，30 秒检查一次）',
                int(get_settings().get('hour') or 0))
    while not stop_event.wait(TICK_SECONDS):
        try:
            _tick()
        except Exception as error:      # noqa: BLE001
            logger.error('新歌首发巡检异常：%s', error, exc_info=True)


def _tick() -> None:
    state = get_settings()
    if not state.get('enabled'):
        return
    hour = int(state.get('hour') or 0)
    now = time.localtime()
    if now.tm_hour != hour:
        return
    today = time.strftime('%Y-%m-%d')
    if state.get('last_date') == today:
        return

    # 先记日期再跑：一天最多跑一次。失败了也不会在整点内反复重试
    # （想立刻重试可以用 /new now）。
    update_settings(last_date=today)
    logger.info('新歌首发：到达设定整点 %d 点，开始查新', hour)
    report = scan(download=True)
    owner = state.get('owner') or ''
    if not owner:
        return
    if report.errors and not report.tabs:
        send_text(notices.newsong_error_text('；'.join(report.errors)), owner)
    elif report.baseline:
        send_text(notices.newsong_baseline_text(report.fetched, report.tab_text), owner)
    elif report.queued == 0:
        logger.info('新歌首发：无新增，保持安静')
