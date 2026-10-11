"""S10 验收：新歌首发自动下载 + 专辑批量下载（打桩网络与消息，不联网）。

覆盖：
1. `app/qq_web.py` 的链接解析与「接口原始数据 → 严格匹配三段」的转换；
2. 跨栏目去重、缓存差分、「首次只建基线」；
3. 「下载数量」限制与「锁定严格匹配」的任务构造（含回执先于入队）；
4. `/new` 与 `/album` 的参数校验；
5. 帮助页仍在企微 2048 字节以内。

用法：python tests/s10_check.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import newsong, notices, qq_web                        # noqa: E402
from app.config import settings                                 # noqa: E402
from app.songinfo import SongFields                             # noqa: E402
from app.pipeline import KIND_STRICT_BLIND                      # noqa: E402

sys.path.insert(0, TESTS_DIR)
from _common import read_version                                # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


# --- 打桩 --------------------------------------------------------------------

TEMP = tempfile.mkdtemp(prefix='wb-s10-', dir=os.path.dirname(ROOT))
settings.data_dir = TEMP                       # 状态文件写到项目外的临时目录

EVENTS: list[str] = []


def fake_send_text(text: str, user: str) -> None:
    EVENTS.append(f'msg:{user}:{text.splitlines()[0][:40]}')


class FakeQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[object, int]] = []

    def reserve_position(self) -> int:
        EVENTS.append('reserve')
        return len(self.tasks) + 1

    def submit(self, task, *, position: int = 0) -> int:
        EVENTS.append('submit')
        self.tasks.append((task, position))
        return position


FAKE_QUEUE = FakeQueue()
newsong.send_text = fake_send_text                      # type: ignore[assignment]
newsong.get_queue = lambda: FAKE_QUEUE                  # type: ignore[assignment]


def song(name: str, mid: str, singers: list[str], album: str) -> qq_web.WebSong:
    return qq_web.WebSong(mid=mid, fields=SongFields(
        song=name, singer=', '.join(singers), album=album))


TAB_DATA: dict[str, list[qq_web.WebSong]] = {}


def fake_fetch_all(tab_names, **kwargs):
    counts, merged, errors = {}, {}, []
    for name in tab_names:
        songs = TAB_DATA.get(name, [])
        counts[name] = len(songs)
        for item in songs:
            merged.setdefault(item.key, item)
    return counts, list(merged.values()), errors


qq_web.fetch_all_debut_songs = fake_fetch_all           # type: ignore[assignment]


def reset_state() -> None:
    newsong.reset_cache()
    newsong.update_settings(enabled=False, owner='', count=0, hour=8,
                            tabs=list(qq_web.DEBUT_TAB_NAMES))


# --- 1. 栏目与链接解析 -------------------------------------------------------

def test_tabs_and_links() -> None:
    print()
    print('=' * 72)
    print('1. 栏目映射与专辑链接解析')
    print('=' * 72)
    check('6 个栏目且 type 与实测一致',
          qq_web.DEBUT_TABS == (('最新', 5), ('内地', 1), ('港台', 6),
                                ('欧美', 2), ('韩国', 4), ('日本', 3)),
          str(qq_web.DEBUT_TABS))
    check('DEBUT_TAB_TYPES 反查正确', qq_web.DEBUT_TAB_TYPES['港台'] == 6)

    cases = [
        ('https://y.qq.com/n/ryqq_v2/albumDetail/0041WVfh2vtlJE', '0041WVfh2vtlJE'),
        ('https://y.qq.com/n/ryqq_v2/albumDetail/0041WVfh2vtlJE?x=1&y=2', '0041WVfh2vtlJE'),
        ('https://y.qq.com/n/ryqq_v2/albumDetail?albummid=0041WVfh2vtlJE', '0041WVfh2vtlJE'),
        ('0041WVfh2vtlJE', '0041WVfh2vtlJE'),
        ('https://y.qq.com/n/ryqq_v2/songDetail/000uSajZ0Wdo2x', ''),
        ('', ''),
        ('随便一段中文', ''),
    ]
    ok = True
    for text, expect in cases:
        got = qq_web.parse_album_mid(text)
        if got != expect:
            ok = False
            print(f'     {text!r} -> {got!r}（期望 {expect!r}）')
    check('专辑链接/裸 mid 解析（含拒绝 songDetail 与非链接）', ok)


# --- 2. 接口原始数据 → 严格匹配三段 ------------------------------------------

def test_payload_conversion() -> None:
    print()
    print('=' * 72)
    print('2. 接口数据 → 三段（歌手拼接必须与 musicdl 一致）')
    print('=' * 72)
    raw = {
        'mid': '000uSajZ0Wdo2x', 'name': '月亮桥', 'title': '月亮桥',
        'singer': [{'name': '周深'}],
        'album': {'mid': '003hhs9h4aezZo', 'name': '月亮桥', 'title': '月亮桥'},
    }
    got = qq_web._to_song(raw)
    check('单歌手：歌名/歌手/专辑/mid 全部取到',
          got is not None and (got.fields.song, got.fields.singer, got.fields.album,
                               got.mid) == ('月亮桥', '周深', '月亮桥', '000uSajZ0Wdo2x'),
          f'{got.fields if got else None}')

    choir = {
        'mid': '002PSdC42Rr1yM', 'name': '听从裙摆',
        'singer': [{'name': '王者荣耀闪光计划'}, {'name': '王者荣耀'}],
        'album': {'name': '听从裙摆'},
    }
    got = qq_web._to_song(choir)
    check('合唱：多个歌手用「, 」拼接（与 musicdl 一致）',
          got is not None and got.fields.singer == '王者荣耀闪光计划, 王者荣耀',
          got.fields.singer if got else '')
    check('search_keyword 不含专辑（自动下载用的关键词）',
          got is not None and got.fields.search_keyword() == '听从裙摆 王者荣耀闪光计划, 王者荣耀',
          got.fields.search_keyword() if got else '')
    check('keyword() 仍含专辑（严格模式手工输入那条路径不变）',
          got is not None and got.fields.keyword().endswith('听从裙摆'),
          got.fields.keyword() if got else '')

    check('没有歌名 → 丢弃', qq_web._to_song({'mid': 'x', 'singer': []}) is None)
    check('非 dict → 丢弃', qq_web._to_song('x') is None)

    no_album = qq_web._to_song({'mid': 'x', 'name': 'A', 'singer': [{'name': 'B'}]})
    check('专辑缺失 → 空字符串（不是 None）',
          no_album is not None and no_album.fields.album == '',
          repr(no_album.fields.album if no_album else None))

    # 与 musicdl 搜索结果做严格匹配（模拟搜索结果对象）
    class SearchLike:
        song_name = '听从裙摆'
        singers = ', '.join(['王者荣耀闪光计划', '王者荣耀'])   # musicdl 的拼法
        album = '听从裙摆'

    from app import songinfo
    ok, reason = songinfo.matches(got.fields if got else SongFields(), SearchLike())
    check('web 三段与 musicdl 搜索结果严格匹配成功', ok, reason)


# --- 3. 缓存差分 / 基线 / 数量限制 -------------------------------------------

def test_scan() -> None:
    print()
    print('=' * 72)
    print('3. 缓存差分：建基线 / 去重 / 增量 / 数量限制')
    print('=' * 72)
    reset_state()
    FAKE_QUEUE.tasks.clear()

    a = song('月亮桥', 'midA', ['周深'], '月亮桥')
    b = song('最难得', 'midB', ['单依纯'], '最难得')
    c = song('BOYFRIEND', 'midC', ['HOSHI'], 'BOYFRIEND')
    TAB_DATA.clear()
    TAB_DATA.update({'最新': [a, b, c], '内地': [a], '港台': []})
    newsong.update_settings(enabled=True, owner='user1', hour=8)

    r1 = newsong.scan(download=True)
    check('首次（缓存为空）→ 识别为建基线', r1.baseline is True, str(r1.baseline))
    check('首次不生成任何下载任务', len(FAKE_QUEUE.tasks) == 0, len(FAKE_QUEUE.tasks))
    check('跨栏目去重：3+1 首只算 3 首', r1.fetched == 3, r1.fetched)
    check('基线把 3 首全部记入缓存', newsong.cache_size() == 3, newsong.cache_size())

    r2 = newsong.scan(download=True)
    check('第二次同样的数据 → 没有新歌', r2.baseline is False and not r2.new_songs
          and r2.queued == 0, f'new={len(r2.new_songs)} queued={r2.queued}')

    d = song('新歌来了', 'midD', ['某歌手'], '新专辑')
    TAB_DATA['最新'] = [d, a, b, c]
    r3 = newsong.scan(download=True)
    check('新增 1 首 → 只认这 1 首', len(r3.new_songs) == 1 and r3.new_songs[0].key == 'midD',
          [s.key for s in r3.new_songs])
    check('新增 1 首 → 入队 1 条', r3.queued == 1 and len(FAKE_QUEUE.tasks) == 1,
          f'queued={r3.queued} tasks={len(FAKE_QUEUE.tasks)}')

    task, position = FAKE_QUEUE.tasks[0]
    check('入队任务锁定严格匹配（KIND_STRICT_BLIND + strict + fields）',
          task.kind == KIND_STRICT_BLIND and task.strict is True
          and isinstance(task.fields, SongFields),
          f'{task.kind} strict={task.strict}')
    check('关键词是「歌名 歌手」空格拼接（不含专辑，实测召回更好）',
          task.keyword == '新歌来了 某歌手', task.keyword)
    check('通知发给 owner', task.user == 'user1', task.user)
    check('批量任务被标记为静默 + 带批次号',
          task.silent is True and bool(task.batch),
          f'silent={task.silent} batch={task.batch}')

    # 回执必须早于第一次 submit（pipeline 不变量 2）
    order = [e for e in EVENTS if e in ('reserve', 'submit') or e.startswith('msg:')]
    first_submit = order.index('submit') if 'submit' in order else -1
    first_msg = next((i for i, e in enumerate(order) if e.startswith('msg:')), -1)
    check('回执先于入队（reserve → 消息 → submit）',
          first_msg >= 0 and first_submit > first_msg and order[0] == 'reserve',
          str(order[:4]))

    # 数量限制
    reset_state()
    FAKE_QUEUE.tasks.clear()
    newsong.update_settings(enabled=True, owner='user1', count=1)
    newsong.update_settings(tabs=['最新'])
    TAB_DATA.clear()
    TAB_DATA['最新'] = [song('一', 'm1', ['a'], 'x'), song('二', 'm2', ['b'], 'x'),
                        song('三', 'm3', ['c'], 'x')]
    newsong.reset_cache()
    newsong.scan(download=False)                  # 建基线
    TAB_DATA['最新'] = [song('新一', 'n1', ['a'], 'x'), song('新二', 'n2', ['b'], 'x')]
    r4 = newsong.scan(download=True)
    check('count=1 时 2 首新增只入队 1 首',
          len(r4.new_songs) == 2 and r4.queued == 1 and len(FAKE_QUEUE.tasks) == 1,
          f'new={len(r4.new_songs)} queued={r4.queued}')

    # 没有启用栏目
    newsong.update_settings(tabs=[])
    r5 = newsong.scan(download=True)
    check('没有启用栏目 → 跳过并给出说明',
          r5.fetched == 0 and '没有启用任何栏目' in r5.note, r5.note)

    # reset 后重新建基线
    newsong.update_settings(tabs=['最新'])
    removed = newsong.reset_cache()
    check('reset 清空历史', removed >= 1 and newsong.cache_size() == 0, removed)
    newsong.scan(download=False)
    check('reset 后下次扫描又按基线处理', newsong.cache_size() >= 1, newsong.cache_size())


# --- 4. 批次登记（静默 + 汇总）----------------------------------------------

def test_batch() -> None:
    print()
    print('=' * 72)
    print('4. 批次登记与汇总文案（静默模式）')
    print('=' * 72)
    from app import notices as n

    batch = newsong._start_batch('user1', '新歌首发', 3)
    check('批次登记返回批次号', bool(batch), batch)
    newsong.record_result(batch, 'A', True)
    newsong.record_result(batch, 'B', True)
    newsong.record_result(batch, 'C', False, '严格匹配未命中')
    with newsong._batch_lock:
        info = newsong._batches.get(batch)
        done, failed = int(info['done']), list(info['failed'])
    check('三条结果都登记上了', done == 3 and failed == ['C'], f'done={done} failed={failed}')

    text = n.batch_summary_text('新歌首发', 3, 2, ['C'])
    check('汇总文案含总数/成功数/失败数',
          '共 3 首' in text and '成功 2 首' in text and '失败 1 首' in text, text.splitlines()[0])
    check('汇总文案列出失败清单', '· C' in text)
    ok_text = n.batch_summary_text('专辑《X》', 2, 2, [])
    check('全成功时的汇总文案', '全部下载成功' in ok_text and '失败' not in ok_text,
          ok_text.splitlines()[0])
    long_text = n.batch_summary_text('新歌首发', 30, 0, [f'歌{i}' for i in range(25)])
    check('失败过多时截断（避免超长）', len(long_text.encode('utf-8')) < 2048,
          f'{len(long_text.encode("utf-8"))} 字节')

    # 把批次摘掉，别让看护线程在测试里发汇总
    with newsong._batch_lock:
        newsong._batches.pop(batch, None)


# --- 5. 命令参数校验 ---------------------------------------------------------

def test_commands() -> None:
    print()
    print('=' * 72)
    print('5. /new 与 /album 参数校验')
    print('=' * 72)
    from app import commands

    sent: list[str] = []
    commands.send_text = lambda text, user: sent.append(text)      # type: ignore[assignment]
    reset_state()

    commands._handle_new([], 'user1')
    check('/new 无参数 → 显示状态', any('新歌首发' in t for t in sent), sent[-1][:30])

    sent.clear()
    commands._handle_new(['on'], 'user1')
    st = newsong.get_settings()
    check('/new on → 开启并记录 owner', st['enabled'] is True and st['owner'] == 'user1',
          f"enabled={st['enabled']} owner={st['owner']}")
    sent.clear()
    commands._handle_new(['off'], 'user1')
    check('/new off → 关闭', newsong.get_settings()['enabled'] is False)

    sent.clear()
    commands._handle_new(['count', '5'], 'user1')
    check('/new count 5 → 数量设为 5', newsong.get_settings()['count'] == 5)
    sent.clear()
    commands._handle_new(['count', 'all'], 'user1')
    check('/new count all → 数量设为 0（全部）', newsong.get_settings()['count'] == 0)
    sent.clear()
    commands._handle_new(['count', '9999'], 'user1')
    check('/new count 9999 → 拒绝且不改设置',
          newsong.get_settings()['count'] == 0 and '超出范围' in sent[-1], sent[-1][:30])

    sent.clear()
    commands._handle_new(['hour', '21'], 'user1')
    check('/new hour 21 → 时间设为 21', newsong.get_settings()['hour'] == 21)
    sent.clear()
    commands._handle_new(['hour', '25'], 'user1')
    check('/new hour 25 → 拒绝且不改设置',
          newsong.get_settings()['hour'] == 21 and '超出范围' in sent[-1], sent[-1][:30])

    sent.clear()
    commands._handle_new(['内地', 'off'], 'user1')
    check('/new 内地 off → 栏目被移除', '内地' not in newsong.get_settings()['tabs'],
          str(newsong.get_settings()['tabs']))
    sent.clear()
    commands._handle_new(['内地', 'on'], 'user1')
    check('/new 内地 on → 栏目被加回且顺序固定',
          newsong.get_settings()['tabs'] == list(qq_web.DEBUT_TAB_NAMES),
          str(newsong.get_settings()['tabs']))

    sent.clear()
    commands._handle_new(['乱写的'], 'user1')
    check('/new 未知子命令 → 给用法', '用法' in sent[-1])

    sent.clear()
    commands._handle_album([], 'user1')
    check('/album 无参数 → 给用法', '专辑' in sent[-1], sent[-1][:30])
    sent.clear()
    commands._handle_album(['https://y.qq.com/n/ryqq_v2/songDetail/000uSajZ0Wdo2x'], 'user1')
    check('/album 给了歌曲链接 → 提示链接不对', '没认出专辑地址' in sent[-1], sent[-1][:30])


# --- 6. 帮助页与版本 ---------------------------------------------------------

def test_help_and_version() -> None:
    print()
    print('=' * 72)
    print('6. 帮助页字节数与版本')
    print('=' * 72)
    for page in (1, 2):
        body = notices.help_text(page)
        size = len(body.encode('utf-8'))
        check(f'帮助第 {page} 页 < 2048 字节（企微上限）', size < 2048, f'{size} 字节')
    check('帮助页提到了新歌首发与专辑',
          '/new' in notices.help_text(2) and '/album' in notices.help_text(2))
    check('版本号已递增到 1.0.10', read_version() == '1.0.10', read_version())


def main() -> int:
    print('S10 验收：新歌首发自动下载 + 专辑批量下载')
    try:
        test_tabs_and_links()
        test_payload_conversion()
        test_scan()
        test_batch()
        test_commands()
        test_help_and_version()
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
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
