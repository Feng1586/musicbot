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

IMAGE = "66211900/wecom-musicbot:1.0.0"
CONTAINER = "wb-musicbot-check"
PORT = 18002
EXPECT_DIGEST = "sha256:00ea1ebaaa314fb43c6ca92335ff26a9733c50f5afd717e4ae920061e346ab43"
# 这些是真正的凭据值，镜像里绝不能出现。
# 注意不要放「字段名」（如 MUSIC_U）—— 那是代码里本来就有的键名，不是凭据。
SENSITIVE = ["qHiJCbw4", "zSaPfhO6", "1CPKUYR6", "wwd52b9b68a7d192fe",
             "zd.i-am-a.gay", "27417187", "秋枫"]

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
        check("远端已有 1.0.0 与 latest", "1.0.0" in names and "latest" in names,
              list(names))
        if "1.0.0" in names:
            t = names["1.0.0"]
            archs = sorted({i["architecture"] + "/" + i["os"] for i in t.get("images", [])})
            check("双架构", set(archs) == {"amd64/linux", "arm64/linux"}, archs)
            check("digest 与推送一致", t.get("digest", "").startswith("sha256:00ea1eba"),
                  t.get("digest", ""))
    except Exception as e:
        check("查询 Docker Hub", False, repr(e))

    print()
    print("2. 拉回镜像")
    r = run([DOCKER, "pull", IMAGE], timeout=900)
    check("docker pull 成功", r.returncode == 0, (r.stderr or r.stdout)[-120:])

    print()
    print("3. 镜像内容")
    check("版本号 = 1.0.0",
          inside("python -c 'from utils.version import __version__;print(__version__)'")
          == "1.0.0")
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
    for token in SENSITIVE:
        found = inside(f"grep -rl '{token}' /app 2>/dev/null | wc -l")
        check(f"不含字面量 {token}", found == "0", found)

    print()
    print("5. 以「客户 compose 的写法」起容器（6 个环境变量 + 卷映射）")
    env = load_env()
    run([DOCKER, "rm", "-f", CONTAINER])
    host_dl = os.path.join(ROOT, ".wb_s8_downloads")
    host_data = os.path.join(ROOT, ".wb_s8_data")
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
          "musicbot v1.0.0 启动中" in log_text and "引擎与下载队列就绪" in log_text,
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
