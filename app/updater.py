"""musicdl 包版本检查与在线更新。

三个必须记住的事实（都吃过亏或有实测依据）：

1. **查版本只能打官方 PyPI。** 清华镜像的 `/pypi/musicdl/json` 是 2023 年的过期快照
   （最高只到 2.3.6），照它提示"更新"会把包**降到 2.3.6**，直接搞坏。
2. **比较必须用 PEP 440 语义**，且只认**严格大于**当前版本（防降级）。
   字符串比较会得出 "2.9.1" > "2.13.11" 这种错误结论。
3. **容器内 pip install 只改可写层**：更新只对当前容器实例有效，
   容器一旦重建就回到镜像里的版本。

自愈回滚（v1.0.7 重写，之前那版是**错的**）
------------------------------------------------------------------
流程：更新前置 `data/.update_pending` 标记 → 用户 `/restart` → 启动时验证 → 通过就清标记。

⚠️ **判据不是「标记在不在」，而是「这个标记已经被启动过几次」**：
标记在盘上是**正常状态**（刚装完、还没重启，或者重启后的这次启动就是来验证它的），
所以**第一次**带着标记启动必须放行、不回滚；只有**第二次**还带着它启动，
才说明上一次启动没走到验证那一步（进程挂了 / 引擎造不出来）→ 这才该回滚。

老实现只看「标记在不在」就回滚，于是「`/update confirm` → `/restart`」这条**正常流程
100% 被自己回滚掉** —— 2026-09-23 用户实测（容器日志：回滚发生在
`Waiting for application startup` 之前，新版本连一次启动机会都没有）。

⚠️ 另一条：**「启动成功」这个判据必须是真探针**。光看版本号不够（版本对了但 API 变了
照样炸），也不能只看"代码走到了这一行"（`init_queue()` 的异常是被吞掉的）。
`finish_update_startup()` 调 `sources.compat_report()` 做真检查。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from app.config import settings
from utils.logger import logger

PYPI_JSON_URL = 'https://pypi.org/pypi/musicdl/json'
PACKAGE = 'musicdl'
PIP_TIMEOUT_SECONDS = 420

# 带着标记最多允许启动几次而不回滚（1 = 给第一次启动机会）
MAX_STARTUP_TRIES = 1

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
    """置「更新待验证」标记。

    `tries` 计的是「带着这个标记启动过几次」，是自愈判据的核心（见模块文档）。
    装完包**标记留在盘上是正常的** —— 它要等到下一次启动验证过才清掉。
    """
    _write_json(_state_path(), {
        'previous': previous, 'target': target,
        'tries': 0,
        'attempted_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'attempted_ts': time.time(),
    })
    with open(_pending_marker(), 'w', encoding='utf-8') as fp:
        fp.write(target)
    logger.warning('已置「更新待验证」标记：%s → %s（重启后自动验证）', previous, target)


def clear_pending() -> None:
    try:
        os.remove(_pending_marker())
    except OSError:
        pass


def pending_exists() -> bool:
    return os.path.isfile(_pending_marker())


def previous_version() -> str:
    return str(_read_json(_state_path()).get('previous') or '')


def pending_target() -> str:
    return str(_read_json(_state_path()).get('target') or '')


def pending_tries() -> int:
    try:
        return int(_read_json(_state_path()).get('tries') or 0)
    except (TypeError, ValueError):
        return 0


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
    """启动**最开头**调用的自愈检查。返回需要在启动消息里告知的一句话。

    ⚠️ 只在「这个标记已经被启动过一次、这一次还带着它」时才回滚（见模块文档）。
    第一次带着标记启动是**正常**的 —— 这次启动本身就是来验证新版本的，
    必须放行，否则每次更新都会被自己回滚掉（v1.0.6 线上实测的坑）。
    """
    if not pending_exists():
        return ''

    state = _read_json(_state_path())
    previous = str(state.get('previous') or '')
    target = str(state.get('target') or '')
    try:
        tries = int(state.get('tries') or 0)
    except (TypeError, ValueError):
        tries = 0

    if tries < MAX_STARTUP_TRIES:
        # 第一次带着标记启动：给它机会，先别回滚
        state['tries'] = tries + 1
        _write_json(_state_path(), state)
        logger.info('检测到待验证的更新（%s → %s）：本次启动验证新版本，暂不回滚',
                    previous or '?', target or '?')
        return ''

    logger.error('上次更新（%s → %s）后服务未能启动（已带标记启动 %d 次），开始自动回滚',
                 previous or '?', target or '?', tries + 1)
    if not previous:
        clear_pending()
        return '⚠️ 上次更新后启动失败，但没有可回滚的版本记录，请手动处理'
    ok, detail = _run_pip(_pip_command(previous, dry_run=False))
    clear_pending()
    if ok:
        return f'⚠️ 上次更新到 {target} 后启动失败，已自动回滚到 {previous}'
    return f'⚠️ 上次更新后启动失败，自动回滚也失败了（{detail}），请手动重建容器'


def finish_update_startup(probe: Optional[Callable[[], tuple[bool, str]]] = None) -> str:
    """启动**尾部**调用：真验证待验证的更新，然后决定清标记还是回滚。

    返回要告知用户的一句话（无需告知则空串）。三件事按顺序判断：

    1. 标记要求的版本和**实际装着的**版本不一致 → 说明可写层没了（容器被重建），
       更新本来就没保住，没有可回滚的东西 → 清标记 + 说明原因；
    2. 跑真探针（`sources.compat_report`）→ 通过就清标记；
    3. 探针不通过 → **立刻**回滚到旧版本并告知用户（不用再等下一次重启）。

    第 3 条是 v1.0.7 补的：老实现只清标记、从不校验，所以"引擎都造不出来"也会被
    当成更新成功盖章通过。
    """
    if not pending_exists():
        return ''

    target = pending_target()
    previous = previous_version()
    current = installed_version()

    if target and current and target != current:
        clear_pending()
        logger.warning('待验证标记要求 %s，实际装着 %s —— 更新没保住（容器被重建过？）',
                       target, current)
        return (f'ℹ️ 上次更新到 {target} 没有保住：当前实际是 {current}。'
                f'容器重建会回到镜像内置的版本，这是预期行为')

    if probe is None:
        from app.sources import compat_report     # 惰性导入，避免模块级循环
        probe = compat_report

    try:
        ok, detail = probe()
    except Exception as e:                        # 探针自己炸了也不能把启动搞挂
        ok, detail = False, f'自检异常：{type(e).__name__}: {e}'

    clear_pending()
    if ok:
        logger.info('新版本 %s 自检通过：%s', current, detail)
        return ''

    logger.error('新版本 %s 自检未通过：%s → 立刻回滚', current, detail)
    rolled, result = rollback()
    if rolled:
        return (f'⚠️ musicdl {current} 自检未通过（{detail}），已自动回滚到 {previous}。\n'
                f'请发送 /restart 让旧版本生效')
    return (f'⚠️ musicdl {current} 自检未通过（{detail}），自动回滚也失败了'
            f'（{result}），请手动重建容器')
