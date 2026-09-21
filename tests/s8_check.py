"""S8 验收：拉回刚发布的镜像，独立校验内容与运行状态。

重点：
* 镜像里**不能有** .env / data/ / Cookie / 任何凭据字面量
* 代码是新的（版本号、关键模块都在）
* 依赖版本正确（cryptography 卡在 <47）
* 以「老用户 compose 的写法」起容器：只给 6 个环境变量 + 卷映射，能否健康运行

用法：python tests/s8_check.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKER = r"C:\Users\27417\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
os.environ["PATH"] = os.path.dirname(DOCKER) + os.pathsep + os.environ.get("PATH", "")
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import read_version                           # noqa: E402

BOT_VERSION = read_version()

IMAGE = f"66211900/wecom-musicbot:{BOT_VERSION}"
CONTAINER = "wb-musicbot-check"
PORT = 18002
# 期望的 manifest digest。每次发布都会变，所以从环境变量读；不传就跳过这一项：
#   WB_EXPECT_DIGEST=sha256:xxxx python tests/s8_check.py
EXPECT_DIGEST = os.environ.get("WB_EXPECT_DIGEST", "")


def sensitive_tokens() -> list[str]:
    """要从镜像里确认「不存在」的凭据片段。

    ⚠️ **这些值绝不能硬编码在源码里。** v1.0.1 那版就是把它们写死在
    一个 `SENSITIVE = [...]` 列表里，而 `tests/` 又被 `COPY . .` 带进了镜像，
    于是校验脚本自己命中了这些字面量 —— **检查清单本身成了泄露源**。

    现在改成运行时从 `.env` 现取（`.env` 本来就被 `.dockerignore` 排除），
    源码里只留「取哪几个字段、取多少位」。顺带说明：
    别把「字段名」放进来（如前面那个 MUSIC_U）—— 那是代码里本来就有的键名。
    """
    env = load_env()
    tokens: list[str] = []
    # 三个凭据各取前 8 位：够长到不会误命中，又不需要把整个密钥搬进来
    for key in ("STOKEN", "S_ENCODING_AES_KEY", "SECRET"):
        value = env.get(key, "")
        if len(value) >= 8:
            tokens.append(value[:8])
    if env.get("S_CORP_ID"):
        tokens.append(env["S_CORP_ID"])
    # 反代主机名 / QQ 账号也从运行时的值里取，不写死
    match = re.match(r"https?://([^/:]+)", env.get("WECHAT_PROXY", ""))
    if match:
        tokens.append(match.group(1))
    cookie_file = os.path.join(ROOT, "data", "cookies", "qq.json")
    try:
        with open(cookie_file, encoding="utf-8") as fp:
            account = str(json.load(fp).get("account") or "").strip()
        if account:
            tokens.append(account)
    except (OSError, ValueError):
        pass
    return tokens

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))


def run(args, timeout=600):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def inside(script: str) -> str:
    r = run([DOCKER, "run", "--rm", "--entrypoint", "sh", IMAGE, "-c", script])
    return ((r.stdout or "") + (r.stderr or "")).strip()


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def main() -> int:
    print("=" * 72)
    print("S8 验收：发布镜像校验")
    print("=" * 72)

    print()
    print("1. 远端 manifest")
    url = "https://hub.docker.com/v2/repositories/66211900/wecom-musicbot/tags/?page_size=5"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wb"})
        with urllib.request.urlopen(req, timeout=30) as r:
            tags = json.loads(r.read().decode())
        names = {t["name"]: t for t in tags["results"]}
        check(f"远端已有 {BOT_VERSION} 与 latest",
              BOT_VERSION in names and "latest" in names, list(names))
        if BOT_VERSION in names:
            t = names[BOT_VERSION]
            archs = sorted({i["architecture"] + "/" + i["os"] for i in t.get("images", [])})
            check("双架构", set(archs) == {"amd64/linux", "arm64/linux"}, archs)
            if EXPECT_DIGEST:
                check("digest 与推送记录一致",
                      t.get("digest", "").startswith(EXPECT_DIGEST[:26]),
                      t.get("digest", ""))
            else:
                check("远端 digest（未传 WB_EXPECT_DIGEST，仅记录）", True,
                      t.get("digest", "")[:28])
    except Exception as e:
        check("查询 Docker Hub", False, repr(e))

    print()
    print("2. 拉回镜像")
    r = run([DOCKER, "pull", IMAGE], timeout=900)
    check("docker pull 成功", r.returncode == 0, (r.stderr or r.stdout)[-120:])

    print()
    print("3. 镜像内容")
    check(f"版本号 = {BOT_VERSION}",
          inside("python -c 'from utils.version import __version__;print(__version__)'")
          == BOT_VERSION)
    for path, name in (("app/crypto.py", "回调加解密"),
                       ("app/sources.py", "引擎"),
                       ("app/cookies.py", "Cookie 管理"),
                       ("app/login/manager.py", "登录编排"),
                       ("app/login/netease.py", "网易云扫码"),
                       ("app/login/qq_login.py", "QQ 扫码"),
                       ("app/router/login_page.py", "扫码页"),
                       ("app/updater.py", "版本更新"),
                       ("app/runtime.py", "重启"),
                       ("app/downloader.py", "下载队列")):
        check(f"{name}（{path}）在镜像内", os.path.basename(path) in inside(f"ls {path}"))
    check("依赖：cryptography <47", inside(
        "python -c 'import importlib.metadata as m;print(m.version(\"cryptography\"))'").startswith("46."),
        inside("python -c 'import importlib.metadata as m;print(m.version(\"cryptography\"))'"))
    check("依赖：musicdl 2.13.11", inside(
        "python -c 'import importlib.metadata as m;print(m.version(\"musicdl\"))'") == "2.13.11")
    check("无 __pycache__ / .pyc",
          inside("find /app -name '__pycache__' -o -name '*.pyc' | wc -l") == "0")

    print()
    print("4. 敏感内容（最关键）")
    check("镜像内没有 .env", inside("test -f /app/.env && echo YES || echo NO") == "NO")
    # data/cookies 会作为**空目录**存在（Dockerfile 里 mkdir，方便没挂卷时也能跑），
    # 所以这里要数「文件」而不是「目录项」。
    check("镜像内 data/ 下没有任何文件",
          inside("find /app/data -type f 2>/dev/null | wc -l") == "0",
          inside("find /app/data -type f 2>/dev/null | head -3"))
    check("镜像内没有 cookies/*.json",
          inside("ls /app/data/cookies/*.json 2>/dev/null | wc -l") == "0")
    # 注意：检查项的名字里**不能出现完整凭据** —— 测试日志本身也会被留下/被读到。
    # 这里只显示前三位的打码形式，够定位是哪个字段即可。
    tokens = sensitive_tokens()
    print(f"  （本次检查 {len(tokens)} 个凭据片段，日志中一律打码）")
    for token in tokens:
        found = inside(f"grep -rl '{token}' /app 2>/dev/null | wc -l")
        check(f"不含凭据片段 {token[:3]}***", found == "0", found)

    print()
    print("5. 以「客户 compose 的写法」起容器（6 个环境变量 + 卷映射）")
    env = load_env()
    run([DOCKER, "rm", "-f", CONTAINER])
    # ⚠️ 临时映射目录必须放在**项目目录之外**。
    # 放在 ROOT 里会被 `COPY . .` 带进镜像 —— v1.0.2 实际发生过：
    # 构建出的镜像里出现了 /app/.wb_s8_data/cookie_state.json。
    # （.dockerignore 里也补了 `.wb_*` 兜底，但根因是别把临时物建在构建上下文里。）
    outside = os.path.dirname(ROOT)
    host_dl = os.path.join(outside, ".wb_s8_downloads")
    host_data = os.path.join(outside, ".wb_s8_data")
    os.makedirs(host_dl, exist_ok=True)
    os.makedirs(host_data, exist_ok=True)
    r = run([DOCKER, "run", "-d", "--name", CONTAINER,
             "-p", f"{PORT}:8000",
             "-e", f"STOKEN={env['STOKEN']}",
             "-e", f"S_ENCODING_AES_KEY={env['S_ENCODING_AES_KEY']}",
             "-e", f"S_CORP_ID={env['S_CORP_ID']}",
             "-e", f"AGENT_ID={env['AGENT_ID']}",
             "-e", f"SECRET={env['SECRET']}",
             "-e", "WECHAT_PROXY=http://127.0.0.1:1/",   # 指向黑洞，避免真发消息
             "-v", f"{host_dl}:/app/downloads",
             "-v", f"{host_data}:/app/data",
             IMAGE])
    check("容器启动", r.returncode == 0, (r.stderr or "")[-120:])

    health = None
    for _ in range(30):
        time.sleep(3)
        health = run([DOCKER, "inspect", "--format", "{{.State.Health.Status}}",
                      CONTAINER]).stdout.strip()
        if health in ("healthy", "unhealthy"):
            break
    check("容器 healthy", health == "healthy", health)

    logs = run([DOCKER, "logs", CONTAINER])
    log_text = (logs.stdout or "") + (logs.stderr or "")
    check("日志显示版本与配置检查通过",
          f"musicbot v{BOT_VERSION} 启动中" in log_text and "引擎与下载队列就绪" in log_text,
          [l for l in log_text.splitlines() if "启动中" in l][:1])
    check("日志无 Traceback", "Traceback" not in log_text)

    print()
    print("6. 容器内接口")
    for path, name in (("/health", "健康检查"), ("/", "状态页"),
                       ("/login/qq", "扫码页"), ("/login/wyy/state", "登录状态接口")):
        r = run([DOCKER, "exec", CONTAINER, "python", "-c",
                 f"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000{path}',timeout=8).status)"])
        check(f"{name} {path} 可用", r.stdout.strip() == "200", r.stdout.strip() or r.stderr[-80:])

    print()
    print("7. 数据目录可写（Cookie 持久化依赖它）")
    r = run([DOCKER, "exec", CONTAINER, "sh", "-c",
             "touch /app/data/.write_test && ls /app/data/.write_test"])
    check("容器内 /app/data 可写", r.returncode == 0, r.stdout.strip())
    check("写了的东西出现在宿主机映射目录",
          os.path.exists(os.path.join(host_data, ".write_test")))

    run([DOCKER, "rm", "-f", CONTAINER])

    print()
    print("=" * 72)
    ok = sum(1 for _n, o, _d in results if o)
    print(f"结果：{ok}/{len(results)} 通过")
    for n, o, d in results:
        if not o:
            print(f"   FAIL -> {n}  {d}")
    print("=" * 72)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
