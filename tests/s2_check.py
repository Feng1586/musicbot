"""S2 验收：QQ 源真实搜索 + 编号下载 + 落盘（含歌词/封面/标签）。

不经过企业微信，直接调引擎，重点验证：
1. 真实 Cookie 能搜到歌，条数 = `/limit` 设定值
2. 搜索结果缓存跨调用命中（同关键词第二次不再打接口）
3. 下载确实产出文件，且 **歌词、封面、基础标签都写进了音频文件**
4. .lrc 伴随文件存在
5. musicdl 顺手写的 download_results.pkl 已被清理
6. 进度条没有漏进 stdout（我们重定向掉了）

用法：python tests/s2_check.py [关键词]
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.chdir(ROOT)

from app.config import settings                     # noqa: E402
from app.downloader import build_target_path        # noqa: E402
from app.sources import init_engine                 # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


def title_of(song) -> str:
    return f'{getattr(song, "song_name", "?")}-{getattr(song, "singers", "?")}'


def inspect_audio(path: str) -> dict:
    """检查歌词/封面/基础标签是否真的写进文件。"""
    from mutagen import File
    info = {'lyrics': '', 'cover_bytes': 0, 'title': '', 'album': '', 'artist': ''}
    audio = File(path, easy=False)
    if audio is None:
        return info
    # 歌词
    tags = getattr(audio, 'tags', None)
    if tags is not None:
        for key in tags.keys():
            name = str(key).upper()
            if name.startswith('USLT'):
                value = tags[key]
                text = getattr(value, 'text', value)
                info['lyrics'] = str(text[0] if isinstance(text, (list, tuple)) else text)
                break
            if 'LYRIC' in name:
                value = tags[key]
                if isinstance(value, (list, tuple)):
                    value = value[0] if value else ''
                info['lyrics'] = str(value)
                break
    # 封面
    pictures = getattr(audio, 'pictures', None)
    if pictures:
        info['cover_bytes'] = len(pictures[0].data or b'')
    elif tags is not None:
        for key in tags.keys():
            if str(key).upper().startswith('APIC'):
                info['cover_bytes'] = len(getattr(tags[key], 'data', b'') or b'')
                break
            if str(key).upper() == 'COVR':
                value = tags[key]
                if isinstance(value, (list, tuple)) and value:
                    info['cover_bytes'] = len(value[0])
                break
    # 基础标签
    easy = File(path, easy=True) or {}
    def first(key: str) -> str:
        value = easy.get(key)
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else ''
        return str(value or '')
    info['title'] = first('title')
    info['album'] = first('album')
    info['artist'] = first('artist')
    return info


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else '青花瓷'
    print('=' * 72)
    print(f'S2 验收：QQ 源搜索 + 下载「{keyword}」')
    print('=' * 72)

    from app import cookies as cookie_store
    record = cookie_store.load('qq')
    result = cookie_store.probe('qq')
    print(f'  QQ Cookie：账号 {record.account or "?"}，登录于 {record.age_text}，'
          f'预计有效 {record.expiry_text()}')
    check('前置：QQ Cookie 可用', result.ok is True, result.reason or 'ok')
    if result.ok is not True:
        print('  Cookie 不可用，后续无法继续。请先 /qq login')
        return 1

    print()
    print('-' * 72)
    print('1. 搜索')
    print('-' * 72)
    engine = init_engine('S2 测试')
    started = time.time()
    outcome = engine.search('qq', keyword)
    cost = time.time() - started
    check('搜索成功', outcome.ok, outcome.error or 'ok')
    if not outcome.ok:
        return 1
    check(f'返回条数 = /limit({settings.search_limit})',
          len(outcome.items) == settings.search_limit, f'{len(outcome.items)} 条')
    check('每条都有下载地址',
          all(getattr(s, 'with_valid_download_url', False) for s in outcome.items))
    print(f'  耗时 {cost:.2f}s，前 3 条：')
    for i, song in enumerate(outcome.items[:3], 1):
        print(f'    {i}. {title_of(song)}  [{getattr(song, "ext", "?")}, '
              f'{getattr(song, "file_size", "?")}]')

    hits_before = engine._cache_hits
    started = time.time()
    again = engine.search('qq', keyword)
    cost2 = time.time() - started
    check('第二次同关键词命中缓存（不打接口）',
          engine._cache_hits == hits_before + 1 and cost2 < cost,
          f'{cost2 * 1000:.1f}ms vs 首次 {cost:.2f}s')
    check('缓存返回的是同一批对象',
          again.items and again.items[0] is outcome.items[0])

    print()
    print('-' * 72)
    print('2. 下载')
    print('-' * 72)
    song = outcome.items[0]
    target = build_target_path('qq', keyword, getattr(song, 'song_name', '未知'),
                               getattr(song, 'singers', '未知'),
                               str(getattr(song, 'ext', '') or 'mp3').lstrip('.'))
    print(f'  目标路径：{os.path.relpath(target, ROOT)}')
    started = time.time()
    path, size = engine.download(song, target)
    cost3 = time.time() - started
    check('下载产出文件', os.path.isfile(path) and size > 0,
          f'{os.path.relpath(path, ROOT)}  {size / 1048576:.1f} MB  {cost3:.1f}s')
    if not os.path.isfile(path):
        return 1

    work_dir = os.path.dirname(path)
    # build_target_path 返回的是 downloads/{源客户端}/{批次 关键词}/{文件}，
    # 所以「文件所在目录」就已经是批次目录了（原来多剥了一层）。
    # 另外批次目录是 `{时间戳} {关键词}` —— 关键词在**尾部**，所以只能是
    # endswith，不能是 startswith。这条断言从写出来那天起就一直红着。
    batch = os.path.basename(work_dir)
    check('目录结构符合预期（源/批次关键词/）',
          batch.endswith(keyword) and ' ' in batch.strip()
          and os.path.basename(os.path.dirname(work_dir)) == 'QQMusicClient',
          f'{os.path.relpath(work_dir, ROOT)}　批次目录={batch}')
    check('文件名是「歌名 - 歌手.ext」',
          os.path.basename(path).startswith(getattr(song, 'song_name', '')),
          os.path.basename(path))

    print()
    print('-' * 72)
    print('3. 歌词 / 封面 / 标签（本项目自己一行都没写，全由 musicdl 完成）')
    print('-' * 72)
    info = inspect_audio(path)
    check('内嵌歌词已写入', len(info['lyrics']) > 20,
          f'{len(info["lyrics"])} 字符，开头：{info["lyrics"][:24]!r}')
    check('内嵌封面已写入', info['cover_bytes'] > 1000, f'{info["cover_bytes"]} 字节')
    check('标题 / 歌手标签已写入', bool(info['title'] or info['artist']),
          f'title={info["title"]!r} artist={info["artist"]!r} album={info["album"]!r}')

    lrc_path = os.path.splitext(path)[0] + '.lrc'
    has_lrc = os.path.isfile(lrc_path)
    check('同名 .lrc 伴随文件存在', has_lrc,
          f'{os.path.basename(lrc_path)} {os.path.getsize(lrc_path) if has_lrc else 0} 字节')
    if has_lrc:
        head = open(lrc_path, encoding='utf-8', errors='ignore').read(60)
        check('.lrc 是标准 LRC（含时间轴）', '[' in head and ':' in head, head.replace('\n', ' ')[:50])

    check('musicdl 的 download_results.pkl 已被清理',
          not os.path.isfile(os.path.join(work_dir, 'download_results.pkl')))

    print()
    print('=' * 72)
    ok = sum(1 for _n, o, _d in results if o)
    print(f'结果：{ok}/{len(results)} 通过')
    for n, o, d in results:
        if not o:
            print(f'   FAIL -> {n}  {d}')
    print('=' * 72)
    print(f'（下载文件保留在 {os.path.relpath(work_dir, ROOT)}，可手动删除）')
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
