"""配置：全部来自环境变量（可选 `.env`）。

设计上**刻意不做 `config/config.py` 那样的双份配置**（旧项目吃过亏：
本地文件和容器环境变量两套值，容易一边改了另一边没改，还容易被误打进镜像）。
凭据只从环境变量来，仓库里永远没有真值。

⚠️ 顺序很重要：`main.py` 必须**先** `settings.validate()` 通过，**再** import 那些
会在导入期读取配置的模块（旧项目踩过：解码器在导入时就构造，配置为空直接抛异常，
自检写在后面等于永远不执行）。
"""

from __future__ import annotations

import os
from typing import Optional

# 企微 API 官方地址；到不了官方时可用自建反代（结尾不带 /）
DEFAULT_WECHAT_PROXY = 'https://qyapi.weixin.qq.com'
# 装包源：客户环境到不了 pypi.org，默认走清华镜像（S7 的在线更新用）
DEFAULT_PIP_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
# 搜索条数上限：QQ 源实测 =100 会返回 0 条且不报错，50 是安全上限
SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX = 1, 50

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    """加载项目根目录的 `.env`（已存在的环境变量优先，不被覆盖）。"""
    path = os.path.join(PROJECT_ROOT, '.env')
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
    except ImportError:
        # 没装 python-dotenv 也不该让服务起不来，只是 .env 不生效
        pass


def _env_str(key: str, default: str = '') -> str:
    value = os.environ.get(key)
    return default if value is None else value.strip()


def _env_int(key: str, default: int, *, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> int:
    raw = _env_str(key)
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


class Settings:
    """运行期配置。字段名与旧项目保持一致，现有 `.env` 可直接复用。"""

    def __init__(self) -> None:
        _load_dotenv()
        self.reload()

    def reload(self) -> None:
        # --- 企业微信应用凭据（必填）---
        self.stoken = _env_str('STOKEN')
        self.encoding_aes_key = _env_str('S_ENCODING_AES_KEY')
        self.corp_id = _env_str('S_CORP_ID')
        self.agent_id_raw = _env_str('AGENT_ID')
        self.agent_secret = _env_str('SECRET')

        # --- 网络 ---
        # 企微 API 入口。能直连官方就用官方；本机到不了官方时填自建反代。
        self.wechat_proxy = _env_str('WECHAT_PROXY', DEFAULT_WECHAT_PROXY).rstrip('/')
        # 对外可访问的地址（反代后的域名），用于拼接扫码登录页链接；留空则消息里不显示
        self.public_base_url = _env_str('MUSICBOT_PUBLIC_BASE_URL').rstrip('/')
        self.host = _env_str('MUSICBOT_HOST', '0.0.0.0')
        self.port = _env_int('MUSICBOT_PORT', 8000, minimum=1, maximum=65535)

        # --- 业务 ---
        self.default_source = _env_str('MUSICBOT_DEFAULT_SOURCE', 'qq').lower()
        self.search_limit = _env_int('MUSICBOT_SEARCH_LIMIT', 8,
                                     minimum=SEARCH_LIMIT_MIN, maximum=SEARCH_LIMIT_MAX)
        self.result_cache_minutes = _env_int('MUSICBOT_RESULT_CACHE_MINUTES', 5, minimum=1)
        self.cookie_check_interval_minutes = _env_int(
            'MUSICBOT_COOKIE_CHECK_INTERVAL_MINUTES', 30, minimum=1)
        self.cookie_expired_years_fallback = _env_int(
            'MUSICBOT_COOKIE_TTL_HOURS', 48, minimum=1)   # 服务端不给 TTL 时的兜底

        # --- 路径与运维 ---
        self.data_dir = _env_str('MUSICBOT_DATA_DIR', os.path.join(PROJECT_ROOT, 'data'))
        self.download_dir = _env_str('MUSICBOT_DOWNLOAD_DIR', os.path.join(PROJECT_ROOT, 'downloads'))
        self.log_level = _env_str('MUSICBOT_LOG_LEVEL', 'INFO').upper()
        self.pip_index_url = _env_str('PIP_INDEX_URL', DEFAULT_PIP_INDEX)
        self.image_enabled = _env_str('MUSICBOT_IMAGE_ENABLED', 'true').lower() not in ('0', 'false', 'no')
        # 启动时那两条广播（使用教程 + Cookie 体检）。留在这里是因为
        # 客户容器会被 watchtower 反复拉起，而每次拉起都会广播一轮；
        # 想安静更新的部署可以把它关掉，Cookie 失效时仍会单独告警。
        self.startup_broadcast = (_env_str('MUSICBOT_STARTUP_BROADCAST', 'true')
                                  .lower() not in ('0', 'false', 'no'))

    # ------------------------------------------------------------------
    @property
    def agent_id(self) -> int:
        """企微 agentid 要整数，填错时返回 0（自检会报）。"""
        try:
            return int(self.agent_id_raw)
        except (TypeError, ValueError):
            return 0

    def missing_items(self) -> list[str]:
        """自检：返回缺失/非法的配置项说明（空列表 = 配置完整）。"""
        problems: list[str] = []
        if not self.stoken:
            problems.append('STOKEN                    回调校验 Token（企微后台「接收消息」处设置）')
        if not self.encoding_aes_key:
            problems.append('S_ENCODING_AES_KEY        回调加密 EncodingAESKey（43 位）')
        elif len(self.encoding_aes_key) != 43:
            problems.append(
                f'S_ENCODING_AES_KEY        长度应为 43，当前 {len(self.encoding_aes_key)}')
        if not self.corp_id:
            problems.append('S_CORP_ID                 企业 ID')
        if not self.agent_id_raw:
            problems.append('AGENT_ID                  应用 AgentId')
        elif self.agent_id <= 0:
            problems.append(f'AGENT_ID                  应为正整数，当前 {self.agent_id_raw!r}')
        if not self.agent_secret:
            problems.append('SECRET                    应用 Secret')
        return problems

    def summary_lines(self) -> list[str]:
        """给状态页/日志用的摘要（**不含任何凭据明文**）。"""
        return [
            f'企微接入      : {"已配置" if not self.missing_items() else "配置不全"}'
            f'（corp_id={_mask(self.corp_id)}，agent_id={self.agent_id or "-"}）',
            f'企微 API 入口 : {self.wechat_proxy}',
            f'对外地址      : {self.public_base_url or "（未配置，消息里不显示扫码链接）"}',
            f'监听          : {self.host}:{self.port}',
            f'默认音乐源    : {self.default_source}',
            f'搜索条数      : {self.search_limit}（{SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX}）',
            f'结果缓存      : {self.result_cache_minutes} 分钟',
            f'Cookie 巡检   : 每 {self.cookie_check_interval_minutes} 分钟',
            f'数据目录      : {self.data_dir}',
            f'下载目录      : {self.download_dir}',
        ]


def _mask(value: str) -> str:
    """只显示前后各 4 位。"""
    if not value:
        return '-'
    if len(value) <= 10:
        return value[:2] + '***'
    return f'{value[:4]}***{value[-4:]}'


# --- 运行期可改的设置（/limit 这类）-----------------------------------------
# 用户改过之后要持久化，否则重启就回到默认值。

def runtime_path() -> str:
    return os.path.join(settings.data_dir, 'settings.json')


def load_runtime_overrides() -> dict:
    """把 data/settings.json 里的覆盖项应用到当前 settings。"""
    import json
    try:
        with open(runtime_path(), encoding='utf-8') as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if 'search_limit' in data:
        try:
            value = int(data['search_limit'])
            settings.search_limit = max(SEARCH_LIMIT_MIN, min(value, SEARCH_LIMIT_MAX))
        except (TypeError, ValueError):
            pass
    return data


def save_runtime_override(key: str, value) -> None:
    """写入一个覆盖项（读-改-写，保留其他键）。"""
    import json
    path = runtime_path()
    data: dict = {}
    try:
        with open(path, encoding='utf-8') as fp:
            loaded = json.load(fp)
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError):
        pass
    data[key] = value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


settings_loaded_from_env = True


settings = Settings()
