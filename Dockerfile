# syntax=docker/dockerfile:1
#
# AgentLab 生产镜像：后端一并托管前端构建产物，单进程。
#
#   docker buildx build --platform linux/arm64 --load -t agentlab:dev .
#
# 打离线包（镜像 + 一键部署脚本）用 scripts/package-docker.sh。
# 密钥和开发库进不来：.dockerignore 排除了 .env、data/、*.db，下面也只 COPY 用得到的目录。

# ---- 前端：只产出 JS / CSS，和目标架构无关，所以固定在构建机的架构上跑，跨架构构建时不用模拟 ----
FROM --platform=$BUILDPLATFORM node:22-slim AS web
# 和仓库开发时用的 pnpm 同一个大版本，锁文件格式对得上
ARG PNPM_VERSION=12.3.4
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=true
RUN corepack enable pnpm && corepack prepare "pnpm@${PNPM_VERSION}" --activate
WORKDIR /src/frontend
# 先只拷依赖清单：源码改了，依赖这一层还能用缓存
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- 运行 ----
FROM python:3.12-slim

# bubblewrap：代码沙箱。默认容器权限下它建不了命名空间，服务端会自动退回本地子进程，
# 给了额外权限才用得上（见 deploy/agentlab.env 的沙箱一节）。健康检查用 python 自己探，不装 curl
RUN apt-get update \
 && apt-get install -y --no-install-recommends bubblewrap \
 && rm -rf /var/lib/apt/lists/*

# 依赖装进独立的 venv：系统自带的 /usr/local/bin/python3 因此保持干净，
# 沙箱跑用户代码时用的就是它，看不见 fastapi、cryptography 这些服务端的包
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY backend/requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
 && pip install --no-cache-dir -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

# 固定的 uid / gid：部署脚本按它把宿主机上的数据目录改成容器能写
RUN groupadd --gid 10001 agentlab \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/agentlab --shell /usr/sbin/nologin agentlab \
 && mkdir -p /data \
 && chown 10001:10001 /data

WORKDIR /app
COPY LICENSE COMMERCIAL-LICENSE.md /app/
COPY backend/app /app/backend/app
COPY --from=web /src/frontend/dist /app/web

ENV AGENTLAB_WEB_DIST=/app/web \
    AGENTLAB_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
USER 10001:10001
WORKDIR /app/backend

# 探 /api/health，并确认答话的是 agentlab。绕开代理：agentlab.env 里配了 HTTP(S)_PROXY 时，
# urllib 默认会连 127.0.0.1 也往代理上送
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request as u; r = u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/api/health', timeout=4); sys.exit(0 if b'agentlab' in r.read() else 1)"]

# 版本号放在最后：它每次打包都不同，放前面会让装依赖那几层的缓存全部失效
ARG VERSION=dev
LABEL org.opencontainers.image.title="AgentLab" \
      org.opencontainers.image.description="可视化 Agent 编排实验台" \
      org.opencontainers.image.version="${VERSION}"

# 单进程：运行管理器在进程内、数据库是 SQLite，不能开多个 worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
