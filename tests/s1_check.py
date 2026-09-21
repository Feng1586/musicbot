"""S1 验收：企微回调加解密 + 回调链路 + 主动发送。

分三段：
A. 加解密与 musicdl 项目里那份 `WXBizMsgCrypt`（SDK 拷贝）**交叉验证** ——
   自己跟自己对不算数，必须和一份独立的实现结果一致。
B. 起真实的 uvicorn 服务，用真实的 Token/AESKey/CorpID 走一遍：
   /health、状态页、URL 验证、错误签名被拒、加密文本消息回调。
C. 用本地 mock 企微接住主动发送，确认报文内容与 access_token 缓存生效。

用法：python tests/s1_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.crypto import WeComCrypto, build_reply_xml, sign  # noqa: E402

MOCK_PORT = 18997
APP_PORT = 18000
SDK_ROOT = r'G:\Desktop\Code\WeChat-Music\musicdl'   # 仅用于交叉验证，可缺省

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.path.join(ROOT, '.env')
    with open(path, encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


# --- A. 与 SDK 交叉验证 ------------------------------------------------------

def part_a(env: dict[str, str]) -> None:
    print('=' * 72)
    print('A. 加解密：与 musicdl 项目的 WXBizMsgCrypt 交叉验证')
    print('=' * 72)

    token = env['STOKEN']
    aes_key = env['S_ENCODING_AES_KEY']
    corp_id = env['S_CORP_ID']
    ours = WeComCrypto(token, aes_key, corp_id)

    # A1 自测往返
    plain = '<xml><Content><![CDATA[青花瓷 & test 测试]]></Content></xml>'
    enc = ours.encrypt(plain)
    check('A1 自己加密→自己解密', ours.decrypt(enc) == plain, f'密文 {len(enc)} 字符')

    # A2 签名算法一致性（独立复算一遍）
    ts, nonce = '1711111111', 'abcdef'
    expected = __import__('hashlib').sha1(
        ''.join(sorted([token, ts, nonce, enc])).encode()).hexdigest()
    check('A2 签名算法与规范一致', sign(token, ts, nonce, enc) == expected)

    sdk_path = os.path.join(SDK_ROOT, 'weworkapi', 'callback_python3', 'WXBizMsgCrypt.py')
    if not os.path.exists(sdk_path):
        check('A3 与 SDK 交叉验证', True, '跳过（未找到 SDK，仅自测）')
        return

    if SDK_ROOT not in sys.path:
        sys.path.insert(0, SDK_ROOT)
    try:
        from weworkapi.callback_python3.WXBizMsgCrypt import WXBizMsgCrypt
    except Exception as e:                       # SDK 依赖不在也跳过
        check('A3 与 SDK 交叉验证', True, f'跳过（SDK 导入失败：{e}）')
        return

    sdk = WXBizMsgCrypt(token, aes_key, corp_id)

    # A3：我们加密 → SDK 解密
    err, decrypted = sdk.DecryptMsg(
        _wrap_encrypt_xml(enc), sign(token, ts, nonce, enc), ts, nonce)
    check('A3 我们加密 → SDK 解密', err == 0 and _as_text(decrypted) == plain,
          f'errcode={err}')

    # A4：SDK 加密 → 我们解密
    err, enc_xml = sdk.EncryptMsg(plain, nonce, ts)
    if err != 0:
        check('A4 SDK 加密 → 我们解密', False, f'SDK 加密失败 errcode={err}')
        return
    enc_from_sdk = _extract(enc_xml, 'Encrypt')
    ok = ours.decrypt(enc_from_sdk, check_signature=True, timestamp=ts,
                      nonce=nonce, msg_signature=sign(token, ts, nonce, enc_from_sdk)) == plain
    check('A4 SDK 加密 → 我们解密（含验签）', ok)

    # A5：echostr 的 URL 验证路径（SDK 的 VerifyURL 对拍）
    echo = 'echo_string_12345'
    err, enc_echo_xml = sdk.EncryptMsg(echo, nonce, ts)
    enc_echo = _extract(enc_echo_xml, 'Encrypt')
    our_echo = ours.verify_url(sign(token, ts, nonce, enc_echo), ts, nonce, enc_echo)
    err2, sdk_echo = sdk.VerifyURL(sign(token, ts, nonce, enc_echo), ts, nonce, enc_echo)
    check('A5 URL 验证：我们与 SDK 解出同一个 echostr',
          our_echo == echo and _as_text(sdk_echo) == echo,
          f'ours={our_echo!r} sdk={_as_text(sdk_echo)!r}')

    # A6：篡改签名必须被拒
    try:
        ours.verify_url('0' * 40, ts, nonce, enc_echo)
        check('A6 篡改签名被拒绝', False, '竟然通过了')
    except Exception as e:
        check('A6 篡改签名被拒绝', True, type(e).__name__)


def _wrap_encrypt_xml(encrypt: str) -> str:
    return f'<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>'


def _as_text(value) -> str:
    """SDK 在部分版本里返回 bytes，统一成 str 再比较。"""
    if isinstance(value, (bytes, bytearray)):
        return value.decode('utf-8')
    return value if isinstance(value, str) else str(value)


def _extract(xml_text: str, tag: str) -> str:
    import re
    m = re.search(rf'<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>', xml_text, re.S)
    return m.group(1) if m else ''


# --- C. mock 企微 -----------------------------------------------------------

class MockWeCom(BaseHTTPRequestHandler):
    records: list[dict] = []

    def log_message(self, *args):        # 静音
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        MockWeCom.records.append({'method': 'GET', 'path': parsed.path, 'query': query})
        if parsed.path.endswith('/gettoken'):
            self._json({'errcode': 0, 'errmsg': 'ok',
                        'access_token': 'MOCK_TOKEN_1', 'expires_in': 7200})
        else:
            self._json({'errcode': 0, 'errmsg': 'ok'})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception:
            payload = {'_raw_len': len(raw)}
        MockWeCom.records.append({'method': 'POST', 'path': parsed.path,
                                  'query': dict(urllib.parse.parse_qsl(parsed.query)),
                                  'json': payload})
        self._json({'errcode': 0, 'errmsg': 'ok'})


def start_mock() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), MockWeCom)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- B + C. 端到端 ----------------------------------------------------------

def wait_for(url: str, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def part_bc(env: dict[str, str]) -> None:
    print()
    print('=' * 72)
    print('B/C. 起真实服务 + mock 企微，走完整回调链路')
    print('=' * 72)

    # 版本号从文件读，不走 import —— 原因见 tests/_common.py 的说明
    # （工作区里旧项目也有个顶层 utils 包，import 会撞名）
    from _common import read_version

    bot_version = read_version()

    start_mock()
    child_env = dict(os.environ)
    child_env.update({
        'WECHAT_PROXY': f'http://127.0.0.1:{MOCK_PORT}',
        'MUSICBOT_HOST': '127.0.0.1',
        'MUSICBOT_PORT': str(APP_PORT),
        'MUSICBOT_DEFAULT_SOURCE': 'qq',
        # 关掉启动广播与 Cookie 体检：这两条后台线程发的 @all 消息会混进
        # mock 记录，让 C 段「发了几条、发给谁」的断言随机失败。
        # 启动广播本身由 e2e_check.py 负责验证。
        'MUSICBOT_STARTUP_BROADCAST': 'false',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    log_path = os.path.join(ROOT, '.wb_s1_server.log')
    log_file = open(log_path, 'w', encoding='utf-8', buffering=1)
    proc = subprocess.Popen([sys.executable, 'main.py'], cwd=ROOT, env=child_env,
                            stdout=log_file, stderr=subprocess.STDOUT)
    try:
        base = f'http://127.0.0.1:{APP_PORT}'
        up = wait_for(f'{base}/health')
        check('B1 服务启动，/health 可用', up)
        if not up:
            return

        with urllib.request.urlopen(f'{base}/') as resp:
            page = resp.read().decode('utf-8')
        check('B2 状态页 200 且含版本号',
              'musicbot' in page and f'v{bot_version}' in page,
              f'页面 {len(page)} 字节，期望 v{bot_version}，'
              f'含 musicbot={"musicbot" in page}')

        crypto = WeComCrypto(env['STOKEN'], env['S_ENCODING_AES_KEY'], env['S_CORP_ID'])
        ts, nonce = str(int(time.time())), 'wbnonce'

        # B3 URL 验证
        echo = 'wb-echo-string'
        enc_echo = crypto.encrypt(echo)
        url = (f'{base}/wechat/callback?msg_signature={sign(env["STOKEN"], ts, nonce, enc_echo)}'
               f'&timestamp={ts}&nonce={nonce}&echostr={urllib.parse.quote(enc_echo)}')
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode()
        check('B3 URL 验证返回解出的 echostr', body == echo, f'返回 {body[:30]!r}')

        # B4 错误签名被拒
        bad = url.replace(sign(env['STOKEN'], ts, nonce, enc_echo), '0' * 40)
        try:
            with urllib.request.urlopen(bad) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        check('B4 错误签名返回 401', code == 401, f'HTTP {code}')

        # B5 加密文本消息
        text_msg = (f'<xml><ToUserName><![CDATA[{env["S_CORP_ID"]}]]></ToUserName>'
                    f'<FromUserName><![CDATA[wb_test_user]]></FromUserName>'
                    f'<CreateTime>{ts}</CreateTime>'
                    f'<MsgType><![CDATA[text]]></MsgType>'
                    f'<Content><![CDATA[/version]]></Content>'
                    f'<MsgId>1234567890</MsgId>'
                    f'<AgentID>{env["AGENT_ID"]}</AgentID></xml>')
        enc_msg = crypto.encrypt(text_msg)
        post_body = _wrap_encrypt_xml(enc_msg).encode('utf-8')
        req = urllib.request.Request(
            f'{base}/wechat/callback?msg_signature='
            f'{sign(env["STOKEN"], ts, nonce, enc_msg)}&timestamp={ts}&nonce={nonce}',
            data=post_body, method='POST',
            headers={'Content-Type': 'application/xml'})
        t0 = time.time()
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            post_code = resp.status
        cost = time.time() - t0
        check('B5 文本回调立即返回 success',
              post_code == 200 and body.strip() == 'success', f'{cost * 1000:.0f}ms')

        # B6 事件消息（无 MsgId）不应报错
        event_msg = (f'<xml><ToUserName><![CDATA[{env["S_CORP_ID"]}]]></ToUserName>'
                     f'<FromUserName><![CDATA[wb_test_user]]></FromUserName>'
                     f'<CreateTime>{ts}</CreateTime>'
                     f'<MsgType><![CDATA[event]]></MsgType>'
                     f'<Event><![CDATA[change_app_admin]]></Event></xml>')
        enc_ev = crypto.encrypt(event_msg)
        req = urllib.request.Request(
            f'{base}/wechat/callback?msg_signature='
            f'{sign(env["STOKEN"], ts, nonce, enc_ev)}&timestamp={ts}&nonce={nonce}',
            data=_wrap_encrypt_xml(enc_ev).encode(), method='POST',
            headers={'Content-Type': 'application/xml'})
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
        check('B6 事件消息返回 success（不 400/500）', body.strip() == 'success')

        time.sleep(2.5)     # 等后台任务把消息发出去

        # C1/C2 mock 收到的报文
        sends = [r for r in MockWeCom.records if r['path'].endswith('/message/send')]
        tokens = [r for r in MockWeCom.records if r['path'].endswith('/gettoken')]
        check('C1 主动发送被 mock 收到', bool(sends), f'{len(sends)} 条')
        check('C2 access_token 只取了一次（缓存生效）', len(tokens) == 1,
              f'gettoken 调用 {len(tokens)} 次')

        sent_texts = [s['json'].get('text', {}).get('content', '') for s in sends]
        joined = '\n'.join(sent_texts)
        check('C3 /version 的回复内容正确', f'musicbot v{bot_version}' in joined,
              f'期望 v{bot_version}；收到的文本={[t[:36] for t in sent_texts]}')

        # 只看「发给发消息那个人」的：@all 是广播，不该混进这里的断言。
        # （上面已经用 MUSICBOT_STARTUP_BROADCAST=false 关了启动广播，
        #  但保留这层过滤，免得以后有人改回默认值时又变成偶发失败。）
        replies = [s for s in sends if s['json'].get('touser') != '@all']
        if replies:
            payload = replies[0]['json']
            check('C4 收件人、msgtype、agentid 正确',
                  payload.get('touser') == 'wb_test_user'
                  and payload.get('msgtype') == 'text'
                  and payload.get('agentid') == int(env['AGENT_ID']),
                  f"touser={payload.get('touser')} agentid={payload.get('agentid')}")
        else:
            check('C4 收件人、msgtype、agentid 正确', False, '没有发给测试用户的回复')
        check('C5 只回了一条给发消息的人（事件消息没有触发发送）',
              len(replies) == 1, f'发给测试用户 {len(replies)} 条，总 {len(sends)} 条')

        # B7 日志不重复（旧项目同一行打两遍）
        log_file.flush()
        log_text = open(log_path, encoding='utf-8', errors='replace').read()
        hit = log_text.count('回调 URL 验证通过')
        check('B7 日志没有重复输出（同一行只出现一次）', hit == 1, f'出现 {hit} 次')
        check('B8 日志里没有 Traceback', 'Traceback' not in log_text)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def main() -> int:
    print()
    env = load_env()
    part_a(env)
    # part_a 为了让旧项目的 SDK 参与交叉验证，把 `musicdl/` 的根目录插进了 sys.path。
    # 那边**也有一个顶层 `utils` 包**（版本号是旧机器人的 1.2.0）。如果留着它，
    # 之后第一次 `import utils` 就会解析到旧项目去 —— 这个坑实际踩过一次：
    # 版本断言拿到 1.2.0，看起来像服务端版本不对，其实是测试自己导错了包。
    from _common import drop_from_sys_path

    drop_from_sys_path(SDK_ROOT)
    part_bc(env)

    print()
    print('=' * 72)
    ok = sum(1 for _n, o, _d in results if o)
    print(f'结果：{ok}/{len(results)} 通过')
    for n, o, d in results:
        if not o:
            print(f'   FAIL -> {n}  {d}')
    print('=' * 72)
    print(f'（服务端日志：{os.path.join(ROOT, ".wb_s1_server.log")}）')
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
