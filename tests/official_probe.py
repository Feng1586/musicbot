"""探测：能否绕开反代，直连企业微信官方 API。

背景
    用户那台反代 `zd.i-am-a.gay` 是按路径白名单的，只放行了 gettoken 与
    message/send，`media/upload` 返回 nginx 404 —— 于是二维码图片发不出去。
    但企微官方域名在国内本来就可直连，若直连可用，这个限制就绕开了。

本脚本只做**只读**与**一次临时素材上传**（上传一张 1x1 的 PNG 到素材库，
3 天后自动过期，不会推送给任何人）。**不发任何消息。**
"""
from __future__ import annotations

import io
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from app.config import settings  # noqa: E402

# 1x1 透明 PNG（手工构造，不依赖 Pillow）
def _tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, 'big') + tag + data
                + zlib.crc32(tag + data).to_bytes(4, 'big'))
    ihdr = (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + bytes([8, 6, 0, 0, 0])
    idat = zlib.compress(bytes([0, 0, 0, 0, 0]))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', idat) + chunk(b'IEND', b''))


OFFICIAL = 'https://qyapi.weixin.qq.com'
CORP = settings.corp_id
SECRET = settings.agent_secret

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, ok, detail))
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  → {detail}' if detail else ''))


def session_for(proxy: str | None) -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
    return s


def get_token(sess: requests.Session, base: str) -> tuple[str, str]:
    """返回 (token, 原始说明)。"""
    try:
        r = sess.get(f'{base}/cgi-bin/gettoken',
                     params={'corpid': CORP, 'corpsecret': SECRET},
                     timeout=(10, 20))
    except Exception as e:
        return '', f'请求异常 {e!r}'[:90]
    if r.status_code != 200:
        return '', f'HTTP {r.status_code} {r.text[:60]}'
    try:
        d = r.json()
    except Exception:
        return '', f'非 JSON：{r.text[:60]!r}'
    if d.get('errcode') != 0:
        return '', f"errcode={d.get('errcode')} {d.get('errmsg')}"
    return d.get('access_token', ''), f"token {len(d.get('access_token', ''))} 字符"


print('=' * 74)
print(f'1. 直连官方 {OFFICIAL}（清空代理变量，走 TUN 分流）')
print('=' * 74)
sess = session_for(None)
token, note = get_token(sess, OFFICIAL)
check('直连官方取 access_token', bool(token), note)
official_ok = bool(token)

if official_ok:
    print()
    print('=' * 74)
    print('2. 直连官方 media/upload（上传一张 1x1 PNG 到临时素材库，不会发消息）')
    print('=' * 74)
    png = _tiny_png()
    print(f'  待上传 PNG：{len(png)} 字节')
    try:
        r = sess.post(f'{OFFICIAL}/cgi-bin/media/upload',
                      params={'access_token': token, 'type': 'image'},
                      files={'media': ('wb_probe.png', png, 'image/png')},
                      timeout=(10, 30))
        print(f'  HTTP {r.status_code}  Content-Type={r.headers.get("Content-Type")}')
        print(f'  响应前 200 字：{r.text[:200]!r}')
        ok = False
        detail = ''
        if r.status_code == 200:
            try:
                d = r.json()
                ok = bool(d.get('media_id'))
                detail = (f"media_id {len(d.get('media_id') or '')} 字符"
                          if ok else f"errcode={d.get('errcode')} {d.get('errmsg')}")
            except Exception as e:
                detail = f'非 JSON：{r.text[:80]!r}'
        else:
            detail = f'HTTP {r.status_code}'
        check('直连官方 media/upload 可用（→ B1 可直接绕开）', ok, detail)
    except Exception as e:
        check('直连官方 media/upload 可用（→ B1 可直接绕开）', False, repr(e)[:90])

    print()
    print('=' * 74)
    print('3. 顺手确认另外两个被反代挡掉的只读接口')
    print('=' * 74)
    for path, params, label in [
        ('/cgi-bin/agent/get', {'agentid': settings.agent_id}, 'agent/get'),
        ('/cgi-bin/user/simplelist', {'department_id': 1}, 'user/simplelist'),
    ]:
        try:
            r = sess.get(OFFICIAL + path,
                         params={'access_token': token, **params}, timeout=(10, 20))
            try:
                d = r.json()
                check(f'直连 {label}', d.get('errcode') == 0,
                      f"errcode={d.get('errcode')} {d.get('errmsg')}")
            except Exception:
                check(f'直连 {label}', False, f'HTTP {r.status_code}')
        except Exception as e:
            check(f'直连 {label}', False, repr(e)[:80])

print()
print('=' * 74)
print('4. 对比：经现有反代', settings.wechat_proxy)
print('=' * 74)
sess2 = session_for(None)
token2, note2 = get_token(sess2, settings.wechat_proxy.rstrip('/'))
check('反代取 access_token', bool(token2), note2)
if token2:
    try:
        r = sess2.post(f'{settings.wechat_proxy.rstrip("/")}/cgi-bin/media/upload',
                       params={'access_token': token2, 'type': 'image'},
                       files={'media': ('wb_probe.png', _tiny_png(), 'image/png')},
                       timeout=(10, 30))
        print(f'  media/upload HTTP {r.status_code}：{r.text[:150]!r}')
    except Exception as e:
        print(f'  media/upload 异常：{e!r}')

print()
passed = sum(1 for _n, ok, _d in results if ok)
print('=' * 74)
print(f'结果：{passed}/{len(results)}')
print('=' * 74)
print()
if official_ok and any('media/upload 可用' in n and ok for n, ok, _ in results):
    print('结论：直连官方完全可用 → 推荐把 WECHAT_PROXY 改为 https://qyapi.weixin.qq.com/，')
    print('      这样 media/upload 不再受反代白名单限制，二维码图片可以直接发。')
else:
    print('结论：直连官方不可用或 media/upload 仍失败，B1 只能靠 PUBLIC_BASE_URL 兜底。')
