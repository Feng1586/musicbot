"""musicbot：单进程的企业微信音乐机器人（本地 musicdl，无后端、无机器码）。

启动顺序（很重要）：
1. 日志与**配置自检**（缺项就打印清单退出）
2. 读取运行期覆盖（`/limit` 改过的值）
3. **更新自愈检查**：若上次更新后没能起来，先装回旧版本
4. 初始化引擎与任务队列
5. 清除「更新待验证」标记（能走到这里说明这次启动成功了）
6. 起后台线程：启动消息 / Cookie 体检 / 定时巡检

注意第 1 步必须在 import 任何会读配置的模块**之前**完成（旧项目的教训：
某个模块在导入期就用 AESKey 造解码器，自检写在后面等于永远不执行）。
"""

from __future__ import annotations

import os
import sys
import threading
import time

from app.config import load_runtime_overrides, settings
from utils.logger import logger, setup as setup_logger

setup_logger(settings.log_level)

_missing = settings.missing_items()
if _missing:
    print('❌ 配置不完整，无法启动。缺少以下项：\n', file=sys.stderr)
    for item in _missing:
        print(f'   - {item}', file=sys.stderr)
    print('\n请在环境变量或项目根目录的 .env 里补齐后重试。', file=sys.stderr)
    print('（.env.example 里有完整的变量清单）', file=sys.stderr)
    raise SystemExit(2)

load_runtime_overrides()

from app import commands, updater, wecom                              # noqa: E402
from app.pipeline import init_queue                                   # noqa: E402
from app.router import callback as callback_router                    # noqa: E402
from app.router import login_page as login_page_router                # noqa: E402
from app.sources import SOURCE_META, SOURCE_ORDER                     # noqa: E402
from utils.version import __version__                                 # noqa: E402

# 上次更新后没能启动 → 先回滚（这一步要在任何 musicdl 调用之前）
# ⚠️ 它只在「标记已被启动过一次、这次还带着它」时才动手；第一次带标记启动是正常的
#    （这次启动就是来验证新版本），不能回滚 —— 见 updater 的模块文档。
_update_guard_note = updater.startup_guard()
# 启动尾部（真验证）产生的话，也要一并带进启动消息
_update_notes: list[str] = []


def _finalize_update() -> None:
    """启动走到这一步 → 对待验证的更新做一次**真检查**，再决定清标记还是回滚。

    以前这里只无条件 `clear_pending()`：只要代码走到这行就当成"更新成功"，
    哪怕 musicdl 装坏了、引擎根本建不起来也一样盖章 —— 那是错的。
    """
    if not updater.pending_exists():
        return
    try:
        note = updater.finish_update_startup()
    except Exception as e:
        logger.error('  更新验证异常：%s', e, exc_info=True)
        return
    if note:
        logger.warning('  更新验证：%s', note.replace('\n', ' '))
        _update_notes.append(note)

for _path in (settings.data_dir, settings.download_dir,
              os.path.join(settings.data_dir, 'cookies')):
    os.makedirs(_path, exist_ok=True)

from fastapi import FastAPI                                          # noqa: E402
from fastapi.responses import HTMLResponse                            # noqa: E402

_stop_event = threading.Event()


def _source_summary() -> str:
    return ' / '.join(SOURCE_META[s]['name'] for s in SOURCE_ORDER)


def _send_startup_messages() -> None:
    """启动广播（一条）+ Cookie 体检（另一条）。用户明确要求分开两条。

    这两条可以用 `MUSICBOT_STARTUP_BROADCAST=false` 关掉：客户容器被
    watchtower 拉起就会广播一轮，有人会觉得吵。**Cookie 失效告警不受它影响** ——
    那条是「需要用户动手」的消息，不该被静音。
    """
    try:
        from app import notices
        engine_name = SOURCE_META[settings.default_source]['name']
        status_lines = []
        try:
            status_lines, alerts = commands.check_cookies_and_alert()
            commands.broadcast_cookie_alerts(alerts)
        except Exception as e:
            logger.warning('启动时检查 Cookie 失败：%s', e)
            status_lines = ['（Cookie 状态检查失败）']

        if not settings.startup_broadcast:
            logger.info('启动广播已关闭（MUSICBOT_STARTUP_BROADCAST），Cookie 失效告警仍然照发')
            return

        update_line = ''
        try:
            update_line = updater.update_line()
        except Exception as e:
            logger.debug('检查 musicdl 更新失败：%s', e)
        notes = [n for n in (_update_guard_note, *_update_notes) if n]
        if notes:
            update_line = '\n'.join(notes + ([update_line] if update_line else []))

        wecom.broadcast_text(notices.startup_text(
            engine_name, settings.search_limit, update_line=update_line))

        # 第二条：Cookie 体检
        if status_lines:
            wecom.broadcast_text(notices.cookie_report_text(status_lines))
    except Exception as e:
        logger.error('启动消息发送失败：%s', e, exc_info=True)


def _cookie_watch_loop() -> None:
    """定时巡检：只在「可用 → 失效」时广播一次。"""
    interval = max(settings.cookie_check_interval_minutes, 1) * 60
    while not _stop_event.wait(interval):
        try:
            _lines, alerts = commands.check_cookies_and_alert()
            commands.broadcast_cookie_alerts(alerts)
        except Exception as e:
            logger.warning('Cookie 巡检失败：%s', e)


from contextlib import asynccontextmanager                            # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info('=' * 58)
    logger.info('musicbot v%s 启动中…', __version__)
    for line in settings.summary_lines():
        logger.info('  %s', line)

    ok, detail = wecom.probe_credentials()
    if ok:
        logger.info('  企微凭据自检：%s', detail)
    else:
        logger.warning('  企微凭据自检失败：%s（服务继续启动，稍后仍可恢复）', detail)

    try:
        init_queue()
        logger.info('  引擎与任务队列就绪（源：%s，条数 %d，间隔 %d 秒，超时 %d 分钟）',
                    _source_summary(), settings.search_limit,
                    settings.task_interval_seconds, settings.task_timeout_minutes)
    except Exception as e:
        logger.error('  引擎初始化失败：%s', e, exc_info=True)

    # 能走到这里说明这次启动基本是成功的 → 对待验证的更新做真检查
    _finalize_update()

    threading.Thread(target=_send_startup_messages, daemon=True,
                     name='musicbot-startup').start()
    threading.Thread(target=_cookie_watch_loop, daemon=True,
                     name='musicbot-cookie-watch').start()
    logger.info('=' * 58)
    try:
        yield
    finally:
        _stop_event.set()
        logger.info('musicbot 已停止')


app = FastAPI(title='musicbot', version=__version__, lifespan=lifespan,
              docs_url='/docs', redoc_url=None)
app.include_router(callback_router.router)
app.include_router(login_page_router.router)


@app.get('/health')
def health() -> dict:
    return {'status': 'ok', 'version': __version__}


@app.get('/', response_class=HTMLResponse)
def index() -> str:
    """状态页：避免裸域名访问吃 404（旧项目的教训）。"""
    rows = ''.join(
        f'<tr><td>{line.split(":")[0]}</td><td>{line.split(":", 1)[-1].strip()}</td></tr>'
        for line in settings.summary_lines())
    login_links = ' · '.join(
        f'<a href="/login/{s}">{SOURCE_META[s]["name"]}</a>' for s in SOURCE_ORDER)
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>musicbot v{__version__}</title>
<style>
 body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#f6f7f9;margin:0;padding:32px;color:#1f2329}}
 .card{{max-width:700px;margin:0 auto;background:#fff;border-radius:12px;padding:24px 28px;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8a9099;font-size:13px;margin-bottom:20px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 td{{padding:7px 0;border-bottom:1px solid #eef0f2;vertical-align:top}}
 td:first-child{{color:#8a9099;width:34%}}
 .hint{{margin-top:18px;font-size:12px;color:#8a9099;line-height:1.8}}
 a{{color:#185fa5;text-decoration:none}}
</style></head><body>
<div class="card">
  <h1>🎵 musicbot</h1>
  <div class="sub">企业微信音乐机器人 · v{__version__}</div>
  <table>{rows}</table>
  <div class="hint">
    扫码登录页：{login_links}<br>
    企业微信回调：<code>/wechat/callback</code>　健康检查：<code>/health</code>
  </div>
</div></body></html>"""


if __name__ == '__main__':
    import uvicorn
    # ⚠️ 必须传 app **对象**，不能用 `'main:app'` 这种字符串。
    # 用字符串时 uvicorn 会**再 import 一次 main**，本模块的顶层代码会被执行**两遍**
    # —— 实测后果（2026-09-23 线上）：`startup_guard()` 被调两次，第二次把正常更新
    # 误判成「上次启动失败」而回滚，于是 `/update` 永远不生效。
    # 传对象则不会二次 import（reload/多 worker 才需要字符串，本项目都不需要）。
    uvicorn.run(app, host=settings.host, port=settings.port,
                log_level=settings.log_level.lower())
