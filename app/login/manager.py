"""扫码登录编排：把 QQ / 网易云两条流程统一成「发起 → 轮询 → 落盘 → 复活引擎」。

设计要点
* **只有用户主动发 `/qq login`、`/wyy login` 才会执行**（不自动重登）。
* 二维码**以图片消息发送**（企微原生支持 `msgtype=image`）。
  图片发不出去时不影响流程：文本里已经带了 `{域名}/login/{源}` 链接。
* 同一源同时只允许一个登录流程，重复发起会被拒绝。
* 登录成功后：Cookie 落盘 → **重建引擎**（否则新 Cookie 不会生效）→ 清除失效标记 → 回消息。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app import cookies as cookie_store
from app import notices
from app.config import settings
from app.sources import SOURCE_META
from app.wecom import send_image, send_text
from utils.logger import logger

# 单张二维码最长等待时间（分钟）
QRCODE_TIMEOUT_MINUTES = 5
# 一张二维码过期后最多自动换几张
MAX_QRCODE_REFRESH = 3
# 轮询间隔（秒）
POLL_INTERVAL_SECONDS = 2.0

STATUS_IDLE = 'idle'
STATUS_WAITING = 'waiting'
STATUS_SCANNED = 'scanned'
STATUS_SUCCESS = 'success'
STATUS_FAILED = 'failed'


@dataclass
class LoginSession:
    source: str
    status: str = STATUS_IDLE
    message: str = ''
    png: bytes = b''
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    account: str = ''
    notify_user: str = ''

    @property
    def running(self) -> bool:
        return self.status in (STATUS_WAITING, STATUS_SCANNED)

    def to_public(self) -> dict[str, Any]:
        return {
            'source': self.source,
            'status': self.status,
            'message': self.message,
            'account': self.account,
            'elapsed': int(time.time() - self.started_at),
        }


_lock = threading.Lock()
_sessions: dict[str, LoginSession] = {}


def session_of(source: str) -> LoginSession:
    with _lock:
        return _sessions.get(source) or LoginSession(source=source)


def qrcode_png(source: str) -> bytes:
    with _lock:
        return (_sessions.get(source) or LoginSession(source=source)).png


def start_login(source: str, notify_user: str, on_success: Optional[Callable[[], None]] = None
                ) -> tuple[bool, str]:
    """发起一次扫码登录。返回 (是否已开始, 说明)。"""
    meta = SOURCE_META.get(source)
    if meta is None:
        return False, f'不支持的源 {source}'

    with _lock:
        current = _sessions.get(source)
        if current and current.running:
            return False, f'{meta["name"]} 正在登录中，请完成扫码或稍后再试'
        session = LoginSession(source=source, status=STATUS_WAITING,
                               message='正在生成二维码…', notify_user=notify_user)
        _sessions[source] = session

    threading.Thread(target=_run_login, args=(session, on_success),
                     name=f'musicbot-login-{source}', daemon=True).start()
    return True, 'started'


def _set(session: LoginSession, *, status: Optional[str] = None,
         message: Optional[str] = None, png: Optional[bytes] = None) -> None:
    with _lock:
        if status is not None:
            session.status = status
        if message is not None:
            session.message = message
        if png is not None:
            session.png = png


def _run_login(session: LoginSession, on_success: Optional[Callable[[], None]]) -> None:
    meta = SOURCE_META[session.source]
    name = meta['name']
    user = session.notify_user
    try:
        if session.source == 'qq':
            ok, message, account, expires_in = _login_qq(session, user)
        else:
            ok, message, account, expires_in = _login_netease(session, user)
    except Exception as e:
        logger.error('%s 登录异常：%s', name, e, exc_info=True)
        ok, message, account, expires_in = False, f'未预期的错误：{e}', '', 0

    session.finished_at = time.time()
    if not ok:
        _set(session, status=STATUS_FAILED, message=message)
        if message:
            send_text(message, user)
        return

    _set(session, status=STATUS_SUCCESS, message='登录成功', account=account)
    cookie_store.reset_alert(session.source)
    if on_success:
        try:
            on_success()          # 回调：重建引擎
        except Exception as e:
            logger.error('登录后重建引擎失败：%s', e, exc_info=True)
    send_text(notices.login_success_text(name, account), user)


# --- QQ ---------------------------------------------------------------------

def _login_qq(session: LoginSession, user: str
              ) -> tuple[bool, str, str, int]:
    from app.login import qq_login
    from app.login.qq_constants import (STATUS_CONFIRMED, STATUS_EXPIRED,
                                        STATUS_REFUSED, STATUS_SCANNED,
                                        STATUS_WAITING)
    from app.login.qq_login import QQLoginError

    deadline = time.time() + QRCODE_TIMEOUT_MINUTES * 60
    refreshed = 0
    nickname = ''

    while True:
        try:
            qr = qq_login.request_qrcode()
        except QQLoginError as e:
            return False, notices.LOGIN_FAILED.format(source='qq', reason=e), '', 0

        _set(session, png=qr.png, status=STATUS_WAITING, message='等待扫码')
        _announce_qrcode(session, user, 'qq')

        expired = False
        while time.time() < deadline:
            poll = qq_login.poll_qrcode(qr)
            nickname = poll.nickname or nickname
            if poll.status == STATUS_WAITING:
                pass
            elif poll.status == STATUS_SCANNED:
                _set(session, status=STATUS_SCANNED, message='已扫码，请在手机上确认')
            elif poll.status == STATUS_CONFIRMED:
                credential = qq_login.complete_login(qr, poll.uin, poll.sigx)
                account = credential.get('nickname') or nickname or str(credential.get('musicid') or '')
                cookies = qq_login.build_cookies(credential, time.time())
                cookie_store.save('qq', cookies, account=account,
                                  key_expires_in=int(credential.get('key_expires_in') or 0))
                return True, '', account, int(credential.get('key_expires_in') or 0)
            elif poll.status == STATUS_REFUSED:
                return (False, notices.LOGIN_FAILED.format(
                    source='qq', reason='二维码被取消或拒绝授权'), '', 0)
            elif poll.status == STATUS_EXPIRED:
                expired = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        if expired and refreshed < MAX_QRCODE_REFRESH:
            refreshed += 1
            logger.info('QQ 二维码过期，自动换一张（第 %d 次）', refreshed)
            continue
        if expired:
            return False, notices.LOGIN_TIMEOUT.format(source='qq'), '', 0
        return False, notices.LOGIN_TIMEOUT.format(source='qq'), '', 0


# --- 网易云 ------------------------------------------------------------------

def _login_netease(session: LoginSession, user: str) -> tuple[bool, str, str, int]:
    from app.login import netease
    from app.login.netease import (STATUS_CONFIRMED, STATUS_EXPIRED,
                                   STATUS_RISK, STATUS_SCANNED,
                                   STATUS_WAITING, NeteaseLoginError)

    deadline = time.time() + QRCODE_TIMEOUT_MINUTES * 60
    refreshed = 0

    while True:
        try:
            unikey, http = netease.request_unikey()
            png = netease.qrcode_png(unikey)
        except (NeteaseLoginError, Exception) as e:
            return False, notices.LOGIN_FAILED.format(source='wyy', reason=e), '', 0

        _set(session, png=png, status=STATUS_WAITING, message='等待扫码')
        _announce_qrcode(session, user, 'wyy')

        expired = False
        while time.time() < deadline:
            result = netease.poll(http, unikey)
            if result.status == STATUS_WAITING:
                pass
            elif result.status == STATUS_SCANNED:
                _set(session, status=STATUS_SCANNED, message='已扫码，请在手机上确认')
            elif result.status == STATUS_CONFIRMED:
                ck = result.cookies
                account = netease.account_of(ck) or ''
                cookie_store.save('wyy', ck, account=account)
                return True, '', account, 0
            elif result.status == STATUS_RISK:
                return (False, notices.LOGIN_FAILED.format(
                    source='wyy', reason='触发风控，请稍后再试'), '', 0)
            elif result.status == STATUS_EXPIRED:
                expired = True
                break
            elif result.status == netease.STATUS_UNKNOWN:
                pass          # 网络抖动，继续等
            time.sleep(POLL_INTERVAL_SECONDS)

        if expired and refreshed < MAX_QRCODE_REFRESH:
            refreshed += 1
            logger.info('网易云二维码过期，自动换一张（第 %d 次）', refreshed)
            continue
        return False, notices.LOGIN_TIMEOUT.format(source='wyy'), '', 0


# --- 通知 -------------------------------------------------------------------

def _announce_qrcode(session: LoginSession, user: str, source: str) -> None:
    """先把链接/说明用文本发出去，再发二维码图片。"""
    meta = SOURCE_META[source]
    page_url = ''
    if settings.public_base_url:
        page_url = f'{settings.public_base_url}/login/{source}'
    send_text(notices.login_start_text(meta['name'], page_url), user)

    png = qrcode_png(source)
    if not png:
        return
    if not send_image(png, user) and page_url:
        send_text(f'二维码图片发送失败，请打开这个链接扫码：{page_url}', user)
