# SPEC：文档停用后禁止被检索（检索入口状态过滤 + 文档身份统一）

> **归档状态**：⏳ 待实施（2026-09-27 定稿）
> 修订历程：初稿 → 两轮对抗性审计（一验事实、一挑漏洞）→ 修正 1 项阻断 + 2 项高危 + 8 项事实错误 → **Q2 已用户确认处置（D11 / §4.10）**。剩余待确认项：Q1、Q3~Q7。
>
> **关联文档**：`docs/项目问题.md` #17a（本条要修掉的已知限制）、`docs/spec_plan/已完成/SPEC_ADMIN_CONSOLE.md` §4.4 正文（第 314 行，当时裁定"不影响检索"的原文）与 §12 风险第 5 条（第 2743 行）、`frontend/src/admin/views/KnowledgeView.vue:85`（tooltip 出处）
>
> **修订记录**：初稿经两个独立 agent 对抗性审计（一验事实、一挑漏洞），修正 1 项阻断级错误（§4.7 迁移语句）、2 项高危（§5 A1 断言空过、语义缓存漏审）、8 项事实错误。审计结论已并入正文。

---

## 1. 背景与问题

### 1.1 现象

管理端把文档状态改为「停用」后，智能客服**仍然检索得到**该文档的内容。

### 1.2 病根：同一份「知识文档」存在三套身份，代码各处混用

| 位置 | 现在按什么认「同一份文档」 |
|---|---|
| 数据库唯一约束 `uq_documents_user_md5` | 上传者 + 文件指纹 `(user_id, md5)` |
| 索引写入 `indexing_service` 第 3 步查重（`:146-154`） | 上传者 + 文件指纹 |
| 索引写入 `indexing_service` 单事务写入（`:241-263`） | 上传者 + 文件指纹 |
| commit 后处理 `_finalize`（`:251-255`） | 上传者 + 文件指纹 |
| stage 重复检查（`:156-159`） | 上传者 + 文件指纹 |
| 管理端列表（前端 `:key="row.md5"`，`KnowledgeView.vue:92`） | 文件指纹 `md5` |
| 删除 `DELETE /{md5}`（`:320-330`） | 文件指纹 `md5` |
| **编辑/停用 `PATCH /{md5}`（`:296-317`）** | 路径按 `md5`，**实际只改 `rows[0]`** ← 四不像 |

这个混用产生三个后果：

1. **停用停不干净**（本 spec 要修的核心问题）—— `PATCH` 落到具体某一行，其余行不受影响
2. **描述被连带改**（副作用 A）—— 一个"文件级"的操作落到具体行上
3. **列表可能渲染重复行**（副作用 B）—— 后端可能返回两行同 `md5`，前端 `:key="md5"` 撞 key

### 1.3 为什么现在必须选一个身份

检索是**全局的**（两条 SQL 都没有 `user_id` 条件：`rag_retriever_service.py:84-88` 无 WHERE，`bm25_sql_retriever.py:46` 只有 tsv 条件）、列表是**全平台的**（`list_knowledge`（`:88-94`）不接收 `user_id`）、删除是**按 md5 全删的**。三处都按"文件级"工作，只有"停用"被做成了按行。**不统一身份，核心问题在多管理员场景下必然重现。**

### 1.4 决定：统一到「文件级（md5）」

**一份知识文档 = 一个文件指纹 `md5`。**

依据（均为实测/原文取证）：

1. 系统**几乎已经是**文件级的：删除按 md5 全删、前端列表 `:key="row.md5"`、检索不分归属、列表**连「归属」列都不显示**（`owner_id` 接口返回了但前端 7 列里没有它）
2. 唯一的上传入口是管理端（`app/api/__init__.py` 只挂 auth + admin；`main.py:387` 是唯一上传路由 `/api/upload/image`），客户端知识库已下线
3. **代码自己就把"多归属"当成污染**：`scripts/ingest_knowledge.py:26-27` 原文
   > `# 知识库归属账号(admin_test)。注意:重跑本脚本的查重键是 (user_id, md5),若归属与既有数据不一致,不会命中去重而是重复入库,污染检索索引。`
4. 删除路径刻意不做级联删商品（`api/admin/products.py:116/149`），说明"静态知识块"被当**全平台共享资产**维护

> **⚠️ 与 1.4 相反的证据（必须知道）**：`document_chunks.user_id` **不是**纯历史遗留，它在三处是有效的：
> ① `chunk_id = {user_id}_{md5}_{index:04d}` 的组成部分（本次不动）；
> ② `evaluation/testset_builder.py:37` 用它选评测语料；
> ③ `evaluation/__main__.py:37` 的 `--user`（默认 `"1"`）是显式入参。
>
> **D5 会切断"同一内容挂两个 user_id"的能力**，原评测入库流程（`ingest_knowledge <dir> 1` → `--user 1`）因此失效 —— 这是本方案初稿认定的**唯一实质代价**。
>
> **✅ 已处置（D11 / §4.10）**：改为"评测语料默认读全库、`--user` 降级为可选收窄"。实测（§2.8）发现该流程本身就在污染指标（为评测再挂副本会让检索 top-8 出现 2 条完全重复条目），且默认值 `"1"` 今天已经是坏的。**换言之 D5 的代价已消除，还顺带修好一个既有 bug 与一处指标偏差。**

---

## 2. 实测取证

> 全部在真实库（`smartcs_agent`）上执行，写操作均在事务内完成并 `ROLLBACK`，生产数据未改动。

### 2.1 库现状（2026-09-27）

```
documents:       2 行（id=365 md5=0613837b… 38 块；id=735 md5=7099bb2d… 4 块）
                 均 owner='6'，md5 各不相同，status 均 'enabled'
document_chunks: 42 行，md5 全部非空，owner 全部 '6'
孤儿块（无对应 documents 行）: 0
多行同 md5: 0
users: 3=test_user(user) 4=浏览器测试(user) 5=blowt(user) 6=admin_test(admin)
pgvector 版本: 0.8.6
```

**结论**：join 键 `(user_id, md5)` 数据完整；迁移零风险。

### 2.2 多行同 md5 可复现（核心证据）

```
[1] 库现状：md5 M 已被管理员 6 上传 → documents 有 (365, '6', M)
[2] 管理员 7 走 stage 重复检查（knowledge.py:156-159 原样查询）
    查到行: []  → duplicate = False          ← 判定"非重复"
[3] commit 走到 indexing_service:248 的 await s.flush()
    （documents 行的 INSERT 由这次 flush 发出；chunks 在 258 行 add_all、259 行 commit）
    INSERT 成功，新行 id = 1009              ← 约束 (user_id,md5) 对 (7,M) 不冲突
[4] 结果：同一 md5 两行并存
    (365,  '6', M)
    (1009, '7', M)
```

全链路**无任何报错或告警**。

### 2.3 过滤器 SQL 端到端验证（核心验收）

把 §4.1 / §4.2 的目标 SQL 在真实数据上跑通，中途停用文档 `0613837b`（38 块），查询词 `"晾衣机 价格保护"`：

| 状态 | BM25 命中分布 | 向量路（limit 50） |
|---|---|---|
| 启用中 | `0613837b: 9 块` + `7099bb2d: 1 块` | 42 块（两个文档都在） |
| **停用 `0613837b`** | **只剩 `7099bb2d: 1 块`** | **只剩 4 块，仅 `7099bb2d`** |
| 回滚后 | 恢复原状 | 恢复原状 |

（BM25 结果被 `BM25_TOP_K` 截断到 10 条，故 "9 块" 并非该文档在库中的全部块数，而是命中并进入 top-10 的数量。）

**结论**：过滤器按预期工作；停用后该文档的块在两条检索路上均被完全排除。

### 2.4 ORM 真实编译产物（确认形态）

由 `str(stmt.compile(dialect=postgresql.dialect()))` 得到（**非手写**）：

```sql
SELECT document_chunks.id, document_chunks.source, document_chunks.file_path,
       document_chunks.user_id, document_chunks.chunk_index, document_chunks.chunk_id,
       document_chunks.md5, document_chunks.file_type, document_chunks.page,
       document_chunks.chapter, document_chunks.sku_codes, document_chunks.content,
       document_chunks.embedding, document_chunks.content_tsv, document_chunks.created_at,
       document_chunks.embedding <=> %(embedding_1)s AS distance
FROM document_chunks
JOIN documents ON documents.user_id = document_chunks.user_id
              AND documents.md5 = document_chunks.md5
WHERE documents.status = %(status_1)s
ORDER BY document_chunks.embedding <=> %(embedding_1)s
 LIMIT %(param_1)s
```

形态正确：join 条件、状态过滤、`ORDER BY distance` 均在位；`SELECT` 只取 `document_chunks` 的列，无歧义。

> 注：SQLAlchemy 的生成式方法链**顺序不影响渲染位置** —— `.join()` 写在 `.order_by()` 之后编译结果完全相同（已实测）。初稿称"`.join()` 必须紧接 `select()`，否则非法"是**错的**，已删除。

### 2.5 EXPLAIN 查询计划

```
A. 现状（无 join）: Limit → Sort → Seq Scan on document_chunks
B. 加 join + 过滤: Limit → Sort → Hash Join
                     ├─ Seq Scan on document_chunks (42 rows)
                     └─ Hash → Seq Scan on documents (Filter: status='enabled')
```

**当前 42 行数据下，HNSW 索引在 A、B 两种计划里都没有被使用**（数据量太小，Seq Scan + Sort 成本更低）。因此：

- ✅ 本改动**不会**造成索引回退（因为本来就没用上）
- ⚠️ 但这也意味着**本次 EXPLAIN 无法证明大规模下的行为**——见 §7 R5

### 2.6 既有 spec 原文（证明当时已预见，但推演停止）

`docs/spec_plan/已完成/SPEC_ADMIN_CONSOLE.md:1094`：

> 「同 md5 多归属的处理：`documents` 的唯一约束是 `(user_id, md5)`，**理论上同 md5 可挂多个 user_id**。本接口若命中多行，**只更新 `id` 最小的那一行**…当前库中 md5 无重复，**此分支不会触发**，但代码不能因为 `scalar_one_or_none()` 抛 `MultipleResultsFound` 而 500。」

当时把它当成"防 500"的防御性编程，**没有**推到"停用还灵不灵"。`rows[0]` 就是本 bug 的直接来源。

### 2.7 约束的真实类型（迁移方案的决定性事实）

```
pg_constraint:  ('uq_documents_user_md5', 'u', 'UNIQUE (user_id, md5)')
pg_indexes:     ['documents_pkey', 'uq_documents_user_md5', 'ix_documents_user_id', 'ix_documents_id']
```

`uq_documents_user_md5` 在 `documents` 上**同时是唯一约束（`contype='u'`）和它拥有的索引**。因此：

```
DROP INDEX IF EXISTS uq_documents_user_md5
→ psycopg.errors.DependentObjectsStillExist:
  cannot drop index uq_documents_user_md5 because constraint
  uq_documents_user_md5 on table documents requires it
  HINT: You can drop constraint uq_documents_user_md5 on table documents instead.
```

`IF EXISTS` 只抑制"对象不存在"，**不抑制"有依赖对象"**。正确写法见 §4.7。

### 2.8 评测语料链路（Q2 的取证）

**`--user` 只用在两处**（grep 整个 `evaluation/` 确认）：`testset_builder.py:37` 的语料筛选、`__main__.py:129` 的报告元数据。**`runner.py` 完全不用 user_id** —— 它跑的是真实子图，检索是全局的。所以 `--user` 筛的是「拿哪些块去出题」，**不是「检索范围」**。

**今天的状态（实测）**：

```
库内分块: user 1 = 0 块, user 6 = 42 块
→ `python -m evaluation`（默认 --user 1）今天报「语料库为空」
```

**历史报告记的 `user_id`**：

| 报告 | user_id |
|---|---|
| `evaluation/results/ragas_20260824_160645.json` | `"5"` |
| `evaluation/results/ragas_20260824_171355.json` | `"5"` |
| `evaluation/results/ragas_20260824_173259.json` | `"5"` |

而 `docs/项目问题.md:89` 记着「数据迁移：`documents` 2 行 + `document_chunks` 42 行 **`user_id` 1→6**」。串起来：**语料主人换过 1 → 5 → 6，而 `--user` 的默认值一直停在 `"1"`** —— 默认值今天就是坏的。

**"再挂一份副本"这一步本身就在污染指标（实测）**：把 owner 6 的 38 块复制一份挂到 user 1 后，同一向量查询的 top-8：

| | top-8 内完全重复的条目 |
|---|---|
| 现状（只有 owner 6） | **0 条** |
| 双挂后 | **2 条**（内容逐字相同） |

原因：RRF 按 `chunk_id` 去重，而 `chunk_id = {user_id}_{md5}_{i}` —— 两个 owner 的副本 `chunk_id` 不同，去重挡不住，两条一起进精排，**白占 top-K 名额、压低 `context_precision`**。这恰好违背 `SPEC_RAGAS_EVAL.md:51` 的原始设计原则（「语料复用生产分块…保证 reference_contexts 在生产检索中找得到」）。

---

## 3. 决策表

| # | 决策 | 取舍与依据 |
|---|---|---|
| **D1** | 检索过滤放在**两处 SQL 里**（向量路 + BM25 路），不做 Python 侧后置过滤 | 后置过滤会让被停用文档白占 ANN/BM25 的 top-K 名额，静默缩小候选池。SQL 过滤让候选池从一开始就干净 |
| **D2** | 过滤语义为**白名单**：`documents.status = 'enabled'` | 孤儿块 / `md5` 为空的块一律排除（用户已确认「宁可漏不可错」）。未知 status 值也被排除，比黑名单安全 |
| **D3** | join 键用 `(user_id, md5)`，不用裸 `md5` | `uq_documents_user_md5` 保证该组合唯一 → join 不产生重复行；且**迁移前后都正确**，让发布顺序自由。代价见 §7 R4（`user_id` 漂移会让块静默消失，但两条写入路径都用同一个 `user_id` 变量写两张表，漂移只能来自手工 SQL） |
| **D4** | 文档身份统一到 **md5（文件级）** | §1.4 |
| **D5** | 唯一约束 `uq_documents_user_md5` → `uq_documents_md5`，**并迁移数据库** | 不改约束则多行同 md5 仍可产生，副作用 A/B 只是概率降低而非消除。初稿认定的唯一实质代价（打断评测入库流程）**已由 D11 消除** —— 见 §1.4 反面证据框与 §7 R7 |
| **D6** | `PATCH /{md5}` 改为**全量更新该 md5 的所有行** | 这是"停用生效"的直接保证。即使 D5 之后通常只有一行，保留循环让接口语义与"文件级"一致 |
| **D7** | `description` **随 status 一起**变成文件级 | 在文件级身份下这是**正确**的，不是副作用——一份文件只有一个描述。（曾考虑"描述按行、状态按文件"，但那是无意义的自相矛盾） |
| **D8** | `stage` 的重复检查改为**只查 md5**；随之删除 `user_id` 表单参数及前端传递链 | 改判 md5 后 `user_id` 在 `stage_knowledge` **函数体内再无任何用处**（已通读 126-173 行确认：仅 157 行使用一次）。留一个死参数正是本 bug 的同类病根 |
| **D9** | `documents.user_id` **保留**，降级为「谁最先上传的」溯源信息 | 删列要动 `documents` + 迁移 + `owner_id` 出参 + 测试，收益不成比例 |
| **D10** | 不做 RAG 结果的"停用"二次校验、不引入 `hnsw.iterative_scan` 等调参 | 当前数据量下无实测依据，属过早优化（见 §7 R5） |
| **D11** | 评测语料改为**默认读全库**（`user_id` 变可选收窄），并排除 `test_%` 账号 | 见 §4.10。依据：① 默认值 `"1"` 今天已是坏的（§2.8）；② "再挂一份副本"实测造成检索重复条目、压低 `context_precision`；③ 对 owner 变更免疫，不用再跟着数据迁移改数字 |

---

## 4. 改动清单

### 4.1 向量检索路 —— `llm_backend/app/services/rag_retriever_service.py`

**位置**：`_vector_search`（75-93 行）

**改前**（82-89 行，逐字）：

```python
        distance = DocumentChunk.embedding.cosine_distance(query_vec)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(DocumentChunk, distance.label("distance"))
                .order_by(distance)
                .limit(top_k)
            )
            rows = result.all()
```

**改后**：

```python
        distance = DocumentChunk.embedding.cosine_distance(query_vec)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(DocumentChunk, distance.label("distance"))
                .join(
                    Document,
                    and_(
                        Document.user_id == DocumentChunk.user_id,
                        Document.md5 == DocumentChunk.md5,
                    ),
                )
                .where(Document.status == "enabled")
                .order_by(distance)
                .limit(top_k)
            )
            rows = result.all()
```

**import 补充**（当前第 27 行 `from sqlalchemy import select`）：

```python
from sqlalchemy import and_, select

from app.models.document import Document
```

**docstring 同步**：模块顶部流程图（1-21 行）第 8/9 行的两处检索说明各补一句「均已过滤 `documents.status='enabled'`」；`_vector_search` 的 docstring 补「仅检索启用中文档的块」。

**禁止**：不要把 `select(DocumentChunk, ...)` 改成 `select("*")`。join 后两表都有 `id` / `md5` / `user_id` / `file_type` / `created_at`，只有显式限定 `DocumentChunk` 才能保证列不歧义。

---

### 4.2 BM25 检索路 —— `bm25_sql_retriever.py`

**位置**：`_OR_QUERY_SQL_TEXT`（33-50 行）

**改前**（作为 SQL 文本，与源文件字面量一致；源文件中 `\S` 写作 `'\\S'`）：

```sql
SELECT document_chunks.*,
       ts_rank_cd(document_chunks.content_tsv, tsq.q) AS bm25_score
FROM document_chunks,
     (SELECT to_tsquery('jiebacfg',
              string_agg(quote_literal(tok), '|' ORDER BY tok)) AS q
      FROM (
          SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', :query))) AS tok
          UNION
          SELECT unnest(tsvector_to_array(to_tsvector('jiebamp', :query))) AS tok
      ) t
      WHERE tok ~ '\S') tsq
WHERE document_chunks.content_tsv @@ tsq.q
ORDER BY bm25_score DESC, document_chunks.id
LIMIT :top_k
```

**改后**（沿用现有"逗号 FROM + WHERE 条件"的写法，改动最小）：

```sql
SELECT document_chunks.*,
       ts_rank_cd(document_chunks.content_tsv, tsq.q) AS bm25_score
FROM document_chunks, documents,
     (SELECT to_tsquery('jiebacfg',
              string_agg(quote_literal(tok), '|' ORDER BY tok)) AS q
      FROM (
          SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', :query))) AS tok
          UNION
          SELECT unnest(tsvector_to_array(to_tsvector('jiebamp', :query))) AS tok
      ) t
      WHERE tok ~ '\S') tsq
WHERE document_chunks.user_id = documents.user_id
  AND document_chunks.md5 = documents.md5
  AND documents.status = 'enabled'
  AND document_chunks.content_tsv @@ tsq.q
ORDER BY bm25_score DESC, document_chunks.id
LIMIT :top_k
```

**必须保持不变的三点**（否则会引入回归）：

1. `SELECT document_chunks.*` 的显式限定（同上，防列歧义）
2. Python 侧按列名取值的字段集（`search` 方法 76-92 行）：`id` / `chunk_id` / `source` / `file_path` / `user_id` / `chunk_index` / `sku_codes` / `chapter` / `content`（映射到 `"text"`）—— 本次不增删返回列
3. `ORDER BY bm25_score DESC, document_chunks.id` 的二级排序（保证同分时结果稳定，测试依赖）

**docstring 同步**：模块顶部"整体流程"（9-14 行）与 `search` 的 Returns（65 行）各补一句「仅检索 `documents.status='enabled'` 的文档块」。

---

### 4.3 `PATCH /api/admin/knowledge/{md5}` 全量更新

**位置**：`llm_backend/app/api/admin/knowledge.py` `update_knowledge`（296-317 行）

**改前**（310-317 行，逐字）：

```python
    doc = rows[0]
    # 必须用 exclude_unset 区分"没传"和"传了 null":写成 `if payload.x is not None`
    # 会让 description 永远回不到 NULL(存量行正是 NULL 状态,改一次就再也恢复不了)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    await db.flush()
    await db.refresh(doc)
    return _doc_row(doc)
```

**改后**：

```python
    # 必须用 exclude_unset 区分"没传"和"传了 null":写成 `if payload.x is not None`
    # 会让 description 永远回不到 NULL(存量行正是 NULL 状态,改一次就再也恢复不了)
    fields = payload.model_dump(exclude_unset=True)
    # 按 md5 全量更新(与 DELETE 同粒度):停用必须对同一文件的所有副本生效。
    # 唯一约束已改为 uq_documents_md5,正常情况下 rows 恒为 1 行;保留循环是为了
    # 接口语义与"文件级身份"一致 —— 命中多行时全部更新,而不是只改 id 最小的那行
    # (旧写法只改 rows[0],正是"停用后仍能检索到"这个 bug 的直接来源)。
    for doc in rows:
        for field, value in fields.items():
            setattr(doc, field, value)
    await db.flush()
    await db.refresh(rows[0])
    return _doc_row(rows[0])
```

同时**删除** 303 行的旧注释「同 md5 可能挂多个 user_id:只更新 id 最小的那一行,不能因 scalar_one_or_none 抛 500」——该注释描述的行为已不成立。

**响应结构不变**（仍返回第一行），前端无需改动。

---

### 4.4 `_finalize` 查文档改为按 md5

**位置**：同文件 `_finalize`（239-270 行），查询在 **251-255 行**

**改前**：

```python
        doc = (await s.execute(
            select(Document)
            .where(Document.user_id == user_id, Document.md5 == payload.md5)
            .order_by(Document.id)
        )).scalars().first()
```

**改后**：

```python
        doc = (await s.execute(
            select(Document)
            .where(Document.md5 == payload.md5)
            .order_by(Document.id)
        )).scalars().first()
```

**为什么必须改**：不改的话，管理员 B 上传已被管理员 A 收录的同一文件时，`process_file` 会因新约束抛 `IntegrityError` → 返回 `duplicate`，但这里按 `(B, md5)` 查不到行 → 走进 `if doc is None` 分支 → 前端收到 `{"error": "internal", "detail": "索引已执行但查不到对应文档记录"}` 的假错误。

**语义后果（有意为之，D7）**：重复提交时描述会写到文件那一行上，覆盖先上传者的描述。文件级身份下这是正确行为。

**`user_id` 参数移除（已验证安全）**：通读 239-270 行确认 `user_id` 只出现在签名（239 行）与 where 条件（253 行）两处，改后在本函数内再无用处 → 一并从签名移除，并同步更新**唯一调用点**（216 行）。

---

### 4.5 `stage` 重复检查改为按 md5（含 `user_id` 链清理）

**位置**：同文件 `stage_knowledge`（126-173 行），重复检查在 **156-159 行**

**改前**：

```python
    dup = (await db.execute(
        select(Document).where(Document.user_id == user_id, Document.md5 == md5_hex)
        .order_by(Document.id)
    )).scalars().first()
```

**改后**：

```python
    dup = (await db.execute(
        select(Document).where(Document.md5 == md5_hex).order_by(Document.id)
    )).scalars().first()
```

**为什么要改**：不改的话，重复检查问的是「**这个上传者**传过吗」，而产品想问的是「**平台里**有吗」。管理员 B 会看到 stage 说"新文件"、commit 又说"重复"的矛盾流程（且拿不到 `existing` 描述回填）。

**连带清理 `user_id`（D8）** —— 已通读 `stage_knowledge` 函数体确认该参数仅 157 行使用一次：

| 文件 | 位置 | 动作 |
|---|---|---|
| `llm_backend/app/api/admin/knowledge.py` | `stage_knowledge` 签名 129 行 `user_id: str = Form(...)` | 删除该参数 |
| 同上 | 第 14 行 `Form` 的 import | **删除** —— 全文 `Form(` 只出现在 129 行，删参数后成为死导入 |
| `frontend/src/admin/api.js` | `stageFile` 签名 **80 行**、`fd.append('user_id', userId)` **84 行** | 去掉 `userId` 参数与该 append |
| `frontend/src/admin/components/KnowledgeFormModal.vue` | `userId` prop **121 行**、传参 **215 行** | 删除 prop 与传参 |
| `frontend/src/admin/views/KnowledgeView.vue` | `meId` **155 行**、`:user-id="meId"` **127 行**、`getMe()` **265 行**、`getMe` import **137 行** | 全部删除（已确认 `getMe` 在本文件无其他用处；它在 `App.vue:116`、`AdminApp.vue:125` 另有使用，不受影响） |
| `llm_backend/tests/test_admin_knowledge.py` | `_stage` helper（**126-135 行**）、`data={"user_id": ...}` 在 **132 / 224 / 278 / 286 行** | 去掉该表单字段 |

> ⚠️ **落地前必须全项目复核**（初稿只写了 `frontend/src/admin/`，范围不足）：
> ```bash
> grep -rn "user_id" frontend/src/ llm_backend/scripts/ llm_backend/evaluation/ | grep -i "stage\|Form"
> grep -rn "userId\|meId\|getMe" frontend/src/
> ```
> 特别检查 `llm_backend/scripts/` 与 `llm_backend/evaluation/` —— 它们**不在** `frontend/src/admin/` 的搜索范围内。

---

### 4.6 唯一约束

**位置**：`llm_backend/app/models/document.py` 第 9 行

**改前**：

```python
    __table_args__ = (UniqueConstraint("user_id", "md5", name="uq_documents_user_md5"),)
```

**改后**：

```python
    # md5 全局唯一:一份知识文档 = 一个文件指纹(见 SPEC_DOCUMENT_STATUS_FILTER D4/D5)。
    # 旧约束 (user_id, md5) 只防"同一上传者重复提交",不防"平台里同一文件存两份",
    # 正是"停用后仍能检索到"的成因。
    __table_args__ = (UniqueConstraint("md5", name="uq_documents_md5"),)
```

**类 docstring 同步**（第 6 行）：「RAG 文件级记录表(索引链路原子写入的载体)」→ 补一句「md5 全平台唯一」。

---

### 4.7 数据库迁移 —— `llm_backend/scripts/init_db.py`

**位置**：第 49-54 行的唯一索引创建处

**改前**：

```python
            # 唯一约束幂等(存量 null 允许多值)
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_user_md5 ON documents (user_id, md5)"
            ))
```

**改后**（**注意：必须先 DROP CONSTRAINT，不能只 DROP INDEX** —— 见 §2.7 实测）：

```python
            # 唯一约束幂等(存量 null 允许多值)
            # 文档身份:由 (user_id, md5) 改为 md5 全局唯一(SPEC_DOCUMENT_STATUS_FILTER D5)。
            # ⚠️ 旧对象在 documents 上【同时是唯一约束和它拥有的索引】(contype='u'),
            # 只写 DROP INDEX 会报 DependentObjectsStillExist 并回滚整个 init_db 事务。
            # 两行都写:兼容"约束形态"与"裸索引形态"两种历史环境。
            await conn.execute(text(
                "ALTER TABLE documents DROP CONSTRAINT IF EXISTS uq_documents_user_md5"
            ))
            await conn.execute(text("DROP INDEX IF EXISTS uq_documents_user_md5"))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_md5 ON documents (md5)"
            ))
```

> **⚠️ 失败模式的严重性**：`init_db.py:25` 把所有语句包在**同一个 `engine.begin()` 事务**里。任何一条失败都会回滚它**之后的全部语句**（含 `uq_documents_md5`、`uq_document_chunks_chunk_id`、HNSW 索引、`content_tsv` 生成列、`users.role`、`documents.description/status`、`orders.signed_date`）。初稿的 `DROP INDEX` 写法正是这种情况，实测确认。

**落地前必做的数据前置检查**（幂等脚本无法自动兜底）：

```sql
SELECT md5, count(*) FROM documents GROUP BY md5 HAVING count(*) > 1;
```

- 返回空 → 可直接执行迁移
- 返回非空 → `CREATE UNIQUE INDEX` 会失败。需先人工裁决，见下方配方

**多行同 md5 的清理配方（初稿有严重错误，已修正）**：

> ❌ 初稿写「保留 `id` 最小者、删除其余行的 `documents` 记录**及其 `document_chunks`**」。
> **该配方会误删保留行的全部块**：`document_chunks` **只有 `md5` 列、没有行归属**，无法区分"哪一行的块"。按 md5 删 = 把该文件的**所有**块删光。而 §2.2 复现出的真实形态恰恰是"chunks 全部属于后插入的 (7, M)，先插入的 (6, M) 一个块都没有" —— 按初稿执行会得到"保留行零块、`chunk_count` 仍写 42"的静默坏数据。

✅ **正确配方**：**保留"拥有 chunks 的那一行"**（不是 id 最小者）。

```sql
-- 1) 对每个重复 md5,找出 chunks 实际归属的 user_id
SELECT dc.md5, dc.user_id AS chunk_owner, d.id, d.user_id AS doc_owner
FROM document_chunks dc
JOIN documents d ON d.md5 = dc.md5
WHERE dc.md5 IN (SELECT md5 FROM documents GROUP BY md5 HAVING count(*) > 1);

-- 2) 保留 chunk_owner 匹配的那一行;删除其余 documents 行(【不要】删 chunks)
-- 3) 若某 md5 的 chunks 归属在 documents 里没有对应行,则改写保留行的 user_id 去匹配 chunks
-- 4) 删完再跑一次前置检查 SQL,确认返回空集
```

**本库实测结果**：2 行 documents、2 个不同 md5、1 个 owner → **前置检查返回空集，零风险**。

---

### 4.8 `indexing_service` 第 3 步快速查重改为按 md5

**位置**：`llm_backend/app/services/indexing_service.py`（146-154 行）

**改前**：

```python
        # 3. 查重(快速路径;并发由唯一约束兜底)
        async with AsyncSessionLocal() as s:
            dup = await s.execute(
                select(Document.id).where(
                    Document.user_id == user_id, Document.md5 == md5_hex
                )
            )
```

**改后**：

```python
        # 3. 查重(快速路径;并发由唯一约束兜底)
        # 按 md5 全平台查重(SPEC_DOCUMENT_STATUS_FILTER D5):否则第二个管理员上传
        # 已被收录的文件时,这里查不到 → 白跑一遍 MinerU 解析 + 全部 embedding 调用
        # → 到最后 flush 才撞唯一约束返回 duplicate。PDF 的 MinerU 是付费外部服务。
        async with AsyncSessionLocal() as s:
            dup = await s.execute(
                select(Document.id).where(Document.md5 == md5_hex)
            )
```

**为什么必须改**：这是**成本问题**。不改的话，管理员 B 上传 A 已收录的 30MB PDF，会完整走完解析 + 嵌入（进度条到 95%）才被判重复，MinerU 调用费用白付。

**注**：`process_file` 的 `except IntegrityError` 分支行为**已验证正确** —— `flush()`（248 行）在 `add_all()`（258 行）**之前**，IntegrityError 在 documents 的 INSERT 就抛出，`DocumentChunk` 对象从未 add，事务随 `async with` 退出回滚，**chunks 不会被写进去**。

---

### 4.9 前端 tooltip 删除

**位置**：`frontend/src/admin/views/KnowledgeView.vue` 83-86 行

**改前**：

```html
            <th
              class="text-left font-medium px-4 py-3 whitespace-nowrap w-20"
              title="停用仅影响管理端展示，智能客服仍会检索到该文档（已知限制）"
            >状态</th>
```

**改后**：

```html
            <th class="text-left font-medium px-4 py-3 whitespace-nowrap w-20">状态</th>
```

**理由**：该提示是旧行为的说明，修完即**错误信息**——留着会让管理员以为停用无效而不敢用。属必须同步的关联改动，不是可选项。

---

### 4.10 评测语料读取改为默认读全库（D11）

**位置**：`llm_backend/evaluation/testset_builder.py` `load_corpus_documents`（24-59 行）

**改前**（35-48 行）：

```python
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.user_id == user_id)
            .order_by(DocumentChunk.id)
            .limit(max_docs)
        )
        chunks = (await session.execute(stmt)).scalars().all()

    if not chunks:
        raise RuntimeError(
            f"语料库为空：user_id='{user_id}' 无 document_chunks 记录"
            "（请先 python -m scripts.ingest_knowledge <目录> <user_id> 入库）"
        )
    logger.info("评测语料加载完成: {} 块（user_id={}）", len(chunks), user_id)
```

**改后**：

```python
        stmt = select(DocumentChunk)
        if user_id:
            stmt = stmt.where(DocumentChunk.user_id == user_id)
        else:
            # 默认读全库:知识库是全平台的,文档身份为 md5(SPEC_DOCUMENT_STATUS_FILTER D4)。
            # 排除 test_ 前缀账号 —— 它们是 pytest 夹具(conftest 的 test_user_id 约定),
            # 测试被强杀时会留下残留块;混进合成语料会生成出自测试夹具的评测题。
            stmt = stmt.where(~DocumentChunk.user_id.like("test_%"))
        stmt = stmt.order_by(DocumentChunk.id).limit(max_docs)
        chunks = (await session.execute(stmt)).scalars().all()

    if not chunks:
        raise RuntimeError(
            "语料库为空：document_chunks 无可用知识分块"
            "（请先经管理端上传知识文档;或显式传 --user <owner_id> 收窄语料）"
        )
    logger.info("评测语料加载完成: {} 块（user_id={}）", len(chunks), user_id or "全库")
```

**函数签名与 docstring 同步**（24-32 行）：

```python
async def load_corpus_documents(max_docs: int, user_id: str | None = None) -> List[Document]:
    """从 document_chunks 读取生产分块，包装为 ragas 合成器可用的 Document。

    Args:
        max_docs: 最多读取的分块数（控制合成器 docstore 建立成本，默认 RAGAS_MAX_CORPUS_DOCS）
        user_id: 可选的归属收窄。**不传 = 读全库**（排除 test_% 测试账号）。
                 旧语义是"必填的语料来源"，已废弃 —— 见 SPEC_DOCUMENT_STATUS_FILTER D11。

    Raises:
        RuntimeError: 无任何可用分块。
    """
```

**同文件第二处 `--user`** —— `evaluation/__main__.py:36-38`：

```python
    p.add_argument("--user", type=str, default=None,
                   help="可选:按知识归属 user_id 收窄评测语料(不传 = 读全库)")
```

**报告元数据** —— `evaluation/__main__.py:129`：

```python
            "user_id": args.user or "all",
```

（`args.user` 为 `None` 时记 `"all"`，避免报告里出现 `null` 且自描述更清楚。）

**为什么改**（完整取证见 §2.8）：

1. **默认值今天就是坏的** —— 数据早已从 user 1 迁到 6，`python -m evaluation` 直接报「语料库为空」
2. **原做法本身在污染指标** —— "为评测再挂一份副本"实测让检索 top-8 出现 2 条完全重复的条目，白占 top-K、压低 `context_precision`；去掉过滤恰好回到 `SPEC_RAGAS_EVAL.md:51` 想要的"语料复用生产分块"
3. **对 owner 变更免疫** —— 语料主人历史上换过 1→5→6，硬编码的默认值每次都失效

**遗留（本次不处理，登记为 R14）**：`max_docs` 仍按 `DocumentChunk.id` 截断，语料超过 `RAGAS_MAX_CORPUS_DOCS`（当前 100，实测语料 42 块未触发）后，后上传的文档出不了题。属既有缺陷，本次不加深。

---

## 5. 验收断言

> **⚠️ 通用防"空过"要求（初稿缺失，审计发现）**：
> ① 两条检索路的返回 dict **都没有 `md5` 键**（`_to_doc`（`rag_retriever_service.py:57-73`）与 BM25 的取值块（`bm25_sql_retriever.py:78-92`）字段集均为 `text/id/chunk_id/source/file_path/user_id/chunk_index/sku_codes/chapter`），且 §4.2 明确要求"本次不增删返回列"。**因此不能按 `d["md5"]` 断言** —— 那样写出来恒为真，过滤器删掉也照样通过。
> ② 两条路都有"静默返回空"的降级：`_vector_search` 在 embedding 全零时 `return []`（`:78-80`），`_safe()` 把任何异常吞成 `[]`（`:97-103`）。**每条路必须自带"启用时命中"的正向断言**，否则整路返回空时否定断言自动成立。

### A1 核心：停用后两条检索路都查不到（自动化）

```
前置：新建一个属于 test_user_id 的独立文档 D（不要用种子文档，见下方"独立文档"要求），
      其内容能被测试 query q 命中；确认 D 的 status='enabled'
步骤：1. 断言"启用时命中"（正向）：
         D 的块出现在结果中 —— 按 source（= 文件名）或 chunk_id 判定，不要用 md5
      2. UPDATE documents SET status='disabled' WHERE md5=D
      3. 断言"停用后不命中"（否定）：结果中不含 source/chunk_id 属于 D 的块
      4. 恢复 status='enabled'
      5. 断言结果恢复到步骤 1 的状态
还原：整个测试必须用 try/finally 保证恢复 status（见下）
```

**两条路分别断言**（不合并，否则无法定位是哪条路没过滤）：

- **A1a**：`BM25SQLRetriever().search(q, top_k)` —— 按 `r["source"]` 或 `r["chunk_id"]` 判定
- **A1b**：`RAGRetrieverService()._vector_search(q, top_k)` —— 同上

> **独立文档要求**：初稿的 A1 直接 `UPDATE` 生产种子文档（`documents.id=365, md5=0613837b…`，owner=`'6'`）。`conftest.cleanup_test_data` **按 `test_user_id` 清理，种子文档不在范围内** —— 测试中途崩溃会把种子文档长期留在停用态：线上客服真的检索不到，且 `test_admin_knowledge.py:210`（`item["status"] == "enabled"`）与 `:518`（`_db_state(md5)[1] == "enabled"`）连环失败。
> **必须**：① 为断言新建独立文档（经 `IndexingService.process_file` 写入，走 `test_user_id` 隔离）；② 若坚持用种子文档，必须 `try/finally` 恢复。

### A2 边界：孤儿块被排除（D2 白名单语义）

插入一个 `md5` 不在 `documents` 白名单内的块（或 `md5 IS NULL` 的块）→ 两条检索路均不返回它。
**注意**：这是**有意行为**，断言注释要写明"孤儿块应被排除"，防止后人误当 bug 修回去。

### A3 重复上传：第二个管理员不再新增行

```
前置：管理员 A 已上传文件 F（documents 行数 n）
步骤：管理员 B（不同 user_id 令牌）上传同一 F 并 commit
断言：1. commit 返回 done 且 document.duplicate == true
     2. documents 行数仍为 n（不新增）
     3. document_chunks 中 md5=F 的块数不变（不重复索引）
     4. 不出现 error 事件（防 §4.4 的假错误）
```

### A4 停用全量生效

构造两行同 md5 的状态 → `PATCH {description, status:'disabled'}` → 两行 `status` 均为 `'disabled'`。

> **⚠️ 构造方式（初稿写反了）**：旧对象是**约束**（`ALTER TABLE documents DROP CONSTRAINT uq_documents_user_md5`），新对象是**裸索引**（`DROP INDEX uq_documents_md5`）。要临时解除约束插入第二行，必须用 **`DROP INDEX`** 去掉新约束，插完再重建。初稿建议的 `DROP CONSTRAINT` 对新约束无效。

### A5 描述可恢复为 NULL（防回归）

`PATCH {description: null}` → 该行 `description` 变回 `NULL`。
（守护 `exclude_unset` 语义，§4.3 重写时最易破坏。）

### A6 检索结果结构不变

`RAGRetrieverService().search()` 返回的 doc 字典字段集不变：`text` / `id` / `chunk_id` / `source` / `file_path` / `user_id` / `chunk_index` / `sku_codes` / `chapter`（+ `score` / `rrf_score` / `rerank_score` 视路径而定）。

### A7 迁移幂等

连续执行两次 `python -m scripts.init_db` → 两次都不报错；`pg_constraint` 中 `uq_documents_user_md5` 消失、`pg_indexes` 中 `uq_documents_md5` 存在。

### A8 评测语料读取（D11）

```
1. load_corpus_documents(max_docs=100) 不传 user_id
   → 返回全库分块（当前实测 42 块），且结果中不含任何 user_id LIKE 'test_%' 的块
2. load_corpus_documents(max_docs=100, user_id='6')
   → 返回 owner 6 的块（收窄语义仍可用）
3. load_corpus_documents(max_docs=1, user_id='不存在的 owner')
   → 抛 RuntimeError，且文案为"无可用知识分块"（不含"请先 ingest"的旧指引）
4. `python -m evaluation --only-synthesize --testset-size 3` 不传 --user
   → 能跑通（**当前会报「语料库为空」**，这是本项修好的既有 bug）
```

> A8-1 的"不含 test_%"断言需要先插入一块 `user_id='test_xxx'` 的假数据（用 `cleanup_test_data` 清理），否则恒真。

---

## 6. 测试落点

沿用仓库既有先例（真库集成测试 + `conftest.cleanup_test_data` 按 `test_user_id` 隔离）：

| 断言 | 落点 | 参照 |
|---|---|---|
| A1a / A2 | `llm_backend/tests/test_bm25_retriever.py` | 同文件 `_insert_corpus`（14-28 行）经 `IndexingService.process_file` 注入迷你语料的模式 |
| A1b / A6 | `llm_backend/tests/test_rag_retriever.py`（**新建**） | 无现成文件；`test_static_dynamic_alignment.py` 有直连真库的写法可参照 |
| A3 / A4 / A5 | `llm_backend/tests/test_admin_knowledge.py` | 同文件已有 `_stage`（126-135）/ `_commit_sse`（138）/ `_counts_by_md5`（83）helper |
| A7 | 手工执行 + 实施记录登记 | 迁移脚本无 pytest 先例 |
| A8-1~3 | `llm_backend/tests/` 内新建（或并入 `test_rag_retriever.py`） | `load_corpus_documents` 是纯 DB 读，无需 LLM；A8-4 依赖 RAGAS judge key，**只做手工验收不写自动化** |

### 既有测试影响评估（逐个核对过）

| 文件 | 影响 |
|---|---|
| `test_bm25_retriever.py` | 语料经 `IndexingService.process_file` 写入（14-28 行）→ 自动带 `documents` 行且 `status='enabled'`（走 ORM default）→ **不受影响** |
| `test_admin_knowledge.py:425` `test_commit_duplicate_does_not_add_row` | 同一 user 重复提交，`_finalize` 改按 md5 后仍能查到行 → **应通过**（但需删掉 `_stage` 的 `user_id` 表单字段） |
| `test_admin_knowledge.py:200` `test_list_item_fields_have_no_title` | **第 209 行**断言 `owner_id == "6"`。与本次无关 → **不受影响**。（初稿把该断言误记在 181 行 `test_list_without_user_id_includes_seed_docs` 名下——那个用例断言的是 total≥2 与种子文档存在性） |
| `test_static_dynamic_alignment.py` | 第 22 行 import 了 retriever 但**从不调用 `.search()`**（全文仅此一处出现，实际用 raw SQL 直查） → **不受影响** |
| `test_rag_tool.py`（`:29`）/ `test_customer_tools_node.py`（`:31`/`:95`） | patch 了 `get_rag_retriever_service`（mock，不连库） → **不受影响** |
| `test_smoke.py` | 只有 `import main` + `SELECT 1`，不经检索入口 → **不受影响** |
| `test_indexing.py` / `test_rrf.py` | 不经过检索入口 → **不受影响** |

### ⚠️ D5 引入的测试脆弱性（初稿未登记）

D5 之后，`test_user_id` **不再能隔离测试语料**：语料内容是**固定字面量**（`test_bm25_retriever.py:18-20` 三条固定文本、`test_admin_knowledge.py` 的 `_md_bytes(paragraphs=N)` 确定性生成），**同一内容 → 同一 md5**，而 md5 现在全平台唯一。

具体风险：
- `test_admin_knowledge.py` 的 `_counts()`（70-81 行）是**全表**计数；`:347` 断言 `_counts() == (before[0] + 1, ...)`、`:343` 断言 `doc["duplicate"] is False`
- 某次运行若在 commit 之后、`_delete_doc` 之前被中断（Ctrl-C / 断言崩溃 / 进程被杀）→ 库里留下该 md5 的 documents 行
- 旧约束下该行 user_id 是上一轮的临时 id，不会命中；**新约束下 md5 全局唯一，下一轮直接命中** → stage 说重复、commit 返回 `duplicate: True`、`_counts()` 不多 1 → 报出与本次改动毫无关系的失败，排查方向被带偏

**处置（二选一，建议 a）**：
- (a) 把测试语料改为**内容带 `test_user_id` 后缀**（如 `f"小米智能门锁 {test_user_id} 续航180天"`），使 md5 随 user 变化 → 恢复隔离
- (b) 在各用例前置断言"该 md5 不存在"，命中即提示"检测到上次中断的残留行，请清理" → 至少把排查方向指对

---

## 7. 风险与已知边界

| # | 风险 | 评估与处置 |
|---|---|---|
| **R1** | **迁移失败**：目标库若已有多行同 md5，`CREATE UNIQUE INDEX` 直接失败 | §4.7 已有前置检查 SQL 与清理配方。本库实测为空集。**其他环境落地前必须先跑该检查** |
| **R2** | **上传语义变更**：第二个管理员上传同一文件会被判为"重复"，不再新建行、也不会成为 owner | **这是 D4/D5 的有意结果**。前端已有 duplicate 处理路径（`KnowledgeFormModal.vue:149/221` 会回填既有描述）。需在 README/用户文档中说明 |
| **R3** | **空描述覆盖**：管理员 B 重复上传同一文件且**不填描述**时，A 那一行的 `description` 会从 `NULL` 变成 `''` | `KnowledgeFormModal.vue:137` `description = ref(props.record?.description \|\| '')` → 新建路径（`:234`）提交 `''` 而非 `null`；`:221` 的预填条件是 `existing?.description` 真值，NULL 不预填；`_finalize:259` 的 `if payload.description is not None` 会放行 `''`。**旧约束下 B 写的是自己的新行，A 不受影响。** 处置：`_finalize` 改判 `if payload.description:`（空串不写入），或在 §6 测试中一并覆盖 |
| **R4** | **`user_id` 漂移导致块静默消失**：D3 的 `(user_id, md5)` join 依赖两表 `user_id` 一致，而两者**既无外键也无测试保证**（`document_chunk.py:15` 与 `document.py:13` 是独立列） | 当前 0 例外（已按 `(user_id, md5)` LEFT JOIN 查过：42 块 0 孤儿）。两条写入路径都用同一个 `user_id` 变量写两张表（`indexing_service`），漂移只能来自手工 SQL。**若消失，表现为整份文档从检索中静默消失、不报错、管理端仍显示正常片段数。** D5 之后 md5 已唯一，裸 `md5` join 是等效且无此失效模式的替代 —— 本 spec 选 `(user_id, md5)` 是为了让**迁移前后都正确**、发布顺序自由。可实施时再权衡 |
| **R5** | **大规模下的 ANN 召回**：数据量增长后，若优化器选择「HNSW 索引扫描 + join 后过滤」，被过滤掉的块会占用 ANN 名额，可能降低有效召回 | **本次无实测依据**（当前 42 行，索引根本未启用），故不预先调参（D10）。pgvector 实测版本 **0.8.6**，其 `hnsw.iterative_scan` 是将来需要时的现成手段。**触发条件**：chunks 量级上万且监控到召回下降时再评估 |
| **R6** | **语义缓存会把停用文档的答案继续端给用户**（审计新发现） | `main.py:311-320` 语义缓存**命中即短路返回、跳过整个图（含检索）**；缓存内容是 `graphrag/chat` 链路的**完整回答**（`:365-370` 回写），不是消解结果。key = user_id + 消解后消息，TTL `REDIS_CACHE_EXPIRE=3600`。**当前 `.env:56` 设 `SEMANTIC_CACHE_ENABLED=false` 已关闭**（`redis_semantic_cache.py:233/301` 双闸门），但 `config.py:64` **默认 `True`** —— 任何没有那行 .env 的环境（新部署、.env 丢失/被覆盖）都会开启，届时停用文档内容可从缓存答案中泄漏最长达 1 小时。**检索层过滤器对此完全够不着。** 见 §8 Q1 |
| **R7** | ~~打断评测入库流程~~（审计新发现） | **✅ 已处置（D11 / §4.10）**：不改约束，改评测侧 —— 语料改为默认读全库、`--user` 降级为可选收窄。原风险链：`evaluation/__main__.py:36-37` 的 `--user` 默认 `"1"`，D5 之后 `ingest_knowledge <dir> 1` 全部判重复 → user 1 语料为 0 → 报「语料库为空」，而报错信息又指引你去做这件做不到的事（死循环）。现按 §4.10 一并修掉，且**顺带修好一个既有 bug（默认值今天就是坏的）与一处指标偏差**（§2.8 实测重复条目） |
| **R14** | **评测语料的 `max_docs` 截断偏差**（既有缺陷） | `testset_builder.py` 按 `DocumentChunk.id` 排序后 `limit(max_docs)`（`RAGAS_MAX_CORPUS_DOCS` 当前 100，实测语料 42 块**未触发**）。语料超限后按上传顺序取前 100 → 后上传的文档出不了题。本次不加深（范围内已有）但**不修**，登记备查 |
| **R8** | **评测合成器不过滤 status**（审计新发现） | `evaluation/testset_builder.py:34-59` 只 `where(user_id==...)`，**没 join `documents`**。停用一份内容过期的文档后，`--only-synthesize` 仍从它生成 `reference_contexts` → 参考答案来自已停用块，而检索侧已排除 → `context_recall`/`context_precision` 掉分，报告看起来像"检索退化"，实为评测集与检索口径不一致 |
| **R9** | **停用会连带关掉该文档 sku 的动态价格/库存补全**（审计新发现） | `customer_tools/node.py:55-77`：动态补全的候选 sku **只从命中块收集**（`d.get("sku_codes")`），零命中就不查 `product_price_stock`。停用《京东智能家具产品知识文档.docx》→ 其 38 块的 sku 全部退出检索 → 客服回答"XX 沙发多少钱"时**既拿不到静态参数，也拿不到本可正常返回的价格库存**（那张表的数据还在）。这比"检索不到该文档内容"更宽。见 §8 Q3 |
| **R10** | **暂存文件路径竞态**（既有缺陷，D8 后命中概率上升） | `knowledge.py:153` 暂存路径 = `STAGING_DIR / f"{md5}{ext}"`（同一文件 = 同一路径）。A 与 B 并发上传同一文件 → 同一路径 → 先完成者 `os.remove`（`_finalize:266`）会抹掉后者的暂存文件 → 后者报 `FileNotFoundError` / error 事件。**D8 之后"第二个管理员上传同一文件"从边角变成被鼓励的正常流程** |
| **R11** | **发布顺序**：前端先去 `user_id`、后端未更新 → 上传直接坏 | `knowledge.py:129 user_id: str = Form(...)` 是**必填**，前端一停发即 422。反向（后端先上、前端后上）安全 —— FastAPI 忽略多余表单字段。**必须后端先发或同批发** |
| **R12** | **不可逆**：D5 一旦迁移没有下行脚本 | `init_db.py` 无版本表，无法判断"是否已迁移"、也判断不了"库里有几行同 md5"（唯一性让回滚前必先裁决数据）。回滚需手工：重建 `(user_id, md5)` 约束 + 回退代码 |
| **R13** | **`status` 无 DB 级校验** | `schemas/admin.py:53` 只有 Pydantic `pattern=r"^(enabled\|disabled)$"`；`models/document.py:21` 无 CHECK；实测 `pg_constraint` 只有 pkey + unique。运维手写 `'ENABLED'` 或将来加 `'draft'` → 两条检索路**静默排除**，前端 StatusBadge 显示"停用"（判据是 `!== 'enabled'`），无日志无告警。fail-closed 本身是对的，缺的是"未知 status"的可见信号 |

---

## 8. 待确认项

| # | 事项 | 建议 |
|---|---|---|
| **Q1** | **语义缓存（R6）本次是否处理？** 处置选项：① 仅登记为已知残留 + 在 README 明确"部署必须把 `SEMANTIC_CACHE_ENABLED` 设为 false"；② 把 `config.py:64` 的默认值改为 `False`（一行，让"忘记配 .env"的失败方向变安全）；③ 停用文档时主动清理缓存（要动缓存层，超出本 spec 范围） | **建议 ②**。一行改动即可让默认失败方向安全，成本最低；③ 留作独立任务 |
| **Q2** | ~~评测入库流程（R7）怎么适配？~~ | **✅ 已定（2026-09-27，用户确认）**：采用「默认读全库 + 排除 `test_%` 测试账号 + `--user` 降级为可选收窄」，代码形态见 §4.10。放弃的备选：①a 默认值改 `"6"`（下次 owner 变更还会坏）；② 评测改用独立内容集（违背 `SPEC_RAGAS_EVAL.md:51`）；③ 因此放弃 D5（不划算） |
| **Q3** | **动态价格/库存连带停用（R9）是否符合预期？** 停用知识文档后，该文档覆盖的 sku 不再能查到价格库存（数据仍在 `product_price_stock`） | 需要你判断。若不符合预期，修法是在 `customer_tools/node.py` 里让 sku 候选不依赖命中块（但那是另一个模块的设计变更，应拆独立任务） |
| **Q4** | **A4 的测试成本**：是否值得为一个"理论上不可能"的状态写绕过唯一约束的测试？ | 建议写。该断言守护的正是本次 bug 的直接来源（`rows[0]` 写法），回归价值高 |
| **Q5** | **R5 的 ANN 召回**：是否接受"当前不调参，待实测触发再处理"？ | 建议接受。当前规模无证据，预先调参违反"先实测再定方案" |
| **Q6** | **R10 的暂存竞态**：是否一并修（如暂存路径加 user_id 前缀）？ | 建议拆独立任务。它是既有缺陷，与本次改动正交 |
| **Q7** | **流程图重生**：本次是否一并重生 `docs/diagrams/50-knowledge-upload`？ | 建议**拆为独立任务**。流程图有独立的"三闸门"流程（见 `docs/superpowers/specs/2026-09-26-系统运行流程图-design.md`），混进本次会放大改动面 |

---

## 9. 文档同步清单

按 `CLAUDE.md` §6「文档内历史状态行保留原文，以归档状态行为最新判定」执行：

| 文件 | 动作 |
|---|---|
| `docs/项目问题.md:114`（#17a） | **改写为已解决**：保留问题描述原文，追加「✅ 已解决（日期）」+ 实施提交 hash + 落地证据。**同时新增一条**记录 R6（语义缓存）与 R9（动态价格连带）两条残留 |
| `docs/spec_plan/已完成/SPEC_ADMIN_CONSOLE.md` | **不改写历史正文**（§13 第 13 行、§4.4 第 314 行、§12 第 2743 行的原文保留）。在该文档的**归档状态行**追加注记：「§4.4 / §12-5 的『停用不影响检索』限制已于 2026-09-27 由 SPEC_DOCUMENT_STATUS_FILTER 推翻」 |
| `docs/spec_plan/已完成/SPEC_RAGAS_EVAL.md` | **不改写历史正文**（第 236 行的 `python -m evaluation --testset-size 20 --user 1  # 全流程（MVP 默认）` 原文保留）。在该文档的**归档状态行**追加注记：「`--user` 已于 2026-09-27 降级为可选收窄参数、默认读全库，由 SPEC_DOCUMENT_STATUS_FILTER D11 推翻」 |
| `docs/superpowers/specs/2026-09-26-系统运行流程图-design.md:334` | 追加注记：该"注意"条目已失效 |
| `docs/superpowers/plans/2026-09-26-系统运行流程图.md:745` | 历史 plan，**不改** |
| `docs/superpowers/plans/2026-09-22-管理端.md`（67-68、2007 行） | 历史 plan，**不改** |
| `docs/diagrams/spec/50-knowledge-upload.json:230` + 生成的 `50-knowledge-upload.html` | **Q7 决定后再动** |
| `frontend/src/admin/views/KnowledgeView.vue:85` | §4.9，本次必改 |
| 本 spec | 实施完成后：填实施记录、`git mv` 至 `docs/spec_plan/已完成/` |

---

## 10. 实施顺序

> **发布约束（R11）**：后端必须先于或同批于前端发布 —— 前端停止发送 `user_id` 后，旧后端的 `Form(...)` 必填会直接 422。

1. **§4.1 + §4.2 检索过滤器** → 验收 A1、A2、A6（**核心需求，先独立验证通过**）
2. **§4.7 迁移**（先跑前置检查 SQL，确认返回空集） → 验收 A7
3. **§4.6 模型约束** + **§4.8 索引查重** → 与步骤 2 同批
4. **§4.3 PATCH 全量更新** → 验收 A4、A5
5. **§4.4 `_finalize` + §4.5 stage 与 `user_id` 链** → 验收 A3（**必须与步骤 3 同批**，否则出现假错误）
6. **§4.10 评测语料（D11）** → 验收 A8（**与步骤 2/3 无依赖，可独立提交**）
7. **前端 §4.9 tooltip 删除** → 浏览器实测停用后客服确实检索不到
8. **§8 剩余 Q 项的处置落地**（Q1/Q3 待定；Q4~Q7 见各自建议）
9. **§9 文档同步** → 按 §6 生命周期规则归档本 spec

> 步骤 1 与 2~9 无依赖，可先合并提交步骤 1 —— 它单独就能让「停用后检索不到」成立（只是多管理员场景不彻底）。
>
> 步骤 6 同理可独立先做 —— 它修的是既有 bug（默认值坏掉）与评测指标偏差，与本次的身份统一没有耦合。
