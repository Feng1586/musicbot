"""扫码登录编排：把 QQ / 网易云两条流程统一成「发起 → 轮询 → 落盘 → 复活引擎」。

设计要点
* **只有用户主动发 `/qq login`、`/wyy login` 才会执行**（不自动重登）。
* 二维码优先**以图片消息发送**（企微原生支持 `msgtype=image`）。
  图片这条路未必通（反代可能没放行 `media/upload`），所以**先上传探路再决定
  文案**，见 `_announce_qrcode`；发不出去时按「对外链接 → 局域网地址 →
  请联系管理员」三级降级，绝不留下一条「下一条消息是二维码」的空头承诺。
* 同一源同时只允许一个登录流程，重复发起会被拒绝。
* 登录成功后：Cookie 落盘 → **重建引擎**（否则新 Cookie 不会生效）→ 清除失效标记 → 回消息。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app import cookies as cookie_store
from app import notices
from app.config import settings
from app.sources import SOURCE_META
from app.wecom import send_image_message, send_text, upload_media
from utils.logger import logger

# 单张二维码最长等待时间（分钟）。超时**只提示**，不自动换码 —— 由用户重新发指令。
QRCODE_TIMEOUT_MINUTES = 5
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


# 状态 → 中文，仅用于日志
_STATUS_TEXT = {
    STATUS_IDLE: '未开始',
    STATUS_WAITING: '等待扫码',
    STATUS_SCANNED: '已扫码待确认',
    STATUS_SUCCESS: '登录成功',
    STATUS_FAILED: '登录失败',
}


def _set(session: LoginSession, *, status: Optional[str] = None,
         message: Optional[str] = None, png: Optional[bytes] = None,
         account: Optional[str] = None) -> None:
    prev = session.status
    with _lock:
        if status is not None:
            session.status = status
        if message is not None:
            session.message = message
        if png is not None:
            session.png = png
        if account is not None:
            session.account = account
    # 状态跃迁单独记一条日志。以前这里一声不响，所以用户说"扫码没反应"时，
    # 日志里完全看不出进展（2026-09-22 排查网易云登录时被这条坑了很久）。
    # 纯观测，不改任何行为；同状态重复设置（例如每张新码都设 waiting）不会重复打。
    if status is not None and status != prev:
        logger.info('%s 登录状态：%s → %s（%s）', session.source,
                    _STATUS_TEXT.get(prev, prev), _STATUS_TEXT.get(status, status),
                    message or '')


def _notify(text: str, user: str) -> None:
    """给发起人发一条登录通知，发送失败只记日志。

    这里刻意不用裸 `send_text`：登录线程一旦因为发消息失败而崩掉，会话就会永远
    停在 `running` 态 —— 用户既收不到通知，又再也发不起登录（两条合起来是个死局）。
    """
    if not text:
        return
    try:
        send_text(text, user)
    except Exception as e:
        logger.warning('发送登录通知失败：%s', e)


def _run_login(session: LoginSession, on_success: Optional[Callable[[], None]]) -> None:
    """后台登录线程的主体。

    **必须保证这个函数无论出什么事，会话都会落到终态**（success / failed）。
    它跑在守护线程里，异常不会有人接 —— 一旦漏出去，会话就永远停在
    `running`（waiting/scanned），用户再也发不起登录。所以这里有两层 try：
    内层处理「登录本身」的异常，外层兜住「收尾动作」的异常。
    """
    meta = SOURCE_META[session.source]
    name = meta['name']
    user = session.notify_user
    try:
        try:
            if session.source == 'qq':
                ok, message, account, expires_in = _login_qq(session, user)
            else:
                ok, message, account, expires_in = _login_netease(session, user)
        except Exception as e:
            logger.error('%s 登录异常：%s', name, e, exc_info=True)
            ok, message, account, expires_in = False, f'未预期的错误：{e}', '', 0

        if not ok:
            _set(session, status=STATUS_FAILED, message=message)
            _notify(message, user)
            return

        # 走到这里 Cookie 已经落盘，登录这个事实不会再变。后面的收尾（清告警、重建
        # 引擎、发通知）逐个包好，任何一步失败都不该推翻「登录成功」、更不能让线程崩掉。
        _set(session, status=STATUS_SUCCESS, message='登录成功', account=account)
        try:
            cookie_store.reset_alert(session.source)
        except Exception as e:
            logger.warning('清除 %s 的 Cookie 告警标记失败：%s', name, e)
        if on_success:
            try:
                on_success()          # 回调：重建引擎
            except Exception as e:
                logger.error('登录后重建引擎失败：%s', e, exc_info=True)
        _notify(notices.login_success_text(name, account), user)
    except Exception as e:
        logger.error('%s 登录流程异常终止：%s', name, e, exc_info=True)
        # 已经判成功就别再改回失败（收尾里的异常都在上面各自兜住了，走到这里
        # 基本只可能是成功之前出的问题）。
        if session.status != STATUS_SUCCESS:
            _set(session, status=STATUS_FAILED, message=f'登录流程异常终止：{e}')
    finally:
        session.finished_at = time.time()


# --- QQ ---------------------------------------------------------------------

def _login_qq(session: LoginSession, user: str
              ) -> tuple[bool, str, str, int]:
    from app.login import qq_login
    from app.login.qq_constants import (STATUS_CONFIRMED, STATUS_EXPIRED,
                                        STATUS_REFUSED, STATUS_SCANNED,
                                        STATUS_WAITING)
    from app.login.qq_login import QQLoginError

    deadline = time.time() + QRCODE_TIMEOUT_MINUTES * 60
    nickname = ''

    try:
        qr = qq_login.request_qrcode()
    except QQLoginError as e:
        return False, notices.LOGIN_FAILED.format(source='qq', reason=e), '', 0

    _set(session, png=qr.png, status=STATUS_WAITING, message='等待扫码')
    _announce_qrcode(session, user, 'qq')

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
            logger.info('QQ 二维码已被服务端判为过期')
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    # 超时（或二维码过期）→ **只提示超时，由用户重新发指令**。
    # 以前这里会自动换码最多 3 张，已按要求删除（2026-09-22）。
    return False, notices.LOGIN_TIMEOUT.format(source='qq'), '', 0


# --- 网易云 ------------------------------------------------------------------

def _login_netease(session: LoginSession, user: str) -> tuple[bool, str, str, int]:
    from app.login import netease
    from app.login.netease import (STATUS_CONFIRMED, STATUS_EXPIRED,
                                   STATUS_RISK, STATUS_SCANNED,
                                   STATUS_WAITING, NeteaseLoginError)

    deadline = time.time() + QRCODE_TIMEOUT_MINUTES * 60

    try:
        unikey, http = netease.request_unikey()
        png = netease.qrcode_png(unikey)
    except (NeteaseLoginError, Exception) as e:
        return False, notices.LOGIN_FAILED.format(source='wyy', reason=e), '', 0

    logger.info('网易云二维码已发出，等待扫码…')
    _set(session, png=png, status=STATUS_WAITING, message='等待扫码')
    _announce_qrcode(session, user, 'wyy')

    unknown_logged = False
    while time.time() < deadline:
        result = netease.poll(http, unikey)
        if result.status == STATUS_WAITING:
            unknown_logged = False
        elif result.status == STATUS_SCANNED:
            unknown_logged = False
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
            logger.info('网易云二维码已被服务端判为过期')
            break
        elif result.status == netease.STATUS_UNKNOWN:
            # 以前这里是静默 pass：网易云若回了预期外的 code，线索会被全部吞掉，
            # 外面看起来就是"扫了没反应"。每次都提醒，但同一张码只提醒一次。
            if not unknown_logged:
                logger.warning('网易云轮询返回未知状态（code=%r）：%s',
                               result.code, result.message)
                unknown_logged = True
        time.sleep(POLL_INTERVAL_SECONDS)

    # 超时（或二维码过期）→ **只提示超时，由用户重新发指令**。
    # 以前这里会自动换码最多 3 张，已按要求删除（2026-09-22）。
    return False, notices.LOGIN_TIMEOUT.format(source='wyy'), '', 0


# --- 网易云：手机号 + 短信验证码登录 ------------------------------------------
#
# 这条流程**不用 LoginSession**（没有二维码、不需要轮询），所以只有两个短动作：
#   1) `/wyy login sms <手机号>`  → 发验证码，并把「该用户正在等验证码」记在内存里
#   2) `/wyy login code <验证码>` → 用验证码换 Cookie
# 记的是**内存字典**：进程一重启就失效，用户重发一次 sms 即可（无需持久化，
# 免得把手机号落盘）。

_sms_pending: dict[str, str] = {}


def start_sms_login(cellphone: str, user: str) -> tuple[bool, str]:
    """短信登录第一步：发送验证码。返回 (是否成功, 手机号 或 失败原因)。"""
    from app.login import netease

    phone = (cellphone or '').strip()
    if not (len(phone) == 11 and phone.isdigit() and phone.startswith('1')):
        return False, f'手机号格式不对：{phone or "(空)"}（应为 11 位数字，例如 13800138000）'
    try:
        netease.send_sms_captcha(phone)
    except Exception as e:
        logger.error('发送网易云验证码失败：%s', e)
        return False, str(e)
    _sms_pending[user] = phone
    return True, phone


def finish_sms_login(user: str, captcha: str,
                     on_success: Optional[Callable[[], None]] = None
                     ) -> tuple[bool, str, str]:
    """短信登录第二步：用验证码换 Cookie。返回 (是否成功, 账号, 失败原因)。"""
    from app.login import netease

    phone = _sms_pending.get(user, '')
    if not phone:
        return False, '', '还没有待验证的手机号，请先发送 /wyy login sms <手机号>'
    code = (captcha or '').strip()
    if not (code.isdigit() and 4 <= len(code) <= 6):
        return False, '', f'验证码应为 4-6 位数字：{code or "(空)"}'
    try:
        cookies, account = netease.login_by_cellphone(phone, code)
    except Exception as e:
        logger.error('网易云验证码登录失败：%s', e)
        return False, '', str(e)

    cookie_store.save('wyy', cookies, account=account)
    _sms_pending.pop(user, None)
    try:
        cookie_store.reset_alert('wyy')
    except Exception as e:
        logger.warning('清除 wyy 的 Cookie 告警标记失败：%s', e)
    if on_success:
        try:
            on_success()          # 回调：重建引擎，让新 Cookie 生效
        except Exception as e:
            logger.error('验证码登录后重建引擎失败：%s', e, exc_info=True)
    return True, account, ''


# --- 通知 -------------------------------------------------------------------

# 这些地址要么是保留段、要么是虚拟网卡（TUN），都不是「别人能连进来」的地址。
#   198.18.0.0/15  RFC 2544 保留段，Clash 之类的 TUN 网卡常用（本机实测踩过）
#   169.254.0.0/16 link-local
#   100.64.0.0/10  CGNAT
_UNREACHABLE_PREFIXES = ('198.18.', '198.19.', '169.254.', '100.64.')


def _is_usable_lan_ip(ip: str) -> bool:
    """这个地址是不是「同一局域网内的人能连上」的 RFC1918 私有地址。"""
    if not ip or ip.startswith(('127.', '0.', '255.')):
        return False
    if ip.startswith(_UNREACHABLE_PREFIXES):
        return False
    try:
        first, second = (int(x) for x in ip.split('.')[:2])
    except ValueError:
        return False
    return (first == 10
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168))


def _lan_url(source: str) -> str:
    """兜底用的局域网扫码地址。

    什么时候会用到：图片发不出去（反代没放行 `media/upload`，或图片功能被关掉），
    同时又没配 `MUSICBOT_PUBLIC_BASE_URL`。这时给一个内网地址，扫码的人只要
    和服务器在同一局域网就能用 —— 比让用户干等一条不会来的图片消息强得多。

    ⚠️ 这里必须**先把虚拟网卡排掉**：本机开着代理时，最简单的
    「UDP connect 看默认路由」会返回 TUN 网卡地址（本机实测拿到 `198.18.0.0`），
    那是 RFC 2544 保留段，别人根本连不上。旧后端当初也踩过同一个坑。
    所以这里改成枚举本机地址 + 只认 RFC1918 私有网段，并优先常见的 192.168.x.x。
    """
    candidates: list[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _is_usable_lan_ip(ip) and ip not in candidates:
                candidates.append(ip)
    except OSError as e:
        logger.debug('枚举本机地址失败：%s', e)

    if not candidates:
        # 兜底：让系统选一次路。这条可能落在 TUN 网卡上，所以要再过一遍过滤
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(('223.5.5.5', 80))
                ip = sock.getsockname()[0]
            finally:
                sock.close()
            if _is_usable_lan_ip(ip):
                candidates.append(ip)
        except OSError as e:
            logger.debug('探测默认路由地址失败：%s', e)

    if not candidates:
        return ''
    # 本机实测会同时枚举出物理网卡与几张虚拟网卡的地址：
    #   192.168.2.6（真实网卡）  192.168.149.1 / 192.168.92.1（VMware）
    #   172.21.208.1（WSL）      198.18.0.0（TUN，已被过滤掉）
    # 虚拟网卡的地址几乎都是网段里的 .1，所以把 .1 结尾的往后排；
    # 再按「192.168 → 10 → 其它私有段」的顺序偏好家用/办公最常见的网段。
    def _rank(ip: str) -> tuple:
        first = int(ip.split('.')[0])
        tier = 0 if ip.startswith('192.168.') else (1 if first == 10 else 2)
        return (ip.endswith('.1'), tier, ip)

    candidates.sort(key=_rank)
    return f'http://{candidates[0]}:{settings.port}/login/{source}'


def _announce_qrcode(session: LoginSession, user: str, source: str) -> None:
    """把登录说明与二维码发给发起登录的人。

    顺序上有个坑：第一条说明里会写「二维码图片见下一条消息」。如果图片其实
    发不出去（图片功能被关掉，或反代没放行 `media/upload`），这句话就成了
    空头支票 —— 用户会一直等一条永远不会来的消息。

    所以这里**先把素材传上去探一次路**：上传是纯 API 调用、不产生任何用户
    可见的消息，等确定了图片发得出去，才发那条带承诺的说明。素材上传得到的
    media_id 直接接着用来发消息，所以总共还是一次上传 + 一次发送，没有多花。
    """
    meta = SOURCE_META[source]
    page_url = ''
    if settings.public_base_url:
        page_url = f'{settings.public_base_url}/login/{source}'

    png = qrcode_png(source)
    if not png:
        return

    media_id, upload_error = '', ''
    if settings.image_enabled:
        try:
            media_id = upload_media(png)
        except Exception as e:
            upload_error = str(e)
            logger.warning('二维码素材上传失败（%s）：%s', source, upload_error)

    send_text(notices.login_start_text(meta['name'], page_url,
                                       image_expected=bool(media_id)), user)

    if media_id:
        send_image_message(media_id, user)
    elif upload_error:
        # 本来该有图片，结果没发出来 —— 必须说一声，否则用户会一直等
        send_text(notices.qrcode_undeliverable_text(
            source, page_url=page_url, lan_url=_lan_url(source),
            reason=upload_error), user)
    elif not page_url:
        # 图片功能被配置主动关掉了，而且没有对外地址
        send_text(notices.qrcode_undeliverable_text(
            source, lan_url=_lan_url(source)), user)
