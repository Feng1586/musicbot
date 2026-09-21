"""所有面向用户的文案集中在这里，改文案不用碰逻辑。

按阶段排列：标注 S1 的现在就在用，其余等对应阶段接入（先写好，避免后面再回头改文案）。
"""

from __future__ import annotations

from utils.version import __version__

_HINT_LIMIT = 2048


def _clip(text: str, limit: int = _HINT_LIMIT) -> str:
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    return raw[:limit - 20].decode('utf-8', 'ignore') + '\n…（已截断）'


# --- S1：基础 ---------------------------------------------------------------

HELP_TEXT = """📖 musicbot 使用帮助

【搜索】
直接发送歌曲名称即可，例如：青花瓷

【下载】
回复搜索结果最前面的数字，多首用英文逗号分隔：
1        下载第 1 首
1,3,5    同时下载第 1、3、5 首

下载在后台队列里按顺序进行，不用等它下完，可以继续搜索。

【音乐源】
/qq      切换到 QQ音乐
/wyy     切换到 网易云音乐
/source  查看当前使用的源

【搜索设置】
/limit       查看当前搜索条数
/limit 20    设置为 20 条（1-50）

【下载模式】
/blind       查看盲下载模式状态
/blind on    开启：之后发送歌名会直接下载搜索结果第 1 首
/blind off   关闭

【Cookie】
/qq login    扫码登录 / 更新 QQ音乐 Cookie
/wyy login   扫码登录 / 更新 网易云音乐 Cookie

【队列】
/queue   查看下载队列
/cancel  清空还没开始下载的任务

【其他】
/status   查看运行状态
/version  查看版本
/update   检查 musicdl 版本更新
/restart  重启服务（用于让更新生效）

💡 搜索结果 5 分钟内有效，请及时回复数字下载"""


def startup_text(source_name: str, limit: int, cookie_lines: list[str] | None = None,
                 update_line: str = '') -> str:
    """启动消息（S4 起会带上 Cookie 体检结果与升级提示）。

    注意：文案与旧项目 1.0.6 那次广播保持一致，用户已认可过那版风格。
    """
    lines = [
        '🎵 musicbot 已启动',
        '',
        f'当前版本：v{__version__}',
        f'音乐源：{source_name}',
        f'搜索条数：{limit}',
        '',
        '━━━━━━━━━━━━━━━━━━',
        '📖 使用教程',
        '━━━━━━━━━━━━━━━━━━',
        '直接发送歌曲名称即可搜索',
        '回复列表最前面的数字下载，多首用逗号：1,3,5',
        '',
        '━━━━━━━━━━━━━━━━━━',
        '⌨️ 常用指令',
        '━━━━━━━━━━━━━━━━━━',
        '/qq /wyy　切换搜索源',
        '/blind　　开启盲下载（发歌名直接下第一首）',
        '/limit N　调整搜索条数（1-50）',
        '/qq login /wyy login　更新 Cookie',
        '/help　　 查看完整帮助',
    ]
    if cookie_lines:
        lines += ['', '━━━━━━━━━━━━━━━━━━', '🍪 Cookie 状态', '━━━━━━━━━━━━━━━━━━',
                  *cookie_lines]
    if update_line:
        lines += ['', update_line]
    return _clip('\n'.join(lines))


# --- S2：搜索与下载 ---------------------------------------------------------

def search_result_text(source_name: str, lines: list[str], *, limit_hint: str) -> str:
    return _clip(f'🎵 {source_name} 搜索结果\n' + '\n'.join(lines) + f'\n{limit_hint}')


SEARCH_HINT = '请回复最前面的数字下载（编号从 1 开始，5 分钟内有效）'
SEARCH_EMPTY = '❌ 没有搜到结果，换个关键词或更换音乐源（/qq /wyy）试试'
SEARCH_FAILED = '❌ 搜索失败，请稍后重试'
NO_SEARCH_YET = '请先发送歌曲名称搜索，或直接回复搜索结果里的数字'
INDEX_EXPIRED = '⏰ 搜索结果已过期，请重新发送歌曲名称搜索'
INDEX_NOT_FOUND = '未找到对应的歌曲编号，请回复搜索结果里的数字'
NOT_DOWNLOADABLE = '这首歌没有可用的下载地址，请换一首'


# --- S3：下载队列 -----------------------------------------------------------

def queued_text(count: int) -> str:
    """count = 当前队列任务数（含正在下载的那首），不是用户选中的编号。"""
    return f'🎧 已加入下载队列：{count}'


def start_download_text(title: str) -> str:
    return f'⚙️ 开始下载：{title}'


def download_done_text(filename: str, size_text: str) -> str:
    return f'✅ 下载完成：{filename}\n💾 大小：{size_text}'


def download_failed_text(title: str, reason: str) -> str:
    return f'❌ 下载失败：{title}\n原因：{reason}'


def queue_text(pending: int, inflight: str, waiting: list[str]) -> str:
    lines = ['📋 下载队列', f'待处理共 {pending} 条']
    if inflight:
        lines.append(f'正在下载：{inflight}')
    if waiting:
        lines.append('排队中：')
        lines += [f'  {i}. {t}' for i, t in enumerate(waiting[:15], 1)]
        if len(waiting) > 15:
            lines.append(f'  …另有 {len(waiting) - 15} 条')
    elif not inflight:
        lines.append('（队列是空的）')
    return _clip('\n'.join(lines))


QUEUE_CLEARED = '🧹 已清空待下载队列（正在下载的那首不受影响）'
QUEUE_ALREADY_EMPTY = '队列里没有待下载的任务'


# --- S4：源、盲模式、Cookie -------------------------------------------------

def source_switched_text(source_name: str, login_hint: str = '') -> str:
    text = f'✅ 已切换到 {source_name}'
    return text + (f'\n{login_hint}' if login_hint else '')


SOURCE_CURRENT = '当前音乐源：{name}'


def blind_mode_text(enabled: bool) -> str:
    if enabled:
        return ('✅ 已开启盲下载模式\n'
                '现在直接发送歌曲名称，会自动下载搜索结果里的第 1 首。\n'
                '关闭请发送 /blind off')
    return '⏸️ 已关闭盲下载模式，恢复为「先搜索、再回复数字」'


BLIND_MODE_STATUS = '盲下载模式：{state}'
BLIND_NO_RESULT = '❌ 没有搜到「{keyword}」，换个歌名或换源（/qq /wyy）试试'
BLIND_QUEUED = '🎧 已加入下载队列：{count}（盲选：{title}）'


def login_start_text(source_name: str, page_url: str = '', *,
                     image_expected: bool = True) -> str:
    """扫码登录的第一条说明。

    `image_expected=False` 表示这次不会再跟一条二维码图片（素材上传走不通）。
    此时**必须**去掉「二维码图片见下一条消息」—— 否则就是一句空头支票，
    用户会一直等一条永远不会来的消息。这正是旧写法在「反代没放行
    media/upload、又没配对外地址」这个组合下的表现。
    """
    lines = [
        f'🔐 正在为「{source_name}」生成登录二维码…',
        '请用手机 App 扫码，并在手机上确认登录',
    ]
    if page_url:
        lines.append(f'也可以打开：{page_url}')
    if image_expected:
        lines.append('（二维码图片见下一条消息）')
    return '\n'.join(lines)


def qrcode_undeliverable_text(source: str, *, page_url: str = '', lan_url: str = '',
                              reason: str = '') -> str:
    """二维码一条路都走不通时的说明。

    三种情况从好到差：有对外地址 → 给链接；只有局域网 → 给内网地址；
    什么都没有 → 明确告诉管理员该改哪里。
    """
    lines = ['⚠️ 二维码图片没能发出来' + (f'（{_clip(reason, 80)}）' if reason else '')]
    if page_url:
        lines.append(f'请打开这个链接扫码：{page_url}')
    elif lan_url:
        lines.append(f'请在同一局域网内打开这个链接扫码：{lan_url}')
    else:
        lines.append('请联系管理员：让反代放行 /cgi-bin/media/upload，'
                     '或配置 MUSICBOT_PUBLIC_BASE_URL 后重试')
    return '\n'.join(lines)


def login_success_text(source_name: str, account: str = '') -> str:
    text = f'✅ {source_name} Cookie 已更新'
    if account:
        text += f'\n账号：{account}'
    return text


LOGIN_FAILED = '❌ 登录失败：{reason}\n可重新发送 /{source} login 再试一次'
LOGIN_TIMEOUT = '⏰ 二维码已过期，请重新发送 /{source} login'


COOKIE_OK = '✅ {name}　正常（账号：{account}）'
COOKIE_EXPIRED = '⚠️ {name}　Cookie 已失效，发 /{cmd} login 更新'
COOKIE_MISSING = '⚠️ {name}　未登录（发 /{cmd} login 更新，不登录音质受限）'
COOKIE_UNKNOWN = '❔ {name}　状态未知（{reason}）'


def cookie_expired_alert(name: str, cmd: str) -> str:
    return (f'⚠️ {name} 的 Cookie 已失效，搜索/下载会失败。\n'
            f'请发送 /{cmd} login 扫码更新。')


def cookie_report_text(status_lines: list[str]) -> str:
    """启动时的 Cookie 体检（与启动广播分开成两条，用户明确要求的）。"""
    return _clip('\n'.join([
        '🍪 Cookie 状态检查',
        '',
        *status_lines,
        '',
        '可发送 /qq login 或 /wyy login 更新对应的 Cookie',
    ]))


# --- S7：版本与更新 ---------------------------------------------------------

def update_available_line(current: str, latest: str) -> str:
    return f'⚠️ musicdl 有新版本 {latest}（当前 {current}），发 /update 查看'


UPDATE_RISK_TEXT = """⚠️ 更新 musicdl 前请先读这段

将在容器内执行 pip install -U musicdl。

【风险】
1. musicdl 新版本的调用方式可能与本项目不兼容，更新后可能导致
   服务无法启动，届时需要重建容器才能恢复。
2. 更新只对当前容器实例有效。容器一旦被重建（自动更新镜像、
   compose 重建等），会回到镜像内置的版本。
3. 更新后需要重启服务才会生效（可用 /restart，或手动重启容器）。
4. 若与锁定依赖冲突（例如 cryptography<47），会被自动拦下。

【已做的保护】
· 更新前会记录当前版本，启动失败会自动装回该版本
· 可随时用 /update rollback 手动回滚

确认执行请回复：/update confirm"""

UPDATE_NOT_AVAILABLE = '当前 musicdl 已是最新版本（{current}），无需更新'
UPDATE_CHECK_FAILED = '无法获取 musicdl 版本信息（{reason}），请稍后再试'
UPDATE_CONFLICT = '❌ 更新已中止：{reason}'
UPDATE_DONE = ('✅ musicdl 已更新：{old} → {new}\n'
               '更新需要重启服务才会生效：\n'
               '· 发送 /restart 立即重启\n'
               '· 或手动重启容器（docker restart）')
UPDATE_ROLLBACK_DONE = '↩️ 已回滚到 musicdl {version}，重启后生效'
UPDATE_ROLLBACK_NONE = '没有可回滚的版本记录'

RESTART_NOTICE = '🔄 正在重启服务…（约 10 秒后可用）'
RESTART_NO_SOCKET = ('🔄 未挂载 docker.sock，将以退出进程的方式重启。\n'
                     '若容器没配置自动重启策略（restart: unless-stopped / always），'
                     '服务会停在停止状态，需要手动启动容器。')


# --- 通用 -------------------------------------------------------------------

def unknown_command_text(raw: str) -> str:
    return f'未知指令：{raw}\n发送 /help 查看可用指令'


def status_text(source_name: str, cookie_lines: list[str], queue_line: str,
                extra_lines: list[str] | None = None) -> str:
    lines = [
        '📊 运行状态',
        f'版本：v{__version__}',
        f'当前音乐源：{source_name}',
        '',
        '🍪 Cookie',
        *(cookie_lines or ['（暂未检查）']),
        '',
        '📋 队列',
        queue_line,
    ]
    if extra_lines:
        lines += ['', *extra_lines]
    return _clip('\n'.join(lines))


def version_text() -> str:
    return f'musicbot v{__version__}'
