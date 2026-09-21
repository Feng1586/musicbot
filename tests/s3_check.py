"""S3 + S6 验收：下载队列、三条进度消息、盲下载、/cancel、/limit 与引擎重建。

用**假的搜索结果与假的下载**（不碰真实接口），只验证本项目自己的逻辑：
* 消息顺序必须是 回执 → 开始下载 → 下载完成
* 回执里的数字 = 队列任务数（含正在下载的那首），不是选中的编号
* 盲模式：非指令文本直接入队第一首
* /cancel 只清还没开始的
* /limit 范围校验 + 改完立刻重建引擎（这正是用户当年在 PyMusicDL 踩过的坑）

用法：python tests/s3_check.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import commands, notices                       # noqa: E402
from app.config import SEARCH_LIMIT_MAX, settings      # noqa: E402
from app.downloader import init_queue                  # noqa: E402
from app.sources import engine                         # noqa: E402

results: list[tuple[str, bool, str]] = []
sent: list[tuple[str, str]] = []
_lock = threading.Lock()


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


# --- 打桩 -------------------------------------------------------------------

def fake_send(text: str, touser: str, **_kw) -> bool:
    with _lock:
        sent.append((touser, text))
    return True


def messages_of(user: str, *, since: int = 0) -> list[str]:
    with _lock:
        return [t for u, t in sent[since:] if u == user]


def make_song(index: int, name: str = '') -> SimpleNamespace:
    return SimpleNamespace(
        song_name=name or f'测试歌{index}', singers=f'歌手{index}',
        ext='flac', source='QQMusicClient', download_url='http://fake/song',
        with_valid_download_url=True, album='专辑', lyric='[00:01]la',
    )


DOWNLOAD_SECONDS = 0.8


def fake_download(song, target_path):
    """假下载：睡一会儿再落盘，方便观察队列行为。"""
    time.sleep(DOWNLOAD_SECONDS)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, 'wb') as fp:
        fp.write(b'x' * 2048)
    return target_path, 2048


def fake_search(source_id: str, keyword: str, **_kw):
    from app.sources import SearchOutcome
    items = [make_song(i, f'{keyword}{i}') for i in range(1, 4)]
    return SearchOutcome(items=items)


def wait_drain(timeout: float = 30) -> None:
    from app.downloader import get_queue
    deadline = time.time() + timeout
    while time.time() < deadline and get_queue().pending() > 0:
        time.sleep(0.05)


def main() -> int:
    print('=' * 72)
    print('S3 + S6 验收：队列 / 三条消息 / 盲下载 / cancel / limit')
    print('=' * 72)

    # 打桩：假企微、假搜索、假下载
    commands.send_text = fake_send
    import app.downloader as dl
    dl.send_text = fake_send

    music_engine = init_queue()[0]
    music_engine.search = fake_search          # type: ignore[assignment]
    music_engine.download = fake_download      # type: ignore[assignment]

    user = 'tester'

    # --- 1. 搜索与列表 -----------------------------------------------------
    print()
    print('1. 搜索与编号列表')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '青花瓷'})
    msgs = messages_of(user)
    check('搜索返回编号列表', len(msgs) == 1 and '1. 青花瓷1-歌手1' in msgs[0],
          msgs[0].replace('\n', ' / ')[:90] if msgs else '(无)')

    # --- 2. 编号下载：顺序与回执 -------------------------------------------
    print()
    print('2. 编号下载（单首）')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '2'})
    first = messages_of(user)
    check('先回执「已加入下载队列：1」',
          bool(first) and first[0] == notices.queued_text(1), first[0] if first else '(无)')
    wait_drain()
    msgs = messages_of(user)
    check('消息顺序：回执 → 开始下载 → 下载完成',
          len(msgs) == 3
          and msgs[0].startswith('🎧 已加入下载队列：1')
          and msgs[1].startswith('⚙️ 开始下载：青花瓷2-歌手2')
          and msgs[2].startswith('✅ 下载完成：') and '💾 大小：2.0 KB' in msgs[2],
          ' | '.join(m.split('\n')[0][:22] for m in msgs))
    check('回执数字 ≠ 用户选的编号（1 vs 2）', msgs[0].endswith('1'))

    # --- 3. 队列计数含「正在下载」 ------------------------------------------
    print()
    print('3. 回执数字 = 队列任务数（含正在下载的那首）')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '1'})
    time.sleep(0.25)                      # 让第一首进入「正在下载」
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '3'})
    queued = [m for m in messages_of(user) if m.startswith('🎧')]
    check('第一首回执 = 1', queued and queued[0].endswith('1'), queued[0] if queued else '(无)')
    check('第二首回执 = 2（含在飞的那首）', len(queued) > 1 and queued[1].endswith('2'),
          queued[1] if len(queued) > 1 else '(无)')
    wait_drain()

    # --- 4. 多选与去重 -----------------------------------------------------
    print()
    print('4. 多选下载与去重')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '1,1,3'})
    queued = [m for m in messages_of(user) if m.startswith('🎧')]
    check('1,1,3 去重后 2 首 → 回执 2', queued and queued[0].endswith('2'),
          queued[0] if queued else '(无)')
    wait_drain()

    # --- 5. 无效编号 -------------------------------------------------------
    print()
    print('5. 无效编号')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '99'})
    msgs = messages_of(user)
    check('无效编号给出明确提示', msgs and msgs[0] == notices.INDEX_NOT_FOUND,
          msgs[0] if msgs else '(无)')

    # --- 6. 盲下载 ---------------------------------------------------------
    print()
    print('6. 盲下载模式')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/blind on'})
    check('开启盲模式有提示', messages_of(user)[0].startswith('✅ 已开启盲下载模式'))
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '稻香'})
    first = messages_of(user)
    check('盲模式下文本直接入队第一首',
          first and first[0].startswith('🎧 已加入下载队列：1（盲选：稻香1-歌手1）'),
          first[0] if first else '(无)')
    wait_drain()
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/blind off'})
    check('关闭盲模式有提示', messages_of(user)[0].startswith('⏸️ 已关闭盲下载模式'))
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '晴天'})
    check('关闭后恢复成搜索列表',
          messages_of(user)[0].startswith('🎵 ') and '1. 晴天1-歌手1' in messages_of(user)[0])

    # --- 7. /cancel --------------------------------------------------------
    print()
    print('7. /cancel 只清待下载')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '1,2,3'})
    time.sleep(0.3)      # 等 worker 把第一首取走，否则三条都还在队列里
    removed = dl.get_queue().cancel_pending()
    check('/cancel 清掉了 2 条（1 条已在下载）', removed == 2, f'removed={removed}')
    wait_drain()
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/cancel'})
    check('队列空时 /cancel 给出提示', messages_of(user)[0] == notices.QUEUE_ALREADY_EMPTY)

    # --- 8. /queue ---------------------------------------------------------
    print()
    print('8. /queue')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '1,2'})
    time.sleep(0.2)
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/queue'})
    text = [m for m in messages_of(user) if m.startswith('📋')]
    check('/queue 显示在飞与排队',
          text and '正在下载：晴天1-歌手1' in text[0] and '排队中' in text[0],
          (text[0].replace('\n', ' / ')[:80] if text else '(无)'))
    wait_drain()

    # --- 9. /limit ---------------------------------------------------------
    print()
    print('9. /limit（S6：改条数必须立刻重建引擎）')
    sent.clear()
    builds_before = music_engine._build_count
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/limit 20'})
    check('接受合法值 20', messages_of(user)[0].startswith('✅ 搜索条数已设为 20'))
    check('条数已生效', settings.search_limit == 20, str(settings.search_limit))
    check('引擎已重建（否则新条数不会生效）',
          music_engine._build_count == builds_before + 1,
          f'{builds_before} → {music_engine._build_count}')
    check('重建原因被记录', '20' in music_engine.last_rebuild_reason,
          music_engine.last_rebuild_reason)
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/limit 99'})
    check('超上限被拒', messages_of(user)[0].startswith('❌ 超出范围'))
    check('被拒后条数未变', settings.search_limit == 20, str(settings.search_limit))
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/limit 0'})
    check('低于下限被拒', messages_of(user)[0].startswith('❌ 超出范围'))
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/limit abc'})
    check('非数字被拒', '请给一个' in messages_of(user)[0])

    # 还原
    settings.search_limit = 8
    from app.config import save_runtime_override
    save_runtime_override('search_limit', 8)
    music_engine.rebuild('测试结束还原条数')

    # --- 10. 通用指令 -------------------------------------------------------
    print()
    print('10. 通用指令')
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/help'})
    check('/help 有内容', 'musicbot 使用帮助' in messages_of(user)[0])
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/version'})
    check('/version 正确', messages_of(user)[0].startswith('musicbot v'))
    sent.clear()
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': '/nosuch'})
    check('未知指令有提示', messages_of(user)[0].startswith('未知指令'))
    sent.clear()
    commands.handle_message({'MsgType': 'event', 'FromUserName': user, 'Content': ''})
    check('事件消息被忽略（不发消息）', not messages_of(user))

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
