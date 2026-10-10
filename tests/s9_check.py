"""S9 验收：歌曲信息解析与严格匹配（纯逻辑，不联网、不发消息）。

覆盖用户在需求里给出的两类真实输入：
* 带专辑：`歌曲名：罗曼蒂克乌托邦 (Utopia)，歌手名：门尼，专辑名：门尼 (MONEY)`
* 专辑为空：`歌曲名：成长的路口，歌手名：咖啡因乐队，专辑名：`

以及从 QQ音乐复制粘贴时常见的几种变体（中英文冒号、全半角、分隔符、
专辑名里自带逗号、字段别名）。严格匹配的归一化规则也在这里验证。

用法：python tests/s9_check.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import songinfo                              # noqa: E402
from app.songinfo import SongFields                    # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), str(detail)))
    print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail else ''))


class FakeSong:
    def __init__(self, name: str, singers: str, album: str) -> None:
        self.song_name = name
        self.singers = singers
        self.album = album


def test_parse() -> None:
    print()
    print('=' * 72)
    print('1. 解析「歌曲信息」格式')
    print('=' * 72)

    f = songinfo.parse('歌曲名：罗曼蒂克乌托邦 (Utopia)，歌手名：门尼，专辑名：门尼 (MONEY)')
    check('用户样例（带专辑）：三段全部正确',
          f.song == '罗曼蒂克乌托邦 (Utopia)' and f.singer == '门尼' and f.album == '门尼 (MONEY)',
          f'{f.song!r} / {f.singer!r} / {f.album!r}')
    check('关键词 = 三段空格拼接',
          f.keyword() == '罗曼蒂克乌托邦 (Utopia) 门尼 门尼 (MONEY)', repr(f.keyword()))

    f2 = songinfo.parse('歌曲名：成长的路口，歌手名：咖啡因乐队，专辑名：')
    check('用户样例（专辑为空）：专辑解析为空字符串',
          f2.song == '成长的路口' and f2.singer == '咖啡因乐队' and f2.album == '',
          f'{f2.song!r} / {f2.singer!r} / {f2.album!r}')
    check('专辑为空时关键词只有两段',
          f2.keyword() == '成长的路口 咖啡因乐队', repr(f2.keyword()))
    check('专辑为空时展示标题没有多余横杠',
          f2.display() == '成长的路口-咖啡因乐队', f2.display())

    # 变体：英文冒号 + 英文逗号
    f3 = songinfo.parse('歌曲名: 青花瓷, 歌手名: 周杰伦, 专辑名: 我很忙')
    check('英文冒号与英文逗号也认',
          (f3.song, f3.singer, f3.album) == ('青花瓷', '周杰伦', '我很忙'),
          f'{f3.song!r}/{f3.singer!r}/{f3.album!r}')

    # 变体：标签别名 + 分号/顿号
    f4 = songinfo.parse('歌名：晴天；演唱者：周杰伦、专辑：叶惠美')
    check('标签别名（歌名/演唱者/专辑）与分号顿号也认',
          (f4.song, f4.singer, f4.album) == ('晴天', '周杰伦', '叶惠美'),
          f'{f4.song!r}/{f4.singer!r}/{f4.album!r}')

    # 专辑名里自带逗号 → 不能被切开
    f5 = songinfo.parse('歌曲名：A，歌手名：B，专辑名：C，特别版')
    check('专辑名里自带逗号不被误切', f5.album == 'C，特别版', repr(f5.album))

    # 不是这个格式
    check('普通歌名不识别为歌曲信息', songinfo.looks_like_song_fields('青花瓷') is False)
    check('只有单个字段标签不算歌曲信息',
          songinfo.looks_like_song_fields('歌曲名：只有歌名') is False)
    check('普通歌名解析结果为空', songinfo.parse('青花瓷').has_song is False)


def test_match() -> None:
    print()
    print('=' * 72)
    print('2. 严格匹配（归一化：大小写 / 全半角 / 空白）')
    print('=' * 72)

    fields = songinfo.parse('歌曲名：罗曼蒂克乌托邦 (Utopia)，歌手名：门尼，专辑名：门尼 (MONEY)')
    song = FakeSong('罗曼蒂克乌托邦 (Utopia)', '门尼', '门尼 (MONEY)')
    ok, reason = songinfo.matches(fields, song)
    check('完全一致 → 匹配', ok is True, reason)

    # 全角括号 / 大小写差异应视为一致
    wide = FakeSong('罗曼蒂克乌托邦（Utopia）', '门尼', '门尼（MONEY）')
    ok, reason = songinfo.matches(fields, wide)
    check('全角括号视为一致（归一化生效）', ok is True, reason)

    upper = FakeSong('罗曼蒂克乌托邦 (UTOPIA)', '门尼', '门尼 (MONEY)')
    ok, reason = songinfo.matches(fields, upper)
    check('大小写差异视为一致', ok is True, reason)

    # 空白差异
    spaced = FakeSong('罗曼蒂克乌托邦  (Utopia)', '门尼', '门尼 (MONEY)')
    ok, _ = songinfo.matches(fields, spaced)
    check('多余空白视为一致', ok is True)

    # 专辑不符 → 不匹配
    bad_album = FakeSong('罗曼蒂克乌托邦 (Utopia)', '门尼', '别的专辑')
    ok, reason = songinfo.matches(fields, bad_album)
    check('专辑不同 → 不匹配', ok is False and '专辑不符' in reason, reason)

    # 歌手不符 → 不匹配
    bad_singer = FakeSong('罗曼蒂克乌托邦 (Utopia)', '别人', '门尼 (MONEY)')
    ok, reason = songinfo.matches(fields, bad_singer)
    check('歌手不同 → 不匹配', ok is False and '歌手不符' in reason, reason)

    # 歌名不符 → 不匹配
    bad_name = FakeSong('另一首歌', '门尼', '门尼 (MONEY)')
    ok, reason = songinfo.matches(fields, bad_name)
    check('歌名不同 → 不匹配', ok is False and '歌名不符' in reason, reason)

    # 用户没给专辑 → 不校验专辑（单曲场景）
    no_album = songinfo.parse('歌曲名：成长的路口，歌手名：咖啡因乐队，专辑名：')
    any_album = FakeSong('成长的路口', '咖啡因乐队', '随便什么专辑')
    ok, reason = songinfo.matches(no_album, any_album)
    check('用户未给专辑时不校验专辑', ok is True, reason)

    empty = FakeSong('成长的路口', '咖啡因乐队', '')
    ok, reason = songinfo.matches(no_album, empty)
    check('用户未给专辑 + 结果也无专辑 → 匹配', ok is True, reason)


def main() -> int:
    print('S9 验收：歌曲信息解析与严格匹配')
    test_parse()
    test_match()
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
