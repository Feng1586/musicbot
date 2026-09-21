"""musicbot：单进程的企业微信音乐机器人（本地 musicdl，无后端、无机器码）。

启动顺序（很重要）：
1. 日志与**配置自检**（缺项就打印清单退出）
2. 读取运行期覆盖（`/limit` 改过的值）
3. **更新自愈检查**：若上次更新后没能起来，先装回旧版本
4. 初始化引擎与下载队列
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
from app.downloader import init_queue                                 # noqa: E402
from app.router import callback as callback_router                    # noqa: E402
from app.router import login_page as login_page_router                # noqa: E402
from app.sources import SOURCE_META, SOURCE_ORDER                     # noqa: E402
from utils.version import __version__                                 # noqa: E402

# 上次更新后没能启动 → 先回滚（这一步要在任何 musicdl 调用之前）
_update_guard_note = updater.startup_guard()

for _path in (settings.data_dir, settings.download_dir,
              os.path.join(settings.data_dir, 'cookies')):
    os.makedirs(_path, exist_ok=True)

from fastapi import FastAPI                                          # noqa: E402
from fastapi.responses import HTMLResponse                            # noqa: E402

_stop_event = threading.Event()


def _source_summary() -> str:
    return ' / '.join(SOURCE_META[s]['name'] for s in SOURCE_ORDER)


def _send_startup_messages() -> None:
    """启动广播（一条）+ Cookie 体检（另一条）。用户明确要求分开两条。"""
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

        update_line = ''
        try:
            update_line = updater.update_line()
        except Exception as e:
            logger.debug('检查 musicdl 更新失败：%s', e)
        if _update_guard_note:
            update_line = (_update_guard_note + '\n' + update_line).strip()

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
        logger.info('  引擎与下载队列就绪（源：%s，条数 %d）', _source_summary(),
                    settings.search_limit)
    except Exception as e:
        logger.error('  引擎初始化失败：%s', e, exc_info=True)

    # 能走到这里说明这次启动是成功的 → 清掉「更新待验证」标记
    if updater.pending_exists():
        updater.clear_pending()
        logger.info('  本次启动成功，已清除更新待验证标记')

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
    uvicorn.run('main:app', host=settings.host, port=settings.port,
                log_level=settings.log_level.lower())
