# musicbot

企业微信音乐机器人 —— **单进程、纯本地**：不依赖任何云端后端，不需要机器码，
搜索与下载全部在本机用 [`musicdl`](https://github.com/CharlesPikachu/musicdl) 完成。

## 特性

- **本地搜索下载**：直接在容器里调 `musicdl`，没有中间服务
- **双源可切换**：QQ音乐 / 网易云音乐，用户发送 `/qq`、`/wyy` 切换（按人记，互不干扰）
- **歌词 / 封面自动写入**：由 musicdl 内置完成 —— 同名 `.lrc` + 内嵌歌词 + 标题/专辑/歌手 + 内嵌封面
- **后台下载队列**：入队即回执，可以连续发歌名，队列一首首下
- **盲下载模式**：`/blind on` 后发送歌名直接下载第一条结果
- **Cookie 自助维护**：`/qq login`、`/wyy login` 扫码登录；过期自动广播提醒（**同一次过期只提醒一次**）
- **可调搜索条数**：`/limit 8`（1-50）
- **在线更新 musicdl**：启动查新给出提示；`/update` 会先说明风险；更新失败会自动回滚

## 快速开始（Docker）

```bash
cp .env.example .env      # 填入企业微信应用凭据
docker compose up -d
```

企业微信后台需要配置：

| 项 | 值 |
|---|---|
| 接收消息 URL | `https://你的域名/wechat/callback` |
| Token | 与 `.env` 的 `STOKEN` 一致 |
| EncodingAESKey | 与 `.env` 的 `S_ENCODING_AES_KEY` 一致 |
| 企业ID / AgentId / Secret | 与 `.env` 对应 |

`MUSICBOT_PUBLIC_BASE_URL` 填你反代后的域名，扫码登录消息里就会附带
`https://你的域名/login/qq` 这样的网页链接。

## 快速开始（本机）

```powershell
.\run.ps1           # 启动（推荐 PowerShell 7）
.\run.ps1 -Check    # 只做配置自检
run.bat             # 双击启动
```

## 指令一览

| 指令 | 说明 |
|---|---|
| 直接发歌名 | 搜索，返回编号列表（默认 8 条） |
| `1` / `1,3,5` | 下载对应编号（编号 5 分钟内有效） |
| `/qq` `/wyy` | 切换音乐源 |
| `/source` | 查看当前源 |
| `/blind on\|off` | 盲下载模式开关 |
| `/limit [N]` | 查看 / 设置搜索条数（1-50，全局生效） |
| `/qq login` `/wyy login` | 扫码登录或更新 Cookie |
| `/queue` `/cancel` | 查看队列 / 清空待下载 |
| `/status` | 运行状态（Cookie、队列、引擎、版本） |
| `/update [confirm\|rollback]` | 检查 / 执行 / 回滚 musicdl 更新 |
| `/restart` | 重启服务（见下） |
| `/help` `/version` | 帮助 / 版本 |

## 配置项

见 `.env.example`。只有 5 个企业微信凭据是必填的，缺任何一个启动时会打印缺项清单并退出。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MUSICBOT_PUBLIC_BASE_URL` | 空 | 对外地址（反代域名）。配了才显示扫码链接 |
| `MUSICBOT_DEFAULT_SOURCE` | `qq` | 默认音乐源 |
| `MUSICBOT_SEARCH_LIMIT` | `8` | 搜索条数，1-50（50 是实测安全上限） |
| `MUSICBOT_RESULT_CACHE_MINUTES` | `5` | 搜索结果（编号）有效期 |
| `MUSICBOT_COOKIE_CHECK_INTERVAL_MINUTES` | `30` | Cookie 巡检间隔 |
| `MUSICBOT_IMAGE_ENABLED` | `true` | 是否用图片消息发二维码；关闭则只发文本+链接 |

## 说明与注意事项

**数据持久化**：`./data` 目录存 Cookie 与状态，**务必用卷映射出去**，否则重建容器就要重新扫码登录。
`./downloads` 是下载的音乐，按 `源/时间戳 关键词/歌名 - 歌手.ext` 分目录。

**`/restart` 的两级降级**：默认**不要求**你映射 `docker.sock`。
- 映射了 → 通过 Docker API 重启容器
- 没映射 → 优雅退出进程，靠容器的 `restart` 策略（`unless-stopped` / `always`）拉起

**在线更新只对当前容器有效**：容器内 `pip install` 改的是可写层，
容器一旦被重建就会回到镜像里固化的版本。更新前会记录当前版本，
若更新后起不来，下次启动会**自动装回旧版本**（`/update rollback` 也可手动回滚）。

**搜索条数是全局的**：所有源共用一个值。改条数会重建引擎（musicdl 把条数固化在客户端实例里，
不重建不生效）。同理，更新 Cookie 后也会重建引擎。

**为什么不自己写歌词/封面的处理**：musicdl 的 `client.download()` 已经内置了
「写 .lrc + 内嵌歌词 + 基础标签 + 封面」，而且是带备份回滚的安全写入，直接用它。

## 开发

```bash
python tests/s1_check.py    # 回调加解密（与 weworkapi 的 WXBizMsgCrypt 交叉验证）
python tests/s2_check.py    # 真实搜索下载 + 歌词封面落盘
python tests/s3_check.py    # 队列 / 三条消息 / 盲下载 / limit
python tests/s4_check.py    # Cookie 告警去重 / 扫码 / 发图片 / 更新与回滚
python tests/e2e_check.py   # 端到端：加密回调 → 搜索 → 下载（mock 企微，不发真实消息）
```

## License

与 musicdl 一致：PolyForm Noncommercial 1.0.0（非商业使用）。
