# BM25 检索测试与生产数据共库假失败修复（可选 user_id 过滤）实施规格
> **归档状态**: ⏳ 待实施（2026-09-02 定稿，依据 main 代码与 git 历史）
> 尚未有代码提交，本 spec 为方案 A（search 可选 user_id 过滤参数）实施依据；替代"暂不处理"（项目问题 #8 维持现状）。

> **用途**: 修复 `test_bm25_retriever.py::test_bm25_recalls_docs_with_partial_terms` 假失败——回归测试与生产知识库数据共享同一 Postgres 表、检索为全局语义，真实商品 chunks（user_id='0'，41 块，含 11 块门锁）占据全局 top-10，测试自造语料（3 块）全部掉榜导致断言失败。方案：给 `BM25SQLRetriever.search` 增加**可选 user_id 过滤参数**（默认 None = 全局检索，生产行为逐字不变），测试改传 `test_user_id` 只在自己的迷你语料内检索
> **技术栈**: Python + SQLAlchemy text() + PostgreSQL 全文检索（pg_jieba jiebacfg/jiebamp + ts_rank_cd）
> **状态**: **待实施**（2026-09-02 定稿；用户确认先出 spec 再实施）
> **关联文档**: [[SPEC_BM25_QUERY_SEMANTICS_FIX.md]]（OR 语义来源，已完成）[[项目问题.md #8]]（假失败记录）[[SPEC_ENTRY_LLM_RESOLUTION.md]]（同批全量回归时暴露 #8）

---

## 目录

1. [背景与问题定义](#1-背景与问题定义)
2. [根因分析](#2-根因分析)
3. [方案设计](#3-方案设计)
4. [数据流与代码形态](#4-数据流与代码形态)
5. [判定示例](#5-判定示例)
6. [成本与影响面](#6-成本与影响面)
7. [测试方案](#7-测试方案)
8. [验证方案](#8-验证方案)
9. [决策记录](#9-决策记录)
10. [风险与避坑清单](#10-风险与避坑清单)

---

## 1. 背景与问题定义

### 1.1 现象

全量回归 `uv run pytest -q` 长期 1 例失败（知识库入库后 2026-08-27 起，随各次全量回归复现）：

```
FAILED llm_backend/tests/test_bm25_retriever.py::test_bm25_recalls_docs_with_partial_terms
AssertionError: assert []     # llm_backend/tests/test_bm25_retriever.py:44 断言 mine 非空失败
```

### 1.2 该测试验证什么

`test_bm25_recalls_docs_with_partial_terms`（回归用例，防 OR 语义回退 AND）：
1. 向当前测试用户注入 3 块一句话迷你语料：完整参数块 `小米全自动智能门锁Pro 续航 180 天 静音设计。` / 简讯块 `小米智能门锁 卧室门使用。` / 无关沙发块 `芝华仕电动沙发 头等舱 豪华体验。`
2. **全局检索**（不按 user 过滤）：`BM25SQLRetriever().search("小米全自动智能门锁Pro的详细参数配置", top_k=10)`
3. 断言：
   - top-10 结果中至少 1 条属于测试用户（部分词命中也能召回，"宁可多召回"）
   - 测试用户结果中 BM25 分数最高者含"续航"（全词命中块排前）

### 1.3 为什么是"假失败"

产品代码无缺陷——检索器忠实返回全局 top-10，只是榜单被生产数据占满。问题在**测试隔离不彻底**：
- 测试与生产知识库共享 `document_chunks` 表（docker 单库），生产知识库 41 块全部属于 `user_id='0'`
- 查询命中真实商品块（小米智能门锁 M20 / 米家智能门锁青春版等 11 块含"门锁"），文本更长、命中查询词更多 → ts_rank_cd 分数碾压测试玩具块
- 测试作者**已预见**全局干扰（docstring："全局语料存在高分 chunk，如文档总览"），写"先按 test_user_id 过滤再比较"——但过滤发生在 top-10 截断**之后**：真实语料占满前 10 时测试语料根本进不了结果集，过滤无从谈起

---

## 2. 根因分析

| 层面 | 结论 |
|---|---|
| 检索 SQL | `BM25SQLRetriever._OR_QUERY_SQL_TEXT` 无 user 过滤（`WHERE content_tsv @@ tsq.q ... LIMIT top_k`）——**生产语义即全局共享知识库**（知识库文档以 user_id='0' 入库、对全部用户可见），非缺陷 |
| 测试设计 | 断言隐含"库中只有我的数据"假设，知识库入库后假设失效；`top_k=10` 截断先于 user 过滤，隔离形同虚设 |
| 时序 | 2026-08-27 知识库 41 块入库（user 0）→ 该测试开始失败 → 项目问题 #8 记录（50/51）→ 2026-09-02 全量回归仍失败（75/76），与 SPEC_ENTRY_LLM_RESOLUTION 改动无关（未触碰检索/索引代码） |
| 数据库实测 | `SELECT user_id, count(*) FROM document_chunks GROUP BY user_id` → `('0', 41)`；`LIKE '%门锁%'` → 11 块（user 0） |

---

## 3. 方案设计

### 3.1 核心思路

给 `BM25SQLRetriever.search` 增加可选参数 `user_id: Optional[str] = None`：
- **None（默认，生产唯一形态）** → SQL 与原语句逐字一致（模板拼接空串），行为零变化
- **传入值（测试用）** → WHERE 追加 `AND document_chunks.user_id = :user_id`，检索范围收缩到该用户语料

测试改传 `test_user_id` → 候选集只剩自造 3 块（沙发块不命中查询词被 `@@` 过滤，实际进榜 2 块），断言语义完全保留（部分词命中召回 + 完整块排序），但不再受生产数据竞争影响。

### 3.2 设计要点

| 项 | 决策 | 理由 |
|---|---|---|
| 过滤实现 | SQL 模板 + 固定串条件拼接，**非**同一 `text()` 内两处 `:user_id` | SQLAlchemy `text()` 同名命名参数复用有歧义风险；拼接串仅 `""` 或字面量（非用户输入），无注入面 |
| 参数语义 | `user_id=None` **或** 空串 `""` → 全局（不过滤） | 与检索器无状态复用风格一致；空串等同未传，避免误传空过滤返回空列表的坑 |
| 列类型 | `user_id = Column(String(50))` → 字符串等值比较 | 与 conftest `test_user_id`（str）与生产入库（'0'）一致 |
| 生产调用点 | **全部不传**新参数 | 生产检索语义（全局共享知识库）保持不变 |
| 测试改动范围 | 仅改失败的 1 个用例 | 其余 3 个 BM25 用例（blank/single_word/split_brand）当前全局形态稳定通过，不扩大手术面；未来若知识库扩容顶掉其余用例，同法扩展（见风险清单 #4） |
| docstring/注释 | 同步更新（隔离语义 + 假失败背景一句话） | 避免后人误删过滤参数或改回全局 |

### 3.3 明确不做（YAGNI）

- 不做独立测试数据库/schema（方案 B，改动大：建库/建表初始化/fixture/CI 配置，为一个回归测试不值当）
- 不改生产检索语义（不带 user 过滤检索、不按 user 分库）
- 不做 mock 检索器（SQL 检索逻辑无 mock 价值，且会失去真 SQL 回归意义）
- 不清理生产知识库数据验证（动生产数据风险大，非验证正道）

---

## 4. 数据流与代码形态

### 4.1 `bm25_sql_retriever.py`

原 SQL 常量（L33-49）改造为模板 + 条件拼接：

```python
# 可选 user 作用域：None/空串 = 全局检索（生产语义）；测试传入 test_user_id 隔离自造语料
_USER_FILTER_SQL = "AND document_chunks.user_id = :user_id"

_OR_QUERY_SQL_TMPL = """
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
      WHERE tok ~ '\\S') tsq
WHERE document_chunks.content_tsv @@ tsq.q
{user_filter}
ORDER BY bm25_score DESC, document_chunks.id
LIMIT :top_k
"""
```

search 签名与执行：

```python
async def search(self, query: str, top_k: int, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """...（原 docstring 追加 Args: user_id: 按用户过滤（None/空串 = 全局检索，生产默认））"""
    # user_filter 仅 "" 或固定字面量（非用户输入），f-string 拼接无注入面；
    # 不用同一 text() 内两处 :user_id（SQLAlchemy 命名参数复用歧义）
    user_filter = _USER_FILTER_SQL if user_id else ""
    sql = text(_OR_QUERY_SQL_TMPL.format(user_filter=user_filter))
    params: Dict[str, Any] = {"query": query, "top_k": top_k}
    if user_id:
        params["user_id"] = user_id

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).all()
    ...（结果映射不变）
```

import 变更：`from typing import Any, Dict, List` → `Any, Dict, List, Optional`。

### 4.2 `llm_backend/tests/test_bm25_retriever.py`

仅 `test_bm25_recalls_docs_with_partial_terms`（约 L39-44）：

```python
    await _insert_corpus(test_user_id, tmp_path)
    # 隔离检索：搜索限定本测试自造语料（生产数据 user 0 的 41 块真实商品 chunks
    # 会占据全局 top-10，此前导致自造语料掉榜假失败——见项目问题 #8）
    results = await BM25SQLRetriever().search(
        "小米全自动智能门锁Pro的详细参数配置", top_k=10, user_id=test_user_id
    )
    mine = [r for r in results if r["user_id"] == test_user_id]
    assert mine, ...
    # 后续排序断言不变
```

docstring 更新：说明检索已按 test_user_id 隔离（原"全局语料存在高分 chunk…按 test_user_id 过滤后再比较"注释与新行为合并）。

### 4.3 调用方核对清单（全局检索 `BM25SQLRetriever.search`）

| 调用方 | 位置 | 是否传 user_id | 改动 |
|---|---|---|---|
| `RAGRetrieverService._safe(self.bm25.search(...))`（混合检索 BM25 路） | `app/services/rag_retriever_service.py:127` | 不传（None） | 无 |
| 测试 `test_bm25_retriever.py` 其余 3 用例（blank/single_word/split_brand） | `tests/test_bm25_retriever.py:56/57/63/84` | 不传（None） | 无 |
| 本测试用例（partial_terms） | `tests/test_bm25_retriever.py:40` | **传 test_user_id** | 本 spec 唯一改动 |
| 上层检索消费者（customer_tools 等） | `customer_tools/node.py:59` | 调用的是 `RAGRetrieverService.search` 而非 BM25SQLRetriever | 无 |

> 注：新参数仅测试使用属预期——它是测试隔离口子，不是生产功能；生产调用全链路零改动即零影响。

---

## 5. 判定示例

| 场景 | 调用 | 期望行为 |
|---|---|---|
| 生产混合检索（现状） | `search("小米…门锁Pro…", top_k=20)` | 全局检索，SQL 与原语句逐字一致，结果与改动前相同 |
| 测试隔离（本 spec 修复） | `search(同上, top_k=10, user_id="test_xxx")` | 候选集=该 user 全部 chunks；`WHERE ... AND user_id='test_xxx'`，结果只含自造语料 |
| 测试用例断言 | 注入 3 块后 scoped 检索 | 沙发块不命中查询词被 `@@` 排除；`mine` 非空（≥1 块）；"续航"块（全词命中 5 词）分数 > 简讯块（3 词） |
| 传空串 | `search(q, top_k=5, user_id="")` | 等同未传（全局检索）——避免空串过滤语义陷阱 |
| user 无语料 | `search(q, top_k=5, user_id="no_such_user")` | 返回空列表（正常 SQL 行为，`@@` 无匹配） |

---

## 6. 成本与影响面

| 维度 | 分析 |
|---|---|
| 生产行为 | **零变化**：唯一生产直接调用点不传新参数，模板拼 `user_filter=""` 后 SQL 与原 `_OR_QUERY_SQL_TEXT` 逐字符一致 |
| 性能 | 过滤走 `user_id` 列索引（String(50), index=True），命中候选集收窄，只会更快；未过滤路径无额外成本 |
| 测试稳定性 | partial_terms 用例从"全局竞争"变为"自造语料内验证"，随知识库扩容不再复现假失败 |
| 代码面 | 1 个文件 SQL 模板化 + 签名 + 1 行测试参数 + docstring；无新增依赖、无新配置 |
| 全量回归 | 预期 75/76 → **76/76** 全绿（消除 #8） |

---

## 7. 测试方案

### 7.1 修改后 partial_terms 用例断言（逐条）

语料注入后 scoped 检索（top_k=10, user_id=test_user_id）：
- `mine` 非空——OR 语义下部分词命中（"门锁"等）必须召回测试块（原核心回归点，保留）
- `mine_sorted[0]` 文本含"续航"——全词命中完整参数块分数高于部分命中简讯块（保留）
- 隐含新增保障：`mine` 即 `results`（结果天然全属 test_user_id）——由 SQL 过滤保证，无需显式断言

### 7.2 不改动用例

- `test_bm25_blank_query_returns_empty`：空查询与过滤无关，全局形态通过
- `test_bm25_single_word_still_works`：单 token "沙发" 全局 top-5 命中自造块，当前稳定
- `test_bm25_recalls_split_brand_word`：芝华仕拆分召回（top_k=50 全局），当前稳定

### 7.3 全量回归

`uv run pytest -q` → 76/76 通过（无新增/无既有失败）。

---

## 8. 验证方案（按序执行）

1. `uv run pytest -q llm_backend/tests/test_bm25_retriever.py` → 4/4 通过（此前 3/4）
2. 全量回归 `uv run pytest -q` → 76/76 通过
3. （可选实证）SQL 等价性：改动前后对同一 query 各跑一次全局 `search`（git stash 对比），top-10 结果一致——确认生产检索零变化
4. spec 生命周期归档：验证通过后本 spec 归档状态改 ✅ 已完成、`git mv` 至 `docs/spec_plan/已完成/`；项目问题 #8 状态更新为已修复（注明提交 hash）；CLAUDE.md §6 引用同步

---

## 9. 决策记录

| 决策点 | 决议 | 依据 |
|---|---|---|
| 修复思路 | **search 可选 user_id 过滤参数**（默认 None=全局） | user_id 是文档表既有维度（String(50), 有索引），检索缺的只是一个测试隔离口子；生产语义不变量 |
| 测试改动 | 仅 partial_terms 用例 scoped | 失败面唯一；其余用例全局稳定通过，最小手术原则 |
| 被否方案 B | 独立测试数据库 | 建库/建表/fixture/CI 全链路改动，为一个回归测试不值当；若未来测试集扩到需要独立库再评估 |
| 被否方案 C | 维持 #8 记录不动 | 每个全量回归持续红灯，混淆真实回归信号 |
| 被否"清生产数据验证" | 不采用 | 动生产数据风险大；scoped 检索后无需清库即可稳定 |
| 测试断言保留 | OR 召回 + 排序两条断言原样保留 | 它们验证的是检索语义回归点，与数据竞争无关，过滤后语义不变 |

---

## 10. 风险与避坑清单

1. **SQLAlchemy `text()` 同名命名参数**：勿写同一语句内两处 `:user_id`（`AND (:user_id IS NULL OR user_id = :user_id)`）——按 §4.1 模板 + 固定串条件拼接实现，规避歧义
2. **user_id="" 空串语义**：按 `if user_id`（空串等同 None）处理为全局，勿让空串过滤出空结果
3. **生产零变化验证**：模板拼空串后与原 SQL 逐字一致——实施后对同一 query 全局检索结果应完全一致（§8 步骤 3 可选实证）
4. **其余 BM25 用例未 scoped**：blank/single_word/split_brand 仍全局形态，若未来知识库扩容顶掉它们（同款假失败），按本 spec 同法补 `user_id=test_user_id` 即可——不要提前改动
5. **注释误导**：docstring 里"全局语料存在高分 chunk…过滤后再比较"的描述在新形态下过时，需同步更新为"scoped 检索"表述，防止后人以为过滤缺失
6. **回归信号混淆**：修复后全量回归应 76/76；若仍失败先查是否环境未重启/表数据残留（cleanup fixture 已按 user 清理自造语料）
