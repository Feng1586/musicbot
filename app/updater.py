"""musicdl 包版本检查与在线更新。

三个必须记住的事实（都吃过亏或有实测依据）：

1. **查版本只能打官方 PyPI。** 清华镜像的 `/pypi/musicdl/json` 是 2023 年的过期快照
   （最高只到 2.3.6），照它提示"更新"会把包**降到 2.3.6**，直接搞坏。
2. **比较必须用 PEP 440 语义**，且只认**严格大于**当前版本（防降级）。
   字符串比较会得出 "2.9.1" > "2.13.11" 这种错误结论。
3. **容器内 pip install 只改可写层**：更新只对当前容器实例有效，
   容器一旦重建就回到镜像里的版本。

自愈回滚：更新前置 `data/.update_pending` 标记，启动成功后清除；
下次启动若发现标记还在，说明上次更新后**进程没起来** → 自动装回旧版本再启动。
这是唯一能让"更新失败"不变成"必须重建容器"的办法。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests

from app.config import settings
from utils.logger import logger

PYPI_JSON_URL = 'https://pypi.org/pypi/musicdl/json'
PACKAGE = 'musicdl'
PIP_TIMEOUT_SECONDS = 420

_session = requests.Session()
_session.trust_env = False


# --- 路径 -------------------------------------------------------------------

def _state_path() -> str:
    return os.path.join(settings.data_dir, 'update_state.json')


def _pending_marker() -> str:
    return os.path.join(settings.data_dir, '.update_pending')


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)


# --- 版本 -------------------------------------------------------------------

def installed_version() -> str:
    try:
        import importlib.metadata as md
        return md.version(PACKAGE)
    except Exception:
        return ''


def _parse(version: str):
    try:
        from packaging.version import Version
        return Version(version)
    except Exception:
        return None


def fetch_latest(timeout: int = 20) -> tuple[str, str]:
    """查官方 PyPI。返回 (最新版本, 失败原因)。查不到时不要提示更新。"""
    try:
        resp = _session.get(PYPI_JSON_URL, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return '', f'查询 PyPI 失败：{e}'
    latest = str((data.get('info') or {}).get('version') or '')
    if not latest:
        return '', 'PyPI 返回里没有版本号'
    return latest, ''


@dataclass
class UpdateCheck:
    current: str = ''
    latest: str = ''
    has_update: bool = False
    error: str = ''


def check() -> UpdateCheck:
    current = installed_version()
    latest, error = fetch_latest()
    result = UpdateCheck(current=current, latest=latest, error=error)
    if error or not latest:
        return result
    cur_v, new_v = _parse(current), _parse(latest)
    if cur_v is None or new_v is None:
        result.error = f'版本号无法比较（当前 {current}，最新 {latest}）'
        return result
    result.has_update = new_v > cur_v          # 严格大于，防降级
    return result


def update_line() -> str:
    """给启动消息用的一行；无更新或查不到时返回空串。"""
    from app import notices
    result = check()
    if result.error or not result.has_update:
        return ''
    return notices.update_available_line(result.current, result.latest)


# --- pip 操作 ---------------------------------------------------------------

def _pip_command(version: str, *, dry_run: bool, extra: Optional[list[str]] = None) -> list[str]:
    index = settings.pip_index_url or ''
    cmd = [sys.executable, '-m', 'pip', 'install', '--no-input',
           '--disable-pip-version-check']
    if dry_run:
        cmd.append('--dry-run')
    if index:
        cmd += ['-i', index]
        host = index.split('//', 1)[-1].split('/', 1)[0]
        cmd += ['--trusted-host', host]
    cmd += [f'{PACKAGE}=={version}']
    return cmd + (extra or [])


def _run_pip(cmd: list[str]) -> tuple[bool, str]:
    logger.info('执行：%s', ' '.join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              timeout=PIP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, 'pip 执行超时'
    output = (proc.stdout or '') + (proc.stderr or '')
    tail = '\n'.join(output.strip().splitlines()[-6:])
    return proc.returncode == 0, tail


def dry_run(version: str) -> tuple[bool, str]:
    """更新前预检：与锁定依赖冲突（如 cryptography<47）就中止。"""
    return _run_pip(_pip_command(version, dry_run=True))


def mark_pending(previous: str, target: str) -> None:
    _write_json(_state_path(), {
        'previous': previous, 'target': target,
        'attempted_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'attempted_ts': time.time(),
    })
    with open(_pending_marker(), 'w', encoding='utf-8') as fp:
        fp.write(target)
    logger.warning('已置「更新待验证」标记：%s → %s', previous, target)


def clear_pending() -> None:
    try:
        os.remove(_pending_marker())
    except OSError:
        pass


def pending_exists() -> bool:
    return os.path.isfile(_pending_marker())


def previous_version() -> str:
    return str(_read_json(_state_path()).get('previous') or '')


def apply_update(version: str) -> tuple[bool, str]:
    """执行更新。**不自己重启**，由用户决定何时 /restart。"""
    current = installed_version()
    ok, detail = dry_run(version)
    if not ok:
        logger.warning('更新预检未通过：%s', detail)
        return False, f'依赖预检未通过（可能需要处理依赖冲突）：\n{detail}'

    mark_pending(current, version)
    ok, detail = _run_pip(_pip_command(version, dry_run=False))
    if not ok:
        clear_pending()
        return False, f'安装失败：\n{detail}'
    return True, installed_version()


def rollback() -> tuple[bool, str]:
    """装回更新前的版本。"""
    previous = previous_version()
    if not previous:
        return False, '没有可回滚的版本记录'
    ok, detail = _run_pip(_pip_command(previous, dry_run=False))
    clear_pending()
    if not ok:
        return False, f'回滚失败：\n{detail}'
    return True, previous


def startup_guard() -> str:
    """启动时的自愈检查。返回需要在启动消息里告知的一句话（无问题则空串）。"""
    if not pending_exists():
        return ''
    state = _read_json(_state_path())
    previous = str(state.get('previous') or '')
    target = str(state.get('target') or '')
    logger.error('检测到上次更新（%s → %s）后服务未能启动，开始自动回滚',
                 previous or '?', target or '?')
    if not previous:
        clear_pending()
        return '⚠️ 上次更新后启动失败，但没有可回滚的版本记录，请手动处理'
    ok, detail = _run_pip(_pip_command(previous, dry_run=False))
    clear_pending()
    if ok:
        return f'⚠️ 上次更新到 {target} 后启动失败，已自动回滚到 {previous}'
    return f'⚠️ 上次更新后启动失败，自动回滚也失败了（{detail}），请手动重建容器'
