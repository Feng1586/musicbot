'''QQ 音乐扫码登录用到的接口常量。'''
from __future__ import annotations

# 与 y.qq.com 网页端登录弹窗使用同一套 QQ 互联应用参数
APPID = '716027609'
DAID = '383'
PT_3RD_AID = '100497308'

# 登录框（用于下发 pt_login_sig）
XLOGIN_URL = 'https://xui.ptlogin2.qq.com/cgi-bin/xlogin'
# 二维码图片
QRCODE_URL = 'https://ssl.ptlogin2.qq.com/ptqrshow'
# 二维码状态轮询
QRCODE_POLL_URL = 'https://ssl.ptlogin2.qq.com/ptqrlogin'
# 换取 p_skey
CHECK_SIG_URL = 'https://ssl.ptlogin2.graph.qq.com/check_sig'
# 换取 code
OAUTH_AUTHORIZE_URL = 'https://graph.qq.com/oauth2.0/authorize'
# 换取 musickey（QQ 音乐 CGI）
MUSICU_ENDPOINT = 'https://u.y.qq.com/cgi-bin/musicu.fcg'

OAUTH_REDIRECT_URI = 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/'
OAUTH_LOGIN_JUMP = 'https://graph.qq.com/oauth2.0/login_jump'

PTLOGIN_REFERER = 'https://xui.ptlogin2.qq.com/'
QQMUSIC_REFERER = 'https://y.qq.com/'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'
)

# 登录态探测：查询"我是谁"，未登录 / key 失效时返回错误码
USERINFO_MODULE = 'music.UserInfo.userInfoServer'
USERINFO_METHOD = 'GetLoginUserInfo'
# 用 code 换 key
LOGIN_MODULE = 'QQConnectLogin.LoginServer'
LOGIN_METHOD = 'QQLogin'

'''二维码轮询状态'''
STATUS_WAITING = 'waiting'      # 尚未扫码
STATUS_SCANNED = 'scanned'      # 已扫码，等待手机端确认
STATUS_CONFIRMED = 'confirmed'  # 确认登录
STATUS_EXPIRED = 'expired'      # 二维码确实失效，需要换一张
STATUS_REFUSED = 'refused'      # 取消或拒绝授权

# 状态判定以 QQ 返回的**文案**为准。
#
# 原因是数字状态码会漂移。2026-09-20 实测一次完整扫码的真实序列是：
#     code=66  二维码未失效。  -> 尚未扫码
#     code=67  二维码认证中。  -> 已扫码，等待手机端确认
#     code=0   登录成功！      -> 确认登录
# 可以看到 66 在"未扫码"时就出现了，而"已扫码"用的是 67
# —— 早期资料里 65/66/67/68 那套映射（67=已失效）已经完全对不上，
# 照它写会把用户刚扫的二维码当成失效立刻换新，导致登录永远走不完。
# 因此这里只用文案判定，数字码仅用于打日志。
EXPIRED_KEYWORDS = ('已失效', '过期', '已作废')
SCANNED_KEYWORDS = ('已扫描', '认证中', '待确认', '请确认')
REFUSED_KEYWORDS = ('已取消', '取消授权', '拒绝授权', '拒绝登录')

# 兜底展示文案，正常情况下用 QQ 返回的原文
STATUS_TEXT = {
    STATUS_WAITING: '等待扫码',
    STATUS_SCANNED: '已扫码，请在手机上确认登录',
    STATUS_CONFIRMED: '登录成功',
    STATUS_EXPIRED: '二维码已失效，正在重新生成',
    STATUS_REFUSED: '已取消授权',
}
