"""企业微信主动消息。

只做「主动发送」，不做被动回复。原因：被动回复有 5 秒限制，而下载完成之类的
通知是异步的（那时早已过了回复窗口），所以回调统一立即返回 success，
所有消息都走 `message/send`。

access_token 做了缓存：有效期内复用，遇失效错误码强制刷新一次再重试。
（旧项目每条消息都重新 gettoken，既慢又容易撞频次限制。）
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import requests

from app.config import settings
from utils.logger import logger

# 单条文本上限 2048 字节（企微规定），超过要截断
MAX_TEXT_BYTES = 2048
# 提前 5 分钟认为 token 过期，避免边界上翻车
TOKEN_REFRESH_AHEAD_SECONDS = 300
REQUEST_TIMEOUT = (10, 20)

# 这些错误码表示 token 有问题，刷新后重试一次
TOKEN_INVALID_ERRCODES = {40001, 40014, 41001, 42001}

# trust_env=False：不走环境里的 HTTP_PROXY。
# 企微 API 要么直连、要么用 WECHAT_PROXY 指定的自建反代，
# 继承环境代理反而会让内网部署（例如指向本机 mock）失败。
_session = requests.Session()
_session.trust_env = False

_lock = threading.Lock()
_token: Optional[str] = None
_token_expire_at: float = 0.0


class WeComError(RuntimeError):
    """调用企微接口失败。"""


# --- access_token -----------------------------------------------------------

def _fetch_token() -> tuple[str, float]:
    url = f'{settings.wechat_proxy}/cgi-bin/gettoken'
    resp = _session.get(url, params={
        'corpid': settings.corp_id,
        'corpsecret': settings.agent_secret,
    }, timeout=REQUEST_TIMEOUT)
    data = resp.json()
    if data.get('errcode') != 0:
        raise WeComError(f'gettoken 失败: errcode={data.get("errcode")} '
                         f'errmsg={data.get("errmsg")}')
    token = data.get('access_token') or ''
    if not token:
        raise WeComError('gettoken 未返回 access_token')
    expires_in = int(data.get('expires_in') or 7200)
    return token, time.time() + max(expires_in - TOKEN_REFRESH_AHEAD_SECONDS, 60)


def get_access_token(force: bool = False) -> str:
    """取 access_token（带缓存）。"""
    global _token, _token_expire_at
    with _lock:
        if not force and _token and time.time() < _token_expire_at:
            return _token
        _token, _token_expire_at = _fetch_token()
        logger.debug('access_token 已刷新（%d 秒后过期）',
                     int(_token_expire_at - time.time()))
        return _token


# --- 发送 -------------------------------------------------------------------

def _truncate_bytes(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    """按 UTF-8 字节数截断，且不切坏多字节字符。"""
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    suffix = '\n…（内容过长已截断）'
    room = limit - len(suffix.encode('utf-8'))
    truncated = raw[:room].decode('utf-8', 'ignore')
    return truncated + suffix


def _post_message(payload: dict[str, Any]) -> dict[str, Any]:
    """调 message/send，token 失效时自动刷新重试一次。"""
    for attempt in range(2):
        token = get_access_token(force=attempt > 0)
        url = f'{settings.wechat_proxy}/cgi-bin/message/send'
        resp = _session.post(url, params={'access_token': token},
                             json=payload, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        errcode = data.get('errcode')
        if errcode == 0:
            return data
        if errcode in TOKEN_INVALID_ERRCODES and attempt == 0:
            logger.warning('access_token 失效（errcode=%s），刷新后重试', errcode)
            continue
        raise WeComError(f'message/send 失败: errcode={errcode} errmsg={data.get("errmsg")}')
    raise WeComError('message/send 重试后仍失败')


def send_text(content: str, touser: str, *, raise_on_error: bool = False) -> bool:
    """给指定成员发文本（touser 传 '@all' 即全体）。"""
    payload = {
        'touser': touser,
        'msgtype': 'text',
        'agentid': settings.agent_id,
        'text': {'content': _truncate_bytes(content)},
        'safe': 0,
    }
    try:
        _post_message(payload)
        logger.info('已发送文本给 %s（%d 字节）', touser, len(content.encode('utf-8')))
        return True
    except Exception as e:
        logger.error('发送文本失败（touser=%s）：%s', touser, e)
        if raise_on_error:
            raise
        return False


def broadcast_text(content: str, *, raise_on_error: bool = False) -> bool:
    """给应用可见范围内的全体成员发文本。"""
    return send_text(content, '@all', raise_on_error=raise_on_error)


# --- 图片 -------------------------------------------------------------------

def upload_media(data: bytes, *, media_type: str = 'image',
                 filename: str = 'qrcode.png',
                 content_type: str = 'image/png') -> str:
    """上传临时素材，返回 media_id（3 天内有效，所以每次现传）。"""
    token = get_access_token()
    url = f'{settings.wechat_proxy}/cgi-bin/media/upload'
    resp = _session.post(
        url, params={'access_token': token, 'type': media_type},
        files={'media': (filename, data, content_type)},
        timeout=(10, 60))
    result = resp.json()
    if result.get('errcode') != 0:
        raise WeComError(f'media/upload 失败: errcode={result.get("errcode")} '
                         f'errmsg={result.get("errmsg")}')
    media_id = result.get('media_id') or ''
    if not media_id:
        raise WeComError('media/upload 未返回 media_id')
    logger.info('素材已上传（%d 字节，media_id 长度 %d）', len(data), len(media_id))
    return media_id


def send_image(data: bytes, touser: str, *, raise_on_error: bool = False) -> bool:
    """发图片消息（失败时调用方应降级为「文本 + 链接」）。"""
    if not settings.image_enabled:
        logger.info('图片消息已被配置关闭，跳过')
        return False
    try:
        media_id = upload_media(data)
        _post_message({
            'touser': touser,
            'msgtype': 'image',
            'agentid': settings.agent_id,
            'image': {'media_id': media_id},
            'safe': 0,
        })
        logger.info('已发送图片给 %s', touser)
        return True
    except Exception as e:
        logger.error('发送图片失败（touser=%s）：%s', touser, e)
        if raise_on_error:
            raise
        return False


def probe_credentials() -> tuple[bool, str]:
    """启动自检用：能不能拿到 access_token。"""
    try:
        get_access_token(force=True)
        return True, 'access_token 获取成功'
    except Exception as e:
        return False, str(e)
