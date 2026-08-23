# BM25 检索查询语义修复实施规格（plainto_tsquery AND → token OR + ts_rank_cd 排名）

> **用途**: 修复 BM25 SQL 检索"多词自然问题恒 0 命中"缺陷——`plainto_tsquery('jiebacfg', query)` 将查询词全部 AND 连接，在平均 42 字符的微块语料上任意多词问题（如"小米全自动智能门锁Pro的详细参数配置"8 词 AND）几乎必然无 chunk 全词命中。改为 DB 内同配置分词 → token 过滤 → `to_tsquery` OR 连接 → `ts_rank_cd` 排名，文档部分命中即得分（贴近原 rank_bm25 BM25Okapi 的求和打分行为）
> **技术栈**: pg_jieba 1.1.1（jiebacfg 精确模式）+ PostgreSQL 16 + SQLAlchemy 2.x async + pytest-asyncio + IndexingService（测试语料注入）
> **状态**: **待实施**（2026-08-23 根因定稿，方案 A 经用户确认）
> **关联文档**: [[SPEC_RAG_RETRIEVAL_CONVERGENCE.md]]（实施记录第 5 条"如需宽松召回可改 websearch_to_tsquery（未实施）"为本 spec 直接前身）[[SPEC_REMOVE_QUERY_PREPROCESSING.md]] [[PROJECT_ANALYSIS.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题](#2-现状链路与问题)
3. [方案选型决策记录](#3-方案选型决策记录)
4. [目标架构（SQL 与代码形态）](#4-目标架构sql-与代码形态)
5. [边界情况处理表](#5-边界情况处理表)
6. [影响面分析](#6-影响面分析)
7. [文档与 spec 同步](#7-文档与-spec-同步)
8. [实施步骤（TDD）](#8-实施步骤tdd)
9. [验证方案](#9-验证方案)
10. [决策记录](#10-决策记录)
11. [风险与避坑清单](#11-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. `bm25_sql_retriever.py:42` 用 `plainto_tsquery("jiebacfg", query)` 构建查询 tsquery，其为 **AND 严格语义**：`query` 分词后所有词以 `&` 连接，`content_tsv @@ tsquery` 要求**全部词**出现在**同一 chunk** 中
2. 实库语料（`京东智能家具产品知识文档.docx`，310 chunk）为 docx 按小节切分的**微块**：min 5 / max 257 / **avg 41.8 字符**，290/310 个 ≤ 100 字符 —— 一个 chunk 无法同时容纳 8 个查询词
3. 2026-08-21 移除查询预处理管道（`0bbb3ff`）后，原始问题（含"详细/参数/配置"等泛化词）直接进入检索，多词 AND 失败率进一步升高
4. **实库复现**（2026-08-23）：查询 `'小米全自动智能门锁Pro的详细参数配置'` → `plainto_tsquery` 产出 `'小米' & '全自动' & '智能' & '门锁' & 'pro' & '详细' & '参数' & '配置'` → 命中 **0** 条；而库中实际存在 **15 个**含"小米+门锁"的 chunk（`'小米智能门锁'` 3 词查询 → 15 条）
5. 用户在 app 内的真实查询（`logs/app.log` 2026-08-23 15:44:38）即上述查询，日志稳定输出 `混合检索完成: 向量 10 条 + BM25 0 条` —— BM25 路恒空，RRF 融合退化为纯向量排名，表现为"BM25 检索不工作"

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| BM25 部分命中即召回 | 复现查询 `'小米全自动智能门锁Pro的详细参数配置'` 实库命中 ≥ 15 条（旧 AND 为 0） |
| 排名质量对齐旧实现 | 全词命中的 chunk 排名高于部分命中（ts_rank_cd 打分），top-1 为信息最完整 chunk |
| 分词配置不变 | 查询侧仍走 `jiebacfg`（与索引生成列同配置），token 对齐脆弱性问题不扩大 |
| 链路零辐射 | `RAGRetrieverService.search()` 结果结构、RRF 去重键、reranker 输入完全不变 |

### 1.3 设计原则

1. **只改查询侧，不动索引侧**：`content_tsv` 生成列（`init_db.py` 中 `to_tsvector('jiebacfg', content) STORED`）与 310 行存量数据零改动；OR 只是检索端查询形态
2. **分词仍在 DB 内完成**：不引入应用侧 jieba（与 cppjieba 分词不一致会造成新的 token 对齐漂移）；应用侧只负责 token 字符串过滤与结果组装
3. **单次 DB 往返**：保持现有 `search()` 一次 `session.execute` 的形态，不拆两次查询
4. **测试先行**：先写集成测试复现 0 命中缺陷，改 SQL 后转绿；沿用 `tests/test_indexing.py` 的"命中真实 Postgres + test_user_id 隔离 + cleanup"先例

---

## 2. 现状链路与问题

### 2.1 现状查询（bm25_sql_retriever.py:41-49）

```python
query_tsv = func.plainto_tsquery("jiebacfg", query)

stmt = (
    select(DocumentChunk, func.ts_rank_cd(DocumentChunk.content_tsv, query_tsv).label("bm25_score"))
    .where(DocumentChunk.content_tsv.op("@@")(query_tsv))
    .order_by(desc("bm25_score"))
    .limit(top_k)
)
```

生成 SQL 语义：`WHERE content_tsv @@ '小米' & '全自动' & '智能' & '门锁' & 'pro' & '详细' & '参数' & '配置'`（AND）

### 2.2 问题

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | plainto_tsquery 全词 AND，一次命中需全词同现 | bm25_sql_retriever.py:42 | 多词自然问题在微块语料上恒 0，BM25 路形同虚设 |
| 2 | 同为"非严格语义"候选的 websearch_to_tsquery 被记录为"未实施" | SPEC_RAG_RETRIEVAL_CONVERGENCE.md L16 | 遗留待办，方案见 §3 选型 |
| 3 | jiebacfg 对文档侧产生 junk lexeme（`'\n'`/`' '`）与单字切分 | 生成列输出 | 本 spec **不处理**（需重入库，另立 spec）；仅查询侧 token 过滤兜底 |

---

## 3. 方案选型决策记录

| 候选 | 结论 | 理由 |
|---|---|---|
| **DB 内分词 → token 过滤 → `to_tsquery('jiebacfg', "'a'\|\|'b'…")` OR 拼接** | ✅ 采纳（方案 A） | token 与索引同配置同管线；OR 语义贴近原 rank_bm25 BM25Okapi 的"求和打分（部分命中 > 0）"；`ts_rank_cd(D/C/B/A 权重)` 保证全词命中排前；一次 SQL 完成 |
| websearch_to_tsquery | 否决 | 语法为"普通词 AND、引号分组 OR"，行为不可精确控制；对中文 jiebacfg 分词与 token 级拼接无控制力，验收难以保证"部分命中召回" |
| AND/OR 混合（罕见词 AND、高频词 OR） | 否决（后续可选） | 需语料 IDF 统计与语料变更联动，复杂度高；本 spec 不阻塞，记入 §10 |
| 应用侧 jieba 分词后拼接 OR | 否决 | 应用 jieba（Python）与 DB cppjieba 分词结果不一致风险，复现新 token 对齐问题 |
| plainto_tsquery 行为保留 + 只调 chunking | 否决 | 治标不治本；chunking 变更需重建入库 310 条存量，改动面更大 |

---

## 4. 目标架构（SQL 与代码形态）

### 4.1 目标 SQL（psql 验证稿，实施时先手工跑通）

```sql
SELECT document_chunks.*,
       ts_rank_cd(document_chunks.content_tsv, tsq.q) AS bm25_score
FROM document_chunks,
     (SELECT to_tsquery('jiebacfg', string_agg(quote_literal(tok), '|' ORDER BY tok)) AS q
      FROM (SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', :query))) AS tok) t
      WHERE tok ~ '\S') tsq
WHERE document_chunks.content_tsv @@ tsq.q
ORDER BY bm25_score DESC, document_chunks.id
LIMIT :top_k
```

设计要点：

1. `to_tsvector('jiebacfg', :query)` 内层分词 → `tsvector_to_array` 取词条 → `~\S` 过滤纯空白 junk lexeme（jiebacfg 对换行/空格产出的单字符 lexeme）
2. `quote_literal(tok)` 单个词条加引号（词条内 `&`/`|`/`'` 等字符不再参与 tsquery 语法解析），`string_agg(…, '|' ORDER BY tok)` 确定顺序 OR 连接
3. `to_tsquery('jiebacfg', "'a'|'b'")` 对引号包裹词条**不再二次分词**，直接作为 lexeme —— 与已有 `content_tsv` lexeme 集合严格对齐
4. 外层 `tsv @@ tsq.q` 走既有 ix_chunks_tsv GIN 索引（OR 查询 PG 原生支持）
5. `ORDER BY bm25_score DESC, document_chunks.id`：ts_rank_cd 对 OR 查询按命中词贡献打分（全词命中 > 部分命中），id 兜底保证稳定排序

### 4.2 代码形态（bm25_sql_retriever.py 全文替换关键段）

```python
"""
BM25 数据库检索器（pg_jieba + ts_rank_cd）

替代原内存 BM25（jieba + rank_bm25）：
    1. 索引与查询统一走 PostgreSQL 全文检索（jiebacfg 精确模式，cppjieba 分词）
    2. GIN 倒排索引在 DB 内增量维护，无应用内存驻留、无重建窗口
    3. ts_rank_cd 为 BM25 变体排名（词类权重 D/C/B/A），排名行为需与旧实现回归对比

整体流程：
    1. DB 内 jiebacfg 分词查询词 → token 过滤空白 junk → OR 连接构建 tsquery
    2. content_tsv @@ query 走 GIN 索引筛候选（OR 语义，部分命中即入选）
    3. ts_rank_cd 排名，LIMIT top_k
"""

from typing import Any, Dict, List

from sqlalchemy import text

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger

logger = get_logger(service="bm25_sql_retriever")


class BM25SQLRetriever:
    """pg_jieba + ts_rank_cd 的 BM25 检索器（无状态，可复用）"""

    # 查询侧分词与索引生成列同一配置（jiebacfg），token 过滤后 OR 连接：
    # 文档命中部分查询词即可进入排名（plainto_tsquery 的 AND 全词语义在
    # 微块语料上恒 0 命中，见 spec_plan/SPEC_BM25_QUERY_SEMANTICS_FIX.md）
    _OR_QUERY_SQL_TEXT = text(
        """
        SELECT document_chunks.*,
               ts_rank_cd(document_chunks.content_tsv, tsq.q) AS bm25_score
        FROM document_chunks,
             (SELECT to_tsquery('jiebacfg',
                      string_agg(quote_literal(tok), '|' ORDER BY tok)) AS q
              FROM (SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', :query))) AS tok) t
              WHERE tok ~ '\\S') tsq
        WHERE document_chunks.content_tsv @@ tsq.q
        ORDER BY bm25_score DESC, document_chunks.id
        LIMIT :top_k
        """
    )

    async def search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """
        执行 BM25 全文检索（OR 语义）。

        查询词条与索引生成列同一配置（jiebacfg）分词，过滤空白 junk 后 OR 连接：
        文档只要命中部分查询词即可进入排名，由 ts_rank_cd 打分排序。

        Args:
            query: 用户查询文本
            top_k: 返回前 K 个结果

        Returns:
            检索结果列表，每个文档包含 id/source/text/file_path/user_id/chunk_index/bm25_score
        """
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    self._OR_QUERY_SQL_TEXT,
                    {"query": query, "top_k": top_k},
                )
            ).all()

        results = []
        for row in rows:
            r = row._mapping  # text() 查询返回 Row，按列名取
            results.append(
                {
                    "id": r["id"],
                    "chunk_id": r["chunk_id"],
                    "source": r["source"],
                    "file_path": r["file_path"],
                    "user_id": r["user_id"],
                    "chunk_index": r["chunk_index"],
                    "text": r["content"],
                    "bm25_score": float(r["bm25_score"]),
                }
            )

        logger.info("BM25 SQL 检索完成: query='{}', 返回 {} 条结果", query, len(results))
        return results
```

### 4.3 变更点覆盖检查（旧 import 清理）

- 原文件 `from sqlalchemy import desc, func, select` 删除（`desc`/`func`/`select` 在替换后全文零使用）；新增 `from sqlalchemy import text`
- 原文件 `from app.core.config import settings` 亦为**死 import**（已 grep 确认全文无 `settings.` 引用，替换时一并删除）
- 类 docstring "整体流程"第 1 条同步更新（见上）

---

## 5. 边界情况处理表

| 查询形态 | token 结果 | 行为 |
|---|---|---|
| 正常多词问题（如 8 词） | 多 token OR | 部分命中即返回，全词命中排前 ✓ |
| 单 token（如 `'沙发'`） | 1 token | `to_tsquery('jiebacfg', "'沙发'")` = 原语义，行为不变 |
| 全空白 / 纯标点（`"   "`、`"？？？"`） | token 列表为空或全被 `~\S` 过滤 | `string_agg → NULL` → `to_tsquery(...NULL) → NULL` → `tsv @@ NULL` 恒 NULL → 空列表（**不抛错**） |
| token 含引号/操作符（罕见，如 `don't`） | `quote_literal` 完整转义，不参与 tsquery 语法 | 正常，词条原样匹配 |
| 查询命中 0 条（真无语义相关） | — | 返回空列表，下游 `_safe` 融合不变 |

---

## 6. 影响面分析

| 组件 | 状态 | 依据 |
|---|---|---|
| `bm25_sql_retriever.py` | **唯一代码改动** | search() SQL 构建与结果组装替换（§4.2） |
| `__init__.py`（hybrid_retrieval） | 不变 | 仍导出 BM25SQLRetriever，类名/方法签名不变 |
| `rrf_fusion.py` | 不变 | 只消费 id/chunk_id/text，不受影响 |
| `rag_retriever_service.py` | 不变 | 调 `self.bm25.search(query, settings.BM25_TOP_K)`，接口不变 |
| `config.py` | 不变 | BM25_TOP_K 语义不变 |
| `scripts/init_db.py` | 不变 | 生成列、GIN 索引、pg_jieba 扩展均不动 |
| `app/models/document_chunk.py` | 不变 | content_tsv 生成列定义不动 |
| 测试 | **新增 1 文件** | `llm_backend/tests/test_bm25_retriever.py`（§8） |
| 前端 / API | 无引用 | BM25 在服务内部，无对外接口变化 |

---

## 7. 文档与 spec 同步

| 文件 | 位置 | 修改 |
|---|---|---|
| `spec_plan/SPEC_RAG_RETRIEVAL_CONVERGENCE.md` | 实施记录第 5 条（L16） | 追加标注：`⚠️ 2026-08-23 已实施 OR 语义修复（详见 [[SPEC_BM25_QUERY_SEMANTICS_FIX.md]]）：plainto_tsquery AND → token OR + ts_rank_cd` |
| `docs/PROJECT_ANALYSIS.md` | 检索链路相关段落（grep `plainto` 后定位） | 若描述 BM25 为"AND 严格匹配"则同步为"OR 部分命中即召回" |
| `STUDY_NOTES.md` | grep `plainto\|tsquery` 定位 | 同上前置 |
| `bm25_sql_retriever.py` | 模块 docstring | 已并入 §4.2 代码形态 |
| `docs/项目问题.txt` | 第 5 行"BM25 索引空"相关 | 追加"BM25 AND 语义已修复（2026-08-23）"注（若有描述） |

---

## 8. 实施步骤（TDD）

### Step 1：新增失败测试 `llm_backend/tests/test_bm25_retriever.py`

```python
"""BM25 检索语义集成测试：多词查询在部分词命中时也能召回（AND→OR 回归）。

命中真实 Postgres（与 test_indexing.py 同先例），语料按 test_user_id 隔离，
conftest.cleanup_test_data 清理。依赖 pg_jieba（docker postgres 镜像自带）。
"""
import pytest

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval.bm25_sql_retriever import (
    BM25SQLRetriever,
)
from app.services.indexing_service import IndexingService


async def _insert_corpus(test_user_id, tmp_path):
    """注入迷你语料：完整门锁参数 / 门锁简讯(缺参数字段) / 无关沙发。"""
    svc = IndexingService()
    files = {
        "lock_full.txt": "小米全自动智能门锁Pro 续航 180 天 静音设计。",
        "lock_brief.txt": "小米智能门锁 卧室门使用。",
        "sofa.txt": "芝华仕电动沙发 头等舱 豪华体验。",
    }
    for name, content in files.items():
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        r = await svc.process_file(
            {"path": str(p), "original_name": name, "user_id": test_user_id}
        )
        assert r["status"] == "success"


async def test_bm25_recalls_docs_with_partial_terms(
    test_user_id, tmp_path, cleanup_test_data
):
    """8 词查询仅部分词命中时必须召回含'门锁'的 chunk（旧 AND 语义返回 0）。"""
    await _insert_corpus(test_user_id, tmp_path)
    results = await BM25SQLRetriever().search(
        "小米全自动智能门锁Pro的详细参数配置", top_k=10
    )
    tops = [r for r in results if "门锁" in r["text"]]
    assert tops, f"OR 语义应召回含'门锁'的 chunk，实际: {[r['text'][:30] for r in results]}"
    # 全词命中(5 词)的完整参数 chunk 应排 at top-1，而非部分命中(3 词)的简讯 chunk
    assert "续航" in results[0]["text"]


async def test_bm25_blank_query_returns_empty(test_user_id, tmp_path, cleanup_test_data):
    """全空白/纯标点查询：返回空列表而非 SQL 错误。"""
    await _insert_corpus(test_user_id, tmp_path)
    retriever = BM25SQLRetriever()
    assert await retriever.search("？？？", top_k=5) == []
    assert await retriever.search("   ", top_k=5) == []


async def test_bm25_single_word_still_works(test_user_id, tmp_path, cleanup_test_data):
    """单 token 查询行为不回退：'沙发' 必须命中 sofa chunk。"""
    await _insert_corpus(test_user_id, tmp_path)
    results = await BM25SQLRetriever().search("沙发", top_k=5)
    assert results and "沙发" in results[0]["text"]
```

### Step 2：运行测试，确认旧语义下 FAIL

`uv run pytest llm_backend/tests/test_bm25_retriever.py -v`

期望：`test_bm25_recalls_docs_with_partial_terms` **FAIL**（`assert tops` 为空，AND 语义 0 召回）；`test_bm25_blank_query_returns_empty` 与 `test_bm25_single_word_still_works` PASS（单 token 与空查询与旧行为一致）

### Step 3：psql 手工验证目标 SQL（边界全过再改代码）

在 `smartcs-agent-postgres` 容器内验证（§4.1 SQL，`:query`/`:top_k` 换成字面量）：

```bash
docker exec smartcs-agent-postgres psql -U postgres -d smartcs_agent -t -A \
  -c "SELECT count(*) FROM document_chunks, (SELECT to_tsquery('jiebacfg', string_agg(quote_literal(tok), '|' ORDER BY tok)) AS q FROM (SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', '小米全自动智能门锁Pro的详细参数配置'))) AS tok) t WHERE tok ~ '\\S') tsq WHERE document_chunks.content_tsv @@ tsq.q;"
```

期望 ≥ 15（复现查询）；同一模板再验：`'沙发'` → ≥ 17、`'？？？'` → 0 且无报错、`'   '` → 0 且无报错

### Step 4：实现替换（§4.2 代码形态全文替换 bm25_sql_retriever.py）

- 替换模块 docstring、import（含删死 import `from app.core.config import settings`）、`_OR_QUERY_SQL_TEXT`、`search()` 全文

### Step 5：运行测试，确认转绿

`uv run pytest llm_backend/tests/test_bm25_retriever.py -v` → 4 个用例全 PASS

### Step 6：全量回归

`uv run pytest llm_backend/tests -q` → 全绿（重点 test_rrf.py / test_indexing.py / test_smoke.py）

### Step 7：复现案例端到端验证

```bash
cd llm_backend && uv run python -c "
import asyncio
from app.services.rag_retriever_service import get_rag_retriever_service

async def main():
    docs = await get_rag_retriever_service().search('小米全自动智能门锁Pro的详细参数配置')
    print('最终 docs:', len(docs))
    print('含门锁:', sum(1 for d in docs if '门锁' in d['text']))

asyncio.run(main())
"
```

期望：`最终 docs: 5`（精排 top-5）、`含门锁: ≥1`；另观察 `logs/app.log` 新增行 `BM25 SQL 检索完成: query='小米全自动智能门锁Pro的详细参数配置', 返回 N 条结果`（N ≥ 15）

### Step 8：文档同步（§7 清单逐项）→ Step 9：提交推送

```bash
git add spec_plan/SPEC_BM25_QUERY_SEMANTICS_FIX.md llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/hybrid_retrieval/bm25_sql_retriever.py llm_backend/tests/test_bm25_retriever.py <文档同步涉及文件>
git commit -m "fix: BM25 检索查询语义 AND→OR——多词自然问题部分命中即召回(微块语料恒0命中根治)"
git push origin dev
```

---

## 9. 验证方案（汇总）

1. **集成测试**：`uv run pytest llm_backend/tests/test_bm25_retriever.py -v`（复发 → 转绿，前后对照即 TDD 证据）
2. **psql 对照**：§8-Step3 三个边界 SQL 手工验证（多词 / 单词 / 空查询）
3. **全量回归**：`uv run pytest llm_backend/tests -q`
4. **端到端**：`search()` 脚本复现用户案例，日志确认 `BM25 SQL 检索完成 … 返回 ≥15 条`、`向量 N 条 + BM25 M 条` 中 M > 0
5. **影响面 grep**：`grep -rn "plainto_tsquery" llm_backend/app` → 仅剩预期位置（零残留旧调用点）

---

## 10. 决策记录

| 决策点 | 决议（2026-08-23） |
|---|---|
| 修复语义 | **OR 语义（方案 A）**——部分命中即得分，ts_rank_cd 保持全词命中排前；复用现有 ts_rank_cd/g_rank 排序，不引入 IDF 统计 |
| websearch_to_tsquery | 否决（不可精确控制 token 级行为，见 §3） |
| OR/AND 混合（罕见词 AND） | 不做（后续可选优化项，语料更大且需 IDF 统计时再评估） |
| 应用侧 jieba 分词 | 否决（与 DB cppjieba 不一致风险） |
| 索引侧（content_tsv 生成列） | **不改**——涉及 310 行存量重算/重入库，超出本 spec |
| 文档侧单字拆词 / junk lexeme 修复 | 不改（分词器质量另立 spec，需要重建索引） |

---

## 11. 风险与避坑清单

1. **`text()` 查询的结果行形态**：`document_chunks.*` 返回 Row 而非 ORM 实例，必须用 `row._mapping` 按列名组装 dict——对照原方法输出字段逐个映射（id/chunk_id/source/file_path/user_id/chunk_index/content/bm25_score），漏字段会破坏下游 RRF 去重键（chunk_id）
2. **`'\\S'` 转义**：Python 字符串中 `\S` 必须写成 `'\\S'`（text() 直接编译给 PG 的正则），漏转义会变 `DELETE FROM…` 级灾难——不，实际是 SQL 语法错误，开发时立即暴露
3. **psycopg 命名参数**：text() 用 `:query`/`:top_k` 命名绑定，参数必须 dict 传入；禁止 f-string 拼接（注入风险）
4. **`quote_literal` 与 NULL 分支**：全空 token 时 `string_agg → NULL → @@ 恒 NULL → 空列表`，**不要**加"短路抛错"之类的防御逻辑（旧 plainto 对全停用词查询同样返回空）
5. **ORM import 清理**：替换后 `desc`/`func`/`select`/`settings` 均不再使用（已 grep 确认 `settings` 为死 import）——一并清除，否则 ruff/lint 及"修改归零"检查不干净
6. **排序稳定性**：`ORDER BY bm25_score DESC` 平手时无绝对顺序——补 `document_chunks.id` 兜底（§4.1 SQL 已含）
7. **GIN 索引覆盖 OR**：OR tsquery PG 原生支持走反向 GIN/通用搜索；310 chunk 库量级无性能风险（语料 1 万+ 时用 EXPLAIN 复核一次）
8. **并发改动风险**：`bm25_sql_retriever.py` 是 RAG 检索链核心文件，实施前 `git status` 确认工作区干净；`SPEC_RAG_RETRIEVAL_CONVERGENCE.md` 是高频文档，同步时只追加标注不改原文
9. **测试依赖 DB**：`test_bm25_retriever.py` 依赖本地 docker Postgres（pg_jieba 扩展），与 `test_indexing.py` 同样约束——发布到无 DB 环境前跳过（沿用项目现有测试约定，不需额外处理）
10. **__pycache__ 残留**：`hybrid_retrieval/__pycache__/bm25_retriever.cpython-*.pyc`（已删除模块的孤儿缓存）不参与导入，无需处理
