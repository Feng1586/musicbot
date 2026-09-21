'''
QQ 音乐扫码登录实现。

流程与 y.qq.com 网页端登录弹窗完全一致，全部使用 requests 完成：

    1. ptqrshow         -> 拿到二维码图片和 qrsig
    2. ptqrlogin        -> 轮询扫码状态（ptqrtoken = hash33(qrsig)）
    3. check_sig        -> 扫码确认后换取 p_skey
    4. oauth2/authorize -> 用 p_skey 换取 code（g_tk = hash33(p_skey, 5381)）
    5. LoginServer      -> 用 code 换取 musickey / musicid

只依赖 requests，不引入任何 QQ 音乐相关的第三方库。
'''
from __future__ import annotations

import contextlib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from . import qq_constants as C

logger = logging.getLogger('musicdl-backend.qq.login')


class QQLoginError(RuntimeError):
    '''扫码登录流程中可预期的失败。'''


def hash33(text: str, seed: int = 0) -> int:
    '''等价于 QQ 登录页 JS 里的 $.str.hash33。'''
    value = seed
    for char in text:
        value += (value << 5) + ord(char)
    return value & 2147483647


def _decode(content: bytes) -> str:
    '''ptlogin 系列接口有时不带正确 charset，这里做一次兜底解码。'''
    for encoding in ('utf-8', 'gbk'):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode('utf-8', 'replace')


def _headers(referer: str = C.PTLOGIN_REFERER) -> dict[str, str]:
    return {'User-Agent': C.USER_AGENT, 'Referer': referer}


@dataclass
class QRCodeSession:
    '''一次二维码登录会话。'''
    png: bytes
    qrsig: str
    session: requests.Session = field(repr=False)
    created_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.created_at


@dataclass
class PollResult:
    '''一次二维码状态轮询的结果。'''
    status: str
    code: str = ''
    message: str = ''
    nickname: str = ''
    uin: str = ''
    sigx: str = ''
    raw: str = ''


_PTUI_CALL = re.compile(r'ptuiCB\((.*?)\)', re.S)
_PTUI_ARG = re.compile(r"'([^']*)'")
_LOCATION_CODE = re.compile(r'[?&]code=([^&]+)')
_REDIRECT_UIN = re.compile(r'[?&]uin=([^&]+)')
_REDIRECT_SIGX = re.compile(r'[?&]ptsigx=([^&]+)')


def _classify(code: str, message: str, redirect: str) -> str:
    '''
    判断这一次轮询结果的状态。

    判据按可靠性排序：

    1. 返回里带了能解析出 uin + ptsigx 的跳转地址 —— 铁定的登录成功，
       和数字码无关；
    2. 文案关键词 —— QQ 各版本的数字状态码会漂移（实测"尚未扫码"返回 66，
       扫完码后返回的数字还会被误读成"已失效"），文案才是稳定信号；
    3. 都不匹配就当作"继续等待"，**绝不擅自换二维码**。

    注意 "二维码未失效。" 这类否定文案不能被当成失效，所以只匹配 "已失效"
    而不是 "失效"。
    '''
    if _REDIRECT_UIN.search(redirect or '') and _REDIRECT_SIGX.search(redirect or ''):
        return C.STATUS_CONFIRMED
    if code == '0':
        # 明确返回成功码，即便没有跳转地址也按成功处理，交由上层报错
        return C.STATUS_CONFIRMED
    if any(keyword in message for keyword in C.EXPIRED_KEYWORDS):
        return C.STATUS_EXPIRED
    if any(keyword in message for keyword in C.REFUSED_KEYWORDS):
        return C.STATUS_REFUSED
    if any(keyword in message for keyword in C.SCANNED_KEYWORDS):
        return C.STATUS_SCANNED
    return C.STATUS_WAITING


def _parse_ptui(text: str) -> PollResult:
    '''解析 ptqrlogin 返回的 ptuiCB(...) 结构。'''
    call = _PTUI_CALL.search(text)
    if not call:
        raise QQLoginError(f'无法解析 ptqrlogin 响应：{text[:200]!r}')
    args = _PTUI_ARG.findall(call.group(1))
    if not args:
        raise QQLoginError(f'ptqrlogin 响应缺少参数：{text[:200]!r}')

    code = args[0].strip()
    redirect = args[2] if len(args) > 2 else ''
    # 状态文案一律用 QQ 自己返回的原文，避免我们对状态码的理解和服务端不一致
    message = (args[4] if len(args) > 4 else '').strip()
    nickname = args[5] if len(args) > 5 else ''
    status = _classify(code, message, redirect)

    result = PollResult(status=status, code=code, message=message, nickname=nickname, raw=text)
    if status == C.STATUS_CONFIRMED:
        # args[2] 形如 https://ptlogin2.y.qq.com/check_sig?...&uin=xxx&ptsigx=xxx&...
        uin = _REDIRECT_UIN.search(redirect)
        sigx = _REDIRECT_SIGX.search(redirect)
        if not uin or not sigx:
            raise QQLoginError(f'登录成功但无法从跳转地址中解析 uin/ptsigx：{redirect!r}')
        result.uin, result.sigx = uin.group(1), sigx.group(1)
    return result


def request_qrcode(timeout: int = 15) -> QRCodeSession:
    '''申请一张新的登录二维码。'''
    session = requests.Session()
    session.headers.update(_headers())
    # 先访问一次登录框，保证 pt_login_sig 等 cookie 就位（部分风控场景必需）
    with contextlib.suppress(requests.RequestException):
        with session.get(C.XLOGIN_URL, params=_xlogin_params(), timeout=timeout) as _:
            pass

    response = session.get(C.QRCODE_URL, params=_qrcode_params(), timeout=timeout)
    response.raise_for_status()
    qrsig = session.cookies.get('qrsig')
    if not qrsig:
        raise QQLoginError('ptqrshow 未下发 qrsig，无法继续扫码登录')
    return QRCodeSession(png=response.content, qrsig=qrsig, session=session)


def poll_qrcode(qr: QRCodeSession, timeout: int = 10) -> PollResult:
    '''查询二维码当前状态。'''
    params = {
        'u1': C.OAUTH_LOGIN_JUMP,
        'ptqrtoken': str(hash33(qr.qrsig)),
        'ptredirect': '0',
        'h': '1',
        't': '1',
        'g': '1',
        'from_ui': '1',
        'ptlang': '2052',
        'action': f'0-0-{int(time.time() * 1000)}',
        'js_ver': '20102616',
        'js_type': '1',
        'pt_uistyle': '40',
        'aid': C.APPID,
        'daid': C.DAID,
        'pt_3rd_aid': C.PT_3RD_AID,
        'has_onekey': '1',
    }
    login_sig = qr.session.cookies.get('pt_login_sig')
    if login_sig:
        params['login_sig'] = login_sig

    response = qr.session.get(C.QRCODE_POLL_URL, params=params, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    return _parse_ptui(_decode(response.content))


def complete_login(qr: QRCodeSession, uin: str, sigx: str, timeout: int = 15) -> dict[str, Any]:
    '''
    扫码确认后，用 uin + ptsigx 换取 QQ 音乐 musickey。

    返回的字典字段与 musicdl 的 QQMusicClient 配置兼容（musicid / musickey / ...）。
    '''
    credential = _exchange_code_for_key(qr, uin, sigx, timeout=timeout)
    if not credential.get('musickey') or not credential.get('musicid'):
        raise QQLoginError(f'QQLogin 未返回有效的 musickey/musicid：{credential!r}')
    return credential


def _exchange_code_for_key(qr: QRCodeSession, uin: str, sigx: str, timeout: int = 15) -> dict[str, Any]:
    '''check_sig -> authorize -> QQLogin 三步换取凭证。'''
    # 1) 拿 p_skey
    check = qr.session.get(
        C.CHECK_SIG_URL,
        params={
            'uin': uin,
            'pttype': '1',
            'service': 'ptqrlogin',
            'nodirect': '0',
            'ptsigx': sigx,
            's_url': C.OAUTH_LOGIN_JUMP,
            'ptlang': '2052',
            'ptredirect': '100',
            'aid': C.APPID,
            'daid': C.DAID,
            'j_later': '0',
            'low_login_hour': '0',
            'regmaster': '0',
            'pt_login_type': '3',
            'pt_aid': '0',
            'pt_aaid': '16',
            'pt_light': '0',
            'pt_3rd_aid': C.PT_3RD_AID,
        },
        headers=_headers(),
        allow_redirects=False,
        timeout=timeout,
    )
    check.raise_for_status()
    p_skey = check.cookies.get('p_skey')
    if not p_skey:
        raise QQLoginError('check_sig 未返回 p_skey，扫码登录失败')

    # 2) 换取 code（g_tk 由 p_skey 用 seed=5381 的 hash33 得出）
    authorize = qr.session.post(
        C.OAUTH_AUTHORIZE_URL,
        data={
            'response_type': 'code',
            'client_id': C.PT_3RD_AID,
            'redirect_uri': C.OAUTH_REDIRECT_URI,
            'scope': 'get_user_info,get_app_friends',
            'state': 'state',
            'switch': '',
            'from_ptlogin': '1',
            'src': '1',
            'update_auth': '1',
            'openapi': '1010_1030',
            'g_tk': str(hash33(p_skey, 5381)),
            'auth_time': str(int(time.time()) * 1000),
            'ui': _random_uuid(),
        },
        headers=_headers(C.QQMUSIC_REFERER),
        cookies=dict(check.cookies),
        allow_redirects=False,
        timeout=timeout,
    )
    authorize.raise_for_status()
    location = authorize.headers.get('Location', '')
    matched = _LOCATION_CODE.search(location)
    if not matched:
        raise QQLoginError(f'authorize 未返回 code，返回体：{_decode(authorize.content)[:200]!r}')

    # 3) 用 code 换 musickey
    response = requests.post(
        C.MUSICU_ENDPOINT,
        json={
            'comm': _musicu_comm(tme_login_type=2),
            'req_0': {
                'module': C.LOGIN_MODULE,
                'method': C.LOGIN_METHOD,
                'param': {'code': matched.group(1)},
            },
        },
        headers={**_headers(C.QQMUSIC_REFERER), 'Content-Type': 'application/json'},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    request_result = payload.get('req_0') or {}
    data = request_result.get('data') or {}
    if not isinstance(data, dict) or not data:
        raise QQLoginError(f'QQLogin 返回异常：{payload!r}')

    return {
        'musicid': int(data.get('musicid') or 0),
        'musickey': str(data.get('musickey') or ''),
        'refresh_key': str(data.get('refresh_key') or ''),
        'refresh_token': str(data.get('refresh_token') or ''),
        'access_token': str(data.get('access_token') or ''),
        'expired_at': int(data.get('expired_at') or 0),
        'unionid': str(data.get('unionid') or ''),
        'openid': str(data.get('openid') or ''),
        'str_musicid': str(data.get('str_musicid') or ''),
        'encrypt_uin': str(data.get('encryptUin') or ''),
        'key_expires_in': int(data.get('keyExpiresIn') or 0),
        'nickname': str(data.get('nickname') or ''),
    }


def build_cookies(credential: dict[str, Any], login_at: float) -> dict[str, str]:
    '''
    把登录凭据转成 musicdl 认识的一套 cookie。

    musicdl 的 Credential.fromcookiesdict 会读取 uin/musicid、qqmusic_key/musickey
    以及 tmeLoginType，这里把这些字段一次补齐。
    '''
    musicid = str(credential.get('musicid') or '')
    musickey = str(credential.get('musickey') or '')
    cookies = {
        'uin': musicid,
        'musicid': musicid,
        'qqmusic_key': musickey,
        'qm_keyst': musickey,
        'musickey': musickey,
        'tmeLoginType': '2',
        'loginType': '2',
        'psrf_musickey_createtime': str(int(login_at)),
        'psrf_qqaccess_token': str(credential.get('access_token') or ''),
        'psrf_qqrefresh_token': str(credential.get('refresh_token') or ''),
        'psrf_qqopenid': str(credential.get('openid') or ''),
        'psrf_qqunionid': str(credential.get('unionid') or ''),
        'psrf_access_token_expiresAt': str(credential.get('expired_at') or ''),
        'refresh_key': str(credential.get('refresh_key') or ''),
        'str_musicid': str(credential.get('str_musicid') or musicid),
    }
    if credential.get('encrypt_uin'):
        cookies['encryptUin'] = str(credential['encrypt_uin'])
    return cookies


def _musicu_comm(tme_login_type: int = 2) -> dict[str, Any]:
    '''musicu.fcg 公共参数。'''
    return {
        'cv': 4747474,
        'ct': 24,
        'format': 'json',
        'inCharset': 'utf-8',
        'outCharset': 'utf-8',
        'notice': 0,
        'platform': 'yqq.json',
        'needNewCode': 1,
        'uin': '0',
        'g_tk_new_20200303': 5381,
        'g_tk': 5381,
        'tmeLoginType': tme_login_type,
    }


def _qrcode_params() -> dict[str, str]:
    return {
        'appid': C.APPID,
        'e': '2',
        'l': 'M',
        's': '3',
        'd': '72',
        'v': '4',
        't': str(random.random()),
        'daid': C.DAID,
        'pt_3rd_aid': C.PT_3RD_AID,
    }


def _xlogin_params() -> dict[str, str]:
    return {
        'appid': C.APPID,
        'daid': C.DAID,
        'style': '33',
        'login_text': '授权并登录',
        'hide_title_bar': '1',
        'hide_border': '1',
        'target': 'self',
        's_url': C.OAUTH_LOGIN_JUMP,
        'pt_3rd_aid': C.PT_3RD_AID,
        'pt_feedback_link': 'https://support.qq.com/products/77942?customInfo=.appid100497308',
    }


def _random_uuid() -> str:
    '''authorize 的 ui 参数，任意 uuid4 即可。'''
    import uuid
    return str(uuid.uuid4())


def probe_login_state(cookies: dict[str, str], timeout: int = 10) -> Optional[bool]:
    '''
    远程探测 cookie 是否仍然有效。

    返回 True / False 表示明确结论，返回 None 表示网络异常、无法判断
    （调用方不应把 None 当成失效，否则网络抖动会导致误判重登）。
    '''
    if not cookies.get('uin') or not cookies.get('musickey'):
        return False

    comm = _musicu_comm(tme_login_type=2)
    comm.update({'qq': str(cookies['uin']), 'authst': str(cookies['musickey'])})
    try:
        response = requests.post(
            C.MUSICU_ENDPOINT,
            json={
                'comm': comm,
                'req_0': {'module': C.USERINFO_MODULE, 'method': C.USERINFO_METHOD, 'param': {}},
            },
            cookies=cookies,
            headers={**_headers(C.QQMUSIC_REFERER), 'Content-Type': 'application/json'},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None

    request_result = payload.get('req_0')
    if not isinstance(request_result, dict):
        return None
    # 实测未登录 / key 失效时返回 {"code":1000}，正常登录时为 0
    if request_result.get('code') != 0:
        return False
    data = request_result.get('data')
    if isinstance(data, dict):
        inner_code = data.get('code')
        if inner_code is not None and inner_code != 0:
            return False
    return True
