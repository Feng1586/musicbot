"""企业微信回调的加解密与签名。

自己实现（约 100 行）而不引入整套 SDK 拷贝，但**开发时与
`musicdl/weworkapi/callback_python3/WXBizMsgCrypt.py` 交叉验证**，
确保两边对同一份密文结果一致 —— 见 `tests/s1_crypto_check.py`。

协议要点（企微/微信同规则）：
* 签名 = sha1(将 token、timestamp、nonce、encrypt 四个值**字典序排序**后直接拼接)
* AES-256-CBC，密钥 = base64decode(EncodingAESKey + '=')，IV 取密钥前 16 字节
* 明文结构 = random(16 字节) + 4 字节网络序消息长度 + 消息体 + receiveid
* 补位 = PKCS7，块大小 32
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET
from typing import Optional

from Crypto.Cipher import AES

BLOCK_SIZE = 32          # 企微用的是 32，不是 AES 的 16
RANDOM_PREFIX_LEN = 16


class WeComCryptoError(Exception):
    """加解密/验签失败。"""


# --- 基础工具 ---------------------------------------------------------------

def sign(token: str, timestamp: str | int, nonce: str | int, encrypt: str) -> str:
    """计算回调签名。"""
    items = sorted([str(token), str(timestamp), str(nonce), str(encrypt)])
    return hashlib.sha1(''.join(items).encode('utf-8')).hexdigest()


def _pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    pad_len = block_size - len(data) % block_size
    if pad_len == 0:
        pad_len = block_size
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    if not data:
        raise WeComCryptoError('密文为空')
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        # 不是合法补位：原样返回（部分实现对恰好整除的情况不补位）
        return data
    return data[:-pad_len]


def parse_xml(xml_text: str) -> dict[str, str]:
    """把回调 XML 解析成扁平字典（CDATA 会被自动取出）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise WeComCryptoError(f'XML 解析失败: {e}') from e
    return {child.tag: (child.text or '') for child in root}


def extract_encrypt(xml_text: str) -> str:
    """从回调 XML 里取出 <Encrypt>。"""
    value = parse_xml(xml_text).get('Encrypt', '')
    if not value:
        raise WeComCryptoError('回调 XML 里没有 Encrypt 节点')
    return value


# --- 主体 -------------------------------------------------------------------

class WeComCrypto:
    """一个应用对应一个实例（Token + EncodingAESKey + CorpId）。"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str = '') -> None:
        if not token:
            raise WeComCryptoError('Token 为空')
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError(f'EncodingAESKey 长度应为 43，当前 {len(encoding_aes_key)}')
        self.token = token
        self.corp_id = corp_id or ''
        try:
            self.key = base64.b64decode(encoding_aes_key + '=')
        except Exception as e:
            raise WeComCryptoError(f'EncodingAESKey 不是合法 base64: {e}') from e
        if len(self.key) != 32:
            raise WeComCryptoError(f'AES 密钥应为 32 字节，实际 {len(self.key)}')

    # -- 加解密 --------------------------------------------------------------

    def encrypt(self, plain_text: str) -> str:
        """加密成 base64 密文。"""
        msg = plain_text.encode('utf-8')
        payload = (os.urandom(RANDOM_PREFIX_LEN)
                   + struct.pack('>I', len(msg))
                   + msg
                   + self.corp_id.encode('utf-8'))
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(_pkcs7_pad(payload))).decode('ascii')

    def decrypt(self, encrypted: str, *, check_signature: bool = False,
                timestamp: str | int = '', nonce: str | int = '',
                msg_signature: str = '') -> str:
        """解密 base64 密文，返回明文字符串。"""
        if check_signature:
            self.check_signature(msg_signature, timestamp, nonce, encrypted)
        try:
            raw = base64.b64decode(encrypted)
        except Exception as e:
            raise WeComCryptoError(f'密文不是合法 base64: {e}') from e
        if len(raw) % 16 != 0:
            raise WeComCryptoError('密文长度不是 16 的倍数')
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = _pkcs7_unpad(cipher.decrypt(raw))
        if len(plain) < RANDOM_PREFIX_LEN + 4:
            raise WeComCryptoError('解密结果过短')
        msg_len = struct.unpack('>I', plain[RANDOM_PREFIX_LEN:RANDOM_PREFIX_LEN + 4])[0]
        body_start = RANDOM_PREFIX_LEN + 4
        body_end = body_start + msg_len
        if body_end > len(plain):
            raise WeComCryptoError('消息长度字段与内容不符')
        receive_id = plain[body_end:].decode('utf-8', 'ignore')
        if self.corp_id and receive_id != self.corp_id:
            raise WeComCryptoError(
                f'receiveid 不匹配：期望 {self.corp_id}，密文里是 {receive_id}')
        return plain[body_start:body_end].decode('utf-8')

    # -- 签名 ----------------------------------------------------------------

    def check_signature(self, signature: str, timestamp: str | int,
                        nonce: str | int, encrypt: str) -> None:
        expected = sign(self.token, timestamp, nonce, encrypt)
        if not signature or expected != signature:
            raise WeComCryptoError(f'签名校验失败（期望 {expected[:8]}…，收到 {str(signature)[:8]}…）')

    # -- 回调入口 ------------------------------------------------------------

    def verify_url(self, msg_signature: str, timestamp: str | int,
                   nonce: str | int, echostr: str) -> str:
        """GET 回调的 URL 验证：验签 + 解密 echostr，返回要原样回写的内容。"""
        return self.decrypt(echostr, check_signature=True, timestamp=timestamp,
                            nonce=nonce, msg_signature=msg_signature)

    def decrypt_message(self, post_body: str, msg_signature: str,
                        timestamp: str | int, nonce: str | int) -> dict[str, str]:
        """POST 回调：验签 + 解密 + 解析成字典。"""
        encrypted = extract_encrypt(post_body)
        self.check_signature(msg_signature, timestamp, nonce, encrypted)
        return parse_xml(self.decrypt(encrypted))

    def encrypt_reply(self, plain_xml: str, nonce: str,
                      timestamp: Optional[str | int] = None) -> dict[str, str]:
        """构造被动回复报文（本项目主要用主动发送，这里给测试与兜底用）。"""
        ts = str(timestamp or int(time.time()))
        encrypted = self.encrypt(plain_xml)
        return {
            'Encrypt': encrypted,
            'MsgSignature': sign(self.token, ts, nonce, encrypted),
            'TimeStamp': ts,
            'Nonce': str(nonce),
        }


def build_reply_xml(to_user: str, from_user: str, content: str,
                    msg_type: str = 'text') -> str:
    """拼一个文本被动回复的 XML（仅测试/兜底用）。"""
    return (
        '<xml>'
        f'<ToUserName><![CDATA[{to_user}]]></ToUserName>'
        f'<FromUserName><![CDATA[{from_user}]]></FromUserName>'
        f'<CreateTime>{int(time.time())}</CreateTime>'
        f'<MsgType><![CDATA[{msg_type}]]></MsgType>'
        f'<Content><![CDATA[{content}]]></Content>'
        '</xml>'
    )
