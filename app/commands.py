"""指令路由：所有用户可见行为都在这里。

约定：
* 回调线程只负责「解密 → 丢进这里」，本模块内部做网络与下载，跑在后台任务里。
* 所有文案从 `app/notices.py` 取，这里只负责拼数据。
* 每个用户有独立的「当前源 / 盲模式 / 最近一次搜索结果」，互不干扰；
  但引擎与搜索缓存是进程级共享的（所以 `/limit` 是全局单值）。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app import cookies as cookie_store
from app import notices
from app import updater
from app.config import (SEARCH_LIMIT_MAX, SEARCH_LIMIT_MIN, save_runtime_override,
                        settings)
from app.downloader import DownloadJob, build_target_path, get_queue
from app.login import manager as login_manager
from app.runtime import restart
from app.sources import SOURCE_META, SOURCE_ORDER, engine
from app.wecom import send_text
from utils.logger import logger

# 纯数字 / 逗号（含中文逗号）→ 视为「下载第几首」
_INDEX_RE = re.compile(r'^[\d,，\s]+$')


@dataclass
class UserState:
    source: str = ''
    blind: bool = False
    items: list = field(default_factory=list)
    keyword: str = ''
    at: float = 0.0

    def results_fresh(self) -> bool:
        return bool(self.items) and (time.time() - self.at) <= settings.result_cache_minutes * 60


_users: dict[str, UserState] = {}
_users_lock = threading.Lock()


def user_state(user: str) -> UserState:
    with _users_lock:
        state = _users.get(user)
        if state is None:
            state = UserState(source=settings.default_source)
            _users[user] = state
        if not state.source:
            state.source = settings.default_source
        return state


def source_name(source_id: str) -> str:
    meta = SOURCE_META.get(source_id)
    return meta['name'] if meta else source_id


# --- 入口 -------------------------------------------------------------------

def handle_message(msg: dict[str, str]) -> None:
    msg_type = (msg.get('MsgType') or '').lower()
    from_user = msg.get('FromUserName') or ''
    content = (msg.get('Content') or '').strip()

    if msg_type != 'text':
        # 事件类回调（关注、进入应用等）没有 Content/MsgId；
        # 旧项目在这里抛 400 导致企微重试三次 —— 直接忽略。
        logger.info('忽略非文本消息：MsgType=%s Event=%s', msg_type, msg.get('Event', ''))
        return
    if not from_user:
        logger.warning('消息缺少 FromUserName，忽略')
        return
    if not content:
        logger.info('收到空文本消息，忽略')
        return

    logger.info('收到消息 from=%s：%s', from_user, content[:80])
    if content.startswith('/'):
        _handle_command(content, from_user)
    elif _INDEX_RE.match(content):
        _handle_indices(content, from_user)
    else:
        _handle_text(content, from_user)


def handle_error(msg: Optional[dict], error: Exception) -> None:
    logger.error('处理消息异常：%s', error, exc_info=True)
    from_user = (msg or {}).get('FromUserName') or ''
    if from_user:
        try:
            send_text(f'❌ 处理时出错：{error}', from_user)
        except Exception:
            pass


# --- 指令 -------------------------------------------------------------------

def _handle_command(raw: str, user: str) -> None:
    parts = raw[1:].split()
    if not parts:
        send_text(notices.HELP_TEXT, user)
        return

    command = parts[0].lower()
    args = parts[1:]
    state = user_state(user)

    # 源切换（也支持 /qq login 这种带子命令的写法）
    if command in SOURCE_META:
        _handle_source_command(command, args, state, user)
        return

    if command in ('help', 'h', '帮助'):
        send_text(notices.HELP_TEXT, user)
    elif command in ('version', 'v', '版本'):
        send_text(notices.version_text(), user)
    elif command in ('status', '状态'):
        send_text(_status_text(state), user)
    elif command in ('source', '源'):
        send_text(notices.SOURCE_CURRENT.format(name=source_name(state.source)), user)
    elif command in ('limit', '条数'):
        _handle_limit(args, user)
    elif command in ('blind', '盲下载'):
        _handle_blind(args, state, user)
    elif command in ('queue', '队列'):
        send_text(_queue_text(), user)
    elif command in ('cancel', '取消'):
        removed = get_queue().cancel_pending()
        send_text(notices.QUEUE_CLEARED if removed else notices.QUEUE_ALREADY_EMPTY, user)
    elif command in ('update', '更新'):
        _handle_update(args, user)
    elif command in ('restart', '重启'):
        _handle_restart(user)
    else:
        send_text(notices.unknown_command_text(raw), user)


def _handle_source_command(source: str, args: list[str], state: UserState, user: str) -> None:
    meta = SOURCE_META[source]
    if args and args[0].lower() in ('login', '登录'):
        ok, message = login_manager.start_login(
            source, user, on_success=lambda: engine().rebuild('登录成功后刷新 Cookie'))
        if not ok:
            send_text(message, user)
        return

    state.source = source
    hint = ''
    if not cookie_store.cookies_of(source):
        hint = (f'（尚未登录，搜索结果与音质受限，可发送 /{meta["cmd"]} login 扫码登录）')
    send_text(notices.source_switched_text(meta['name'], hint), user)


def _handle_limit(args: list[str], user: str) -> None:
    if not args:
        send_text(f'当前搜索条数：{settings.search_limit}'
                  f'（范围 {SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX}）\n'
                  f'修改：/limit 20', user)
        return
    try:
        value = int(args[0])
    except ValueError:
        send_text(f'请给一个 {SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX} 之间的数字，例如 /limit 20', user)
        return
    if value < SEARCH_LIMIT_MIN or value > SEARCH_LIMIT_MAX:
        send_text(f'❌ 超出范围：只能是 {SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX} 之间的整数'
                  f'（上限 50 是实测安全值，再大音乐源会返回空结果）', user)
        return

    settings.search_limit = value
    save_runtime_override('search_limit', value)
    engine().rebuild(f'/limit 调整为 {value}')
    send_text(f'✅ 搜索条数已设为 {value}（对所有源生效，立即生效）', user)


def _handle_blind(args: list[str], state: UserState, user: str) -> None:
    if not args:
        send_text(notices.BLIND_MODE_STATUS.format(
            state='已开启' if state.blind else '已关闭'), user)
        return
    value = args[0].lower()
    if value in ('on', '1', 'true', '开', '开启'):
        state.blind = True
        send_text(notices.blind_mode_text(True), user)
    elif value in ('off', '0', 'false', '关', '关闭'):
        state.blind = False
        send_text(notices.blind_mode_text(False), user)
    else:
        send_text('用法：/blind on 或 /blind off', user)


def _handle_update(args: list[str], user: str) -> None:
    action = (args[0].lower() if args else '')

    if action in ('confirm', '确定', 'yes'):
        check = updater.check()
        if check.error:
            send_text(notices.UPDATE_CHECK_FAILED.format(reason=check.error), user)
            return
        if not check.has_update:
            send_text(notices.UPDATE_NOT_AVAILABLE.format(current=check.current), user)
            return
        send_text(f'⏳ 正在更新 musicdl 到 {check.latest}，请稍候…', user)
        ok, detail = updater.apply_update(check.latest)
        if not ok:
            send_text(notices.UPDATE_CONFLICT.format(reason=detail), user)
            return
        send_text(notices.UPDATE_DONE.format(old=check.current, new=detail), user)
        return

    if action in ('rollback', '回滚'):
        ok, detail = updater.rollback()
        if not ok:
            send_text(notices.UPDATE_ROLLBACK_NONE if '没有可回滚' in detail else f'❌ {detail}', user)
        else:
            send_text(notices.UPDATE_ROLLBACK_DONE.format(version=detail), user)
        return

    check = updater.check()
    if check.error:
        send_text(f'当前 musicdl：{check.current or "未知"}\n'
                  f'⚠️ 无法检查更新：{check.error}\n\n{notices.UPDATE_RISK_TEXT}', user)
        return
    if not check.has_update:
        send_text(notices.UPDATE_NOT_AVAILABLE.format(current=check.current), user)
        return
    send_text(f'当前 musicdl：{check.current}\n最新版本：{check.latest}\n\n'
              f'{notices.UPDATE_RISK_TEXT}', user)


def _handle_restart(user: str) -> None:
    from app.runtime import docker_socket_available
    if docker_socket_available():
        send_text('🔄 正在通过 docker.sock 重启容器…', user)
    else:
        send_text(notices.RESTART_NO_SOCKET, user)
    restart()


# --- 搜索 / 下载 ------------------------------------------------------------

def _handle_text(content: str, user: str) -> None:
    """非指令文本：盲模式 → 直接下第一首；否则正常搜索。"""
    state = user_state(user)
    outcome = engine().search(state.source, content)
    if not outcome.ok:
        send_text(notices.SEARCH_FAILED, user)
        logger.warning('搜索失败：%s', outcome.error)
        return
    if not outcome.items:
        send_text(notices.SEARCH_EMPTY, user)
        return

    state.items = outcome.items
    state.keyword = content
    state.at = time.time()

    if state.blind:
        target = outcome.items[0]
        _enqueue([target], state, user, blind=True)
        return

    lines = [f'{idx}. {_title_of(song)}' for idx, song in enumerate(outcome.items, 1)]
    send_text(notices.search_result_text(
        source_name(state.source), lines, limit_hint=notices.SEARCH_HINT), user)


def _handle_indices(content: str, user: str) -> None:
    state = user_state(user)
    if not state.items:
        send_text(notices.NO_SEARCH_YET, user)
        return
    if not state.results_fresh():
        state.items = []
        send_text(notices.INDEX_EXPIRED, user)
        return

    picked: list[Any] = []
    seen: set[int] = set()
    for token in re.split(r'[,，\s]+', content.strip()):
        if not token.isdigit():
            continue
        index = int(token)
        if index in seen:
            continue
        seen.add(index)
        if 1 <= index <= len(state.items):
            picked.append(state.items[index - 1])

    if not picked:
        send_text(notices.INDEX_NOT_FOUND, user)
        return
    _enqueue(picked, state, user, blind=False)


def _enqueue(songs: list[Any], state: UserState, user: str, *, blind: bool) -> None:
    """入队并回执。

    ⚠️ 顺序：**先算好队列数 → 发回执 → 再 submit**。
    worker 一旦拿到任务就会立刻推「开始下载」，先入队会让用户先看到
    「开始下载」再看到「已加入下载队列」，顺序是反的。
    """
    queue = get_queue()
    jobs: list[DownloadJob] = []
    batch_keyword = state.keyword or '下载'

    for song in songs:
        title = _title_of(song)
        target = build_target_path(state.source, batch_keyword,
                                   getattr(song, 'song_name', '') or '未知',
                                   getattr(song, 'singers', '') or '未知',
                                   str(getattr(song, 'ext', '') or 'mp3').lstrip('.'))
        jobs.append(DownloadJob(song=song, title=title, target_path=target,
                                from_user=user, source_id=state.source,
                                queued_at=time.time()))

    queue_size = queue.pending() + len(jobs)
    if blind:
        send_text(notices.BLIND_QUEUED.format(count=queue_size, title=jobs[0].title), user)
    else:
        send_text(notices.queued_text(queue_size), user)

    logger.info('入队 %d 首（来源 %s，盲模式=%s）：%s', len(jobs), state.source, blind,
                '、'.join(job.title for job in jobs))
    for job in jobs:
        queue.submit(job)


def _title_of(song: Any) -> str:
    name = getattr(song, 'song_name', '') or '未知'
    singers = getattr(song, 'singers', '') or '未知'
    return f'{name}-{singers}'


# --- 状态 -------------------------------------------------------------------

def _queue_text() -> str:
    queue = get_queue()
    inflight, waiting = queue.snapshot()
    return notices.queue_text(queue.pending(), inflight, waiting)


def _status_text(state: UserState) -> str:
    lines: list[str] = []
    for source in SOURCE_ORDER:
        meta = SOURCE_META[source]
        record = cookie_store.load(source)
        result = cookie_store.probe(source)
        line = cookie_store.status_line(source, meta['name'], meta['cmd'], result, record)
        if record.cookies:
            line += f'\n　　更新于 {record.age_text}，预计有效 {record.expiry_text()}'
        lines.append(line)

    queue = get_queue()
    inflight, waiting = queue.snapshot()
    queue_line = f'待处理 {queue.pending()} 条'
    if inflight:
        queue_line += f'（正在下载：{inflight}）'
    if waiting:
        queue_line += f'，排队中 {len(waiting)} 条'

    check = updater.check()
    extra = [
        f'盲下载模式：{"已开启" if state.blind else "已关闭"}',
        f'搜索条数：{settings.search_limit}',
        f'引擎：{getattr(engine(), "_build_count", 0)} 次构建，'
        f'{engine().cache_stats()}',
    ]
    if not check.error:
        extra.append(f'musicdl：{check.current}'
                     + (f'（有新版本 {check.latest}）' if check.has_update else '（已是最新）'))
    return notices.status_text(source_name(state.source), lines, queue_line, extra)


# --- 供后台线程调用（启动自检 / 定时巡检 / 过期告警）-------------------------

def check_cookies_and_alert() -> tuple[list[str], list[str]]:
    """检查所有源的 Cookie。

    返回 (用于展示的状态行, 需要广播的告警文案)。
    告警只在「可用 → 失效」时产生一次，由 `cookies.should_alert` 保证。
    """
    status_lines: list[str] = []
    alerts: list[str] = []
    for source in SOURCE_ORDER:
        meta = SOURCE_META[source]
        record = cookie_store.load(source)
        result = cookie_store.probe(source)
        status_lines.append(cookie_store.status_line(
            source, meta['name'], meta['cmd'], result, record))
        if cookie_store.should_alert(source, result):
            alerts.append(notices.cookie_expired_alert(meta['name'], meta['cmd']))
    return status_lines, alerts


def broadcast_cookie_alerts(alerts: list[str]) -> None:
    for text in alerts:
        send_text(text, '@all')
