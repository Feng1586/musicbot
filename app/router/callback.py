"""企业微信回调路由。

两条铁律（都是旧项目验证过的）：
1. **立即返回 success**。企微要求 5 秒内响应，而搜索/下载可能几十秒，
   业务一律丢给 BackgroundTasks。
2. **异常也要返回 success**。返回非 200 会让企微重试三次，而我们这边
   重试通常还是同样的结果（比如签名错），只会刷日志。
"""

from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse

from app import commands
from app.config import settings
from app.crypto import WeComCrypto, WeComCryptoError
from utils.logger import logger

router = APIRouter()

_crypto: Optional[WeComCrypto] = None
_crypto_lock = threading.Lock()


def get_crypto() -> WeComCrypto:
    """惰性构造（配置在 main 里已自检过，这里不会拿到空值）。"""
    global _crypto
    with _crypto_lock:
        if _crypto is None:
            _crypto = WeComCrypto(settings.stoken, settings.encoding_aes_key,
                                  settings.corp_id)
        return _crypto


def reset_crypto() -> None:
    """配置变更后重建（给测试用）。"""
    global _crypto
    with _crypto_lock:
        _crypto = None


@router.get('/wechat/callback', response_class=PlainTextResponse)
def verify_url(msg_signature: str = '', timestamp: str = '', nonce: str = '',
               echostr: str = '') -> PlainTextResponse:
    """企微后台「接收消息」保存时的 URL 验证：解出 echostr 原样返回。"""
    if not echostr:
        return PlainTextResponse('musicbot callback is running', status_code=200)
    try:
        plain = get_crypto().verify_url(msg_signature, timestamp, nonce, echostr)
        logger.info('回调 URL 验证通过')
        return PlainTextResponse(plain, status_code=200)
    except WeComCryptoError as e:
        logger.warning('回调 URL 验证失败：%s', e)
        return PlainTextResponse('invalid signature', status_code=401)
    except Exception as e:
        logger.error('回调 URL 验证异常：%s', e, exc_info=True)
        return PlainTextResponse('error', status_code=500)


@router.post('/wechat/callback', response_class=PlainTextResponse)
async def receive(request: Request, background: BackgroundTasks,
                  msg_signature: str = '', timestamp: str = '', nonce: str = ''
                  ) -> PlainTextResponse:
    """接收消息：验签解密 → 交给后台 → 立刻回 success。"""
    raw = await request.body()
    try:
        body = raw.decode('utf-8')
    except UnicodeDecodeError:
        logger.warning('回调请求体不是 UTF-8，忽略')
        return PlainTextResponse('success', status_code=200)

    try:
        msg = get_crypto().decrypt_message(body, msg_signature, timestamp, nonce)
    except WeComCryptoError as e:
        # 验签/解密失败：不重试也没意义，照常回 success，但留日志
        logger.warning('回调解密失败：%s', e)
        return PlainTextResponse('success', status_code=200)
    except Exception as e:
        logger.error('回调处理异常：%s', e, exc_info=True)
        return PlainTextResponse('success', status_code=200)

    background.add_task(_safe_handle, msg)
    return PlainTextResponse('success', status_code=200)


def _safe_handle(msg: dict[str, str]) -> None:
    """后台执行，任何异常都不该冒泡到事件循环。"""
    try:
        commands.handle_message(msg)
    except Exception as e:
        commands.handle_error(msg, e)
