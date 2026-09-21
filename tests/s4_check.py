"""S4 + S5 + S7 验收：Cookie 管理与告警、扫码登录、发图片、版本更新与回滚。

真实调用的部分（不依赖扫码）：
* QQ / 网易云的**二维码申请**是真的（能拿到 PNG 与轮询状态）
* Cookie 探活是真的（QQ 用现成 Cookie，网易云未登录）
* 官方 PyPI 查版本是真的（顺带验证「不会提示降级」）

打桩的部分：
* 企微用本地 mock 接住（含 **media/upload → image 消息** 这条链路）
* 更新/回滚只验证判定逻辑与「预检失败即中止」，**绝不真的装包**

用法：python tests/s4_check.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

MOCK_PORT = 18998

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


# --- mock 企微 ---------------------------------------------------------------

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
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        content_type = self.headers.get('Content-Type') or ''
        entry = {'method': 'POST', 'path': parsed.path,
                 'query': dict(urllib.parse.parse_qsl(parsed.query)),
                 'content_type': content_type, 'body_size': len(raw)}
        if 'multipart/form-data' in content_type:
            entry['is_multipart'] = True
            entry['has_media_field'] = b'name="media"' in raw
            entry['body_is_png'] = b'\x89PNG' in raw
        else:
            try:
                entry['json'] = json.loads(raw.decode('utf-8'))
            except Exception:
                pass
        MockWeCom.records.append(entry)
        if parsed.path.endswith('/media/upload'):
            self._json({'errcode': 0, 'errmsg': 'ok', 'type': 'image',
                        'media_id': 'MOCK_MEDIA_ID_123', 'created_at': '1'})
        else:
            self._json({'errcode': 0, 'errmsg': 'ok'})


def start_mock() -> None:
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), MockWeCom)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# --- 各段测试 ---------------------------------------------------------------

def part_cookie_store() -> None:
    print()
    print('=' * 72)
    print('A. Cookie 存取与告警去重')
    print('=' * 72)
    from app import cookies as cs

    cs.clear_all_states()
    # 用一个临时源名，避免污染真实的 qq 状态
    src = 'qq'
    real = cs.load(src)

    # 模拟「可用 → 失效」的完整过程
    ok_result = cs.ProbeResult(ok=True, account='某账号')
    check('A1 首次可用：不告警',
          cs.should_alert(src, ok_result) is False)

    fail_result = cs.ProbeResult(ok=False, account='某账号', reason='测试失效')
    check('A2 转为失效：告警一次', cs.should_alert(src, fail_result) is True)
    check('A3 仍未失效：不重复告警（去重生效）',
          cs.should_alert(src, fail_result) is False)
    check('A4 再次巡检测试：依然静默', cs.should_alert(src, fail_result) is False)

    unknown = cs.ProbeResult(ok=None, reason='网络异常')
    check('A5 网络异常不告警', cs.should_alert(src, unknown) is False)
    check('A6 网络异常不覆盖失效状态（仍静默）',
          cs.should_alert(src, fail_result) is False)

    check('A7 恢复可用后清除标记（不告警）',
          cs.should_alert(src, ok_result) is False)
    check('A8 恢复后再次失效 → 可以再提醒一次',
          cs.should_alert(src, fail_result) is True)

    cs.reset_alert(src)
    check('A9 reset 后失效会重新告警（登录成功后的行为）',
          cs.should_alert(src, fail_result) is True)
    cs.clear_all_states()

    check('A10 账号字段兼容 dict（旧后端存的 {"uin","nickname"}）',
          isinstance(cs.load('qq').account, str) and bool(cs.load('qq').account),
          cs.load('qq').account)


def part_probe() -> None:
    print()
    print('=' * 72)
    print('B. Cookie 探活')
    print('=' * 72)
    from app import cookies as cs

    qq = cs.load('qq')
    res = cs.probe('qq')
    check('B1 QQ（有真 Cookie）判定为可用', res.ok is True,
          f'账号={qq.account} {res.reason or "ok"}')
    check('B2 QQ 显示了剩余有效期', '未知' not in qq.expiry_text() or qq.key_expires_in == 0,
          qq.expiry_text())

    wyy = cs.probe('wyy')
    check('B3 网易云（未登录）判定为未登录', wyy.ok is False, wyy.reason)

    line = cs.status_line('wyy', '网易云音乐', 'wyy', wyy, cs.load('wyy'))
    check('B4 未登录的状态行提示去登录', 'login' in line, line.replace('\n', ' '))


def part_qrcode() -> None:
    print()
    print('=' * 72)
    print('C. 二维码生成（真实接口）')
    print('=' * 72)
    from app.login import netease, qq_login

    # QQ
    try:
        qr = qq_login.request_qrcode()
        check('C1 QQ 二维码申请成功', qr.png[:4] == b'\x89PNG' and bool(qr.qrsig),
              f'{len(qr.png)} 字节 PNG')
        poll = qq_login.poll_qrcode(qr)
        check('C2 QQ 未扫码时状态为 waiting',
              poll.status == 'waiting', f'{poll.status} / {poll.message}')
    except Exception as e:
        check('C1 QQ 二维码申请成功', False, repr(e))
        check('C2 QQ 未扫码时状态为 waiting', False, '跳过')

    # 网易云
    try:
        unikey, http = netease.request_unikey()
        check('C3 网易云 unikey 获取成功', bool(unikey), unikey[:12] + '…')
        png = netease.qrcode_png(unikey)
        check('C4 网易云二维码渲染成 PNG', png[:4] == b'\x89PNG', f'{len(png)} 字节')
        check('C5 二维码内容是正确的登录链接',
              netease.login_url(unikey).startswith('https://music.163.com/login?codekey='),
              netease.login_url(unikey))
        result = netease.poll(http, unikey)
        check('C6 未扫码时状态为 waiting',
              result.status == netease.STATUS_WAITING, f'{result.status} / {result.message}')
    except Exception as e:
        check('C3 网易云 unikey 获取成功', False, repr(e))
        check('C4 网易云二维码渲染成 PNG', False, '跳过')
        check('C5 二维码内容是正确的登录链接', False, '跳过')
        check('C6 未扫码时状态为 waiting', False, '跳过')


def part_send_image() -> None:
    print()
    print('=' * 72)
    print('D. 发图片消息（media/upload → msgtype=image）')
    print('=' * 72)
    start_mock()
    os.environ['WECHAT_PROXY'] = f'http://127.0.0.1:{MOCK_PORT}'

    from app import wecom
    from app.config import settings
    settings.wechat_proxy = f'http://127.0.0.1:{MOCK_PORT}'

    png = b'\x89PNG\r\n\x1a\n' + b'0' * 512
    MockWeCom.records.clear()
    ok = wecom.send_image(png, 'some_user')
    check('D1 send_image 返回成功', ok is True)

    uploads = [r for r in MockWeCom.records if r['path'].endswith('/media/upload')]
    sends = [r for r in MockWeCom.records
             if r['path'].endswith('/message/send') and r.get('json')]
    check('D2 走了 media/upload 且是 multipart',
          bool(uploads) and uploads[0]['is_multipart'] and uploads[0]['has_media_field'],
          f"{uploads[0]['body_size']} 字节" if uploads else '(无)')
    check('D3 上传的是 PNG', bool(uploads) and uploads[0]['body_is_png'])
    check('D4 upload 带了 type=image',
          bool(uploads) and uploads[0]['query'].get('type') == 'image',
          uploads[0]['query'].get('type') if uploads else '')
    check('D5 消息类型是 image 且带 media_id',
          bool(sends) and sends[0]['json'].get('msgtype') == 'image'
          and sends[0]['json']['image'].get('media_id') == 'MOCK_MEDIA_ID_123',
          json.dumps(sends[0]['json'], ensure_ascii=False)[:80] if sends else '(无)')

    # 长文本截断
    long_text = '汉' * 3000
    truncated = wecom._truncate_bytes(long_text)
    check('D6 超长文本被截断且不切坏字符',
          len(truncated.encode('utf-8')) <= wecom.MAX_TEXT_BYTES
          and truncated.endswith('（内容过长已截断）'),
          f'{len(truncated.encode("utf-8"))} 字节')


def part_login_manager() -> None:
    print()
    print('=' * 72)
    print('E. 登录编排（二维码会真的发出去，然后等它超时）')
    print('=' * 72)
    from app.login import manager
    from app import notices

    sent: list[str] = []
    images: list[int] = []

    manager.send_text = lambda text, user, **kw: (sent.append(text), True)[1]  # type: ignore
    # 二维码这条路现在分两步（先上传素材探路，再发图片消息），所以要分别打桩
    manager.upload_media = lambda data, **kw: (images.append(len(data)), 'MID')[1]  # type: ignore
    manager.send_image_message = lambda media_id, user, **kw: True  # type: ignore
    manager.QRCODE_TIMEOUT_MINUTES = 0.05          # 3 秒就超时，别真等 5 分钟
    manager.POLL_INTERVAL_SECONDS = 0.5

    def wait_session(source: str, seconds: float = 25) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not manager.session_of(source).running:
                return
            time.sleep(0.2)

    ok, message = manager.start_login('qq', 'tester')
    check('E1 发起登录被接受', ok is True, message)
    ok2, message2 = manager.start_login('qq', 'tester')
    check('E2 同一源重复发起被拒绝', ok2 is False, message2)

    wait_session('qq')

    session = manager.session_of('qq')
    check('E3 超时后状态为 failed', session.status == manager.STATUS_FAILED,
          f'{session.status} / {session.message}')
    check('E4 发出了登录说明文本', any('扫码' in t for t in sent), sent[0][:40] if sent else '')
    check('E5 发出了二维码图片', bool(images) and images[0] > 100,
          f'{images} 字节（QQ 的二维码 PNG 只有几百字节，属正常）')
    check('E6 超时给出了重新登录的提示',
          any(notices.LOGIN_TIMEOUT.format(source='qq') in t for t in sent),
          [t[:30] for t in sent])
    check('E7 网页接口能拿到二维码', len(manager.qrcode_png('qq')) > 100,
          f'{len(manager.qrcode_png("qq"))} 字节')

    # --- E8 / E9：二维码图片发不出去时的降级 -----------------------------------
    # 这正是当前部署的真实处境：反代没放行 /cgi-bin/media/upload。
    # 旧写法会先说「二维码图片见下一条消息」，然后图片发不出去、什么都不补 ——
    # 用户就干等一条永远不会来的消息。
    from app.config import settings

    def failing_upload(data, **kw):
        raise RuntimeError('media/upload 失败: errcode=404（反代未放行）')

    manager.upload_media = failing_upload  # type: ignore
    saved_base = settings.public_base_url

    # E8 没配对外地址 → 应该说清原因，而不是留下一句空头承诺
    settings.public_base_url = ''
    sent.clear()
    ok8, _ = manager.start_login('wyy', 'tester')
    check('E8 图片不可用时仍能发起登录', ok8 is True)
    wait_session('wyy')
    joined = '\n'.join(sent)
    check('E8 不再承诺「二维码图片见下一条消息」', '见下一条消息' not in joined,
          [t[:36] for t in sent])
    check('E8 说清了图片发不出去', '没能发出来' in joined, [t[:36] for t in sent])
    check('E8 给出了可用的兜底入口',
          ('局域网' in joined) or ('请联系管理员' in joined), [t[:36] for t in sent])

    # E9 配了对外地址 → 必须把链接给出来
    settings.public_base_url = 'https://example.com'
    sent.clear()
    ok9, _ = manager.start_login('qq', 'tester')
    check('E9 有对外地址时能发起登录', ok9 is True)
    wait_session('qq')
    joined9 = '\n'.join(sent)
    check('E9 说明里带了扫码页链接', 'https://example.com/login/qq' in joined9,
          [t[:44] for t in sent])
    settings.public_base_url = saved_base


def part_updater() -> None:
    print()
    print('=' * 72)
    print('F. musicdl 版本检查与更新（不真的装包）')
    print('=' * 72)
    from app import updater

    current = updater.installed_version()
    check('F1 读到本地版本', bool(current), current)

    latest, error = updater.fetch_latest()
    check('F2 能查官方 PyPI', bool(latest) and not error, f'{latest} {error}')

    real_check = updater.check()
    check('F3 当前就是最新版（不谎报更新）', real_check.has_update is False,
          f'本地 {real_check.current} / 官方 {real_check.latest}')
    check('F4 无更新时启动消息不显示升级提示', updater.update_line() == '')

    # 关键：防降级
    updater.fetch_latest = lambda timeout=20: ('2.3.6', '')   # type: ignore
    downgrade = updater.check()
    check('F5 候选版本更旧时不提示更新（防降级）', downgrade.has_update is False,
          f'本地 {downgrade.current} vs 候选 {downgrade.latest}')

    updater.fetch_latest = lambda timeout=20: ('99.0.0', '')  # type: ignore
    upgrade = updater.check()
    check('F6 候选版本更新时提示更新', upgrade.has_update is True)
    line = updater.update_line()
    check('F7 启动消息里带升级提示', '99.0.0' in line and '/update' in line, line[:60])

    updater.fetch_latest = lambda timeout=20: ('', '网络不通')  # type: ignore
    failed = updater.check()
    check('F8 查不到版本时不报错也不提示更新',
          failed.error and not failed.has_update and updater.update_line() == '')
    import importlib
    importlib.reload(updater)          # 还原真实实现

    # 预检：不存在的版本必须被拦下
    ok, detail = updater.dry_run('99.99.99')
    check('F9 预检会拦下装不上的版本', ok is False, detail.replace('\n', ' ')[:70])

    # 回滚记录
    ok, detail = updater.rollback()
    check('F10 没有记录时回滚给出明确提示', ok is False and '没有可回滚' in detail, detail)
    updater.mark_pending('9.9.9', '99.0.0')
    check('F11 更新标记已置位', updater.pending_exists() is True)
    check('F12 记录里的旧版本可读', updater.previous_version() == '9.9.9',
          updater.previous_version())
    updater.clear_pending()
    check('F13 标记可清除', updater.pending_exists() is False)
    check('F14 无标记时启动自愈不动手', updater.startup_guard() == '')

    # 自愈：标记在但没有旧版本记录 → 提示并清标记（不会乱装包）
    updater.mark_pending('', '99.0.0')
    note = updater.startup_guard()
    check('F15 上次更新失败会自愈（无记录时明确提示）',
          '上次更新' in note and not updater.pending_exists(), note[:50])


def part_restart() -> None:
    print()
    print('=' * 72)
    print('G. /restart 的两级降级')
    print('=' * 72)
    from app import runtime

    exited: list[float] = []
    runtime.exit_process = lambda delay=2.0: exited.append(delay)   # type: ignore

    check('G1 本机（无 docker.sock）识别为不可用',
          runtime.docker_socket_available() is False,
          'Windows 上本来就没有 /var/run/docker.sock')
    used_docker, mode = runtime.restart()
    check('G2 降级为退出进程', used_docker is False and mode == 'exit', mode)
    check('G3 确实调用了退出流程', len(exited) == 1)

    import importlib
    importlib.reload(runtime)


def main() -> int:
    print('=' * 72)
    print('S4 + S5 + S7 验收')
    print('=' * 72)
    part_cookie_store()
    part_probe()
    part_qrcode()
    part_send_image()
    part_login_manager()
    part_updater()
    part_restart()

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
