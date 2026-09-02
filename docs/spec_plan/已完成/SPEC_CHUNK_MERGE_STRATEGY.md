# 分块策略修订规格（全文直接切分 + 块首章节定位）
> **归档状态**: ✅ 已完成（2026-09-02 审计，依据 main 代码与 git 历史）
> 全文统一递归切分落地：7778c32（310→31 块，块归属=块首字符所在段），indexing_service.py spans/full_text 实现与 §8.2-a 自述一致；已知偏差 metadata start_index（spec 风险表已预述）。

> **用途**: 解决商品知识文档（docx）分块碎片化问题——当前"逐 Segment 独立切分"在章节粒度≈段落粒度的文档上完全失效，产出 72% 的 <50 字碎片块。方案：解析后用 `\n\n` 连接全文 → RecursiveCharacterTextSplitter(500/50) **一次性全文切分** → 每块按起始字符位置定位所属段获取章节归属（spec §4"块内首个非空字符所在章节"）。实测三方案对比后选定（合并缓冲改判，见 §10）
> **技术栈**: LangChain RecursiveCharacterTextSplitter 0.3.11 + FastAPI + pgvector
> **状态**: **待实施**（2026-08-23 修订：合并缓冲 → 全文直接切，实测数据支撑）
> **关联文档**: [[docs/superpowers/specs/2026-08-21-索引构建流程重构-mvp-design.md]] [[SPEC_REMOVE_QUERY_PREPROCESSING.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与实测数据](#2-现状链路与实测数据)
3. [方案设计](#3-方案设计)
4. [问题 2/3/4 处理决策](#4-问题-234-处理决策)
5. [影响面分析](#5-影响面分析)
6. [配置变更](#6-配置变更)
7. [文档与 spec 同步](#7-文档与-spec-同步)
8. [验证方案](#8-验证方案)
9. [存量数据重建](#9-存量数据重建)
10. [决策记录](#10-决策记录)
11. [风险与避坑清单](#11-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 2026-08-21 索引构建重构后，分块策略为"**逐 Segment 独立切分**"（`indexing_service.py` 循环内 `split_text` + `CHUNK_MIN_SIZE=5` 过滤），Segment 由 `doc_parser.py` 产出（md/docx 按标题切段、txt 整文件一段）
2. 商品知识文档（**京东智能家具产品知识文档.docx**，50 款商品/9 品类）结构特殊：**几乎所有段落都是 Heading 样式**，导致"每段一个章节"——章节粒度 ≈ 段落粒度
3. 该结构下"逐段切分"直接退化为"逐段成块"：切分器（≥500 字才切）完全不触发，每个段（中位 33 字）直接成为独立 chunk
4. 碎片块导致：embedding 上下文不足（41 字向量无法承载语义）、向量库膨胀（310 块 vs 实际约 31 块）、检索召回碎片化

### 1.2 目标

| 目标 | 量化指标（实测基线） |
|---|---|
| 消除碎片块 | 商品 docx：<50 字块占比 72% → **0%**；块均长 41 → **≈450 字** |
| chunk 数收敛 | 310 → **31**（-90%） |
| 检索信息不丢 | 标题行（含产品名）保留进正文；每块归属=块首字符所在章节（可溯源） |
| 简单性 | **无新增配置、无分块前处理辅助机制**——只复用框架 splitter 一次全文切分 + 字符定位 |
| 兼容性 | chunk_id 生成规则、CHUNK_MIN_SIZE 过滤、嵌入/事务链路、检索侧字段全部不变 |
| 文档一致 | doc_parser docstring 与 spec §4 归属规则与实际实现对齐（消除死规则描述） |

### 1.3 设计原则

1. **复用框架能力，不加自造机制**：切分用 RecursiveCharacterTextSplitter 的 `split_documents`（自带字符位置元数据）；不引入合并缓冲、自定义分块器
2. **归属规则 = spec §4 原文语义**：块归属"块内首个非空字符所在段"的章节——起点落在段间 `\n\n` 空位时顺延到下一段（空位字符视为空）
3. **零结构性改动**：不动 `doc_parser.py` 的 Segment 产出、不动 splitter 参数、不动嵌入/入库
4. **确定性保持**：全文拼接与切分无随机、无时间依赖 → chunk_id `{user}_{md5}_{index:04d}` 仍确定

---

## 2. 现状链路与实测数据

### 2.1 现状分块链路

```
doc_parser.py → Segment(text, chapter) 列表
  ├─ md / MinerU-PDF:  # 标题切段（标题保留进正文），章节栈 " > " join
  ├─ docx:            Heading 样式切段；表格整行 " | "拼接 append 进当前段
  └─ txt:             整个文件 = 1 个 Segment（无章节概念）

indexing_service.py:
  for seg in segments:
      text = clean_text(seg.text)
      for c in splitter.split_text(text):      # 逐段独立切分 ← 问题根源
          if len(c.strip()) >= CHUNK_MIN_SIZE:  # 5 字符
              chunks.append(c)                  # → 310 个 5~257 字碎片
```

### 2.2 实测数据（京东智能家具产品知识文档.docx，2026-08-23 实测）

**Segment 层**：310 个，长度 min=5 / max=257 / **median=33**，**唯一章节数 310**（章节粒度完全等于段落粒度）

**三方案对比**（同一文档、同一 splitter 参数，含跨章统计）：

| 方案 | chunk 数 | 均长 | <50 | 50-200 | 200-400 | 400+ | 跨章块 |
|---|---|---|---|---|---|---|---|
| 现状（逐段切） | 310 | 41 | 222 | 83 | 5 | 0 | 0（每块1章但全是碎片） |
| 合并缓冲 @400 | 35 | 387 | 0 | 2 | 11 | 22 | — |
| **全文直接切** | **31** | **450** | **0** | **1** | **3** | **27** | **31/31（100%）** |

**关键发现**（决定方案选择）：

1. **全文切块边界在段间**：`\n\n` 是 splitter 最高优先级分隔符，块边界绝大部分落在段与段之间——块纯度与"合并缓冲"同档，不存在"边界落在段中间"的担忧
2. **跨章不可避免**：章节粒度=段落粒度，任何 500 字级块必然横跨约 3~18 章——**两种方案都无法避免**，这是文档形态本性，不是方案能解决的
3. **分布全文切更优**：31 块/均长 450 vs 合并缓冲 35/387——全文切让 splitter 自由寻找最优切点（无缓冲阈值强行截断），overlap 浪费更少
4. **归属精度全文切更准**：合并缓冲的组级归属让组内第 2+ 块引用组首章节（轻微失准）；全文切逐块按字符定位，每块归属自己的块首段
5. **实现全文切反而更简单**：无需合并阈值、无需新增配置项、无需两阶段控制流

---

## 3. 方案设计

### 3.1 分块阶段（indexing_service.py）

```python
# 5. 清洗（不变）6. 分块:全文 \n\n 连接 → split_documents 一次切分 → 字符定位归属
clean_segments = []          # [(text, chapter)]
for seg in segments:
    text = clean_text(seg.text) if settings.TEXT_CLEAN_ENABLED else seg.text.strip()
    if text:
        clean_segments.append((text, seg.chapter))
if not clean_segments:
    return self._fail("empty_file", "清洗分块后无内容")

# 段字符轴:每段起始/结束字符位置(含 \n\n 连接符),用于块归属定位
spans, cap = [], 0
for text, chapter in clean_segments:
    spans.append((cap, cap + len(text), chapter))
    cap += len(text) + 2                    # +2 = "\n\n" 连接符
full_text = "\n\n".join(t for t, _ in clean_segments)

from langchain_core.documents import Document
docs = self.text_splitter.split_documents([Document(page_content=full_text)])

chunks, chapters = [], []
for d in docs:
    content = d.page_content
    if len(content.strip()) < settings.CHUNK_MIN_SIZE:
        continue
    start = self._locate_chapter(spans, d.metadata["loc"]["start"])   # 块首字符定位
    chunks.append(content)
    chapters.append(start)
if not chunks:
    return self._fail("empty_file", "清洗分块后无内容")
```

```python
def _locate_chapter(spans, pos) -> str:
    """块内首个非空字符所在段 → 章节;起点落在段间空位时顺延到下一段。"""
    for start, end, chapter in spans:
        if pos < end:
            return chapter
    return ""
```

要点：
1. **全部文本流一次切分**——splitter 自主选择最优切点（优先 `\n\n` 段边界、次 `\n`/中英文句号），块大小自然收敛到 450~500，尾块 ≤500 为全文字符流的自然结尾
2. **归属定位 O(1) 语义**：块起点落在段间 `\n\n` 空位 → 顺延取下一段章节（空位字符=空，符合"首个**非空**字符"）；找 `pos < end`（起点落在段间空位时恰好跳过上一段、命中下一段——因为上一段 end = 自己 start+len，段间空位 start+len ≤ pos < 下段 end）
3. **标题可检索性**：标题行保留进正文不受影响，仍作为块首/中段内容存在
4. **chunk_id 确定性**：切分顺序确定、`enumerate` 递增不变

### 3.2 边界情况

| 场景 | 行为 |
|---|---|
| 单段 >500 字（长段落/大表格） | 段内按 `\n`/中文标点切分（框架行为），块仍归属该段章节——与现状一致 |
| 块起点恰在段间连接符上 | `_locate_chapter` 顺延到下一段（spec §4 精确语义） |
| 文件末尾不足 5 字残块 | `CHUNK_MIN_SIZE` 过滤（现状同） |
| 全文无标题（txt 纯文本） | 单 Segment → 全文流切分与"全文切"语义天然一致；chapter 为空串 |
| 块横跨多章（不可避免） | 归属=块首字符章节，块内其余章节内容自然承载（产品名/属性标题混在块内，标题行可检索） |

---

## 4. 问题 2/3/4 处理决策

### 4.1 问题 2（docx 表格结构）—— **不单独处理**

1. 表格行是 `"属性 | 值"` 整行文本、行间 `\n` 分隔——块边界只落在"行与行"之间，**行内结构从不在块内被切断**
2. 表格行与相邻段并入同一块（块起点处通常是产品标题），表头行贴近标题，语义可检索
3. 自定义"表格感知分块器"需替换框架递归行为——成本高、破坏标准行为，不做

### 4.2 问题 3（txt 无章节）—— **小改动**

`doc_parser.py::parse_text_file` 中 `ext == "md"` 条件放开：txt 内 `#` 标题同样切段获得章节路径；无标题 txt 退化为单段（行为不变）。**不做**通用编号标题检测（误判风险）。

### 4.3 问题 4（文档与实现不匹配）—— **随方案同步修订**

- `doc_parser.py` 模块 docstring：改为准确描述"解析产出段落级 Segment；分块阶段全文切分，块归属块内首个非空字符所在段（见 spec §4）"
- `docs/superpowers/specs/2026-08-21-索引构建流程重构-mvp-design.md` 分块段落同步补一句全文切分实现说明

---

## 5. 影响面分析

| 文件 | 改动 | 说明 |
|---|---|---|
| `llm_backend/app/services/indexing_service.py` | 分块循环重写（§3.1）：全文 `\n\n` 连接 + `split_documents` + `_locate_chapter` + 新增 `from langchain_core.documents import Document` 导入 | 核心；嵌入/入库/原子事务不改 |
| `llm_backend/app/services/doc_parser.py` | ① txt 走标题切段（§4.2） ② docstring 修订（§4.3） | 各 2~3 行 |
| `llm_backend/tests/test_indexing.py` | `test_success_with_metadata` fixture 扩充（扩至 >700 字）+ 断言更新 | 现 fixture 仅 40 字，全文切后单块、"SF-2000 归属"断言失效；扩内容后应真实测"跨段块归属=块首段" |
| spec 文档 | 本 spec + §4.3 同步条目 | — |
| **配置** | **无新增**（合并缓冲方案已淘汰，CHUNK_MERGE_TARGET 不再需要） | 零配置变更 |
| `embedding_provider`、`rag_retriever_service`、`bm25_sql_retriever`、semantic cache、RRF | **零改动** | chunk 字段不变 |

**不受影响但需知晓**：`tests/test_parser.py`（Segment 层测试不进索引循环；`test_md_cross_chapter_chunk_ownership` 测的是章节栈，语义保留）。

---

## 6. 配置变更

**无。** 沿用现有 `CHUNK_SIZE=500 / CHUNK_OVERLAP=50 / CHUNK_MIN_SIZE=5`，不新增配置项（简单性原则）。

---

## 7. 文档与 spec 同步

1. `doc_parser.py` docstring（§4.3）
2. `docs/superpowers/specs/2026-08-21-索引构建流程重构-mvp-design.md` §4 分块段落
3. 本 spec 落地后，将 §9 存量重建步骤回填到 `docs/索引链路核查问题清单.md` 或知识数据 README（如存在）

---

## 8. 验证方案

### 8.1 单元测试（必过）

```bash
cd llm_backend && python -m pytest tests/test_indexing.py tests/test_parser.py -v
```

`test_success_with_metadata` 断言矩阵（fixture 扩至 ~800 字、多段 md）：

| 断言 | 语义 |
|---|---|
| `result["chunks"] == N`（与全文切实测一致的确定值） | 全文切分生效 |
| 含指定产品名的块 `chapter` == 块首段章节 | 归属定位准确（含"块首=空位顺延"边界用例） |
| `chunk_id` 连续 `_0000.._00NN` | 确定性保持 |
| 所有块 `len ≥ CHUNK_MIN_SIZE` | 过滤不变 |

### 8.2 真实文档统计复跑（不依赖嵌入/DB）

对 `knowledge_data/product_knowledge_docx/京东智能家具产品知识文档.docx`（monkeypatch 掉 `embed_in_batches` 或仅调 `_locate_chapter` 链路）断言：chunk 数 25~40、均值 ≥400、<50 字块 = 0（复制 2026-08-23 实测脚本）。

### 8.2-a 语义完整性检查（2026-08-23 已完成，生产实现实测）

对真实商品文档用生产实现（`IndexingService` 的 splitter + `_locate_chapter`）实测：

| 指标 | 结果 |
|---|---|
| chunk 数 / 均长 / 最短 | 31 / 450 字 / 154 字（最短为文件尾部自然余量） |
| 碎片块（<100 字） | 0 |
| 块首落点 | **31/31（100%）在段首**——无任何块从段中间开始 |
| 块尾后相邻原文字符 | 30 块=段边界（`\n\n`）、1 块=文件自然结尾 → **块止于语义边界 100%** |
| 块首字符 | 全部为完整段首字（规格/功能/参考等标题行首字），无标点/半截文字开头 |

结论：每个块 = 若干**完整段落**组成的语义单元，段落（文档语义最小单元）从不被切断。此方案下块边界质量与"逐块语义完整"均达标。

### 8.3 存量重建后抽查（手工）

删除旧文档 + 重跑 ingest 后，随机检索 2~3 个产品问题（如"芝华仕 50611B 的参数"）：召回块含完整产品段落、`chapter` 可溯源。

---

## 9. 存量数据重建

已入库文档（md5 相同）在 ingest 幂等逻辑下命中 `duplicate` 直接跳过，**不会自动重建**。重建步骤（手工，一次性；API 契约：`GET /api/documents?user_id=` 查列表、`DELETE /api/documents/{md5}?user_id=` 单文件级联删，无全量删除接口）：

```bash
# 1. 查出现存文档 md5 列表
curl "http://localhost:8000/api/documents?user_id=1"
# 2. 逐个删除(级联删 chunks)
curl -X DELETE "http://localhost:8000/api/documents/<md5>?user_id=1"
# 3. 重跑导入(重新全文切分+嵌入+入库)
cd llm_backend && python -m scripts.ingest_knowledge knowledge_data 1
```

> 一键重建选项（`ingest_knowledge.py --rebuild`）本期不做——低频手工操作，YAGNI。

---

## 10. 决策记录

| 决策 | 依据 | 日期 |
|---|---|---|
| 分块边界不用章节（实测否决"按章节合并"） | 商品文档 310 段 = 310 唯一章节，章节粒度≈段落粒度 | 2026-08-23 |
| 先落"合并缓冲@400"（曾写入初版 spec） | 阈值 400/500/600 模拟对比（35 块/均 387 最优） | 2026-08-23 |
| **改判：全文直接切**（后续深度实测修正） | 全文切 31 块/均长 450/零碎片，分布更优；block 边界同样落在段间；归属逐块准确；实现无新增配置更简单 | 2026-08-23 |
| 归属规则 = 块内首个非空字符所在段（spec §4） | 全文切下每块位置可精确映射；起点在 `\n\n` 空位时顺延下一段（空位=空字符） | 2026-08-23 |
| 表格问题不单独处理 | 行内结构不被切断（行间 `\n` 边界）；自定义表格分块器成本高 | 2026-08-23 |
| txt 仅识别 `#` 标题 | 低成本（2 行）；通用编号标题检测误判风险高 | 2026-08-23 |
| 不新增配置/不新增 --rebuild | 简单性原则：复用框架能力，不带入未验证的机制 | 2026-08-23 |

---

## 11. 风险与避坑清单

1. **测试 fixture 更新是必做项**：现 fixture 仅 40 字，全文切后必 fail（`chunks>=2` 与 SF-2000 归属断言都会崩）——先改测试再改实现（TDD 顺序）
2. **`split_documents` 元数据字段确认**：0.3.11 实测 `d.metadata["loc"]["start"]`（`split_documents` 自动注入；若实现时字段名变化（`start_index`），以实测为准）——实施第一步先打印元数据确认
3. **字符轴与真实拼接必须严格一致**：`spans` 的 `+2`（`\n\n`）与 `"\n\n".join(...)` 必须同步——建议共用同一构建函数或用 `join` 前逐段累计，防止偏移 bug（测试断言"块首章节"时该 bug 立即可见）
4. **chunk_id 与存量冲突**：同 md5 重传命中 duplicate，旧碎片块不自动更新——须先删除旧文档（§9）
5. **阈值是字符不是 token**：中文 1 字 = 1 字符（与 chunk_size 同单位）
6. **`spans` 定位为顺序扫描**（段数 0~几千，块数几十）：性能 O(块×段) 完全够用；不做二分（复杂度不值）
7. **不实现语义分块/SemanticChunker**：破坏 500 字确定性与 chunk_id 确定性（RRF 去重键依赖），本期明确不做
8. **跨章块的归属语义**：章节是"块首章节"而非"块内所有章节"——检索展示/溯源按块首章节展示即可，勿在实现中尝试"块内多章节聚合"（过度设计）
