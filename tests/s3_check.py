"""S3 + S6 验收：统一任务队列、回执与位次、盲下载、超时、/cancel、/limit。

用**假的搜索结果与假的下载**（不碰真实接口），只验证本项目自己的逻辑：
* 消息顺序必须是 回执 → 开始下载 → 下载完成（回执先于 worker 的任何消息）
* 连发多首时：位次依次递增、**开始下载的顺序 = 发送顺序**（v1.0.6 的核心）
* 排队期间回数字 → 提示「还在排队」，不是误导性的「请先搜索」
* 单任务超时 → 放弃等待并继续处理下一个
* /cancel 只清还没开始的
* /interval、/timeout 的范围校验与生效
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

import app.pipeline as pipeline                          # noqa: E402
from app import commands, notices                        # noqa: E402
from app.config import (SEARCH_LIMIT_MAX, save_runtime_override,   # noqa: E402
                        settings)
from app.pipeline import get_queue, init_queue            # noqa: E402

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


def wait_msgs(user: str, count: int, timeout: float = 20) -> list[str]:
    """等该用户的消息攒到 count 条（队列是异步的，不能再同步断言）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = messages_of(user)
        if len(msgs) >= count:
            return msgs
        time.sleep(0.05)
    return messages_of(user)


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


def drain(timeout: float = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and get_queue().pending() > 0:
        time.sleep(0.05)
    time.sleep(0.1)


def text(user: str, content: str) -> None:
    commands.handle_message({'MsgType': 'text', 'FromUserName': user, 'Content': content})


def main() -> int:
    print('=' * 72)
    print('S3 + S6 验收：统一任务队列 / 位次 / 盲下载 / 超时 / cancel / limit')
    print('=' * 72)

    # 打桩：假企微（三个模块各自 import 了 send_text，都要换掉）
    commands.send_text = fake_send
    import app.downloader as dl
    dl.send_text = fake_send
    pipeline.send_text = fake_send

    music_engine = init_queue()[0]
    music_engine.search = fake_search          # type: ignore[assignment]
    music_engine.download = fake_download      # type: ignore[assignment]

    # 测试里不等间隔（正常默认是 5 秒）；**只改内存值、不落盘**，
    # 否则会把用户的 data/settings.json 改掉。
    settings.task_interval_seconds = 0

    user = 'tester'

    # --- 1. 搜索：先回执、再出结果 -----------------------------------------
    print()
    print('1. 搜索（入口只回执 + 入队，搜索在 worker 里）')
    sent.clear()
    text(user, '青花瓷')
    first = messages_of(user)
    check('搜索也先发「已受理」（不再等搜索完才说话）',
          bool(first) and first[0] == notices.accepted_text(1, '青花瓷'),
          first[0] if first else '(无)')
    msgs = wait_msgs(user, 2)
    check('随后收到编号列表',
          len(msgs) == 2 and msgs[1].startswith('🎵 ') and '1. 青花瓷1-歌手1' in msgs[1],
          ' | '.join(m.split('\n')[0][:26] for m in msgs))

    # --- 2. 编号下载：三条消息 ---------------------------------------------
    print()
    print('2. 编号下载（单首）')
    sent.clear()
    text(user, '2')
    msgs = wait_msgs(user, 3)
    check('消息顺序：受理 → 开始下载 → 下载完成',
          len(msgs) == 3
          and msgs[0] == notices.accepted_text(1, '青花瓷2-歌手2')
          and msgs[1].startswith('⚙️ 开始下载：青花瓷2-歌手2')
          and msgs[2].startswith('✅ 下载完成：') and '💾 大小：2.0 KB' in msgs[2],
          ' | '.join(m.split('\n')[0][:24] for m in msgs))
    check('回执里是歌名而不是用户回的编号', '青花瓷2' in msgs[0], msgs[0])
    drain()

    # --- 3. 连发多首：位次 + 顺序（v1.0.6 核心）----------------------------
    print()
    print('3. 连发多首：回执位次依次递增，下载顺序 = 发送顺序')
    text(user, '/blind on')
    sent.clear()
    for name in ('稻香', '晴天', '七里香'):
        text(user, name)
    receipts = [m for m in messages_of(user) if m.startswith('🧾')]
    check('三条都**立刻**收到回执（不再有空白期）', len(receipts) == 3,
          f'{len(receipts)} 条')
    check('位次依次为 1 / 2 / 3',
          receipts[:3] == [notices.accepted_text(1, '稻香'),
                           notices.accepted_text(2, '晴天'),
                           notices.accepted_text(3, '七里香')],
          ' | '.join(r[:22] for r in receipts[:3]))
    wait_msgs(user, 3 * 3 + 3)
    drain()
    starts = [m for m in messages_of(user) if m.startswith('⚙️')]
    check('开始下载的顺序 = 发送顺序',
          starts == ['⚙️ 开始下载：稻香1-歌手1',
                     '⚙️ 开始下载：晴天1-歌手1',
                     '⚙️ 开始下载：七里香1-歌手1'],
          ' | '.join(s[3:16] for s in starts))
    dones = [m for m in messages_of(user) if m.startswith('✅')]
    check('三首都下完了', len(dones) == 3, f'{len(dones)} 首')

    # --- 4. 排队期间回数字 -------------------------------------------------
    print()
    print('4. 排队期间回数字 → 提示还在排队（不是「请先搜索」）')
    other = 'tester2'
    sent.clear()
    text(other, '1')
    check('没搜过时回数字 → 原提示不变',
          messages_of(other) == [notices.NO_SEARCH_YET], messages_of(other))

    def slow_search(source_id: str, keyword: str, **_kw):
        time.sleep(1.5)
        return fake_search(source_id, keyword)

    music_engine.search = slow_search           # type: ignore[assignment]
    sent.clear()
    text(other, '慢歌')
    text(other, '1')                            # 搜索还在跑
    fine = messages_of(other)
    check('排队期间回数字 → 提示「还有搜索在队列里」',
          notices.SEARCH_PENDING in fine, ' | '.join(m[:30] for m in fine))
    drain()
    music_engine.search = fake_search           # type: ignore[assignment]

    # --- 5. 多选与去重 -----------------------------------------------------
    print()
    print('5. 多选下载与去重')
    sent.clear()
    text(user, '1,1,3')
    receipt = [m for m in messages_of(user) if m.startswith('🧾')]
    check('1,1,3 去重后 2 首 → 回执一条、注明 2 首',
          len(receipt) == 1 and '2 首' in receipt[0], receipt[0] if receipt else '(无)')
    drain()

    # --- 6. 无效编号 -------------------------------------------------------
    print()
    print('6. 无效编号')
    sent.clear()
    text(user, '99')
    check('无效编号给出明确提示',
          messages_of(user) and messages_of(user)[0] == notices.INDEX_NOT_FOUND,
          messages_of(user)[0] if messages_of(user) else '(无)')

    # --- 7. 盲下载开关 -----------------------------------------------------
    print()
    print('7. 盲下载模式')
    sent.clear()
    text(user, '/blind off')
    check('关闭盲模式有提示', messages_of(user)[0].startswith('⏸️ 已关闭盲下载模式'))
    sent.clear()
    text(user, '晴天')
    msgs = wait_msgs(user, 2)
    check('关闭后恢复成「受理 + 搜索列表」',
          len(msgs) == 2 and msgs[0].startswith('🧾') and msgs[1].startswith('🎵 '),
          ' | '.join(m.split('\n')[0][:22] for m in msgs))

    # --- 8. /cancel --------------------------------------------------------
    print()
    print('8. /cancel 只清还没开始的')
    sent.clear()
    text(user, '1,2,3')
    time.sleep(0.3)      # 等 worker 把第一条取走，否则三条都还在队列里
    removed = get_queue().cancel_pending()
    check('/cancel 清掉了 2 条（1 条已在处理）', removed == 2, f'removed={removed}')
    drain()
    sent.clear()
    text(user, '/cancel')
    check('队列空时 /cancel 给出提示', messages_of(user)[0] == notices.QUEUE_ALREADY_EMPTY)

    # --- 9. /queue ---------------------------------------------------------
    print()
    print('9. /queue')
    sent.clear()
    text(user, '1,2')
    time.sleep(0.2)
    text(user, '/queue')
    shown = [m for m in messages_of(user) if m.startswith('📋')]
    check('/queue 显示正在处理与排队中，并标注任务类型',
          bool(shown) and '正在处理：' in shown[0] and '排队中' in shown[0]
          and '[下载]' in shown[0],
          (shown[0].replace('\n', ' / ')[:80] if shown else '(无)'))
    drain()

    # --- 10. 单任务超时 ----------------------------------------------------
    print()
    print('10. 单任务超时（放弃等待并继续下一个）')

    def hang_search(source_id: str, keyword: str, **_kw):
        if keyword == '卡住':
            time.sleep(4)
        return fake_search(source_id, keyword)

    pipeline._timeout_seconds = lambda: 1.5     # type: ignore[assignment]
    music_engine.search = hang_search           # type: ignore[assignment]
    text(user, '/blind on')
    sent.clear()
    text(user, '卡住')
    text(user, '青花瓷')
    msgs = wait_msgs(user, 5, timeout=15)
    check('超时任务给用户发了超时提示',
          any('任务超时' in m for m in msgs), ' | '.join(m.split('\n')[0][:20] for m in msgs))
    check('超时后仍继续处理下一个（流水线没停）',
          any('青花瓷1-歌手1' in m and m.startswith('⚙️') for m in msgs),
          ' | '.join(m[3:18] for m in msgs if m.startswith('⚙️')))
    # 后面那条（搜索秒回 + 下载 0.8s）必须**没**被超时误伤，否则就是阈值卡太紧了
    check('超时只影响卡住的那条，后续任务不受牵连',
          sum(1 for m in msgs if '任务超时' in m) == 1,
          f'{sum(1 for m in msgs if "任务超时" in m)} 条超时')
    drain()
    time.sleep(4.5)                             # 等被放弃的那条自己跑完，别污染后面的断言
    pipeline._timeout_seconds = lambda: max(1, int(settings.task_timeout_minutes)) * 60
    music_engine.search = fake_search           # type: ignore[assignment]
    sent.clear()

    # --- 11. /interval 与 /timeout -----------------------------------------
    print()
    print('11. /interval 与 /timeout')
    sent.clear()
    text(user, '/interval')
    check('查询任务间隔', '当前任务间隔' in messages_of(user)[0], messages_of(user)[0][:30])
    sent.clear()
    text(user, '/interval 30')
    check('设置间隔有确认',
          messages_of(user)[0] == notices.TASK_INTERVAL_SET.format(value=30))
    check('间隔已生效', settings.task_interval_seconds == 30,
          str(settings.task_interval_seconds))
    sent.clear()
    text(user, '/interval 99')
    check('间隔超上限被拒', messages_of(user)[0].startswith('❌ 超出范围'))
    sent.clear()
    text(user, '/interval abc')
    check('间隔非数字被拒', '请给一个' in messages_of(user)[0])

    sent.clear()
    text(user, '/timeout')
    check('查询单任务超时', '当前单任务超时' in messages_of(user)[0])
    sent.clear()
    text(user, '/timeout 9')
    check('设置超时有确认',
          messages_of(user)[0] == notices.TASK_TIMEOUT_SET.format(value=9))
    check('超时已生效', settings.task_timeout_minutes == 9,
          str(settings.task_timeout_minutes))
    sent.clear()
    text(user, '/timeout 0')
    check('超时低于下限被拒', messages_of(user)[0].startswith('❌ 超出范围'))

    # 还原：运行期用 0 秒（测试要快），落盘值恢复默认，别污染 data/settings.json
    settings.task_interval_seconds = 0
    settings.task_timeout_minutes = 5
    save_runtime_override('task_interval_seconds', 5)
    save_runtime_override('task_timeout_minutes', 5)

    # --- 12. /limit --------------------------------------------------------
    print()
    print('12. /limit（S6：改条数必须立刻重建引擎）')
    sent.clear()
    builds_before = music_engine._build_count
    text(user, '/limit 20')
    check('接受合法值 20', messages_of(user)[0].startswith('✅ 搜索条数已设为 20'))
    check('条数已生效', settings.search_limit == 20, str(settings.search_limit))
    check('引擎已重建（否则新条数不会生效）',
          music_engine._build_count == builds_before + 1,
          f'{builds_before} → {music_engine._build_count}')
    check('重建原因被记录', '20' in music_engine.last_rebuild_reason,
          music_engine.last_rebuild_reason)
    sent.clear()
    text(user, '/limit 99')
    check('超上限被拒', messages_of(user)[0].startswith('❌ 超出范围'))
    check('被拒后条数未变', settings.search_limit == 20, str(settings.search_limit))
    sent.clear()
    text(user, '/limit 0')
    check('低于下限被拒', messages_of(user)[0].startswith('❌ 超出范围'))
    sent.clear()
    text(user, '/limit abc')
    check('非数字被拒', '请给一个' in messages_of(user)[0])

    # 还原
    settings.search_limit = 8
    save_runtime_override('search_limit', 8)
    music_engine.rebuild('测试结束还原条数')

    # --- 13. 通用指令 -------------------------------------------------------
    print()
    print('13. 通用指令')
    sent.clear()
    text(user, '/help')
    check('/help 有内容', 'musicbot 使用帮助' in messages_of(user)[0])
    check('/help 提到了 /interval 与 /timeout',
          '/interval' in messages_of(user)[0] and '/timeout' in messages_of(user)[0])
    sent.clear()
    text(user, '/version')
    check('/version 正确', messages_of(user)[0].startswith('musicbot v'))
    sent.clear()
    text(user, '/status')
    check('/status 里有任务节奏', '任务间隔' in messages_of(user)[0])
    sent.clear()
    text(user, '/nosuch')
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
