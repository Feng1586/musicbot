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
from app.config import (SEARCH_LIMIT_MAX, SEARCH_LIMIT_MIN, TASK_INTERVAL_MAX,
                        TASK_INTERVAL_MIN, TASK_TIMEOUT_MAX, TASK_TIMEOUT_MIN,
                        save_runtime_override, settings)
from app.downloader import build_target_path, download_and_report
from app.login import manager as login_manager
from app.pipeline import (KIND_BLIND, KIND_DOWNLOAD, KIND_SEARCH, Task,
                          get_queue)
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
    # 这批结果是用哪个源搜出来的。**必须单独记**：用户搜完可以立刻切源，
    # 若用 state.source 去拼下载路径，歌会落到另一个源的目录里。
    items_source: str = ''
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
    elif command in ('interval', '间隔'):
        _handle_interval(args, user)
    elif command in ('timeout', '超时'):
        _handle_timeout(args, user)
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
        _handle_login(source, args[1:], user)
        return

    state.source = source
    hint = ''
    if not cookie_store.cookies_of(source):
        hint = (f'（尚未登录，搜索结果与音质受限，可发送 /{meta["cmd"]} login 扫码登录）')
    send_text(notices.source_switched_text(meta['name'], hint), user)


def _handle_login(source: str, extra: list[str], user: str) -> None:
    """登录入口。默认扫码；网易云还支持手机验证码（两步）。

        /wyy login                    扫码登录（默认）
        /wyy login sms 13800138000    给该手机号发验证码
        /wyy login code 123456        用验证码完成登录
    """
    meta = SOURCE_META[source]
    cmd = meta['cmd']
    mode = extra[0].lower() if extra else ''

    def _rebuild() -> None:
        engine().rebuild('登录成功后刷新 Cookie')

    if not mode:
        ok, message = login_manager.start_login(source, user, on_success=_rebuild)
        if not ok:
            send_text(message, user)
        return

    if source != 'wyy':
        send_text(f'「{meta["name"]}」只支持扫码登录：直接发送 /{cmd} login 即可', user)
        return

    if mode in ('sms', '短信', 'phone', '手机'):
        if len(extra) < 2:
            send_text(f'用法：/{cmd} login sms <手机号>\n例如：/{cmd} login sms 13800138000', user)
            return
        ok, payload = login_manager.start_sms_login(extra[1], user)
        if not ok:
            send_text(f'❌ 发送验证码失败：{payload}', user)
            return
        send_text(notices.SMS_CODE_SENT.format(
            phone=f'{payload[:3]}****{payload[-4:]}', source=cmd), user)
        return

    if mode in ('code', '验证码'):
        if len(extra) < 2:
            send_text(f'用法：/{cmd} login code <验证码>\n例如：/{cmd} login code 123456', user)
            return
        ok, account, detail = login_manager.finish_sms_login(
            user, extra[1], on_success=_rebuild)
        if not ok:
            send_text(f'❌ 登录失败：{detail}', user)
            return
        send_text(notices.login_success_text(meta['name'], account), user)
        return

    send_text(f'未识别的登录方式「{mode}」。可用：\n'
              f'/{cmd} login                扫码登录\n'
              f'/{cmd} login sms <手机号>    手机验证码登录\n'
              f'/{cmd} login code <验证码>   提交验证码', user)


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


def _handle_interval(args: list[str], user: str) -> None:
    """任务之间的间隔（秒）。串行 + 间隔能降低被平台风控的概率。"""
    if not args:
        send_text(notices.task_interval_status(
            settings.task_interval_seconds, TASK_INTERVAL_MIN, TASK_INTERVAL_MAX), user)
        return
    try:
        value = int(args[0])
    except ValueError:
        send_text(f'请给一个 {TASK_INTERVAL_MIN}-{TASK_INTERVAL_MAX} 之间的数字，'
                  f'例如 /interval 5', user)
        return
    if value < TASK_INTERVAL_MIN or value > TASK_INTERVAL_MAX:
        send_text(f'❌ 超出范围：只能是 {TASK_INTERVAL_MIN}-{TASK_INTERVAL_MAX} 秒之间的整数'
                  f'（0 = 任务之间不等待）', user)
        return
    settings.task_interval_seconds = value
    save_runtime_override('task_interval_seconds', value)
    send_text(notices.TASK_INTERVAL_SET.format(value=value), user)


def _handle_timeout(args: list[str], user: str) -> None:
    """单任务超时（分钟）。超时后放弃等待、继续处理后面的任务。"""
    if not args:
        send_text(notices.task_timeout_status(
            settings.task_timeout_minutes, TASK_TIMEOUT_MIN, TASK_TIMEOUT_MAX), user)
        return
    try:
        value = int(args[0])
    except ValueError:
        send_text(f'请给一个 {TASK_TIMEOUT_MIN}-{TASK_TIMEOUT_MAX} 之间的数字，'
                  f'例如 /timeout 5', user)
        return
    if value < TASK_TIMEOUT_MIN or value > TASK_TIMEOUT_MAX:
        send_text(f'❌ 超出范围：只能是 {TASK_TIMEOUT_MIN}-{TASK_TIMEOUT_MAX} 分钟之间的整数',
                  user)
        return
    settings.task_timeout_minutes = value
    save_runtime_override('task_timeout_minutes', value)
    send_text(notices.TASK_TIMEOUT_SET.format(value=value), user)


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


# --- 搜索 / 下载：入口只做「回执 + 入队」-------------------------------------
# 真正的搜索与下载在下面 execute_task 里，由 app/pipeline.py 的 worker 串行调用。

def _handle_text(content: str, user: str) -> None:
    """非指令文本：盲下 → 搜到第 1 首直接下；否则只搜、回编号列表。

    ⚠️ 这里**不做搜索**。搜索以前跑在回调线程里、和下载抢同一把引擎锁，
    用户连发多首歌名时「等锁 + 搜索」整段时间一条反馈都发不出去，看着就像卡死。
    现在只做两件事：发回执、入队。
    """
    state = user_state(user)
    kind = KIND_BLIND if state.blind else KIND_SEARCH
    _submit([Task(kind=kind, user=user, title=content,
                  source=state.source, keyword=content)], user)


def _handle_indices(content: str, user: str) -> None:
    state = user_state(user)
    if not state.items:
        # 「还没结果」有两种，提示不能混：正在排队搜索 vs 压根没搜过。
        # 混了会让人以为自己的搜索没被受理。
        send_text(notices.SEARCH_PENDING if get_queue().has_pending_search(user)
                  else notices.NO_SEARCH_YET, user)
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

    # 路径用**搜出这批结果的源**，不是「当前源」—— 用户完全可以在搜索后切源，
    # 用当前源会把歌落进另一个源的目录。
    source = state.items_source or state.source
    keyword = state.keyword or '下载'
    tasks: list[Task] = []
    for song in picked:
        tasks.append(Task(
            kind=KIND_DOWNLOAD, user=user, title=_title_of(song),
            source=source, keyword=keyword, song=song,
            target_path=build_target_path(
                source, keyword,
                getattr(song, 'song_name', '') or '未知',
                getattr(song, 'singers', '') or '未知',
                str(getattr(song, 'ext', '') or 'mp3').lstrip('.'))))
    _submit(tasks, user)


def _submit(tasks: list[Task], user: str) -> None:
    """发回执 → 入队。

    ⚠️ 顺序不能反：worker 一拿到任务就推「开始下载」，先入队会让用户先看到
    「开始下载」再看到「已受理」。所以先占位次、发回执，再按位次入队。
    """
    queue = get_queue()
    position = queue.reserve_position()
    send_text(notices.accepted_text(position, tasks[0].title, count=len(tasks)), user)
    logger.info('受理 %d 条任务（源 %s）：%s', len(tasks), tasks[0].source,
                '、'.join(t.title for t in tasks))
    for task in tasks:
        queue.submit(task, position=position)
        position = 0        # 只有第一条用预留位次，其余顺位递增


# --- 队列 worker 的实际执行 --------------------------------------------------

def execute_task(task: Task) -> None:
    """队列 worker 的回调：一条任务**完整**走完（pipeline 的不变量 1 与 4）。

    这里的异常会被 worker 兜住然后继续跑下一个，所以每个分支都要自己把
    「失败」变成一条给用户的消息，不能指望上层处理。
    """
    if task.kind == KIND_DOWNLOAD:
        _run_download(task)
    elif task.kind == KIND_BLIND:
        _run_blind(task)
    else:
        _run_search(task)


def _run_search(task: Task) -> None:
    outcome = _search(task)
    if outcome is None:
        return
    lines = [f'{idx}. {_title_of(song)}' for idx, song in enumerate(outcome.items, 1)]
    send_text(notices.search_result_text(
        source_name(task.source), lines, limit_hint=notices.SEARCH_HINT), task.user)


def _run_blind(task: Task) -> None:
    outcome = _search(task)
    if outcome is None:
        return
    song = outcome.items[0]
    title = _title_of(song)
    target = build_target_path(
        task.source, task.keyword,
        getattr(song, 'song_name', '') or '未知',
        getattr(song, 'singers', '') or '未知',
        str(getattr(song, 'ext', '') or 'mp3').lstrip('.'))
    send_text(notices.start_download_text(title), task.user)
    download_and_report(song, title, target, task.user)


def _run_download(task: Task) -> None:
    send_text(notices.start_download_text(task.title), task.user)
    download_and_report(task.song, task.title, task.target_path, task.user)


def _search(task: Task):
    """搜一次并写入该用户的结果槽。

    失败/无结果时**在这里就把消息回了**，返回 None 表示没有结果可用。
    结果槽写在搜索完成之后（不是入队时）—— 这样用户回数字时看到的一定是
    已经搜出来的那批，而不是一个「说好了会有、其实还没搜」的空槽。
    """
    outcome = engine().search(task.source, task.keyword)
    if not outcome.ok:
        logger.warning('搜索失败（%s，源 %s）：%s', task.keyword, task.source, outcome.error)
        send_text(notices.SEARCH_FAILED, task.user)
        return None
    if not outcome.items:
        send_text(notices.BLIND_NO_RESULT.format(keyword=task.keyword)
                  if task.kind == KIND_BLIND else notices.SEARCH_EMPTY, task.user)
        return None

    state = user_state(task.user)
    state.items = outcome.items
    state.keyword = task.keyword
    state.items_source = task.source
    state.at = time.time()
    return outcome


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
        queue_line += f'（正在处理：{inflight}）'
    if waiting:
        queue_line += f'，排队中 {len(waiting)} 条'

    check = updater.check()
    extra = [
        f'盲下载模式：{"已开启" if state.blind else "已关闭"}',
        f'搜索条数：{settings.search_limit}',
        f'任务间隔：{settings.task_interval_seconds} 秒'
        f'（/interval 可改）　单任务超时：{settings.task_timeout_minutes} 分钟'
        f'（/timeout 可改）',
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
