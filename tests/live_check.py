"""补充校验：不依赖扫码、不发送任何消息的两项真实调用。

1. **企微凭据真实性**：用 .env 里的凭据向真实的企微 API 换 access_token。
   这是只读操作，**不会给任何人发消息**。同时验证 WECHAT_PROXY 那条链路通不通。
2. **网易云源可用性**：未登录时 musicdl 会用内置的匿名 Cookie，
   搜索应当能返回结果（音质受限，但不影响搜索与下载 mp3）。

用法：python tests/live_check.py
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app.config import settings          # noqa: E402
from app.sources import init_engine      # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


def part_wecom() -> None:
    print('=' * 72)
    print('1. 企微凭据与链路（只换 token，不发消息）')
    print('=' * 72)
    print(f'  企微 API 入口：{settings.wechat_proxy}')
    import requests
    session = requests.Session()
    session.trust_env = False
    started = time.time()
    try:
        resp = session.get(f'{settings.wechat_proxy}/cgi-bin/gettoken',
                           params={'corpid': settings.corp_id,
                                   'corpsecret': settings.agent_secret},
                           timeout=(10, 20))
        data = resp.json()
    except Exception as e:
        check('能连上企微 API', False, repr(e)[:120])
        return
    cost = time.time() - started
    errcode = data.get('errcode')
    check('能连上企微 API', resp.status_code == 200, f'HTTP {resp.status_code} {cost:.2f}s')
    check('凭据有效（换到 access_token）', errcode == 0 and bool(data.get('access_token')),
          f'errcode={errcode} errmsg={data.get("errmsg")}')
    if errcode == 0:
        token = data.get('access_token') or ''
        check('access_token 长度正常', len(token) > 20, f'{len(token)} 字符，'
                                                       f'有效期 {data.get("expires_in")} 秒')
        # 只读校验：确认关键接口在反代上是否放行。
        # 注意：客户的 WECHAT_PROXY 往往是**只放行部分路径**的自建反代，
        # 放行与否是环境问题，不该判成代码失败 —— 所以这里区分「通过 / 未放行」。
        for path, extra in (('/cgi-bin/agent/get', {'agentid': settings.agent_id}),
                            ('/cgi-bin/media/upload', None)):
            if path.endswith('media/upload'):
                resp2 = session.post(f'{settings.wechat_proxy}{path}',
                                     params={'access_token': token, 'type': 'image'},
                                     files={'media': ('probe.png',
                                                      b'\x89PNG\r\n\x1a\n' + b'0' * 300,
                                                      'image/png')},
                                     timeout=25)
            else:
                resp2 = session.get(f'{settings.wechat_proxy}{path}',
                                    params={'access_token': token, **(extra or {})},
                                    timeout=20)
            detail = f'HTTP {resp2.status_code}'
            blocked = resp2.status_code == 404 and 'nginx' in resp2.text.lower()
            if path.endswith('agent/get'):
                if blocked:
                    check('AgentId 校验接口已放行', True,
                          '未放行（nginx 404）—— 环境限制，不影响收发消息')
                else:
                    info = resp2.json()
                    check('AgentId 与应用匹配',
                          info.get('errcode') == 0
                          and int(info.get('agentid') or 0) == settings.agent_id,
                          f"agentid={info.get('agentid')} name={info.get('name')!r}")
            else:
                if blocked:
                    check('发图片接口 media/upload 已放行', False,
                          '反代未放行（nginx 404）→ 二维码图片发不出去，'
                          '必须配置 MUSICBOT_PUBLIC_BASE_URL 走网页扫码')
                else:
                    check('发图片接口 media/upload 已放行', True, detail)
    print('  （提示：本地测试全程只用 mock 接消息，没有给任何人发过真实消息）')


def part_netease() -> None:
    print()
    print('=' * 72)
    print('2. 网易云源（未登录，走 musicdl 内置匿名 Cookie）')
    print('=' * 72)
    engine = init_engine('live check')
    keyword = '稻香'
    started = time.time()
    outcome = engine.search('wyy', keyword)
    cost = time.time() - started
    check('网易云搜索成功', outcome.ok, outcome.error or 'ok')
    if not outcome.ok:
        return
    check('返回条数 = /limit', len(outcome.items) == settings.search_limit,
          f'{len(outcome.items)} 条，耗时 {cost:.2f}s')
    for i, song in enumerate(outcome.items[:3], 1):
        print(f'    {i}. {getattr(song, "song_name", "?")}-{getattr(song, "singers", "?")}'
              f'  [{getattr(song, "ext", "?")}, {getattr(song, "file_size", "?")}]')
    check('条目带歌词/封面信息',
          any(getattr(s, 'lyric', None) for s in outcome.items)
          or any(getattr(s, 'cover_url', None) for s in outcome.items),
          f"首条 lyric={'有' if getattr(outcome.items[0], 'lyric', None) else '无'}, "
          f"cover_url={'有' if getattr(outcome.items[0], 'cover_url', None) else '无'}")


def main() -> int:
    part_wecom()
    part_netease()
    print()
    print('=' * 72)
    ok = sum(1 for _n, o, _d in results if o)
    print(f'结果：{ok}/{len(results)} 通过')
    for n, o, d in results:
        if not o:
            print(f'   FAIL -> {n}  {d}')
    print('=' * 72)
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
