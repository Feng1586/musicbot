"""扫码登录页（浏览器打开用）。

为什么要有它：企微消息里图片可能被折叠/看不方便，而客户**必然**有一个公网地址
（回调就要求公网可达），所以顺手提供一个网页版的扫码页。
链接由 `MUSICBOT_PUBLIC_BASE_URL` 拼出来，没配就不显示。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.config import settings
from app.login import manager
from app.sources import SOURCE_META
from utils.version import __version__

router = APIRouter()


@router.get('/login/{source}', response_class=HTMLResponse)
def login_page(source: str) -> str:
    meta = SOURCE_META.get(source)
    if meta is None:
        raise HTTPException(status_code=404, detail='不支持的音乐源')
    return _render(meta['id'], meta['name'], meta['cmd'])


@router.get('/login/{source}/qrcode')
def login_qrcode(source: str) -> Response:
    png = manager.qrcode_png(source)
    if not png:
        raise HTTPException(status_code=404, detail='还没有二维码，请先在企微里发送登录指令')
    return Response(content=png, media_type='image/png',
                    headers={'Cache-Control': 'no-store'})


@router.get('/login/{source}/state')
def login_state(source: str) -> JSONResponse:
    if source not in SOURCE_META:
        raise HTTPException(status_code=404, detail='不支持的音乐源')
    return JSONResponse(manager.session_of(source).to_public())


def _render(source: str, name: str, cmd: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} 扫码登录 · musicbot</title>
<style>
 body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#f6f7f9;margin:0;
      padding:32px 16px;color:#1f2329}}
 .card{{max-width:420px;margin:0 auto;background:#fff;border-radius:12px;
        padding:26px 24px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 h1{{font-size:17px;margin:0 0 6px}} .sub{{color:#8a9099;font-size:13px;margin-bottom:18px}}
 .box{{min-height:232px;display:flex;align-items:center;justify-content:center;
       border:1px dashed #e3e6ea;border-radius:10px;background:#fafbfc}}
 img{{width:220px;height:220px;display:block}}
 .status{{margin-top:16px;font-size:14px;font-weight:500}}
 .tip{{margin-top:10px;font-size:12px;color:#8a9099;line-height:1.7}}
 code{{background:#f1f3f5;padding:1px 5px;border-radius:4px}}
</style></head><body>
<div class="card">
  <h1>🎵 {name} 扫码登录</h1>
  <div class="sub">musicbot v{__version__}</div>
  <div class="box"><img id="qr" alt="二维码"></div>
  <div class="status" id="status">正在读取状态…</div>
  <div class="tip" id="tip">
    请用手机 App 扫码并在手机上确认。<br>
    若这里没有二维码，请先在企微里发送 <code>/{cmd} login</code>。
  </div>
</div>
<script>
const SRC = {source!r};
const img = document.getElementById('qr');
const statusEl = document.getElementById('status');
const tipEl = document.getElementById('tip');
let lastPng = '';

function refreshQr() {{
  img.src = '/login/' + SRC + '/qrcode?t=' + Date.now();
}}

async function tick() {{
  try {{
    const r = await fetch('/login/' + SRC + '/state', {{cache: 'no-store'}});
    const s = await r.json();
    const st = s.status;
    if (st === 'idle') {{
      img.style.display = 'none';
      statusEl.textContent = '尚未开始登录';
      tipEl.innerHTML = '请先在企微里发送 <code>/' + SRC + ' login</code>';
    }} else {{
      img.style.display = 'block';
      if (s.elapsed % 2 === 0 && lastPng !== '') {{ /* 占位，保持节奏 */ }}
      statusEl.textContent = s.message + (s.account ? '（' + s.account + '）' : '');
      if (st === 'success') {{
        tipEl.textContent = '登录成功，可以回到企微继续使用了。';
      }} else if (st === 'failed') {{
        tipEl.innerHTML = '本次登录未完成，请回到企微重新发送 <code>/' + SRC + ' login</code>';
      }} else {{
        tipEl.textContent = '请用手机 App 扫码并在手机上确认。';
      }}
    }}
  }} catch (e) {{
    statusEl.textContent = '无法连接服务';
  }}
}}

refreshQr();
setInterval(refreshQr, 15000);
setInterval(tick, 2000);
tick();
</script>
</body></html>"""
