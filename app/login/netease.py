"""网易云音乐扫码登录。

流程（与官方 H5 一致）：
1. `weapi/login/qrcode/unikey` 拿 unikey
2. 二维码内容 = `https://music.163.com/login?codekey={unikey}`
3. 轮询 `weapi/login/qrcode/client/login`：
   * 800 二维码过期
   * 801 等待扫码
   * 802 已扫码，等待手机确认
   * 803 成功（响应里带 MUSIC_U 等 Cookie）
   * 8821 触发风控，建议稍后再试

加密直接用 musicdl 自带的 `WeapiCryptoUtils`（它本来就是 musicdl 的依赖），
省掉最容易写错的 AES+RSA 那一段。
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Optional

import requests

from utils.logger import logger

BASE = 'https://music.163.com'
UNIKEY_URL = f'{BASE}/weapi/login/qrcode/unikey'
POLL_URL = f'{BASE}/weapi/login/qrcode/client/login'
LOGIN_URL_TEMPLATE = f'{BASE}/login?codekey={{unikey}}'

# 部分脚本会带上 deviceId/os 之类，缺失时服务端偶尔会拒；给一套保守默认值
BASE_COOKIES = {
    'os': 'pc',
    'appver': '8.9.70',
    'osver': '',
    'deviceId': 'musicbot',
    'channel': 'netease',
}

STATUS_EXPIRED = 'expired'
STATUS_WAITING = 'waiting'
STATUS_SCANNED = 'scanned'
STATUS_CONFIRMED = 'confirmed'
STATUS_RISK = 'risk'
STATUS_UNKNOWN = 'unknown'

_CODE_TO_STATUS = {
    800: STATUS_EXPIRED,
    801: STATUS_WAITING,
    802: STATUS_SCANNED,
    803: STATUS_CONFIRMED,
    8821: STATUS_RISK,
}

STATUS_TEXT = {
    STATUS_EXPIRED: '二维码已过期',
    STATUS_WAITING: '等待扫码',
    STATUS_SCANNED: '已扫码，请在手机上确认',
    STATUS_CONFIRMED: '登录成功',
    STATUS_RISK: '触发风控，请稍后再试',
    STATUS_UNKNOWN: '未知状态',
}

_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Referer': f'{BASE}/',
    'Origin': BASE,
}


class NeteaseLoginError(RuntimeError):
    """网易云登录过程中的错误。"""


@dataclass
class PollResult:
    status: str
    code: Any = None
    message: str = ''
    cookies: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _encrypt(payload: dict) -> dict:
    from musicdl.modules.utils.neteaseutils import WeapiCryptoUtils
    return WeapiCryptoUtils.encryptparams(payload)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    session.trust_env = False          # 不走环境代理
    return session


def request_unikey(timeout: int = 15) -> tuple[str, requests.Session]:
    """申请二维码令牌。"""
    session = _session()
    resp = session.post(UNIKEY_URL, params={'csrf_token': ''},
                        data=_encrypt({'type': 1}), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    unikey = (data.get('unikey') or '').strip()
    if not unikey:
        raise NeteaseLoginError(f'unikey 获取失败：{data}')
    logger.info('网易云 unikey 已获取')
    return unikey, session


def login_url(unikey: str) -> str:
    return LOGIN_URL_TEMPLATE.format(unikey=unikey)


def qrcode_png(unikey: str) -> bytes:
    """把登录链接渲染成二维码 PNG。"""
    import qrcode
    image = qrcode.make(login_url(unikey))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _parse_cookie_string(text: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    if not text:
        return jar
    cookie = SimpleCookie()
    try:
        cookie.load(text)
    except Exception:
        return jar
    for key, morsel in cookie.items():
        if morsel.value:
            jar[key] = morsel.value
    return jar


def poll(session: requests.Session, unikey: str, timeout: int = 10) -> PollResult:
    """查询一次扫码状态。"""
    try:
        resp = session.post(POLL_URL, params={'csrf_token': ''},
                            data=_encrypt({'key': unikey, 'type': 1}), timeout=timeout)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        # 网络抖动不该被当成「过期」，交给调用方继续轮询
        return PollResult(status=STATUS_UNKNOWN, message=f'轮询异常：{e}')

    code = data.get('code')
    status = _CODE_TO_STATUS.get(code, STATUS_UNKNOWN)
    result = PollResult(status=status, code=code,
                        message=STATUS_TEXT.get(status, f'code={code}'), raw=data)

    if status == STATUS_CONFIRMED:
        jar = dict(BASE_COOKIES)
        # 优先用响应体里给的 cookie 字符串，其次才用 Set-Cookie
        jar.update(_parse_cookie_string(data.get('cookie') or ''))
        for key, value in resp.cookies.items():
            jar.setdefault(key, value)
        result.cookies = jar
        result.message = STATUS_TEXT[STATUS_CONFIRMED]
    return result


def probe(cookies: dict[str, str], timeout: int = 10) -> Optional[bool]:
    """远程探测 Cookie 是否有效（None = 无法判断）。"""
    if not cookies.get('MUSIC_U'):
        return False
    try:
        session = _session()
        resp = session.post(f'{BASE}/weapi/w/nuser/account/get',
                            params={'csrf_token': ''},
                            data=_encrypt({'csrf_token': ''}),
                            cookies=cookies, timeout=timeout)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if data.get('code') == 200 and (data.get('account') or data.get('profile')):
        return True
    if data.get('code') in (200, 301, 250):
        return False
    return None


def account_of(cookies: dict[str, str], timeout: int = 10) -> str:
    """尽力取昵称（失败返回空串，不影响主流程）。"""
    try:
        session = _session()
        resp = session.post(f'{BASE}/weapi/w/nuser/account/get',
                            params={'csrf_token': ''},
                            data=_encrypt({'csrf_token': ''}),
                            cookies=cookies, timeout=timeout)
        data = resp.json()
        return str((data.get('profile') or {}).get('nickname') or '')
    except Exception:
        return ''


def now() -> float:
    return time.time()


def dumps(cookies: dict[str, str]) -> str:
    return json.dumps(cookies, ensure_ascii=False)
