# musicbot —— 单进程企业微信音乐机器人
#
# 只复制源码，不带任何凭据：凭据全部通过环境变量注入（见 docker-compose.yml / .env.example）。
# `.dockerignore` 已经把 .env、data/、downloads/、**/__pycache__ 等排除在构建上下文之外。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai

# tzdata 让日志时间跟随 TZ；confd 之类不需要，保持镜像小
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层，改代码不用重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ 放 Cookie 与状态（务必用卷映射出去，否则重建容器会丢登录态）
RUN mkdir -p /app/data/cookies /app/downloads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["python", "main.py"]
