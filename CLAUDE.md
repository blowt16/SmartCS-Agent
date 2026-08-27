# CLAUDE.md — SmartCS-Agent 项目规范

## 商品知识文档编写规范

所有知识库文档（docx）必须遵循以下统一标准，保证 RAG 检索精度。规范来源：`docs/superpowers/specs/2026-08-27-商品动态信息入库与知识库静态化-design.md`（含 2026-08-27 实测召回验证结论）。

### 1. 知识分层原则

| 信息类型 | 存储位置 | 规则 |
|---|---|---|
| 动态信息（价格、库存） | 数据库 `product_price_stock` 表 | **禁止写入 docx**，会快速过期 |
| 静态信息（参数、外观、售后政策） | docx → 上传索引 → pgvector | 向量检索 |

### 2. docx 格式规范

- **样式体系**：Heading 1 文档标题 → Heading 2 品类/章节 → Heading 3 商品 → Heading 4 子章节（商品信息/功能特点/规格参数/售后服务/参考来源）；正文用 Normal 段落 / List Bullet 要点；全部默认样式，无自定义字体
- **章节内容量 ≥ 450 字符**（含标题与条款）：索引管道按 500 字符/块、50 重叠贪心切分，章节内容不足时多个主题会被拼进同一混合块，reranker 相关度被稀释导致掉出检索 top-5。章节 ≥450 字符可强制章节边界成为切块边界。**适用边界：仅限内容可自行设计的文档（如售后政策文档）**；数据驱动的商品文档（每商品内容量天然不一致，由上游数据源决定）无法用内容约束解决，其混合块问题需靠切分策略演进（章节感知切分）或检索侧实体识别（见 spec 2026-08-27 §3.3 已知局限与实测）
- **文档开头必须有总述段**（说明适用范围，如"本政策适用于所有京东自营商品"）：作为"XX 商品 + 主题"类 query 的检索桥接块
- **条款措辞使用高频业务词**（七天无理由、价格保护、退换货、运费、质保等）：提升 BM25 关键词命中率

### 3. 售后政策文档规范

- 售后政策**独立成文档**《京东自营售后政策.docx》，与商品文档同目录 `llm_backend/knowledge_data/product_knowledge_docx/`，由 `scripts/build_jd_aftersales_docx.py` 单独生成（内容独立于 TSV，来源为京东官方帮助中心 help.jd.com 检索结果，附来源链接）
- 平台政策章节：七天无理由退货、价格保护、售后流程、运费与上门取件、商品类目差异说明等
- 京东自营商品（TSV 售后列 = "京东自营"）在商品文档 H4 售后服务章节写引用句："本商品为京东自营，适用《京东自营售后政策》（独立知识文档，已随知识库上传）"；此引用句同时含商品名与政策指向，是召回桥接的关键，不可省略
- 已知局限：含具体商品名 + 政策主题的混合 query（如"米家智能晾衣机有价格保护吗"）中，商品块在召回竞争天然占优，政策块可能掉出 top-5——需售后 Agent 接入时配合实体识别解决，静态知识库无法完全避免

### 4. 生成管道（单一数据源）

```
scripts/data/jd_smart_furniture.tsv（商品静态信息源）
  ├─ scripts/build_smart_furniture_docx.py → 商品知识 docx（静态）
  ├─ scripts/build_jd_aftersales_docx.py → 京东自营售后政策 docx（静态）
  └─ llm_backend/scripts/import_product_price_stock.py → product_price_stock 表（动态）
```

- 商品文档由 TSV 生成，**禁止直接手工编辑 docx**（不可复现、gitignore 内无法追踪）
- 生成后通过上传接口/索引脚本入库（parse → 清洗 → 分块 → embedding → pgvector）
- docx 文件在 `.gitignore` 内，变更只提交生成脚本与 TSV

### 5. 数据表规范

- 商品动态信息表 `product_price_stock`：product_name（唯一键，与 TSV 商品名称完全一致）、category、current_price（Numeric，范围价取均值）、stock_quantity（Integer，0=无货）、updated_at
- 导入脚本按 product_name 幂等 upsert，可重复执行
