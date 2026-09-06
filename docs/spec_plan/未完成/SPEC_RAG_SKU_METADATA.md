# RAG 索引构建 sku 注入与 rag_retrieval 返回 metadata 优化实施规格
> **归档状态**: ⏳ 待实施（2026-09-06 起草，本期实施范围 = **阶段 P0 docx 标注 + 阶段 C 索引 sku_codes 注入 + 阶段 D rag 返回 metadata 透出**；D-2（product_stock_lookup sku 精确参数）为可裁剪子项见 §7-3）
> **依赖前置**: [[SPEC_SKU_ALIGNMENT.md]] 阶段 A 已实施（TSV 含 `sku` 列 + `product_price_stock.sku` unique 已入库）——本 spec 的 docx 标注与导入重灌全部消费阶段 A 产物；阶段 A 未落地前本 spec 不可开工

> **用途**: 打通"RAG 静态知识块 ↔ 动态库价格行"的确定性对齐链路（两 tool 不一致问题第二阶段）：① 商品 docx H3 标题标注 `(SKU:xxx)`（提取锚点，文档自包含）；② 索引构建阶段把 sku 以**多值列表**注入 chunk 元数据（chunk 跨商品是切分必然态，单值注入必错）；③ `rag_retrieval` 返回层透出 LLM 决策所需 metadata（商品编码/知识类型/来源），并同步硬编码消费通道（customer_tools/summarize）——两通道渲染一致，杜绝"agent 场景有元数据、生产节点裸文本"的分叉
> **技术栈**: python-docx（构建）+ doc_parser/indexing_service（docx 解析→分段→全文递归切分→入库）+ pgvector(document_chunks) + rag_tool/customer_tools/summarize（消费通道），零 LLM 链路改动（summarize 仅提示词加规则句）
> **状态**: 待实施 —— 范围界定：把 [[SPEC_SKU_ALIGNMENT.md]] §6.2 的"阶段 B（docx 标注）+ 阶段 C（chunk metadata）+ 阶段 D（检索/工具侧）"纲领正式化，其中 docx 标注作为本 spec 前置 P0 一并实施（索引提取锚点依赖它，拆为独立 spec 会造成"docx 已重建、注入空转"的中间态）
> **关联文档**: [[SPEC_SKU_ALIGNMENT.md]]（阶段 A 前置）[[SPEC_PRODUCT_STOCK_TOOL.md]]（§12.1 SKU 对齐演进承接）[[SPEC_RAG_TOOL_OPTIMIZATION.md]]（决策 #10 片段前缀约定）[[SPEC_CHUNK_MERGE_STRATEGY.md]]（块归属机制出处）[[PROJECT_ANALYSIS.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状核查（代码实测）](#2-现状核查代码实测)
3. [方案决策记录](#3-方案决策记录)
4. [详细设计](#4-详细设计)
5. [验证方案与验收断言](#5-验证方案与验收断言)
6. [分阶段实施步骤](#6-分阶段实施步骤)
7. [待确认事项](#7-待确认事项)
8. [风险与避坑清单](#8-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 阶段 A（[[SPEC_SKU_ALIGNMENT.md]]）已在数据源层建立 sku 确定性键（TSV 列 + DB unique），但**检索侧尚无 sku 可对齐**：chunk 元数据只有单值 `chapter`（块首段章节路径，`document_chunk.py:22`），商品身份只以文本形式存在于块内容里
2. `rag_retrieval` 返回层丢弃全部元数据（`rag_tool.py:128-131` 仅【来源:文件名】+ text）——LLM 拿裸文本自行抽商品名再走 ILIKE 模糊查库，正是名称歧义/抽取失败两类不一致的温床
3. 生产链路（customer_tools 直连 `search()` → summarize，零 LLM 工具调用）与 @tool 通道消费同一批 doc dict 但**渲染各自为政**：customer_tools 只取 `text` 拼上下文（`customer_tools/node.py:63`），元数据即使进 doc dict 也不会到 summarize——两通道必须同步设计，否则 Agent 接入后行为分叉（CLAUDE.md 全项目约束）
4. chunk 跨商品为切分必然态（[[SPEC_CHUNK_MERGE_STRATEGY.md]]：全文统一递归切分、实测块横跨多章），**sku 注入必须是多值收集**；归档 spec §12.1 已定论"chunk 可跨多商品，SKU 需多值挂载，主体判断下放 LLM"

### 1.2 目标（本 spec 范围）

| 目标 | 量化验收 |
|---|---|
| docx 商品标题携带可解析编码锚点 | H3 = `商品名 (SKU:JD-XXX-NNN)`，50 商品全覆盖，格式与 TSV 一致 |
| chunk 元数据携带 sku 多值列表 | `document_chunks.sku_codes` JSONB 列非空比例符合预期；跨商品块含 ≥2 编码且与文本实际覆盖一致 |
| 检索返回 doc 带齐消费所需字段 | doc dict：`sku_codes` + `chapter`（向量/BM25 两路同构）；rag 输出每块前缀行 |
| 双消费通道渲染一致 | @tool 输出与 customer_tools 上下文用**同一渲染函数**，格式零漂移 |
| LLM 可用 metadata 决策 | 前缀行含：商品编码（多值）/知识类型/来源；空 sku 块有明确"无商品归属"语义 |
| 不回归 | 无 sku 文档（政策 docx/txt/md/PDF）索引正常（空列表）；现有工具单测/检索断言全绿 |

### 1.3 明确不做

- 不改 `rag_retrieval` 入参/三态协议（`query` 单参、ok/空结果/error JSON 不变）
- 不做 H3 硬边界切分、不做按 sku 的检索过滤（方案 A"先召回后对齐"不需要 filter；需"定向检索"另议）
- 不注入商品名称列表到元数据（名称在块文本/chapter 中已可见，注入重复存储；见决策 D2）
- `product_stock_lookup` 的 sku 参数为**可裁剪子项** D-2（§7-3），核心范围不含

## 2. 现状核查（代码实测）

### 2.1 索引构建链路（docx → chunk，2026-09-06 读码）

- `scripts/build_smart_furniture_docx.py`：单份 `京东智能家具产品知识文档.docx`，H3 = `row[COL_NAME].strip()`（`:50`，add_product 函数），8 列 COL_* header 名读取（`:36-37`，`:96` DictReader）；政策文档独立脚本（无商品标题，天然无 sku）
- `doc_parser.py`：`Segment{text, chapter}`（`:15-19`）；heading 时 flush + push 章节栈 `(level, title)`（`parse_docx:92-110`）；**标题行保留进正文**（`:108`，注释"商品文档标题含产品名,需可检索"）；章节路径 = 祖先栈 join（`:88`）
- `indexing_service.py`：全文统一切分 `RecursiveCharacterTextSplitter(chunk_size, overlap, add_start_index=True)`（`:34-39`）；段→字符轴 `spans:(start,end,chapter)`（`:124-129`，每段 +2 补偿 `\n\n`）；块归属 `_locate_chapter(spans, start_index)` 块首单值（`:44-53,133-140`）；`CHUNK_MIN_SIZE` 过滤（`:137`）；ORM 行构造（`:160-168`，含 `chapter=ch`）
- `document_chunk.py`：列 = id/source/file_path/user_id/chunk_index/chunk_id/md5/file_type/page/**chapter(String 255 单值)**/content/embedding/content_tsv/created_at（`:13-32`），**无任何多值 metadata 列**
- `text_cleaner.py`：清洗仅控制字符/换行规范化/连续空格压缩/行 strip/页码目录清除（`:17-27`）——**括号与 ASCII 完全保留**（实测关闭项：标题行 `(SKU:...)` 进块文本后可见可检索 ✓）

### 2.2 检索返回链路（service → doc dict → 两通道）

- 向量路 `_to_doc`（`rag_retriever_service.py:54-69`）：`text/id/chunk_id/source/file_path/user_id/chunk_index(+score)`——**无 chapter、无 sku_codes**；BM25 路（`bm25_sql_retriever.py:75-89`）：同上（键 `bm25_score`），SQL 为 `SELECT document_chunks.*`（`:35`）→ 新列加后 `r["sku_codes"]` 可直接取（仅需补 dict 取值）
- RRF 融合按 `chunk_id` 去重（`rag_retriever_service.py:133-137`）；rerank 输入融合 dict 列表（`:140-147`）——**rerank 是否重建 dict（丢未知键）为实施验证点**（§8-6）
- @tool 通道 `rag_tool.py:128-131`：仅 `【来源:文件名】+ text` 拼接
- 硬编码通道 `customer_tools/node.py:54-63`：`response_text = "\n\n".join(d.get("text",""))`，`records = {result: response_text, hybrid_docs: docs}`（`:71-75`）
- summarize 消费 `records.result`（`summarize/node.py:44-48`）→ prompt `事实信息：{results}`（`prompts.py:29`），提示词无 metadata 使用规则

### 2.3 相关测试（影响面）

`tests/`：`test_parser.py`（Segment 结构断言）、`test_indexing.py`（chunk 入库断言）、`test_rag_tool.py`（输出格式断言）、`test_bm25_retriever.py`（doc dict 键断言）、`test_product_stock_tool.py`、`test_aftersales_docx_build.py`、`test_import_price_stock.py`、`test_product_price_stock_model.py`、`test_smoke.py`

## 3. 方案决策记录

| # | 决策 | 结论 | 理由 |
|---|---|---|---|
| D1 | docx 承载方式 | **H3 标题后缀 `(SKU:xxx)`**，推翻归档 spec"SKU 不进 docx"裁定 | 文档自包含：parse 标题行即提取锚点，不依赖 TSV 联动（用户上传任意 docx 同样可解析）；顺带补"型号/编码纯向量召回弱"短板（[[OPTIMIZATION_QA]] Q10）——代价（回答回显）由 D6 提示词规则抑制 |
| D2 | metadata 注入内容 | **仅 sku（多值列表），不注入商品名称** | 名称在块文本与 chapter 已可见；注入名称=重复存储与同步负担。LLM 对齐 = 文本商品名（人读）+ 前缀 sku（机读精确键）配对使用 |
| D3 | sku 注入时机与粒度 | parse 段级挂载（随章节栈）+ 切分后**字符轴区间多值收集** | 与现 chapter 归属同一 spans 机制（`indexing_service.py:124-129`）扩展，最小侵入；混合块全量列出不猜主体，主体判断下放 LLM（归档 §12.1 定论） |
| D4 | 存储列 | `document_chunks.sku_codes` **JSONB** 多值列表 | 现无多值列（chapter 单值 String 255 不可复用）；JSONB 支持未来 `@>` 过滤检索；空列表语义 = 无商品归属（政策/通用/未标注文档） |
| D5 | 输出前缀格式 | 单行 `【商品编码:S1\|S2｜知识类型:X｜来源:F】`；空列表 → `【商品编码:—（无商品归属）…】` | 一行为单位、字段定界清晰、LLM 可解析；空语义显式标注，防政策块被拿去查动态 |
| D6 | 双通道渲染 | 抽**公共渲染函数**，@tool 与 customer_tools 共用 | 两处手写必漂移（现 @tool 有【来源】而节点无）；渲染含回显抑制约定写入 summarize 提示词 |
| D7 | 知识类型字段 | 取 chapter 末段（H4 小节名/末级标题），无则空 | 参数/功能/政策/来源的语义区分（LLM 回答口径依据）；完整 chapter 路径含 H3 重复冗余，只给末段控 token |
| D8 | docx 重建与重灌 | 重建后**手动清理旧行 + 重传**（md5 变 → 自动新 doc，旧行残留需删） | 上传 duplicate 按 md5 短路（`indexing_service.py:92-100`），docx 文本变 → md5 变 → 视为新文件；旧行按 source/文件名清理（步骤见 §4.4） |
| D9 | sku 正则锚点 | 标题行尾 `(SKU[:：]?([A-Za-z0-9_-]+))`（`\)` 结尾，宽容 `:`/`：`） | 只认标题后缀防正文误匹配；TSV 生成值固定 `JD-[A-Z]{3}-\d{3}` 天然满足 |

## 4. 详细设计

### 4.1 阶段 P0：docx 商品标题标注（scripts/build_smart_furniture_docx.py）

```python
COL_SKU = "sku"     # 加入 :36-37 列名常量组(阶段 A 已加的 TSV 第 9 列)

# add_product(:48-50) H3 改为:
sku = row[COL_SKU].strip()
title = f"{row[COL_NAME].strip()} (SKU:{sku})" if sku else row[COL_NAME].strip()
doc.add_heading(title, level=3)
```

- sku 空（阶段 A 未落地/TSV 缺值）→ 回退原标题，文档仍可构建（向前兼容）
- 政策文档脚本 `build_jd_aftersales_docx.py` **不改**
- 输出格式半角 ` (SKU:JD-LCK-001)`：与 D9 锚点正则、`_normalize_keyword`（空格处理）无冲突；标题带 sku 进 chapter 与块文本（现状 `doc_parser.py:108` 保留标题行）

### 4.2 阶段 C：索引构建 sku_codes 注入

#### 4.2.1 存储列（llm_backend/app/models/document_chunk.py）

```python
from sqlalchemy.dialects.postgresql import JSONB

sku_codes = Column(JSONB, nullable=True)   # 块覆盖商品编码列表(多值);[]/NULL=政策/通用块
```

#### 4.2.2 parse 段级挂载（llm_backend/app/services/doc_parser.py）

```python
_SKU_SUFFIX = re.compile(r"\((?:SKU|sku)[:：]\s*([A-Za-z0-9_-]+)\)\s*$")

@dataclass
class Segment:
    text: str
    chapter: str = ""
    sku: str = ""               # 本段所属商品编码;政策/总述/未标注段为空

# 章节栈 (level, title) → (level, title, sku);md 与 docx 两条 parse 共用同一栈逻辑(:37-67/:80-131)
def push_heading(level: int, title: str):
    chapter_stack[:] = [(lv, t, s) for lv, t, s in chapter_stack if lv < level]
    m = _SKU_SUFFIX.search(title)
    chapter_stack.append((level, title, m.group(1) if m else ""))

# flush 段写入: 取栈内最深非空 sku(产品 H3 段以下继承;同级新产品标题替换时随栈剪枝自动切换)
def _active_sku() -> str:
    for _, _, s in reversed(chapter_stack):
        if s:
            return s
    return ""
```

- 提取用**原始标题文本**（clean 前）→ 注入不受清洗影响；标题行进正文的现状不变（正文可见 sku，D1 目的）
- txt/md/PDF 路径复用同一栈（无标注自然为空）

#### 4.2.3 字符轴区间多值收集（llm_backend/app/services/indexing_service.py）

```python
# :116-120 clean_segments 携带 sku → :124-129 spans 升级四元组 (start, end, chapter, sku)
spans: List[tuple[int, int, str, str]] = []
cap = 0
for text, chapter, sku in clean_segments:
    spans.append((cap, cap + len(text), chapter, sku))
    cap += len(text) + 2

def _collect_skus(spans, start: int, end: int) -> list[str]:
    """块文本区间 [start, end) 覆盖段的全部 sku,按首次出现序去重;无覆盖返回 []"""
    skus, seen = [], set()
    for s, e, _ch, sku in spans:
        if sku and s < end and e > start and sku not in seen:
            seen.add(sku); skus.append(sku)
    return skus

# 切分循环(:133-140)内,每块(跳过 CHUNK_MIN_SIZE 后):
sku_list = _collect_skus(spans, d.metadata["start_index"],
                         d.metadata["start_index"] + len(content))
chunk_skus.append(sku_list or None)      # 空 → NULL(存储面与 [] 等价,JSONB 统一)

# ORM 行(:160-168)增加 sku_codes=chunk_skus[i];zip 元组同步扩列
# 构建统计(归档前置验证项落地): 多 sku 块计数 + 占比 logger.info
multi = sum(1 for s in chunk_skus if s and len(s) > 1)
logger.info("sku 注入完成: 多商品块 {} / {} ({:.1%})", multi, len(chunks), multi / len(chunks))
```

- chunk 文本是 full_text 连续子串（splitter 保证）→ `end = start + len(content)` 精确
- overlap=50 使边界块与邻块重复收集同 sku —— 符合预期（两块的文本都真实包含该段）
- `chapter`（块首单值）与 `sku_codes`（区间多值）**并存不动 chapter 语义**（D 轮检索侧需 chapter 时另取）

#### 4.2.4 存量重灌（md5 短路机制下的正确步骤）

1. 重建 docx（P0）→ md5 变化
2. 清理旧行（按 source 删除，避免新旧双份共存）：
   ```sql
   DELETE FROM document_chunks WHERE source = '京东智能家具产品知识文档.docx';
   DELETE FROM documents     WHERE original_filename = '京东智能家具产品知识文档.docx';
   ```
   （或调 DELETE API；`user_id` 过滤按实际部署追加）
3. 重传/重跑 ingest → 新 md5 全量入库；policy docx 若未变则 md5 相同 → duplicate 短路（正确：其 sku_codes 为空，无需重建）
4. 断言 A3 占比日志出现

### 4.3 阶段 D：检索返回与工具输出 metadata 透出

#### 4.3.1 doc dict 扩列（检索返回层）

```python
# rag_retriever_service.py _to_doc(:54-69) 增加:
"sku_codes": chunk.sku_codes or [],
"chapter": chunk.chapter,          # 现状返回层无 chapter(仅存储层有)

# bm25_sql_retriever.py :78-89 dict 同步增加(列已在 SELECT *,SQL 无需改):
"sku_codes": r["sku_codes"] or [],
"chapter": r["chapter"],
```

#### 4.3.2 公共渲染函数（新文件 app/tools/doc_block_renderer.py 或并入 rag_tool.py 模块级）

```python
def render_doc_blocks(docs: list[dict]) -> str:
    """doc dict → LLM 上下文文本块(每块前缀行 + 文本)。@tool 与 customer_tools 共用。"""
    blocks = []
    for d in docs:
        skus = d.get("sku_codes") or []
        sku_part = "|".join(skus) if skus else "—（无商品归属:政策/通用,不可查动态数据）"
        chapter = d.get("chapter") or ""
        ctype = chapter.split(">")[-1].strip() if chapter else ""
        src = Path(d.get("file_path") or "未知").name
        blocks.append(
            f"【商品编码:{sku_part}｜知识类型:{ctype or '—'}｜来源:{src}】\n{d.get('text','')}"
        )
    return "\n\n".join(blocks)
```

- `rag_tool.py:128-131` 替换为 `return render_doc_blocks(docs)`
- `customer_tools/node.py:63` 替换为 `from ... import render_doc_blocks; response_text = render_doc_blocks(docs)`——records 结构与 `hybrid_docs` 字段不变（D 轮其他消费无感）

#### 4.3.3 rag_retrieval docstring 使用约定（rag_tool.py:73-99 增补）

```
【元数据说明】每段含"商品编码:S1|S2"前缀:1) 用户问该商品价格/库存时,以
product_stock_lookup 查询并优先传 sku=精确匹配(回退传正文商品名关键词);
2) 前缀为"—（无商品归属）"的段(政策/通用)只能佐证政策与通用条款,不得据此
查询或断言任何单一商品动态数据;3) 引用商品名时省略标题中括号编码。
```

#### 4.3.4 summarize 提示词规则句（summarize/prompts.py:33-39 增补）

```
* 引用商品时使用正文商品名,省略标题中的(SKU:xxx)编码
* 事实块带"无商品归属"标记的仅用于政策/通用条款,不据此编造具体商品动态信息
* 若用户询问价格/库存而事实信息中不含动态数据,如实告知可进一步查询,不得估算
```

#### 4.3.5 D-2（可裁剪）：product_stock_lookup sku 精确参数（product_stock_tool.py）

```python
class ProductStockLookupInput(BaseModel):   # :100-120 增加可选字段(置于 product_name 之前语义优先)
    sku: Optional[str] = Field(default=None,
        description="商品编码(来自 rag_retrieval 返回的【商品编码:】前缀),精确匹配优先于名称模糊;"
                    "无编码时留空走名称/品类模糊查询")
    product_name: str = ...

# 查询(:161-170)改为: sku 提供时 WHERE sku = :sku(精确,单值命中)直接短路名称过滤;
# 返回 records(:188-197) 增加 "sku": r.sku
```

- 精确命中 ≤1 行 → 不再有同前缀多商品歧义（两 tool 不一致根因 1 的终态消除）
- 名称/品类模糊通道**保留**（无 sku 场景:条件型选购/零命中/用户直接口语问价）
- 不影响阶段 A 的 upsert 键切换（查询与写入互不干扰）

### 4.4 影响面清单（全项目扫描）

| 文件 | 改动 |
|---|---|
| scripts/build_smart_furniture_docx.py | H3 标题加 `(SKU:xxx)`（P0） |
| llm_backend/app/models/document_chunk.py | 加 `sku_codes` JSONB 列 |
| llm_backend/app/services/doc_parser.py | Segment+章节栈带 sku；_SKU_SUFFIX |
| llm_backend/app/services/indexing_service.py | spans 四元组、_collect_skus、ORM 行、多商品块占比日志 |
| llm_backend/app/services/rag_retriever_service.py | _to_doc 加 sku_codes/chapter |
| llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/hybrid_retrieval/bm25_sql_retriever.py | dict 加 sku_codes/chapter（SQL 不变） |
| llm_backend/app/tools/rag_tool.py | 输出换 render_doc_blocks；docstring 约定 |
| app/tools/doc_block_renderer.py（新增） | 公共渲染函数 |
| customer_tools/node.py | response_text 换 render_doc_blocks |
| summarize/prompts.py | 规则句 |
| product_stock_tool.py（D-2） | sku 可选参数 + 返回含 sku |
| tests：test_parser/test_indexing/test_rag_tool/test_bm25_retriever/test_aftersales_docx_build（+D-2 的 test_product_stock_tool） | 断言同步 |
| 存量库 | docx 重建 + 清理旧行 + 重灌（§4.2.4） |

## 5. 验证方案与验收断言

### 5.1 阶段 P0/C（构建与注入）

- **A1** 重建 docx 后抽查 3 个商品：H3 文本 = `原商品名 (SKU:JD-XXX-NNN)`，sku 与 TSV 逐字符一致
- **A2** `test_parser` 扩展：含标注标题的 docx/md → 段 `sku` 正确继承/切换/清空（新产品标题替换后旧 sku 不再出现）
- **A3** 重灌后 SQL：`SELECT count(*) FROM document_chunks WHERE sku_codes IS NULL` = 政策文档块数 + 总述/说明块数；抽查跨商品边界块 `sku_codes` 长度 ≥2（与块文本实际含两商品对照）；构建日志出现多商品块占比
- **A4** 注入幂等：同一 docx 重灌两次 sku_codes 全等
- **A5** 无标注文件不回归：txt/md/政策 docx 入库 sku_codes 空、检索行为不变（test_smoke/test_indexing 既有断言绿）
- **A6** chunk 文本中可见 `(SKU:...)`（clean_text 保留性）且可被检索命中（编码类 query 召回提升为附带收益，不设硬指标）

### 5.2 阶段 D（返回与输出）

- **A7** `_to_doc`/BM25 dict 含 `sku_codes`+`chapter`（test_bm25_retriever 键断言同步）
- **A8** `render_doc_blocks` 单测：有 sku 块/空 sku 块（"—（无商品归属…）"）/无 chapter 块三态输出正确；@tool 输出与该函数一致（test_rag_tool）
- **A9** customer_tools 上下文含前缀行（records.result 断言）；summarize 回答不含 `(SKU:` 编码（端到端抽查 1 query）
- **A10**（D-2）`product_stock_lookup(sku="JD-LCK-001")` 单值精确命中；sku 提供时名称参数非法不影响（精确短路）；无 sku 时原模糊行为回归全绿（test_product_stock_tool）

### 5.3 一致性抽查（人工，端到端）

以"米家智能晾衣机2 现在多少钱"类 query 跑一次：RAG 命中块前缀含商品编码 → 以 sku 调 product_stock_lookup → 返回行 sku 与块一致、商品名完全匹配 → summarize 回答商品名不带编码、价格来自 DB（不编造）。

## 6. 分阶段实施步骤

1. P0：build 脚本标注 → 重建 docx → 抽查 A1
2. C：model 加列（建表/迁移）→ doc_parser → indexing_service（spans/_collect_skus/统计）→ 清理旧行 + 重灌 → A2~A6
3. D：_to_doc/BM25 扩列 → render_doc_blocks → rag_tool/customer_tools 换渲染 → docstring + summarize 规则 → A7~A9
4. D-2（确认后）：product_stock_lookup sku 参数 → A10
5. 按 CLAUDE.md §6 更新本 spec 归档状态 → 归档 `已完成/`；SPEC_SKU_ALIGNMENT §6.2 阶段 B/C/D 完成状态同步标注
6. git 提交推送

## 7. 待确认事项

| # | 事项 | 默认/建议 | 说明 |
|---|---|---|---|
| 1 | 前缀行字段集合与文案（§4.3.2） | `商品编码/知识类型/来源` 三字段 | 若需商品名称也进前缀（而非文本自读）需元数据另存名称列（D2 反悔项,成本↑） |
| 2 | docx 标注括号格式 | ` (SKU:JD-XXX-NNN)` | 与 D9 锚点一致；若改全角括号需同步锚点正则 |
| 3 | D-2（product_stock_lookup sku 参数）本期是否包含 | 包含（闭环必需,改动小） | 若另有 product_tool 专项 spec 计划可裁剪,本 spec 降级为纯 rag 侧 |
| 4 | 重灌时机 | P0+C 完成后一次性 | 依赖阶段 A 已入库（sku 列有值） |
| 5 | rerank 后 dict 键保留性验证结果（§8-6） | 实施期实测,若重建则 rerank 后 merge 元数据 | 影响 _to_doc 扩列是否够用 |

## 8. 风险与避坑清单

1. **单值注入陷阱**：严禁把 sku 挂到 `chapter` 或做"块首段 sku"——混合块后段商品身份丢失即不一致复发；_collect_skus 必须走区间（实施时以 A3 边界块用例钉死）
2. **rerank dict 重建**：`reranker.rerank` 返回若只保留 text/score 等已知键，sku_codes/chapter 会被静默丢弃——实施期先跑一次带日志断言（最终输出含元数据键），必要时 rerank 后按 chunk_id merge 回补
3. **旧行残留**：docx md5 变 → 上传 duplicate 短路不触发 → 直接重传会新旧双份共存（检索重复）。必须按 §4.2.4 先删旧行（source 匹配），勿依赖"自动覆盖"
4. **政策文档/通用块误用**：空 sku 前缀语义若缺失，LLM 可能拿政策块断言单一商品动态——前缀文案与 docstring 负向规则（§4.3.3）缺一不可；summarize 规则句同源
5. **两通道漂移**：@tool 与 customer_tools 若手写两份格式必漂移（历史教训:现@tool 有【来源】而节点无）——render_doc_blocks 单函数是硬约束,代码评审关注点
6. **JSONB 空值**：`[]` 与 NULL 并存会引入断言歧义——写入侧统一 `or None`(NULL),读取侧统一 `or []`,两处约定写进函数 docstring
7. **构建统计日志**为归档前置验证项（多商品块占比）落地,勿删;若占比 > 预期(如 >50%)记录到归档 spec 备注,供后续章节感知切分排期依据
8. **阶段 A 依赖**：本 spec 所有验收依赖 TSV sku 列与 DB sku unique（[[SPEC_SKU_ALIGNMENT]] A3~A5 通过）；未落地前开工 = P0 无值可标、C 无锚可提,直接拒收
