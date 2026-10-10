"""歌曲信息解析：把「从 QQ音乐复制出来的一行」拆成 歌名 / 歌手 / 专辑。

用户从 QQ 音乐（网页或客户端）复制歌曲信息时，拿到的是这种形态：

    歌曲名：罗曼蒂克乌托邦 (Utopia)，歌手名：门尼，专辑名：门尼 (MONEY)

有时专辑为空（单曲没有专辑）：

    歌曲名：成长的路口，歌手名：咖啡因乐队，专辑名：

这个模块只做**解析**，不碰网络、不碰用户状态，便于单独测试。

为什么单独成模块而不是塞进 commands.py
----------------------------------------
解析规则会随用户反馈反复调整（分隔符、全半角、空格、缺字段），
而 `commands.py` 已经很长了。把它隔离出来，测试直接对着它写就好。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 三个字段的中文标签。用户从 QQ 音乐复制出来的就是这几个字，别改。
FIELD_SONG = '歌曲名'
FIELD_SINGER = '歌手名'
FIELD_ALBUM = '专辑名'

# 标签的容错写法：允许用户粘贴时把「歌曲名」写成「歌名」、或者两端带空格。
_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    'song': ('歌曲名', '歌名', '曲名', '歌曲'),
    'singer': ('歌手名', '歌手', '演唱者', '艺人'),
    'album': ('专辑名', '专辑'),
}

# 中英文冒号都认
_COLON = ':：'
# 字段之间的分隔符字符集（用于剥掉值末尾多余的逗号/分号）
_SEP_CHARS = ',，;；、'

# 「标签 + 冒号」在整行里的位置（用来切段）。用非贪婪 + 前瞻下一个标签，
# 这样专辑名里本身带逗号（例如「门尼 (MONEY)，特别版」）也不会被切错。
_ORDER = ('song', 'singer', 'album')


@dataclass
class SongFields:
    """解析结果。空字符串表示该字段没有（例如单曲没有专辑）。"""

    song: str = ''
    singer: str = ''
    album: str = ''

    @property
    def has_song(self) -> bool:
        return bool(self.song)

    def keyword(self) -> str:
        """拼搜索关键词：非空字段用**空格**连接（用户要求的实例：
        「罗曼蒂克乌托邦 (Utopia) 门尼 门尼 (MONEY)」）。"""
        return ' '.join(part for part in (self.song, self.singer, self.album) if part)

    def display(self) -> str:
        """给回执/队列展示用的短标题：歌名-歌手[-专辑]。"""
        parts = [self.song or '未知', self.singer or '未知']
        if self.album:
            parts.append(self.album)
        return '-'.join(parts)


def _label_pattern(key: str) -> str:
    """标签的容错正则：多个别名任一匹配，尾部允许空格（`歌曲名 ： xxx`）。"""
    names = _LABEL_ALIASES[key]
    alternation = '|'.join(map(re.escape, names))
    return r'(?:' + alternation + r')\s*'


def looks_like_song_fields(text: str) -> bool:
    """粗略判断这一行是不是「歌曲信息」格式。

    判据：**至少**出现两个字段标签（带冒号）。只写「歌曲名：xxx」这种单字段的
    不算 —— 那种更像是用户随口带了个前缀，按普通关键词搜更合适。
    """
    hits = 0
    for key in _ORDER:
        if re.search(_label_pattern(key) + r'[' + _COLON + r']', text):
            hits += 1
    return hits >= 2


def parse(text: str) -> SongFields:
    """把「歌曲名：X，歌手名：Y，专辑名：Z」解析成三个字段。

    容错点：
    * 中英文冒号、标签与冒号之间的空格；
    * 字段顺序固定按 歌曲名 → 歌手名 → 专辑名，但**允许缺字段**；
    * 专辑为空（`专辑名：` 后什么都没有）→ album 为空字符串；
    * 值尾部多余的分隔符会被剥掉。
    解析不出任何字段时返回全空对象（调用方据此决定是否拒绝）。
    """
    found: dict[str, str] = {}
    # 先定位每个标签的出现位置，再按位置切段 —— 比 split(',') 稳，
    # 因为专辑名里可能自带逗号。
    positions: list[tuple[int, int, str]] = []      # (标签起点, 值起点, key)
    for key in _ORDER:
        match = re.search(_label_pattern(key) + r'[' + _COLON + r']', text)
        if match:
            positions.append((match.start(), match.end(), key))
    if not positions:
        return SongFields()

    positions.sort(key=lambda item: item[0])
    for idx, (_label_start, value_start, key) in enumerate(positions):
        # 值到下一个标签起点为止（没有下一个就到行尾）
        value_end = positions[idx + 1][0] if idx + 1 < len(positions) else len(text)
        raw = text[value_start:value_end]
        found[key] = _clean_value(raw)

    return SongFields(song=found.get('song', ''),
                      singer=found.get('singer', ''),
                      album=found.get('album', ''))


def _clean_value(raw: str) -> str:
    """去掉值两端的分隔符、空白，以及用户误粘进去的尾部逗号。"""
    value = raw.strip()
    value = re.sub(r'^[' + re.escape(_SEP_CHARS) + r'\s]+', '', value)
    value = re.sub(r'[' + re.escape(_SEP_CHARS) + r'\s]+$', '', value)
    return value.strip()


# --- 严格匹配 ---------------------------------------------------------------

def _normalize(text: str) -> str:
    """严格比较前的归一化：忽略大小写、全角半角、连续空白与括号差异。

    为什么要归一化：用户从 QQ 音乐复制的歌名和搜索接口返回的歌名，
    在两处空白、大小写上常有出入（`罗曼蒂克乌托邦 (Utopia)` vs
    `罗曼蒂克乌托邦(Utopia)`），逐字节比较会全军覆没。
    但归一化**不改动实质内容** —— 只是把「看起来一样」视为一样。
    """
    if not text:
        return ''
    # 全角 → 半角（ASCII 可见字符区间）
    chars = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    value = ''.join(chars).lower()
    # 全角/半角括号统一成半角，再去掉所有空白
    value = value.replace('（', '(').replace('）', ')').replace('【', '[').replace('】', ']')
    value = re.sub(r'\s+', '', value)
    return value


def matches(fields: SongFields, song: object, *, require_album: bool = True) -> tuple[bool, str]:
    """判断一首搜索结果是否与用户给的三段**严格一致**。

    返回 (是否匹配, 不匹配的原因)。规则：
    * 歌名、歌手必须一致（归一化后）；
    * 专辑：用户给了专辑就要求一致；用户**没给**专辑（空）则不校验专辑
      —— 用户明说了「部分单曲没有专辑」，这时不该拿空专辑去卡。
    * `require_album=False` 时连用户给了专辑也不强制（留给未来的宽松档）。
    """
    name = getattr(song, 'song_name', '') or ''
    singers = getattr(song, 'singers', '') or ''
    album = getattr(song, 'album', '') or ''

    if _normalize(fields.song) != _normalize(name):
        return False, f'歌名不符（期望「{fields.song}」，实际「{name}」）'
    if fields.singer and _normalize(fields.singer) != _normalize(singers):
        return False, f'歌手不符（期望「{fields.singer}」，实际「{singers}」）'
    if require_album and fields.album:
        if _normalize(fields.album) != _normalize(album):
            return False, f'专辑不符（期望「{fields.album}」，实际「{album or "空"}」）'
    return True, ''
