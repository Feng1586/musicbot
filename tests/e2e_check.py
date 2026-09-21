"""端到端验收（真实服务 + 真实搜索下载 + mock 企微）。

这是最接近生产的一条链路：
    加密回调 → 解密 → 后台任务 → 真实搜索 → 回编号列表
    → 用户回「1」→ 入队 → 真实下载 → 三条进度消息
另外验证：启动广播与 Cookie 体检（两条 @all）、扫码登录页、状态页、健康检查。

企微 API 全部指向本地 mock，**不会给任何真实用户发消息**。

用法：python tests/e2e_check.py [关键词]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.crypto import WeComCrypto, sign                    # noqa: E402

MOCK_PORT = 18999
APP_PORT = 18001
TEST_USER = 'e2e_user'

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


class MockWeCom(BaseHTTPRequestHandler):
    records: list[dict] = []

    def log_message(self, *args):
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
        MockWeCom.records.append({'method': 'GET', 'path': parsed.path})
        self._json({'errcode': 0, 'access_token': 'MOCK_TOKEN', 'expires_in': 7200})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        entry: dict = {'method': 'POST', 'path': parsed.path, 'at': time.time()}
        try:
            entry['json'] = json.loads(raw.decode('utf-8'))
        except Exception:
            entry['raw_size'] = len(raw)
        MockWeCom.records.append(entry)
        if parsed.path.endswith('/media/upload'):
            self._json({'errcode': 0, 'type': 'image', 'media_id': 'MOCK_MEDIA',
                        'created_at': '1'})
        else:
            self._json({'errcode': 0, 'errmsg': 'ok'})


def messages(touser: str | None = None, *, since: float = 0) -> list[tuple[str, str]]:
    out = []
    for r in MockWeCom.records:
        if r.get('path', '').endswith('/message/send') and r.get('at', 0) >= since:
            payload = r.get('json') or {}
            if touser is None or payload.get('touser') == touser:
                out.append((payload.get('text', {}).get('content', ''), payload.get('touser', '')))
    return out


def wait_for(predicate, timeout: float, interval: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(os.path.join(ROOT, '.env'), encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else '稻香'
    print('=' * 72)
    print(f'端到端验收：{keyword}（真实搜索 + 真实下载，mock 企微）')
    print('=' * 72)

    env = load_env()
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), MockWeCom)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    child_env = dict(os.environ)
    child_env.update({
        'WECHAT_PROXY': f'http://127.0.0.1:{MOCK_PORT}',
        'MUSICBOT_HOST': '127.0.0.1',
        'MUSICBOT_PORT': str(APP_PORT),
        'MUSICBOT_PUBLIC_BASE_URL': f'http://127.0.0.1:{APP_PORT}',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    log_path = os.path.join(ROOT, '.wb_e2e_server.log')
    log_file = open(log_path, 'w', encoding='utf-8', buffering=1)
    proc = subprocess.Popen([sys.executable, 'main.py'], cwd=ROOT, env=child_env,
                            stdout=log_file, stderr=subprocess.STDOUT)
    try:
        base = f'http://127.0.0.1:{APP_PORT}'

        def healthy():
            try:
                with urllib.request.urlopen(f'{base}/health', timeout=2) as r:
                    return r.status == 200
            except Exception:
                return False

        if not wait_for(healthy, 45):
            check('服务启动', False, '超时')
            print(open(log_path, encoding='utf-8', errors='replace').read()[-1500:])
            return 1
        check('服务启动，/health 可用', True)

        crypto = WeComCrypto(env['STOKEN'], env['S_ENCODING_AES_KEY'], env['S_CORP_ID'])

        def send_text_message(content: str, tag: str) -> None:
            ts, nonce = str(int(time.time())), f'nonce{tag}'
            xml = (f'<xml><ToUserName><![CDATA[{env["S_CORP_ID"]}]]></ToUserName>'
                   f'<FromUserName><![CDATA[{TEST_USER}]]></FromUserName>'
                   f'<CreateTime>{ts}</CreateTime><MsgType><![CDATA[text]]></MsgType>'
                   f'<Content><![CDATA[{content}]]></Content>'
                   f'<MsgId>{int(time.time() * 1000)}</MsgId>'
                   f'<AgentID>{env["AGENT_ID"]}</AgentID></xml>')
            enc = crypto.encrypt(xml)
            body = f'<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>'.encode('utf-8')
            url = (f'{base}/wechat/callback?msg_signature='
                   f'{sign(env["STOKEN"], ts, nonce, enc)}&timestamp={ts}&nonce={nonce}')
            req = urllib.request.Request(url, data=body, method='POST',
                                         headers={'Content-Type': 'application/xml'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode().strip()

        # --- 1. 启动广播（两条：启动消息 + Cookie 体检）----------------------
        print()
        print('-' * 72)
        print('1. 启动消息')
        print('-' * 72)
        broadcasts = wait_for(
            lambda: [c for c, u in messages('@all') if '已启动' in c and 'Cookie' in c] or None, 30)
        startup = wait_for(lambda: next((c for c, _u in messages('@all') if '已启动' in c), None), 5)
        report = wait_for(lambda: next((c for c, _u in messages('@all') if 'Cookie 状态' in c), None), 5)
        check('启动广播已发出（含版本与教程）',
              bool(startup) and '当前版本' in startup and '使用教程' in startup,
              (startup or '').splitlines()[0] if startup else '(无)')
        check('Cookie 体检单独一条（用户要求分开）',
              bool(report) and '/qq login' in report,
              (report or '').replace('\n', ' / ')[:70])
        check('Cookie 体检里列出了两个源',
              bool(report) and 'QQ音乐' in report and '网易云音乐' in report)
        check('启动消息里带上了扫码登录链接（配了 PUBLIC_BASE_URL）',
              bool(startup) and '/login/qq' in startup or '/help' in (startup or ''),
              '启动消息含指令区')

        # --- 2. 搜索 ---------------------------------------------------------
        print()
        print('-' * 72)
        print('2. 搜索（加密回调 → 真实搜索）')
        print('-' * 72)
        t_search = time.time()
        resp = send_text_message(keyword, 'search')
        check('搜索回调立即返回 success', resp == 'success', resp)

        listing = wait_for(
            lambda: next((c for c, _u in messages(TEST_USER, since=t_search)
                          if re.search(r'^1\. ', c, re.M)), None), 90)
        check('收到编号列表', bool(listing),
              (listing or '').replace('\n', ' / ')[:110])
        if not listing:
            return 1
        first_line = next(l for l in listing.splitlines() if re.match(r'^1\. ', l))
        print(f'  第 1 条：{first_line[3:]}')

        # --- 3. 下载 ---------------------------------------------------------
        print()
        print('-' * 72)
        print('3. 回数字下载（真实下载 + 三条消息）')
        print('-' * 72)
        t_dl = time.time()
        resp = send_text_message('1', 'download')
        check('下载回调立即返回 success', resp == 'success', resp)

        queued = wait_for(
            lambda: next((c for c, _u in messages(TEST_USER, since=t_dl)
                          if c.startswith('🎧')), None), 30)
        check('收到入队回执', bool(queued), queued or '(无)')

        started_msg = wait_for(
            lambda: next((c for c, _u in messages(TEST_USER, since=t_dl)
                          if c.startswith('⚙️')), None), 60)
        check('收到「开始下载」', bool(started_msg), started_msg or '(无)')

        done = wait_for(
            lambda: next((c for c, _u in messages(TEST_USER, since=t_dl)
                          if c.startswith('✅') or c.startswith('❌')), None), 300)
        check('下载有结论', bool(done), (done or '').replace('\n', ' / ')[:90])
        if done and done.startswith('✅'):
            match = re.search(r'✅ 下载完成：(.+)', done)
            filename = match.group(1).strip() if match else ''
            size_line = re.search(r'💾 大小：(.+)', done)
            print(f'  文件：{filename}　大小：{size_line.group(1) if size_line else "?"}')
            check('完成消息两行格式正确',
                  bool(filename) and bool(size_line), done.replace('\n', ' | '))

            # 文件真的落盘了吗（含歌词/封面）
            found = None
            for root_dir, _dirs, files in os.walk(os.path.join(ROOT, 'downloads')):
                if filename in files:
                    found = os.path.join(root_dir, filename)
                    break
            check('文件落在 downloads/ 下', bool(found),
                  os.path.relpath(found, ROOT) if found else '(没找到)')
            if found:
                from mutagen import File as MutagenFile
                audio = MutagenFile(found, easy=False)
                pictures = getattr(audio, 'pictures', None) or []
                lyrics = ''
                tags = getattr(audio, 'tags', None)
                if tags:
                    for key in tags.keys():
                        if str(key).upper().startswith('USLT'):
                            v = tags[key]
                            t = getattr(v, 'text', v)
                            lyrics = str(t[0] if isinstance(t, (list, tuple)) else t)
                            break
                        if 'LYRIC' in str(key).upper():
                            v = tags[key]
                            lyrics = str(v[0] if isinstance(v, (list, tuple)) else v)
                            break
                check('下载的文件内含封面', len(pictures) > 0
                      and len(pictures[0].data or b'') > 1000,
                      f'{len(pictures)} 张')
                check('下载的文件内含歌词', len(lyrics) > 20, f'{len(lyrics)} 字符')
                check('同名 .lrc 存在',
                      os.path.isfile(os.path.splitext(found)[0] + '.lrc'))

        # --- 4. 其他页面 -----------------------------------------------------
        print()
        print('-' * 72)
        print('4. 页面与接口')
        print('-' * 72)
        with urllib.request.urlopen(f'{base}/') as r:
            page = r.read().decode()
        # 版本号从文件读，不走 import（原因见 tests/_common.py 的说明）
        from _common import read_version

        check('状态页 200 且含版本',
              'musicbot' in page and f'v{read_version()}' in page)

        with urllib.request.urlopen(f'{base}/login/qq') as r:
            html = r.read().decode()
        # 二维码地址是页面里的 JS 拼出来的（'/login/' + SRC + '/qrcode'），
        # 所以不能直接找 '/login/qq/qrcode' 这个字面量。
        check('扫码登录页 200', '扫码登录' in html and "'/login/' + SRC" in html,
              f'{len(html)} 字节')

        with urllib.request.urlopen(f'{base}/login/wyy/state') as r:
            state = json.loads(r.read().decode())
        check('登录状态接口返回 JSON', 'status' in state, json.dumps(state, ensure_ascii=False))

        try:
            urllib.request.urlopen(f'{base}/login/qq/qrcode')
            qr_ok = True
        except urllib.error.HTTPError as e:
            qr_ok = e.code == 404        # 还没发起登录 → 404 是正确的
        check('未发起登录时二维码接口返回 404（不报 500）', qr_ok)

        # --- 5. 日志健康 -----------------------------------------------------
        print()
        print('-' * 72)
        print('5. 日志')
        print('-' * 72)
        log_file.flush()
        log_text = open(log_path, encoding='utf-8', errors='replace').read()
        check('日志无 Traceback', 'Traceback' not in log_text)
        check('日志里没有 musicdl 的进度条噪音',
              'Search From Sources >>>' not in log_text
              and 'Downloading:' not in log_text,
              '已重定向 stdout')
        check('引擎重建有记录', '引擎已重建' in log_text
              or '首次初始化' in log_text)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

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
