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

# ⚠️ 二维码里必须编码「确认登录页」这个**新入口**（2026-09-22 用户抓包实测确定）。
#
# 老写法 `{BASE}/login?codekey=` 是个过时入口，实测会这样：
#   未登录的浏览器打开它 → 先被要求登录 → 而登录跳转（例如微信 OAuth）会把
#   `codekey` 丢掉 → 「确认这张码」这一步**永远不会发生**。
#   用户实测：扫码登录后页面直接落到「首页-推荐音乐」，而我们这边一直是 801
#   「等待扫码」，看起来像"根本没人扫"。
#
# 新入口 /st/platform/scanlogin 才是网页端当前在用的那个页面：
#   浏览器**已登录**时打开它，就直接显示「确认登录」，点一下就完成 ——
#   所以微信扫码那条路（在微信内置浏览器里）也能走通。
#
# `hdw_device/hdw_appid/hitExp` 是网页端一起带的参数，照抄。
# `chainId` 网页端是**现场生成**的（形如 v1_<随机串>_web_login_<毫秒时间戳>），
# 实测带不带该页面都返回 200，所以这里先不带。
SCANLOGIN_URL = (f'{BASE}/st/platform/scanlogin?codekey={{unikey}}'
                 '&hdw_device=web&hdw_appid=web&hitExp=1')

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
    """二维码要编码的地址 —— 必须是网页端的**确认登录页**，见上面 SCANLOGIN_URL 的说明。"""
    return SCANLOGIN_URL.format(unikey=unikey)


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


# --- 手机号 + 短信验证码登录（2026-09-22 实测确认接口与参数名）---------------
#
# 两步：
#   1) POST /weapi/sms/captcha/sent     {cellphone, ctcode}        → 给手机发验证码
#   2) POST /weapi/login/cellphone      {phone, countrycode,
#                                        captcha, rememberLogin}   → 换回 Cookie
# 实测（用非法号码探，不会真发短信）：
#   sent    → {"code":400,"message":"手机号码不符合规范"}
#   login   → {"code":503,"message":"ursToken或验证码不存在"}
# 注意：这条链路同样可能触发网易云风控（和扫码一样，风控是账号/IP 层面的）。

SMS_CAPTCHA_URL = f'{BASE}/weapi/sms/captcha/sent'
CELLPHONE_LOGIN_URL = f'{BASE}/weapi/login/cellphone'


def send_sms_captcha(cellphone: str, ctcode: str = '86', timeout: int = 15) -> None:
    """给手机号发送登录验证码。失败抛 NeteaseLoginError。"""
    resp = _session().post(SMS_CAPTCHA_URL, params={'csrf_token': ''},
                           data=_encrypt({'cellphone': cellphone, 'ctcode': ctcode}),
                           timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise NeteaseLoginError(f'发送验证码返回非 JSON（HTTP {resp.status_code}）')
    if data.get('code') != 200:
        raise NeteaseLoginError(data.get('message') or data.get('msg') or f'code={data.get("code")}')
    logger.info('网易云验证码已发送（%s****%s）', cellphone[:3], cellphone[-4:])


def login_by_cellphone(cellphone: str, captcha: str, ctcode: str = '86',
                       timeout: int = 15) -> tuple[dict[str, str], str]:
    """手机号 + 验证码登录。返回 (cookies, 昵称)。"""
    resp = _session().post(CELLPHONE_LOGIN_URL, params={'csrf_token': ''},
                           data=_encrypt({'phone': cellphone, 'countrycode': ctcode,
                                          'captcha': captcha, 'rememberLogin': True}),
                           timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise NeteaseLoginError(f'登录返回非 JSON（HTTP {resp.status_code}）')
    if data.get('code') != 200:
        raise NeteaseLoginError(data.get('message') or data.get('msg') or f'code={data.get("code")}')

    # 与扫码登录一致：优先响应体里的 cookie 字符串，其次 Set-Cookie
    jar = dict(BASE_COOKIES)
    jar.update(_parse_cookie_string(data.get('cookie') or ''))
    for key, value in resp.cookies.items():
        jar.setdefault(key, value)
    if not jar.get('MUSIC_U'):
        raise NeteaseLoginError('登录成功但没拿到 MUSIC_U（Cookie 缺失）')
    account = str((data.get('profile') or {}).get('nickname') or '')
    logger.info('网易云手机验证码登录成功（账号 %s）', account or '未知')
    return jar, account
