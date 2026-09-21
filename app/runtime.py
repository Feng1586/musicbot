"""重启：优先用 docker.sock，没有就优雅退出进程。

用户明确要求：**不强制客户映射 `docker.sock`**。
* 挂载了且可用 → 通过 Docker API 重启本容器（体验最好）
* 没挂载 → 优雅退出进程，依赖容器的 `restart` 策略（`unless-stopped` / `always`）
  自动拉起；如果客户没配自动重启策略，服务就会停在停止状态 —— 这一点会在消息里说清。

容器内 `unix:///var/run/docker.sock` 在 Windows 上不存在，所以本机开发时
永远走「退出进程」这条分支，这是预期行为。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from utils.logger import logger

DOCKER_SOCKET = '/var/run/docker.sock'
# 重启前留一点时间让消息发出去
EXIT_DELAY_SECONDS = 2.0


class _UnixSocketAdapter(HTTPAdapter):
    """让 requests 走 unix socket（docker.sock）。"""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):           # noqa: D102
        kwargs['socket_options'] = self._socket_options()
        return super().init_poolmanager(*args, **kwargs)

    def _socket_options(self):
        return []

    def send(self, request, **kwargs):                      # noqa: D102
        import http.client
        from urllib3.connection import HTTPConnection
        from urllib3.connectionpool import HTTPConnectionPool

        socket_path = self._socket_path
        timeout = kwargs.pop('timeout', 10)

        class UnixConnection(HTTPConnection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._socket_path = socket_path

            def connect(self):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect(self._socket_path)
                self.sock = sock

        class UnixPool(HTTPConnectionPool):
            def _new_conn(self):
                return UnixConnection(self.host, self.port)

        pool = UnixPool('localhost')
        conn = pool._new_conn()
        conn.request(request.method, request.url, body=request.body,
                     headers=dict(request.headers))
        resp = conn.getresponse()
        # 用 urllib3 的 Response 包一层给 requests 用
        from requests.models import Response
        response = Response()
        response.status_code = resp.status
        response.headers.update(dict(resp.getheaders()))
        response._content = resp.read()
        response.url = request.url
        response.request = request
        return response


def docker_socket_available() -> bool:
    if not hasattr(socket, 'AF_UNIX'):
        return False
    return os.path.exists(DOCKER_SOCKET)


def restart_via_docker(timeout: int = 10) -> bool:
    """通过 Docker API 重启本容器。失败返回 False（调用方会降级）。"""
    if not docker_socket_available():
        return False
    container = socket.gethostname()          # Docker 把容器 ID 设为 hostname
    session = requests.Session()
    session.trust_env = False
    session.mount('http://localhost', _UnixSocketAdapter(DOCKER_SOCKET))
    try:
        resp = session.post(
            f'http://localhost/containers/{container}/restart',
            params={'t': 5}, timeout=timeout)
        ok = resp.status_code in (204, 200)
        logger.info('通过 docker.sock 重启容器：HTTP %s', resp.status_code)
        return ok
    except Exception as e:
        logger.warning('通过 docker.sock 重启失败：%s', e)
        return False


def exit_process(delay: float = EXIT_DELAY_SECONDS) -> None:
    """延迟一点再优雅退出（让最后一条消息能发出去）。"""
    def _worker() -> None:
        time.sleep(delay)
        logger.info('主动退出进程，交由容器的 restart 策略拉起')
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    threading.Thread(target=_worker, daemon=True, name='musicbot-exit').start()


def restart() -> tuple[bool, str]:
    """返回 (是否走了 docker 通道, 说明)。"""
    if restart_via_docker():
        return True, 'docker'
    exit_process()
    return False, 'exit'
