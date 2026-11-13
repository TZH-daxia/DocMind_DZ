# DocMind DZ

航空货运单证（主单 / 分单 / 舱单）字段抽取服务：所有页面统一走视觉模型转写，识别并跳过主单；**有舱单时以舱单为准**，逐行抽取出分单字段并把舱单行转换成前端可核对的分单页签；没有舱单时才逐份抽取分单页。不再做分单/舱单一致性核对，输出字段级候选供人工核对后提交 ERP。

## 技术栈

- **后端**：Python 3.12+、FastAPI、Uvicorn、LangGraph、LangChain（OpenAI 兼容接口）、Pydantic v2
- **文档解析**：PyMuPDF（页面渲染 + 文本层定位）、python-docx、openpyxl / xlrd、LibreOffice（Office 转 PDF）
- **前端**：原生 HTML + CSS + ES Modules，无构建步骤
- **存储**：本地文件（`data/` 下 JSON + Markdown，无数据库）
- **工具**：uv、pytest、ruff

## 工作流程

```mermaid
flowchart TD
    A[上传文件 PDF / Word / Excel] --> M{识别方式}
    M -- 单票 single --> M1[多个文件合并成一个任务]
    M -- 批量 batch --> M2[一个文件一个任务]
    M1 --> B[解析文档：渲染页图 + 文本层]
    M2 --> B
    B --> C[图像转写：逐页 VLM]
    C --> Q{单页转写质量}
    Q -- 空单元格 / 分隔符占比过高 --> Q1[该页回退 PDF 文本层]
    Q -- 正常 --> D
    Q1 --> D[识别主单 / 分单 / 舱单<br/>规则优先，规则判不了再问模型]
    D --> S{单票且出现多个主单号?}
    S -- 是 --> S1[任务失败<br/>SINGLE_TICKET_MULTIPLE_MASTER]
    S -- 否 --> E{分类结果}
    E -- 有舱单（分单页也不再抽） --> G1[抽取舱单行：30 字段<br/>无分单号的汇总行跳过]
    E -- 无舱单且有分单 --> F[分单页四象限局部精读<br/>每页 4 图]
    E -- 只有主单 --> H[生成结果：无分单数据]
    F --> G2[抽取分单字段：35 字段 × 每份分单]
    G1 --> T[舱单行 → 分单<br/>字段映射 + 坐标重映射]
    G2 --> V[跳过一致性核对<br/>写空 comparisons.json]
    T --> V
    V --> H2[生成结果：分单页签]
    H2 --> P[人工核对分单页签<br/>入库已上传 / 提交 ERP]
```

## 关键设计

- **单票 / 批量**：由接口显式传 `mode`。`single` 是同一主单的多个文件合并成**一个任务**，识别出多个主单即失败（`SINGLE_TICKET_MULTIPLE_MASTER`）；`batch` 是一个文件一个任务。
- **VLM 转写 + 质量门禁**：不信任 PDF 文本层（扫描件、字体乱码、表格错位），文本层只保留给坐标定位；但转写若读成「大量空单元格 / 分隔符」（竖线占比 ≥60%）则判为失败，回退到文本层并写 warning。
- **分单局部精读**：无舱单时，分类后只对分单页做四象限 300dpi 放大转写（横纵各 12% 重叠），抽取时局部优先；切图只在内存传给模型，不落盘。有舱单时该节点由路由跳过（以舱单为准，不抽分单页）。
- **候选层**：分单 35 个字段全部输出候选（value / status / confidence / evidence.quote），抽不到也补 missing；模型逐字引用原文，后端校验 quote 是否出现在转写文本中，对不上只降级为待审核、不丢值。计费重量（`jfweight`）与申报价值（`declarecustom`）不抽取，恒为空、由前端人工填写。
- **舱单以舱单为准**：舱单按行抽取 30 个字段（主单号 / 分单号 / 始发港 / 目的港 / 航班号 / 航班日期 + 收发货人各 10 项 + 件数 / 重量 / 体积 / 品名），没有分单号的行按总运单汇总行跳过（也不产出汇总块）；随后**每一行转换成分单**（字段映射到分单接口名、坐标一并重映射），前端只渲染分单页签、用分单表单模板，高亮落在舱单页上。有舱单时不再抽分单页。
- **取消一致性核对**：分单与舱单不再互相比对（有舱单时两者本就是同一份数据）。核对节点仍保留但固定跳过，并落一份空 `comparisons.json`，保证结果结构与断点续跑判定稳定。
- **进度**：只在节点边界推进（5 → 25 → 45 → 48 → 55 → 85 → 95 → 100），节点内部的逐页/逐份事件只进时间线。
- **字段定位**：先用引用找锚点，再取同栏同行的值；同名值多处命中时用同栏约束消歧，无法确定就不框（宁可不框，也不框错）。页头字段（主单号 / 航班号 / 日期）与只印在页头航线栏的港口允许整页定位；电话按「去掉非数字后包含」匹配（`0752-5855105` ↔ `07525855105`），国家二字码会按 `CHINA / USA` 等全称兜底；邮箱还会用 PDF 文本层做一次相似度校正（修正 VLM 漏字母）。
- **港口三字码**：`data/reference_cache/hbinfo.json`（6394 条）是唯一来源，模型输出三字码、后端查表校验；多义城市保留原文进待审核；前端只能从下拉候选中选。
- **Office 宽表**：Excel 转换前设置 fitToWidth=1 / fitToHeight=0，先缩到一页宽、放不下再上下分页；`.xls` 先转 xlsx 再套同一逻辑。
- **思考开关**：识图转写关思考（逐字转写不需要推理，更快且准确率差异很小），字段抽取与分类开思考。
- **品名清洗**：品名栏里的 HS CODE、发票号、P.O. 等附加行会被确定性剔除。
- **主单判定**：必须能读到主单号才算主单，运单条款页不会再被误判成主单。页首同时印着主单号与货代单号（如 `416 PVG 52342905` + `SZA26100005`）的 AWB 判为**分单**，不再当成第二个主单。
- **单票多主单**：只比较规范化的主单号（3 位航司代码 + 8 位序号，允许 `416-52342905` / `41652342905` 两种写法）；分单号、发票号这类 11 位数字不参与，失败提示会列出所有涉及的主单号及页码。

## 数据层产物

```text
data/
├─ reference_cache/hbinfo.json   # 港口三字码主数据（唯一来源）
├─ tasks/<task_id>/
│  ├─ <source>.pdf               # 原件
│  ├─ src01/page_00N.png        # 渲染页图（送模型的原图）
│  ├─ transcription/page_00N.md # 整页 VLM 转写（正文唯一来源）
│  ├─ transcription/page_00N_zoom.md  # 分单四象限局部精读
│  ├─ document.json             # 页面元数据 + 分段 + 主单登记
│  ├─ candidates.json           # 字段候选唯一来源
│  ├─ documents.json            # 分单字段与定位 + 舱单
│  ├─ comparisons.json          # 核对结果（已停用，恒为空；仅作断点判定）
│  └─ task_status.json / process.log
└─ results/<task_id>.json        # 最终交付物（前端 /result）
```

## 运行方式

```bash
uv sync
cp .env.example .env          # Windows: copy .env.example .env
# 在 .env 中填入 DOCMIND_DZ_MODEL_API_KEY
uv run python main.py         # http://127.0.0.1:8010
```

需要真实模型密钥；仓库不再提供 mock 模式，测试通过 `tests/fake_gateway.py` 注入假网关离线跑通管线。

```bash
uv run pytest
uv run python tools/smoke_test.py --base http://127.0.0.1:8010
```

## 目录结构

```text
DocMind_DZ/
├─ main.py                     # 启动入口
├─ pyproject.toml              # 依赖与工具配置
├─ .env.example                # 环境变量样例
├─ app/
│  ├─ main.py                  # FastAPI 装配、静态资源托管、健康检查
│  ├─ config.py                # 配置（DOCMIND_DZ_ 前缀）
│  ├─ agent/                   # 模型网关：provider、重试与兜底、JSON 解析
│  ├─ api/routes/analysis.py   # 任务接口：上传、进度 SSE、结果、港口搜索、提交
│  ├─ collector/               # 多格式解析器、页面分类与分段、字段定位、港口主数据索引
│  ├─ prompts/                 # 转写 / 分类 / 局部精读 / 抽取提示词
│  ├─ schemas/                 # 字段定义、领域模型、提交模型
│  ├─ service/                 # 管线节点、字段抽取、审核清单、港口口径（一致性核对已停用）
│  ├─ storage/                 # file_store 目录契约 + task_docs 统一文档模型
│  ├─ workflow/                # LangGraph 图、节点包装、状态与中断异常
│  └─ static/                  # 前端：index.html / styles.css / ES Modules
├─ tests/                      # 单元与管线测试（含假网关 tests/fake_gateway.py）
├─ tools/smoke_test.py         # 冒烟测试脚本
└─ data/                       # 运行产物（tasks/、results/、reference_cache/，已 gitignore）
```

## 常用配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DOCMIND_DZ_MODEL_API_KEY` | - | 模型密钥（必填） |
| `DOCMIND_DZ_MODEL_THINKING` | true | 字段抽取 / 分类是否开思考 |
| `DOCMIND_DZ_MODEL_VISION_THINKING` | false | 识图转写建议关（更快） |
| `DOCMIND_DZ_MODEL_TIMEOUT_SECONDS` | 300 | 文本调用超时（开思考后更慢） |
| `DOCMIND_DZ_MODEL_VISION_TIMEOUT_SECONDS` | 120 | 视觉调用超时 |
| `DOCMIND_DZ_VISION_PAGE_CONCURRENCY` | 2 | 整页转写并发页数 |
| `DOCMIND_DZ_HOUSE_BILL_CONCURRENCY` | 3 | 分单抽取并发数 |
| `DOCMIND_DZ_OFFICE_CONCURRENCY` | 1 | Office 转 PDF 并发数 |
