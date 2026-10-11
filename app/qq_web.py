"""QQ 音乐网页接口封装（musicu.fcg）。

为什么需要单独一个模块
--------------------------------
musicdl 的 QQ 源**只做搜索**，没有「新歌首发」和「专辑曲目」接口，所以这两个
功能必须自己调 QQ 音乐网页端接口。

网页端现在会把 `cgi-bin/musicu.fcg` 重写成 `cgi-bin/musics.fcg`，并对请求体与
响应体做 **ag-1 加密 + 签名**（见前端 `vendor.chunk.js` 里的 `getSecuritySign` /
`encrypt`），那条路不可复用。但经典入口 `u.y.qq.com/cgi-bin/musicu.fcg`
**仍然可用、且明文返回 JSON**（本项目的 QQ Cookie 体检走的也是它），
所以这里沿用它，不引入任何新依赖。

接口实测结论（2026-10-12，带真实 Cookie）
----------------------------------------
* 新歌首发：`newsong.NewSongServer / get_new_song_info`，参数 `{"type": N}`；
  返回里 `data.lanlist` 直接给出栏目定义，`data.songlist[]` 的每首歌**自带**
  歌名 / 歌手 / 专辑名 / mid —— 所以**不需要**再爬 songDetail 详情页。
* 专辑：`music.musichallAlbum.AlbumInfoServer / GetAlbumDetail` 拿专辑名；
  `music.musichallAlbum.AlbumSongList / GetAlbumSongList` 拿曲目（支持
  `begin` / `num` 分页，网页只请求前 10 首，接口本身能拿全）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from app.config import settings
from app.songinfo import SongFields
from utils.logger import logger

ENDPOINT = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
REFERER = 'https://y.qq.com/'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'
)
TIMEOUT = 15

# 新歌首发的 6 个栏目 -> 接口 type。
# 这张表来自接口返回的 `data.lanlist`（2026-10-12 实测），不是猜的。
DEBUT_TABS: tuple[tuple[str, int], ...] = (
    ('最新', 5),
    ('内地', 1),
    ('港台', 6),
    ('欧美', 2),
    ('韩国', 4),
    ('日本', 3),
)
DEBUT_TAB_TYPES: dict[str, int] = dict(DEBUT_TABS)
DEBUT_TAB_NAMES: tuple[str, ...] = tuple(name for name, _ in DEBUT_TABS)

# 专辑链接形如 https://y.qq.com/n/ryqq_v2/albumDetail/0041WVfh2vtlJE
_ALBUM_MID_IN_URL = re.compile(r'albumDetail/([0-9A-Za-z]+)')
_ALBUM_MID_IN_QUERY = re.compile(r'[?&]album[Mm]id=([0-9A-Za-z]+)')
# 也允许用户直接给 mid（QQ 的 mid 是 14 位左右的大小写字母+数字）
_MID_ONLY = re.compile(r'^[0-9A-Za-z]{8,32}$')


class QQWebError(RuntimeError):
    """调 QQ 网页接口时的可预期失败（网络、返回码、链接非法等）。"""


@dataclass
class WebSong:
    """一首来自网页接口的歌：mid + 严格匹配用的三段。"""

    mid: str
    fields: SongFields

    @property
    def key(self) -> str:
        """去重/缓存用的键。mid 缺失时退回「歌名-歌手」。"""
        return self.mid or f'{self.fields.song}-{self.fields.singer}'


@dataclass
class AlbumInfo:
    """一张专辑的专辑名与曲目。"""

    mid: str
    name: str
    songs: list[WebSong] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.songs)


# --- 底层调用 -----------------------------------------------------------------

def _cookie_jar() -> dict[str, str]:
    """取 QQ 音乐 Cookie。取不到也不报错（部分接口匿名也能用）。"""
    try:
        from app import cookies as cookie_store
        return cookie_store.cookies_of('qq') or {}
    except Exception as error:      # noqa: BLE001 - Cookie 读取失败不该让调用方崩掉
        logger.debug('读取 QQ Cookie 失败：%s', error)
        return {}


def _comm(jar: dict[str, str]) -> dict[str, Any]:
    uin = str(jar.get('uin') or '')
    key = str(jar.get('musickey') or jar.get('qqmusic_key') or '')
    comm: dict[str, Any] = {
        'cv': 4747474, 'ct': 24, 'format': 'json',
        'inCharset': 'utf-8', 'outCharset': 'utf-8', 'notice': 0,
        'platform': 'yqq.json', 'needNewCode': 1, 'uin': uin,
        'g_tk_new_20200303': 5381, 'g_tk': 5381, 'tmeLoginType': 2,
    }
    if uin and key:
        # 已登录时带上，接口会返回更完整的数据（与 Cookie 体检同一套写法）
        comm.update({'qq': uin, 'authst': key})
    return comm


def call(requests_payload: list[dict], *, jar: Optional[dict[str, str]] = None,
         timeout: int = TIMEOUT) -> list[dict]:
    """调一次 musicu.fcg，返回与请求一一对应的 `req_N` 结果列表。"""
    cookie_jar = _cookie_jar() if jar is None else jar
    body: dict[str, Any] = {'comm': _comm(cookie_jar)}
    for index, item in enumerate(requests_payload):
        body[f'req_{index}'] = item
    try:
        response = requests.post(
            ENDPOINT, json=body,
            headers={'User-Agent': USER_AGENT, 'Referer': REFERER,
                     'Content-Type': 'application/json'},
            cookies=cookie_jar, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise QQWebError(f'网络请求失败：{error}') from error
    except ValueError as error:
        raise QQWebError(f'接口返回的不是 JSON：{error}') from error

    results: list[dict] = []
    for index in range(len(requests_payload)):
        item = payload.get(f'req_{index}')
        if not isinstance(item, dict):
            raise QQWebError(f'接口返回缺少 req_{index}：{str(payload)[:200]}')
        results.append(item)
    return results


def _to_fields(raw: dict) -> Optional[SongFields]:
    """把接口返回的一首歌转成严格匹配用的三段。

    歌手的拼接方式必须与 musicdl 搜索结果一致：**用 `', '` 连接**。
    （musicdl 的 QQ 源里是 `', '.join(singer['name'] ...)`，见
    `musicdl/modules/sources/qq.py`；拼法不一致的话严格匹配会全部落空。）
    """
    name = str(raw.get('name') or raw.get('title') or '').strip()
    if not name:
        return None
    singers = ', '.join(
        str(item.get('name')).strip()
        for item in (raw.get('singer') or [])
        if isinstance(item, dict) and item.get('name')
    )
    album = raw.get('album')
    if isinstance(album, dict):
        album_name = str(album.get('name') or album.get('title') or '').strip()
    else:
        album_name = str(album or '').strip()
    return SongFields(song=name, singer=singers, album=album_name)


def _to_song(raw: Any) -> Optional[WebSong]:
    if not isinstance(raw, dict):
        return None
    fields = _to_fields(raw)
    if fields is None:
        return None
    return WebSong(mid=str(raw.get('mid') or '').strip(), fields=fields)


# --- 新歌首发 -----------------------------------------------------------------

def fetch_debut_songs(tab_name: str, *, timeout: int = TIMEOUT) -> list[WebSong]:
    """取某个栏目当前的新歌列表。`tab_name` 用中文栏目名（见 DEBUT_TABS）。"""
    tab_type = DEBUT_TAB_TYPES.get(tab_name)
    if tab_type is None:
        raise QQWebError(f'未知栏目：{tab_name}')
    result = call([{'module': 'newsong.NewSongServer', 'method': 'get_new_song_info',
                    'param': {'type': tab_type}}], timeout=timeout)[0]
    code = result.get('code')
    if code != 0:
        raise QQWebError(f'新歌首发接口返回异常（栏目 {tab_name}，code={code}）')
    data = result.get('data')
    songlist = (data or {}).get('songlist') if isinstance(data, dict) else None
    songs: list[WebSong] = []
    for raw in (songlist or []):
        song = _to_song(raw)
        if song is not None:
            songs.append(song)
    logger.debug('新歌首发[%s] 取到 %d 首', tab_name, len(songs))
    return songs


def fetch_all_debut_songs(tab_names: list[str], *, timeout: int = TIMEOUT
                          ) -> tuple[dict[str, int], list[WebSong], list[str]]:
    """取多个栏目并**跨栏目去重**。

    返回 (各栏目条数, 去重后的歌曲列表, 错误说明列表)。
    栏目之间确实会有重复歌曲（实测「月亮桥」同时在「最新」和「内地」），
    所以按 `WebSong.key` 去重，保留**第一次出现**的那首。
    """
    counts: dict[str, int] = {}
    merged: dict[str, WebSong] = {}
    errors: list[str] = []
    for name in tab_names:
        try:
            songs = fetch_debut_songs(name, timeout=timeout)
        except Exception as error:      # noqa: BLE001 - 单个栏目失败不该拖垮整轮
            logger.warning('取新歌首发[%s] 失败：%s', name, error)
            errors.append(f'{name}：{error}')
            continue
        counts[name] = len(songs)
        for song in songs:
            merged.setdefault(song.key, song)
    return counts, list(merged.values()), errors


# --- 专辑 ---------------------------------------------------------------------

def parse_album_mid(text: str) -> str:
    """从专辑链接或裸 mid 里解析出 albumMid；解析不出返回空串。

    支持：
      * https://y.qq.com/n/ryqq_v2/albumDetail/0041WVfh2vtlJE
      * https://y.qq.com/n/ryqq_v2/albumDetail/0041WVfh2vtlJE?xxx=1
      * ...&albummid=0041WVfh2vtlJE
      * 直接给 0041WVfh2vtlJE
    """
    value = (text or '').strip().strip('"\'<>')
    if not value:
        return ''
    for pattern in (_ALBUM_MID_IN_URL, _ALBUM_MID_IN_QUERY):
        matched = pattern.search(value)
        if matched:
            return matched.group(1)
    if _MID_ONLY.match(value):
        return value
    return ''


def fetch_album(album_mid: str, *, max_tracks: Optional[int] = None,
                timeout: int = TIMEOUT) -> AlbumInfo:
    """取专辑名与全部曲目（分页拉取，尽量下全）。

    网页端只请求前 10 首，但接口支持 `begin`/`num` —— 实测 `num=30` 能返回
    整张专辑（13 首）。`max_tracks` 只是安全上限，防止极端专辑一次排队太多。
    """
    limit = max_tracks if max_tracks is not None else settings.album_max_tracks
    detail = call([{'module': 'music.musichallAlbum.AlbumInfoServer',
                    'method': 'GetAlbumDetail',
                    'param': {'albumMid': album_mid, 'albumID': 0}}],
                  timeout=timeout)[0]
    if detail.get('code') != 0:
        raise QQWebError(f'专辑不存在或接口异常（code={detail.get("code")}）')
    basic = (detail.get('data') or {}).get('basicInfo') or {}
    album_name = str(basic.get('albumName') or '').strip()
    if not album_name:
        raise QQWebError('没能取到专辑名（链接可能不对）')

    songs: list[WebSong] = []
    seen: set[str] = set()
    begin = 0
    page_size = 50
    while len(songs) < limit:
        want = min(page_size, limit - len(songs))
        result = call([{'module': 'music.musichallAlbum.AlbumSongList',
                        'method': 'GetAlbumSongList',
                        'param': {'albumMid': album_mid, 'albumID': 0,
                                  'begin': begin, 'num': want, 'order': 2}}],
                      timeout=timeout)[0]
        if result.get('code') != 0:
            raise QQWebError(f'取专辑曲目失败（code={result.get("code")}）')
        page = (result.get('data') or {}).get('songList') or []
        if not page:
            break
        for raw in page:
            info = raw.get('songInfo') if isinstance(raw, dict) else None
            song = _to_song(info if isinstance(info, dict) else raw)
            if song is not None and song.key not in seen:
                seen.add(song.key)
                songs.append(song)
        if len(page) < want:
            break                   # 已经到最后一页
        begin += len(page)

    logger.info('专辑《%s》取到 %d 首（上限 %d）', album_name, len(songs), limit)
    return AlbumInfo(mid=album_mid, name=album_name, songs=songs)
