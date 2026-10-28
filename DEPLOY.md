# DocMind DZ 部署流程

面向 Linux 服务器的 Docker 部署手册。按本文顺序执行即可完成上线。

---

## 一、架构概览

```
浏览器 / 业务系统（服务端或官网）
   │  https://ai.wecanintl.com:8444/docmind_dz/analysis/**
   ▼
docmind-dz-gateway  (nginx:alpine)     ← 本项目自带，占用宿主 8444
   │  http://docmind-dz:8010  （compose 内部网络，容器名互访）
   ▼
docmind-dz  (FastAPI + LibreOffice)    ← 数据落宿主 ./data 与 ./logs
```

**与同机其它项目（DocMind 8443 / 官网 / 客服）零交集**：不共用 nginx、不占用它们的
80 / 443 / 8443、不加入它们的 Docker 网络、也不需要新增 DNS 记录
（compose 项目名不同，网络天然隔离）。

| 项 | 值 |
|---|---|
| 项目目录 | `/data/apps/DocMind_DZ` |
| 对外地址 | `https://ai.wecanintl.com:8444` |
| 接口基址 | `https://ai.wecanintl.com:8444/docmind_dz` |
| 宿主端口 | `8444`（HTTPS 对外）、`8010`（仅绑回环，调试用） |
| 容器 | `docmind-dz`（应用）、`docmind-dz-gateway`（入口 nginx） |
| 证书 | 只读复用泛域名证书，不新签、续期自动生效 |
| 公网暴露面 | **仅 `/docmind_dz/**` 接口与 `/health`**；内置前端全体 404 |

**为什么用 8444 而不是 443**：宿主 80 / 443 已被同机其它站点占用，8443 归 DocMind。
DocMind DZ 自带入口容器并换用备用端口，从而完全不依赖、也不改动其它项目的配置。
若将来独立到新服务器，只需把端口映射改回 `443:443`，其余配置一字不动。

### 与 DocMind 的差异（照抄原版会踩的点）

| 项 | DocMind | DocMind DZ | 部署侧处理 |
|---|---|---|---|
| 接口前缀 | `/docmind` | `/docmind_dz` | nginx `location` 与接口基址跟着改 |
| 应用端口 | 8000 | 8010 | Dockerfile / compose / healthcheck 全部改 |
| 环境变量前缀 | `DOCMIND_` | `DOCMIND_DZ_` | Dockerfile `ENV` 全部改名 |
| 日志配置 | `DOCMIND_SYSTEM_LOG_FILE`（文件路径） | `DOCMIND_DZ_LOG_DIR`（**目录**） | 容器内 `/app/logs`，文件名固定 `app.log` |
| LibreOffice | 需要 `python3-uno`（XLS 行高修正脚本） | 只用 `soffice` 命令行转 PDF | 只装 `libreoffice-writer/calc`，不装 uno |
| 上传体积 | 单文件 50MB | 一次最多 5 个文件 × 50MB | `client_max_body_size` 从 50m 提到 **256m** |
| 前端 | 联调用，公网 404 | 同为联调用，公网 404 | 一致 |

> **上传体积这一条最容易漏**：只按单文件 50MB 配 `client_max_body_size 50m`，
> 一次传 3 个 20MB 的分单就会被 nginx 拦成 413，而应用侧日志里什么都没有。

---

## 二、前置条件

| 项 | 要求 |
|---|---|
| Docker | 24+ 与 Docker Compose v2（`docker compose version` 可查） |
| 服务器规格 | 建议 ≥ 4 核 8GB（**与 DocMind 同机时要和它分 CPU**，见第四节并发建议） |
| 端口 | 宿主 `8444` 空闲，并在阿里云安全组放行 |
| 证书 | 泛域名证书目录可读（默认只读挂载 `/data/docker/freight-agent/nginx/certs`，含 `fullchain.pem` + `privkey.key`） |
| 密钥 | 主模型 API Key（`DOCMIND_DZ_MODEL_API_KEY`） |

---

## 三、部署步骤

### 1. 获取代码

```bash
mkdir -p /data/apps && cd /data/apps
git clone <仓库地址> DocMind_DZ
cd /data/apps/DocMind_DZ
```

已存在则：

```bash
cd /data/apps/DocMind_DZ && git pull
```

### 2. 准备数据与日志目录

容器以 `uid/gid 10001` 运行，而这两个目录是**绑定挂载**（直接用宿主属主，不像命名卷会
自动复制），所以必须先建好并交给该用户：

```bash
mkdir -p data logs
chown -R 10001:10001 data logs
```

> `data/` 与 `logs/` 都在 `.gitignore` 里，`git clone` 后不存在，**必须手工创建**。
> 漏掉这一步的表现是容器起不来，`docker compose logs docmind-dz` 里是
> `PermissionError: [Errno 13] Permission denied: '/app/logs/app.log'`。
> `git pull` 不会覆盖这两个目录，所以 `chown` 是一次性的。

### 3. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

需要填写的项：

```bash
# ===== 必需 =====
DOCMIND_DZ_MODEL_API_KEY=sk-xxxxxx

# ===== 模型兜底（可选，强烈建议配）=====
# 主模型整体不可用时由它接管：换的是"另一家上游"，账号限流 / 欠费 / 宕机都还能出结果。
# 不填 API Key 则整条兜底链路不生效。
DOCMIND_DZ_FALLBACK_API_KEY=sk-xxxxxx
DOCMIND_DZ_FALLBACK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DOCMIND_DZ_FALLBACK_NAME=qwen-plus
DOCMIND_DZ_FALLBACK_VISION_NAME=qwen-vl-max

# ===== 跨域：填调用方来源，多个用逗号分隔 =====
# 调用方是**服务端**（curl / 后台任务）时留空即可，不涉及浏览器跨域。
# 若是官网等浏览器页面直接调本服务，必须填其真实来源（带协议与端口）：
#   DOCMIND_DZ_CORS_ALLOW_ORIGINS=https://ai.wecanintl.com
# 不要用 DOCMIND_DZ_CORS_ALLOW_ALL=true —— 接口目前无鉴权，放开来源等于放任任意站点读取响应。
DOCMIND_DZ_CORS_ALLOW_ORIGINS=

# ===== 对外端口（改动后访问地址要跟着带新端口）=====
DOCMIND_DZ_HTTPS_PORT=8444

# ===== 构建加速（可选）=====
# APT_MIRROR=mirrors.aliyun.com
# UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

**不要改的项**（compose 会在容器内覆盖，改了也不生效，容易误判）：

```bash
DOCMIND_DZ_APP_HOST=127.0.0.1    # 容器内固定覆盖为 0.0.0.0
DOCMIND_DZ_APP_PORT=8010         # 容器内固定
DOCMIND_DZ_DATA_ROOT=data        # 容器内固定为 /app/data
DOCMIND_DZ_LOG_DIR=logs          # 容器内固定为 /app/logs
```

### 4. 构建并启动

```bash
docker compose up -d --build
docker compose ps
```

预期：`docmind-dz` 为 `healthy`（首次约 40s 起），`docmind-dz-gateway` 为 `Up`。

```bash
docker compose logs docmind-dz | tail -n 20                  # 启动横幅与模型就绪情况
docker exec docmind-dz-gateway nginx -t                      # nginx 配置自检
docker compose exec docmind-dz tail -n 30 /app/logs/app.log  # 应用日志
```

启动横幅会直接打在容器 stdout 上，能看到访问地址、日志与数据目录：

```
DocMind DZ 已启动
  前端页面：http://127.0.0.1:8010/
  局域网访问：http://172.18.0.2:8010/
  接口文档：http://127.0.0.1:8010/docs
  健康检查：http://127.0.0.1:8010/health
  系统日志：/app/logs/app.log
  任务数据：/app/data/tasks
  （实时查看日志：tail -f /app/logs/app.log）
```

> 横幅里的地址是**容器内视角**，公网入口是 `:8444/docmind_dz`，别照着横幅去访问。
> 「局域网访问」那行是容器在 compose 网络里的内部 IP（`172.x`），宿主机与公网都
> 访问不到，忽略即可；它只在直接跑在宿主上时才有意义。

> **改了 `.env` 必须加 `--force-recreate`**：`docker compose up -d --force-recreate`。
> 否则 compose 不一定识别出环境变量变化，表现为"改了没生效"。

### 5. 本机验证（不依赖安全组、不依赖外网）

```bash
curl -sk https://127.0.0.1:8444/health
# 期望：{"status":"ok","app":"DocMind DZ","model_configured":true,"api_prefix":"/docmind_dz"}
# model_configured 为 false 表示模型 Key 没配好，任务一定失败（容器本身不会崩）

curl -sk https://127.0.0.1:8444/docmind_dz/analysis/fields | head -c 120   # 字段目录
curl -sk https://127.0.0.1:8444/docmind_dz/analysis/tasks  | head -c 120   # 任务列表

# 确认该封的都封了
curl -skI https://127.0.0.1:8444/ | head -1                       # 期望 404（内置前端）
curl -skI https://127.0.0.1:8444/docs | head -1                   # 期望 404（接口文档）
curl -skI https://127.0.0.1:8444/openapi.json | head -1           # 期望 404
```

`-k` 是因为证书签给域名，这里用 `127.0.0.1` 访问。

**这一步通过**，说明应用、nginx、证书三者都正确，剩下的只是网络入口问题。

### 6. 放行安全组

阿里云控制台 → 云服务器 ECS → 安全组 → 配置规则 → **入方向** → 手动添加：

| 协议类型 | 端口范围 | 授权对象 | 描述 |
|---|---|---|---|
| 自定义 TCP | `8444/8444` | `0.0.0.0/0`（想更严就填调用方出口 IP） | DocMind DZ HTTPS |

保存后立即生效。**不要改动已有的 80 / 443 / 8443 规则**（其它站点在用）。

### 7. 外网验证

```bash
curl -s  https://ai.wecanintl.com:8444/health
curl -s  https://ai.wecanintl.com:8444/docmind_dz/analysis/fields | head -c 120
curl -sI https://ai.wecanintl.com:8444/ | head -1                 # 404
```

### 8. 联调内置前端（生产不暴露）

内置页面只用于联调，公网访问返回 404。用 SSH 隧道直达容器端口（不经过 nginx）：

```bash
ssh -L 8010:127.0.0.1:8010 root@<服务器IP>
# 本机浏览器打开 http://127.0.0.1:8010/
```

> 本机 8010 若已被本地开发服务占用，改左侧端口即可：
> `ssh -L 18010:127.0.0.1:8010 root@<服务器IP>` → 打开 `http://127.0.0.1:18010/`

隧道内页面与接口同源，可完整走一遍：拖拽上传 → 节点时间线 → 核对弹窗 → 原件预览。
**隧道不经过 nginx，所以 `/`、`/docs`、`/static/**` 在这里仍然可用。**

重点验证四条：

- 传一个 **> 1MB** 的 PDF，不报 413；
- 上传后时间线**逐个**点亮（卡住不动 = SSE 被缓冲，检查 `proxy_buffering off`）；
- 传一个 **.xlsx / .docx**，左侧页面图正常（有图说明镜像里 soffice 可用；
  只有文字没有图 = 退化成纯文本解析了）；
- 中文显示为正常字形、不是方块，且**页数与 Windows 开发机一致**
  （页数变多 = 中文字体别名没生效）。

---

## 四、对外接口

接口基址：`https://ai.wecanintl.com:8444/docmind_dz`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/analysis/tasks` | 上传单证创建任务（multipart：`files`（可多个，最多 5）、`file`（兼容单文件）、`context`） |
| GET | `/analysis/tasks` | 任务列表（状态、进度、差异计数） |
| GET | `/analysis/tasks/{task_id}` | 任务状态与进度 |
| GET | `/analysis/tasks/{task_id}/events` | **SSE** 实时事件流（进度时间线） |
| GET | `/analysis/tasks/{task_id}/events/history` | 历史事件回放 |
| GET | `/analysis/tasks/{task_id}/result` | 结构化结果与字段级元数据 |
| GET | `/analysis/tasks/{task_id}/submission` | 已提交的核对结果 |
| POST | `/analysis/tasks/{task_id}/submit` | 提交人工核对结果 |
| POST | `/analysis/tasks/{task_id}/pause` \| `/resume` \| `/cancel` | 暂停 / 继续 / 取消 |
| GET | `/analysis/tasks/{task_id}/pages/{index}` | 单证页面渲染图 |
| GET | `/analysis/fields` | 字段目录 |
| GET | `/health` | 健康检查 |

**四条注意**：

1. **基址必须带 `:8444`** —— 漏掉端口会打到同域名的 443（其它站点）上。
2. **调用方来源要填进 `DOCMIND_DZ_CORS_ALLOW_ORIGINS`** —— 端口不同即跨域，即使同域名也算。
   服务端调用不需要配。
3. 以下已由 nginx 关闭，公网不可达：内置前端（`/`、`/static/**`）、接口文档
   （`/docs`、`/redoc`、`/openapi.json`）。
4. **接口目前没有鉴权**：拿到地址的任何人都能上传文件、列出全部任务、读取结果。
   `GET /analysis/tasks` 是前端左栏的数据源，不能像 DocMind 那样一关了之，所以只能靠
   网络边界。若需要更强的管控，见附录「可选加固」。

### 并发建议（与 DocMind 同机时）

两边都会跑 LibreOffice 与模型调用，CPU 是共享的。建议把并发控制在机器核数以内、
给 DocMind 留出余量：

```bash
DOCMIND_DZ_MAX_CONCURRENT_TASKS=3    # 同时执行的任务数，超限排队
DOCMIND_DZ_MODEL_MAX_CONCURRENCY=8   # 模型调用总并发
DOCMIND_DZ_HOUSE_BILL_CONCURRENCY=2  # 单任务内多份分单的并行抽取数
DOCMIND_DZ_OFFICE_CONCURRENCY=1      # soffice 单实例，必须串行
```

调大不会更快：瓶颈在 CPU 核数与上游模型限流，超出部分只会排队。

---

## 五、日常运维

| 事项 | 命令 / 说明 |
|---|---|
| 应用日志 | `docker compose exec docmind-dz tail -f /app/logs/app.log`（容器 stdout 只有启动横幅与少量告警） |
| 日志轮转 | 按天轮转成 `logs/app.log.YYYY-MM-DD`，保留 14 天（`DOCMIND_DZ_LOG_RETENTION_DAYS`） |
| 网关日志 | `docker compose logs docmind-dz-gateway` |
| 改 nginx 配置 | 改 `deploy/nginx/docmind_dz.conf`，然后 `docker exec docmind-dz-gateway nginx -t && docker exec docmind-dz-gateway nginx -s reload` |
| 升级 | `git pull && docker compose up -d --build`，**挑没人跑任务时**——重启会把在途任务标记为中断 |
| 副本数 | **只跑一个副本、一个 uvicorn worker**，多开会互相把对方任务误判为中断 |
| 数据位置 | `./data/tasks/<task_id>/`（原件、页面图、任务日志）、`./data/results/`；默认保留 24 小时（与托书识别一致），**启动时清一次、之后每小时清一次** |
| 磁盘 | `df -h /data`；清理用 `rm -rf data/tasks/*`（属主是 10001，root 可删） |
| 停止 | `docker compose down`（本项目的容器与网络，不影响 DocMind 等其它项目） |
| 模型兜底 | 主模型整轮重试失败时自动切到兜底 provider。日志关键字：`兜底`、`熔断`。排查用 `grep -E "provider|熔断|兜底" logs/app.log` |
| 关闭兜底 | 不配 `DOCMIND_DZ_FALLBACK_API_KEY`（或置空）即完全不启用 |

---

## 六、故障排查

| 现象 | 原因 / 排查 |
|---|---|
| 容器起不来，日志里 `Permission denied: '/app/logs/app.log'` | 漏了 `mkdir -p data logs && chown -R 10001:10001 data logs` |
| `127.0.0.1:8444` 连接被拒 | gateway 没起来或端口没映射 → `docker compose ps` |
| 外网**超时**（不是拒绝） | 安全组没放行 8444 |
| **502 Bad Gateway** | gateway 通了但应用没起来 → `docker compose logs docmind-dz`；两个容器都在跑仍 502，是 gateway 缓存了应用重启前的旧 IP → `docker compose restart gateway` |
| 浏览器证书警告 | 用 IP 访问（证书签给域名）；用域名仍警告则检查证书路径 → `nginx -t` |
| 全站 404 | nginx 配置没加载 → `docker exec docmind-dz-gateway nginx -t` |
| 改了 `.env` 没生效 | 少了 `--force-recreate` |
| 改了 nginx 配置没生效 | 少了 `nginx -s reload` |
| `/health` 里 `model_configured: false` | `DOCMIND_DZ_MODEL_API_KEY` 为空，或改了 `.env` 没 `--force-recreate`。容器不会崩，但任务一定失败 |
| 上传大文件 413 | `client_max_body_size` 小于单文件上限；接口只收单个文件，按 `MAX_FILE_SIZE_MB`（默认 50MB）留余量即可（当前 256m） |
| 任务大面积报"模型调用失败" | 主模型侧问题（限流 / 欠费 / 地域故障）。看日志有没有切换兜底；仍失败则检查兜底 Key 与配额 |
| 进度条不动 | SSE 被缓冲 → 检查 `proxy_buffering off` 与 `proxy_read_timeout` |
| DOC/XLS 没页面图，只有文字 | 镜像里 soffice 不可用，退化成纯文本解析 → `docker compose exec docmind-dz which soffice`、`soffice --version` |
| 中文渲染成方块 | 字体缺失 → `docker compose exec docmind-dz fc-list :lang=zh \| head` |
| **页数比 Windows 开发机多** | 中文字体别名未生效（退化到 DejaVu Sans，拉丁度量更宽）→ 确认 `/etc/fonts/conf.d/99-cjk-alias.conf` 存在且 `fc-cache -f` 已跑过 |
| 内存占用高 | 调小 `DOCMIND_DZ_OFFICE_CONCURRENCY`（每个 soffice 约 200~400MB）；PDF 不经过 LibreOffice |
| 任务都卡在 queued | 触到 `DOCMIND_DZ_MAX_CONCURRENT_TASKS`：要么前面有任务暂停占位，要么并发配得太小 |
| **构建**卡在下载依赖 | 加 `--build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` |
| **构建**卡在 `apt-get update` | 加 `--build-arg APT_MIRROR=mirrors.aliyun.com`（或 `mirrors.tuna.tsinghua.edu.cn`） |
| 拉基础镜像超时 | 镜像已用 `ghcr.io` 源；仍慢可在 Docker Engine 配 `registry-mirrors` 加速 |

---

## 七、附录

### 关键文件

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 镜像构建：非 root（uid 10001）、内置 LibreOffice 与中文字体 |
| `docker-compose.yml` | 两个服务：`docmind-dz`（应用）+ `gateway`（入口 nginx）；数据/日志绑定挂载 |
| `deploy/nginx/docmind_dz.conf` | 入口配置（完整 server 块，容器内监听 443） |
| `deploy/fontconfig/cjk-alias.conf` | Windows 中文字体名 → 镜像内开源字体别名（保证分页与开发机一致） |
| `.env.example` | 环境变量模板 |
| `.dockerignore` | 构建上下文排除项（`data` / `logs` / `.env` 不进镜像） |
| `README.md` | 功能说明与接口总览 |

### 可选加固

接口无鉴权，若调用方 IP 固定，最省事的是在安全组里只放行该 IP。若要给整套接口加一层
口令，可在 `deploy/nginx/docmind_dz.conf` 的 `location /docmind_dz/` 里加：

```nginx
auth_basic           "DocMind DZ";
auth_basic_user_file /etc/nginx/certs/.htpasswd;
```

并在 compose 里把对应文件只读挂进 gateway 容器（生成口令：
`docker run --rm httpd:alpine htpasswd -nbB user 'pass'`）。属于浏览器可交互的场景才合适；
服务端机器调用用 Basic Auth 也没问题，但要把凭证写进调用方配置。

### 迁移到独立服务器

整目录搬到新机器即可，**三个部署文件都不用改**：

```bash
mkdir -p /data/apps && cd /data/apps
git clone <仓库地址> DocMind_DZ && cd DocMind_DZ
mkdir -p data logs && chown -R 10001:10001 data logs
cp .env.example .env    # 填同样的值
# 证书若不再共用：拷 fullchain.pem / privkey.key 到 deploy/nginx/certs/，
# 并把 docker-compose.yml 里那一行挂载改为 ./deploy/nginx/certs:/etc/nginx/certs:ro
docker compose up -d
# 最后改 DNS 指向新机器
```

新机器上 443 若空闲，把端口映射改回 `- "443:443"` 即可使用标准端口，无需带端口访问。
