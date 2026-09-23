"""音乐引擎：搜索与下载。

关键设计（对着之前查证过的事实来的）：

* **下载走 musicdl 内置的 `client.download()`** —— 它会自动完成
  「写同名 .lrc + 内嵌歌词 + 标题/专辑/歌手 + 内嵌封面」，而且是
  「复制 → 编辑 → 校验可读 → 原子替换 + 备份回滚」的安全写入。我们自己一行都不用写。
  三个副作用按下面处理：多出来的 `download_results.pkl` 主动删掉；
  `Progress` 进度条用 stdout 重定向挡掉；只传一首所以天然串行。

* **引擎指纹重建**：`MusicClient` 在构造时就把 Cookies、条数、源列表固化进了
  各源客户端的实例属性，改了配置**不重建就完全不生效**。所以每次搜索前比对指纹，
  变了就整体重建并清空搜索缓存（用户当年在 PyMusicDL 上踩过这个坑）。

* **搜索结果缓存跨用户共享**：key 含 `(源, 关键词, 条数)`，
  多人撞同一关键词时复用同一批 `SongInfo` 对象，不重复打接口。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from utils.logger import logger

SOURCE_META: dict[str, dict[str, str]] = {
    'qq': {'id': 'qq', 'name': 'QQ音乐', 'client': 'QQMusicClient', 'cmd': 'qq'},
    'wyy': {'id': 'wyy', 'name': '网易云音乐', 'client': 'NeteaseMusicClient', 'cmd': 'wyy'},
}
SOURCE_ORDER = ('qq', 'wyy')

# 搜索缓存上限（防止内存无限增长）
CACHE_MAX_ENTRIES = 120


class MusicEngineError(RuntimeError):
    """搜索/下载失败。"""


class EngineNotReady(MusicEngineError):
    """引擎尚未初始化完成。"""


@dataclass
class SearchOutcome:
    items: list[Any] = field(default_factory=list)
    error: str = ''

    @property
    def ok(self) -> bool:
        return not self.error


class MusicEngine:
    """全局一个实例（musicdl 的客户端不是为并发设计的，内部加锁）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: Any = None
        self._fingerprint: Optional[tuple] = None
        self._build_count = 0
        self.last_rebuild_reason = ''
        # 搜索缓存：{(source, keyword, limit): (expire_at, [SongInfo, ...])}
        self._cache: dict[tuple, tuple[float, list[Any]]] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    # -- 指纹与重建 ----------------------------------------------------------

    def _cookie_fingerprint(self) -> tuple:
        from app import cookies as cookie_store
        parts = []
        for source in SOURCE_ORDER:
            ck = cookie_store.cookies_of(source)
            digest = hashlib.sha1(
                ';'.join(f'{k}={v}' for k, v in sorted(ck.items())).encode('utf-8')
            ).hexdigest()[:12]
            parts.append((source, digest))
        return tuple(parts)

    def _current_fingerprint(self) -> tuple:
        return (self._cookie_fingerprint(), settings.search_limit)

    def ensure_fresh(self, reason: str = '') -> bool:
        """指纹变了就重建引擎。返回是否发生了重建。"""
        with self._lock:
            fingerprint = self._current_fingerprint()
            if self._client is not None and fingerprint == self._fingerprint:
                return False
            changed = self._fingerprint is not None and fingerprint != self._fingerprint
            detail = reason or ('配置变化' if changed else '首次初始化')
            self._build(detail)
            return True

    def _build(self, reason: str) -> None:
        from musicdl.musicdl import MusicClient

        from app import cookies as cookie_store

        limit = settings.search_limit
        init_cfg: dict[str, dict[str, Any]] = {}
        for source in SOURCE_ORDER:
            meta = SOURCE_META[source]
            ck = cookie_store.cookies_of(source)
            init_cfg[meta['client']] = {
                'work_dir': os.path.join(settings.download_dir, meta['client']),
                'search_size_per_source': limit,
                # 页大小也设成 limit：否则 QQ 会分多次请求凑数（实测 30/10 → 3 次请求）
                'search_size_per_page': min(limit, 50),
                'default_search_cookies': ck,
                'default_download_cookies': ck,
                'disable_print': True,      # 不往 stdout 打印音乐库自己的日志
                'auto_set_proxies': False,  # 不要自动挂免费代理
            }

        started = time.time()
        self._client = MusicClient(
            music_sources=[SOURCE_META[s]['client'] for s in SOURCE_ORDER],
            init_music_clients_cfg=init_cfg,
            clients_threadings={SOURCE_META[s]['client']: 1 for s in SOURCE_ORDER},
        )
        self._fingerprint = self._current_fingerprint()
        self._build_count += 1
        self.last_rebuild_reason = reason
        self._cache.clear()             # 旧结果作废，避免拿着旧对象下载
        logger.info('引擎已重建（原因：%s，条数 %d，耗时 %.2fs，第 %d 次）',
                    reason, limit, time.time() - started, self._build_count)

    def rebuild(self, reason: str) -> None:
        with self._lock:
            self._build(reason)

    @property
    def ready(self) -> bool:
        return self._client is not None

    # -- 搜索 ----------------------------------------------------------------

    def search(self, source_id: str, keyword: str, *, use_cache: bool = True) -> SearchOutcome:
        meta = SOURCE_META.get(source_id)
        if meta is None:
            return SearchOutcome(error=f'未知音乐源 {source_id}')
        keyword = (keyword or '').strip()
        if not keyword:
            return SearchOutcome(error='关键词为空')

        limit = settings.search_limit
        cache_key = (source_id, keyword.lower(), limit)
        now = time.time()

        with self._lock:
            if use_cache and cache_key in self._cache:
                expire_at, items = self._cache[cache_key]
                if expire_at > now:
                    self._cache_hits += 1
                    logger.info('搜索命中缓存：%s「%s」(%d 条)', source_id, keyword, len(items))
                    return SearchOutcome(items=list(items))
                del self._cache[cache_key]

        self.ensure_fresh('搜索前的例行检查')
        with self._lock:
            client = self._client
            if client is None:
                return SearchOutcome(error='引擎未就绪')

        started = time.time()
        try:
            with self._lock:
                logger.info('执行搜索：%s「%s」（条数 %d）', source_id, keyword, limit)
                # musicdl 会用 rich 渲染搜索进度条（disable_print 管不到它），
                # 重定向 stdout 挡掉，避免刷容器日志。
                with contextlib.redirect_stdout(io.StringIO()):
                    raw = client.search(keyword=keyword)
        except Exception as e:                      # 网络/接口异常
            logger.error('搜索异常：%s', e, exc_info=True)
            return SearchOutcome(error=f'搜索异常：{e}')

        items = (raw or {}).get(meta['client']) or []
        # 只保留真有下载地址的条目（musicdl 的 with_valid_download_url 已是这个语义）
        usable = [si for si in items if getattr(si, 'with_valid_download_url', False)]
        dropped = len(items) - len(usable)

        with self._lock:
            self._cache_misses += 1
            self._cache[cache_key] = (now + settings.result_cache_minutes * 60, list(usable))
            self._trim_cache()

        logger.info('搜索完成：%s「%s」可用 %d 条（丢弃 %d 条无下载地址，耗时 %.2fs）',
                    source_id, keyword, len(usable), dropped, time.time() - started)
        return SearchOutcome(items=list(usable))

    def _trim_cache(self) -> None:
        if len(self._cache) <= CACHE_MAX_ENTRIES:
            return
        # 先清过期的，再按到期时间砍最早的
        now = time.time()
        for key in [k for k, (exp, _i) in self._cache.items() if exp <= now]:
            del self._cache[key]
        while len(self._cache) > CACHE_MAX_ENTRIES:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]

    def cache_stats(self) -> str:
        with self._lock:
            return (f'搜索缓存 {len(self._cache)} 条'
                    f'（命中 {self._cache_hits} / 未命中 {self._cache_misses}）')

    def compat_report(self) -> tuple[bool, str]:
        """检查当前装着的 musicdl 是否满足**我们实际用到的那套 API**。

        给 `updater.finish_update_startup()` 当"新版本到底能不能用"的判据。
        为什么不能只看版本号：版本号对了但 API 变了照样会炸。
        为什么不能只看"启动代码走到了这一行"：`init_queue()` 的异常是被吞掉的，
        引擎造不出来也不会阻断启动。
        """
        import importlib.metadata as md

        try:
            from musicdl.musicdl import MusicClient
            from musicdl.modules.utils.misc import IOUtils, sanitize_filepath
            from musicdl.modules.utils.neteaseutils import WeapiCryptoUtils
        except Exception as e:
            return False, f'导入 musicdl 失败：{type(e).__name__}: {e}'

        missing = [name for name, obj in (('MusicClient', MusicClient),
                                          ('IOUtils', IOUtils),
                                          ('sanitize_filepath', sanitize_filepath),
                                          ('WeapiCryptoUtils', WeapiCryptoUtils))
                   if obj is None]
        if missing:
            return False, f'musicdl 里缺少 {missing}'

        with self._lock:
            client = self._client
        if client is None:
            return False, '引擎未就绪（MusicClient 没能构造出来）'

        for attr in ('search', 'download', 'music_clients'):
            if not hasattr(client, attr):
                return False, f'MusicClient 缺少 .{attr}'

        registered = set(getattr(client, 'music_clients', {}) or {})
        wanted = {SOURCE_META[s]['client'] for s in SOURCE_ORDER}
        if not wanted <= registered:
            return False, f'缺少音乐源：{sorted(wanted - registered)}'

        try:
            version = md.version('musicdl')
        except Exception:
            version = '未知'
        return True, f'musicdl {version}，源 {sorted(registered)}'

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- 下载 ----------------------------------------------------------------

    def download(self, song_info: Any, target_path: str) -> tuple[str, int]:
        """下载一首歌，返回 (落盘路径, 字节数)。

        路径由我们把关（`_save_path`），musicdl 只在必要时代为合法化。
        """
        from musicdl.modules.utils.misc import IOUtils, sanitize_filepath

        self.ensure_fresh('下载前的例行检查')
        with self._lock:
            client = self._client
            if client is None:
                raise EngineNotReady('引擎未就绪')

            safe_path = sanitize_filepath(target_path)
            work_dir = os.path.dirname(safe_path)
            IOUtils.touchdir(work_dir)
            song_info.work_dir = work_dir
            song_info._save_path = safe_path

            source = getattr(song_info, 'source', '') or ''
            if source not in client.music_clients:
                raise MusicEngineError(f'引擎里没有源 {source}')

            # download() 内部用 rich 渲染进度条（disable_print 管不到），
            # 重定向 stdout 把它挡掉，避免刷容器日志。
            sink = io.StringIO()
            started = time.time()
            with contextlib.redirect_stdout(sink):
                results = client.download(song_infos=[song_info])

        if not results:
            raise MusicEngineError('musicdl 没有产出文件（可能被判定为不可下载）')

        info = results[0]
        final_path = getattr(info, '_save_path', None) or info.save_path
        if not os.path.isfile(final_path):
            raise MusicEngineError(f'下载结束但文件不存在：{final_path}')

        self._cleanup_musicdl_artifacts(getattr(info, 'work_dir', None) or work_dir)

        size = os.path.getsize(final_path)
        logger.info('下载完成 %s（%d 字节，耗时 %.2fs）',
                    os.path.basename(final_path), size, time.time() - started)
        return final_path, size

    @staticmethod
    def _cleanup_musicdl_artifacts(work_dir: str) -> None:
        """删掉 musicdl 在下完歌后顺手写的 download_results.pkl。

        用户的音乐目录里凭空多一个 pkl 文件很莫名其妙，而且它对本项目毫无用处。
        """
        if not work_dir:
            return
        pkl = os.path.join(work_dir, 'download_results.pkl')
        try:
            if os.path.isfile(pkl):
                os.remove(pkl)
                logger.debug('已清理 musicdl 产物 %s', os.path.basename(pkl))
        except OSError as e:
            logger.debug('清理 %s 失败：%s', pkl, e)


# --- 全局单例 ---------------------------------------------------------------
# 引擎是进程级共享的（搜索缓存也因此能跨用户复用），由 main.py 在启动时初始化。

_engine: Optional[MusicEngine] = None


def init_engine(reason: str = '启动初始化') -> MusicEngine:
    global _engine
    if _engine is None:
        _engine = MusicEngine()
    _engine.ensure_fresh(reason)
    return _engine


def engine() -> MusicEngine:
    """取全局引擎（未初始化时惰性初始化，避免调用方拿到 None）。"""
    if _engine is None:
        return init_engine('惰性初始化')
    return _engine


def compat_report() -> tuple[bool, str]:
    """当前引擎 + musicdl 的兼容性自检（给 updater 的更新验证用）。

    自己不吞异常：调用方（`finish_update_startup`）需要拿到"没通过"这个结论。
    引擎都建不起来时也算没通过 —— 那正是"新版本坏了"的表现。
    """
    try:
        return engine().compat_report()
    except Exception as e:
        return False, f'自检异常：{type(e).__name__}: {e}'
