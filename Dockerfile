# DocMind DZ 运行镜像：Python 3.12 + LibreOffice（Word/Excel 转 PDF）+ uv 依赖
#
# 构建：docker compose build
# 运行：docker compose up -d
#
# uv.lock 里记录的索引是 pypi.org（官方源）。国内服务器拉依赖慢时显式换源：
#   docker compose build \
#     --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#     --build-arg APT_MIRROR=mirrors.aliyun.com

# -------------------------------
# 1) 依赖阶段：用 uv 生成独立 venv
# -------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 覆盖默认索引源（可选，仅在显式传 build-arg 时生效）
ARG UV_INDEX_URL

# 只先拷依赖清单：这两个文件不变时，下面的 uv sync 层直接命中构建缓存
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    sh -c 'if [ -n "$UV_INDEX_URL" ]; then export UV_INDEX_URL; fi; uv sync --frozen --no-dev'

# -------------------------------
# 2) 运行阶段
# -------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

# DOCMIND_DZ_APP_HOST 必须是 0.0.0.0：默认 127.0.0.1 只在容器内可达，
# 网关容器（compose 里的 gateway）会连不上。
# HOME 给 soffice 用（每次转换会写 $HOME/.config/libreoffice，必须是可写目录）。
# LANG/LANGUAGE 决定 LibreOffice 的 LC_PAPER：默认 C.UTF-8 时它按 Letter
# （8.5×11in）分页，比 A4 短 18mm，同一张表更容易溢到下一页，
# 表现为「线上页数比开发机多」。下面 apt 阶段会 locale-gen 出这个 locale。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:$PATH" \
    DOCMIND_DZ_APP_HOST=0.0.0.0 \
    DOCMIND_DZ_APP_PORT=8010 \
    DOCMIND_DZ_DATA_ROOT=/app/data \
    DOCMIND_DZ_LOG_DIR=/app/logs \
    HOME=/home/docmind \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh

# Debian 官方源在国内服务器可能很慢：构建时传 --build-arg APT_MIRROR=主机名 替换。
#
# LibreOffice：Writer 负责 DOC/DOCX、Calc 负责 XLS/XLSX，
#   app/collector/parsers.py 通过 shutil.which("soffice") 定位并以
#   `soffice --convert-to pdf` 转 PDF 后再走 PDF 链路（有页图、文本层一致）。
#   未安装时不会报错，而是退化为 python-docx / openpyxl 纯文本解析——
#   任务能跑，但没有页面预览图，线上表现与开发机不一致。
# 中文字体：转 PDF 与页面光栅化都依赖系统中文字体，缺失会渲染成方块。
ARG APT_MIRROR=""
RUN set -eu; \
    if [ -n "$APT_MIRROR" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then \
                sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" "$f"; \
            fi; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        fonts-wqy-zenhei \
        fonts-wqy-microhei \
        fonts-arphic-uming \
        fonts-arphic-ukai \
        locales \
        ca-certificates \
        tzdata; \
    sed -i 's/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen; \
    locale-gen; \
    rm -rf /var/lib/apt/lists/*

# 单证里普遍指定「宋体 / 仿宋 / 微软雅黑」等 Windows 字体，镜像内没有时 fontconfig
# 会退化到 DejaVu Sans 排版：拉丁度量比宋体宽，同样内容换行变多、纵向溢出，
# 线上页数因此比开发机多。这份别名把常见中文字体名映射到已安装的开源字体，
# 保证分页口径一致（详见文件内注释）。
COPY deploy/fontconfig/cjk-alias.conf /etc/fonts/conf.d/99-cjk-alias.conf
RUN fc-cache -f

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# 非 root 运行：应用只写 /app/data 与 /app/logs。
# 这两个目录在 compose 里是绑定挂载（直接用宿主属主，不像命名卷会复制属主），
# 所以宿主上必须先建好并 chown 10001:10001，见 DEPLOY.md。
RUN groupadd --gid 10001 docmind \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin docmind \
    && mkdir -p /app/data /app/logs \
    && chown -R docmind:docmind /app

COPY --chown=docmind:docmind pyproject.toml uv.lock ./
COPY --chown=docmind:docmind main.py ./
COPY --chown=docmind:docmind app ./app

USER docmind

EXPOSE 8010

# 探活走应用自带的 /health（会回报模型是否就绪）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('DOCMIND_DZ_APP_PORT', '8010') + '/health', timeout=4)"

# uvicorn 只认 SIGTERM 才走优雅退出（lifespan 的 finally 会打印停止信息）
STOPSIGNAL SIGTERM

# 单进程 uvicorn：任务调度与并发闸门都在进程内维护，
# 多 worker / 多副本会互相把对方的任务误判为中断（见 DEPLOY.md「日常运维」）。
CMD ["python", "main.py"]
