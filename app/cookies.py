"""Cookie 的存取、校验与「过期告警去重」。

三件事：
1. **存取**：`data/cookies/{源}.json`。该目录被 gitignore + dockerignore 双重排除。
2. **校验**：远程探活，区分三种结果 —— 可用 / 失效 / **无法判断**。
   网络抖动必须是「无法判断」，否则会误报失效、天天告警。
3. **告警去重**：状态存在 `data/cookie_state.json`，只有「可用 → 失效」这个
   变化才告警一次；之后每次巡检仍是失效则静默，直到登录成功或恢复可用。

QQ 的有效期能用服务端返回的 `key_expires_in` 推（登录时存下来）；
网易云以远程探活结果为准，不猜 TTL。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from app.config import settings
from utils.logger import logger

SOURCE_IDS = ('qq', 'wyy')

# 探活结果：True 可用 / False 失效 / None 无法判断
ProbeOk = Optional[bool]

_session = requests.Session()
_session.trust_env = False

_lock = threading.Lock()

_NETEASE_ACCOUNT_URL = 'https://music.163.com/weapi/w/nuser/account/get'


# --- 路径 -------------------------------------------------------------------

def cookie_dir() -> str:
    return os.path.join(settings.data_dir, 'cookies')


def cookie_path(source: str) -> str:
    return os.path.join(cookie_dir(), f'{source}.json')


def _state_path() -> str:
    return os.path.join(settings.data_dir, 'cookie_state.json')


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --- 存取 -------------------------------------------------------------------

@dataclass
class CookieRecord:
    source: str
    cookies: dict[str, str] = field(default_factory=dict)
    account: str = ''
    login_at: float = 0.0
    key_expires_in: int = 0

    @property
    def age_text(self) -> str:
        if not self.login_at:
            return '未知'
        seconds = max(time.time() - self.login_at, 0)
        if seconds < 3600:
            return f'{int(seconds // 60)} 分钟前'
        if seconds < 86400:
            return f'{seconds / 3600:.1f} 小时前'
        return f'{seconds / 86400:.1f} 天前'

    def expiry_text(self) -> str:
        """QQ 用服务端给的 key_expires_in 推剩余；网易云不猜。"""
        if not self.key_expires_in or not self.login_at:
            return '未知（以远程探活为准）'
        left = self.login_at + self.key_expires_in - time.time()
        if left <= 0:
            return '已过期'
        if left < 3600:
            return f'约 {int(left // 60)} 分钟后'
        if left < 86400:
            return f'约 {left / 3600:.1f} 小时后'
        return f'约 {left / 86400:.1f} 天后'


def _account_text(value) -> str:
    """账号字段可能是字符串，也可能是 {'uin','nickname'}（旧后端就是这么存的）。"""
    if isinstance(value, dict):
        return str(value.get('nickname') or value.get('uin') or '')
    return str(value or '')


def load(source: str) -> CookieRecord:
    data = _read_json(cookie_path(source))
    cookies = data.get('cookies') or {}
    if not isinstance(cookies, dict):
        cookies = {}
    return CookieRecord(
        source=source,
        cookies={str(k): str(v) for k, v in cookies.items()},
        account=_account_text(data.get('account')),
        login_at=float(data.get('login_at') or 0),
        key_expires_in=int(data.get('key_expires_in') or 0),
    )


def save(source: str, cookies: dict[str, str], *, account: str = '',
         key_expires_in: int = 0) -> CookieRecord:
    record = CookieRecord(source=source, cookies=dict(cookies), account=account,
                          login_at=time.time(), key_expires_in=int(key_expires_in or 0))
    _write_json(cookie_path(source), {
        'version': 1,
        'source': source,
        'account': record.account,
        'login_at': record.login_at,
        'login_at_text': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.login_at)),
        'key_expires_in': record.key_expires_in,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'cookies': record.cookies,
    })
    logger.info('%s 的 Cookie 已保存（账号 %s，%d 个字段）',
                source, account or '未知', len(record.cookies))
    return record


def cookies_of(source: str) -> dict[str, str]:
    """取 Cookie 字典（未登录时返回空字典，调用方自行决定是否降级）。"""
    return load(source).cookies


# --- 远程探活 ---------------------------------------------------------------

@dataclass
class ProbeResult:
    ok: ProbeOk
    account: str = ''
    reason: str = ''

    @property
    def state(self) -> str:
        if self.ok is True:
            return 'ok'
        if self.ok is False:
            return 'fail'
        return 'unknown'


def probe(source: str, cookies: Optional[dict[str, str]] = None) -> ProbeResult:
    record = load(source)
    ck = cookies if cookies is not None else record.cookies
    if not ck:
        return ProbeResult(ok=False, reason='未登录（本地没有 Cookie）')
    if source == 'qq':
        return _probe_qq(ck, record)
    if source == 'wyy':
        return _probe_netease(ck, record)
    return ProbeResult(ok=None, reason=f'不支持的源 {source}')


def _probe_qq(cookies: dict[str, str], record: CookieRecord) -> ProbeResult:
    from app.login import qq_login
    try:
        ok = qq_login.probe_login_state(cookies)
    except Exception as e:            # 探测本身出错 → 无法判断
        return ProbeResult(ok=None, reason=f'探活异常：{e}')
    if ok is None:
        return ProbeResult(ok=None, reason='网络异常，无法判断')
    account = record.account or str(cookies.get('uin') or '')
    if ok:
        return ProbeResult(ok=True, account=account)
    return ProbeResult(ok=False, account=account, reason='服务端返回未登录或 key 失效')


def _probe_netease(cookies: dict[str, str], record: CookieRecord) -> ProbeResult:
    try:
        from musicdl.modules.utils.neteaseutils import WeapiCryptoUtils
    except ImportError as e:
        return ProbeResult(ok=None, reason=f'musicdl 不可用：{e}')
    try:
        payload = WeapiCryptoUtils.encryptparams({'csrf_token': ''})
        resp = _session.post(_NETEASE_ACCOUNT_URL,
                             params={'csrf_token': ''}, data=payload,
                             cookies=cookies, timeout=(10, 15))
        data = resp.json()
    except Exception as e:
        return ProbeResult(ok=None, reason=f'探活异常：{e}')

    code = data.get('code')
    profile = data.get('profile') or {}
    account = str(profile.get('nickname') or '') or record.account
    if code == 200 and (data.get('account') or profile):
        return ProbeResult(ok=True, account=account)
    # 301 = 需要登录；-460 = 触发风控；其余按失效处理
    reason = f'服务端 code={code}' + ('（触发风控）' if code == -460 else '')
    return ProbeResult(ok=False, account=record.account, reason=reason)


# --- 告警去重 ---------------------------------------------------------------

def _load_state() -> dict:
    return _read_json(_state_path())


def should_alert(source: str, result: ProbeResult) -> bool:
    """记录状态，并回答「这次要不要告警」。

    规则：只有从「不是 fail」变成「fail」，且没告警过，才返回 True。
    - unknown（网络问题）不改变已有状态、不告警；
    - 恢复 ok 时清除告警标记，下次再失效可以再提醒一次。
    """
    with _lock:
        state = _load_state()
        entry = state.get(source) or {}
        prev = entry.get('state') or 'unknown'
        notified = bool(entry.get('notified'))

        if result.ok is None:
            state[source] = {'state': prev, 'notified': notified,
                             'reason': result.reason, 'checked_at': time.time()}
            _write_json(_state_path(), state)
            return False

        if result.ok:
            state[source] = {'state': 'ok', 'notified': False, 'reason': '',
                             'account': result.account, 'checked_at': time.time()}
            _write_json(_state_path(), state)
            logger.info('%s Cookie 状态：可用', source)
            return False

        # ok / unknown → fail
        newly_failed = prev != 'fail'
        state[source] = {'state': 'fail', 'notified': True, 'reason': result.reason,
                         'account': result.account, 'checked_at': time.time()}
        _write_json(_state_path(), state)
        alert = newly_failed and not notified
        if alert:
            logger.warning('%s Cookie 失效，将提醒用户（%s）', source, result.reason)
        else:
            logger.info('%s Cookie 仍失效，已提醒过，静默', source)
        return alert


def reset_alert(source: str) -> None:
    """登录成功后调用：清掉失效标记与已提醒标记。"""
    with _lock:
        state = _load_state()
        state.pop(source, None)
        _write_json(_state_path(), state)


def clear_all_states() -> None:
    with _lock:
        _write_json(_state_path(), {})


# --- 展示 -------------------------------------------------------------------

def status_line(source: str, name: str, cmd: str, result: ProbeResult,
                record: CookieRecord) -> str:
    """给启动消息 / /status 用的一行状态。"""
    from app import notices
    if result.ok is True:
        return notices.COOKIE_OK.format(name=name, account=result.account or '未知')
    if result.ok is False:
        if not record.cookies:
            return notices.COOKIE_MISSING.format(name=name, cmd=cmd)
        return notices.COOKIE_EXPIRED.format(name=name, cmd=cmd)
    return notices.COOKIE_UNKNOWN.format(
        name=name, reason=result.reason or '暂时无法判断')
