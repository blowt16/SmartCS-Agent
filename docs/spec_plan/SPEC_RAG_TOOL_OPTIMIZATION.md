# RAG 检索 Tool 优化实施规格

> **用途**: 优化 `rag_retrieval` tool（`llm_backend/app/services/rag_tool.py`）——修正过时 description（价格/库存已移出知识库）、空结果与错误返回语义（供 LLM 自主判断）、结果携带来源溯源（可追溯判断依据）；与 `product_stock_lookup`（SPEC_PRODUCT_STOCK_TOOL.md）形成"动态查库 + 静态查知识库"的互补工具对
> **技术栈**: langchain-core `@tool` + RAGRetrieverService（现有管线不动，仅 tool 适配层优化）
> **状态**: 待实施（2026-08-28 设计定稿；description 优化已先行实施）
> **关联文档**: [[SPEC_PRODUCT_STOCK_TOOL.md]]（商品动态数据 tool，三态协议参考）[[PROJECT_ANALYSIS.md]]（RAG 管线）

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状与问题](#2-现状与问题)
3. [设计决策](#3-设计决策)
4. [目标设计](#4-目标设计)
5. [文件改动清单](#5-文件改动清单)
6. [边界情况处理表](#6-边界情况处理表)
7. [影响面分析](#7-影响面分析)
8. [实施步骤](#8-实施步骤)
9. [验证方案](#9-验证方案)
10. [决策记录](#10-决策记录)
11. [风险与避坑清单](#11-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 知识分层落地后（CLAUDE.md）：价格/库存为动态信息移出 docx——`rag_retrieval` docstring 仍声称"知识库包含价格、库存"，**过时误导 LLM**（LLM 用 rag_retrieval 查价格必然空结果）
2. 后续业务子 agent 将同时绑定 `rag_retrieval` 与 `product_stock_lookup`——两个 tool 需**协议一致**（成功/空/错误三态语义 + message 建议），agent 只需一套工具处理逻辑
3. RAG 检索管线（HNSW ∥ BM25 → RRF → Reranker）已稳定（RAGAS 评测通过），**本次只优化 tool 适配层，不动检索管线**

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| description 修正 | 无"价格/库存"误导表述；明确与 product_stock_lookup 分工（动态查库/静态查知识库）；触发/不触发场景 |
| 空结果语义 | 返回带建议的提示（换措辞 / 转 product_stock_lookup / 说明未收录），LLM 可自主决策 |
| 错误兜底 | search 异常不抛异常中断，返回错误信息供 LLM 判断（告知暂不可用/转人工） |
| 来源溯源 | 结果携带 `【来源:文件名】` 前缀——回答可追溯（福客 A4"完整且可追溯的判断依据"） |
| 协议一致 | 与 product_stock_lookup 三态语义对齐（成功=结果；空=empty 提示；错误=error 提示） |

### 1.3 明确不做（MVP 边界）

- 检索管线调优（RAGAS 已覆盖，另属检索调优 spec）
- 结果 token 裁剪/排序——top-5 × ~500 字符 ≈ 2500 字符，可接受
- 结果 JSON 结构化（文档片段是自然语言，保持文本形态，见 §3.1）
- 错误重试机制（LLM 自行决定是否重试）

---

## 2. 现状与问题

| # | 问题 | 现状证据 |
|---|------|---------|
| P1 | **description 过时** | docstring 声称"知识库包含商品信息（产品参数、价格、库存）"——价格/库存已移出 docx 入 product_price_stock 表 |
| P2 | **空结果语义弱** | 无结果返回 `""`——LLM 无法区分"没检索到"与"查询出错"；无下一步建议 |
| P3 | **错误无兜底** | `search()` 内部异常直接上抛——中断 langchain 工具调用，错误信息不经 LLM 判断 |
| P4 | **metadata 丢弃** | `search()` 返回 docs 含 `id/chunk_id/source/file_path/user_id/chunk_index/score` 完整 metadata，但 tool join 时**只取 text**——回答无法溯源 |

---

## 3. 设计决策

### 3.1 返回形态：文本保持 vs JSON 协议

| 候选 | 结论 | 理由 |
|---|---|---|
| **文本形态 + 三态语义**（成功=文档文本列表；空=empty 提示；错误=error 提示） | ✅ 采纳 | RAG 结果是自然语言片段，纯文本利于 LLM 阅读；三态通过 message 语义对齐 product_stock_lookup（status 概念隐含在文本中）——agent 处理逻辑统一：非结果文本=不可用，按 message 建议行动 |
| 完全 JSON 化（status/count/data 包裹） | 否决 | 文档片段 JSON 包裹增加解析负担无收益；与商品 tool（结构化记录）内容形态本质不同 |

### 3.2 来源溯源格式

| 候选 | 结论 | 理由 |
|---|---|---|
| **`【来源:文件名】` 前缀**（`Path(file_path).name`） | ✅ 采纳 | file_path 是完整路径（如 `product_knowledge_docx/京东智能家具产品知识文档.docx`），取文件名更简洁；source 列与 file_path 冗余时取更具体的 file_path |
| 结构化返回 source dict | 否决 | 文本形态下前缀最简洁；LLM 与人工都易读 |

### 3.3 空结果/错误提示措辞

| 场景 | message 要点 |
|------|-------------|
| 空结果 | ① 换措辞/更具体重试；② 若询问价格/库存 → 用 product_stock_lookup；③ 可向用户说明暂未收录该信息 |
| 检索异常 | 告知用户当前知识检索暂不可用，稍后重试或转人工；不编造内容 |

### 3.4 错误处理分层（与 product_stock_lookup 对齐）

| 层 | 手段 | 说明 |
|----|------|------|
| ① 超时保护 | `asyncio.wait_for(10s)` | DB/Embedding API 挂死时 tool 调用不无限等待 |
| ② 自动重试 | 瞬时错误重试 1 次（0.5s 间隔） | `db_timeout`/`db_connection` 类瞬时故障自愈；SELECT 幂等重试安全 |
| ③ 错误分类 + retryable | `_classify_error` 异常映射 | 瞬时（timeout/connection）vs 永久（config/unknown）；**tool 已自动重试过，返回的 error 一律 retryable=false**，防 LLM 盲目重试 |

**错误信息统一 JSON 协议**（与 product_stock_lookup 完全一致）：成功/空结果保持文本形态（文档片段），**错误路径返回 JSON**——`{"status":"error","error_type":"...","retryable":false,"message":"..."}`。agent 处理逻辑：尝试解析 JSON，status=error 即按错误协议处理；解析失败=成功内容。

---

## 4. 目标设计

### 4.1 优化后 Tool（`llm_backend/app/services/rag_tool.py`）

```python
"""
RAG 检索工具（langchain @tool 薄封装）

将整个 RAG 检索管线（HNSW ∥ BM25 → RRF → Reranker 精排）封装为 agent 可调用的工具。
核心逻辑全部在 RAGRetrieverService，此处仅为薄适配层。

用法（后续 agent 接入）：
    from app.services.rag_tool import rag_retrieval
    from app.services.product_stock_tool import product_stock_lookup
    llm.bind_tools([rag_retrieval, product_stock_lookup])
"""

import asyncio
import json
from pathlib import Path

from langchain_core.tools import tool
from sqlalchemy.exc import InvalidPasswordError, OperationalError, ProgrammingError

from app.services.rag_retriever_service import get_rag_retriever_service

# 检索超时与重试配置（与 product_stock_lookup 对齐）
DB_TIMEOUT_SECONDS = 10.0   # 超时保护：检索挂死时 tool 调用不无限等待
DB_RETRY_TIMES = 1          # 瞬时错误自动重试次数（检索幂等，重试安全）
DB_RETRY_INTERVAL = 0.5     # 重试间隔（秒）


def _classify_error(e: Exception) -> tuple[str, bool]:
    """异常 → (error_type, retryable)。瞬时类（超时/连接）可自动重试；永久类不重试。"""
    if isinstance(e, asyncio.TimeoutError):
        return "db_timeout", True
    if isinstance(e, OperationalError):
        return "db_connection", True
    if isinstance(e, (ProgrammingError, InvalidPasswordError)):
        return "db_config", False
    return "unknown", False


async def _search_with_retry(query: str) -> list[dict]:
    """超时保护 + 瞬时错误自动重试（与 product_stock_lookup._query_with_retry 同模式）。"""
    for attempt in range(DB_RETRY_TIMES + 1):
        try:
            return await asyncio.wait_for(
                get_rag_retriever_service().search(query), timeout=DB_TIMEOUT_SECONDS
            )
        except Exception as e:
            _, retryable = _classify_error(e)
            if attempt < DB_RETRY_TIMES and retryable:
                await asyncio.sleep(DB_RETRY_INTERVAL)
                continue
            raise


def _tool_error(error_type: str, retryable: bool, message: str) -> str:
    """错误信息（统一 JSON 协议，与 product_stock_lookup 一致）。

    瞬时类错误 tool 内部已自动重试过 → retryable=false（LLM 不再盲目重试）；
    仅 invalid_argument（入参错误）为 true——修正参数后重试有意义。
    """
    return json.dumps(
        {"status": "error", "error_type": error_type, "retryable": retryable, "message": message},
        ensure_ascii=False,
    )


@tool
async def rag_retrieval(query: str) -> str:
    """
    从企业知识库中检索与用户问题相关的知识文档片段。

    知识库内容（商品静态信息）：商品参数与规格、功能特点、使用指导与故障排查、
    保修与售后政策（含《京东自营售后政策》独立文档）等。
    【动态数据不在本工具】商品价格与库存存储在数据库，请使用 product_stock_lookup 工具查询。

    何时使用本工具：
    - 用户询问商品参数/规格/功能特点（如"这款灯的亮度调节范围是多少"）
    - 用户询问使用方法或故障解决（如"扫地机器人一直报错怎么办"）
    - 用户询问保修、退换货、售后政策条款（如"这个锁保修多久"）
    - 用户询问某类商品的功能/推荐/对比（如"电动沙发和普通沙发有什么区别"）

    何时不要使用本工具：
    - 询问商品价格或是否有货 → 使用 product_stock_lookup（数据库动态数据）
    - 与业务无关的闲聊 → 直接回答，无需检索
    - 违规/高风险内容 → 按风险规则处理，不检索

    Args:
        query: 用户的问题（建议为补全指代后的完整问题）

    Returns:
        成功：相关文档片段列表（每段以换行分隔，含【来源:文件名】前缀）；
        空结果：提示未检索到 + 可执行建议；
        失败：统一错误 JSON（status=error，含 error_type/retryable/message）。
    """
    # 入参校验：query 为空 → 明确错误，引导 LLM 提供问题（retryable=true）
    if not query or not query.strip():
        return _tool_error(
            "invalid_argument", True,
            "参数错误：query 不能为空。请根据用户消息生成完整检索问题（可补全指代）后重试，"
            "若确实无法生成检索问题，请直接回答或向用户澄清。",
        )

    try:
        # 超时保护（10s）+ 瞬时错误自动重试 1 次；重试后仍失败 → 返回 error
        docs = await _search_with_retry(query.strip())
    except Exception as e:
        error_type, _ = _classify_error(e)
        logger.warning("RAG 检索异常: {} ({})", type(e).__name__, error_type)
        return _tool_error(
            error_type, False,
            f"知识检索失败（{error_type}），已自动重试仍未恢复。"
            "请告知用户当前知识检索暂不可用，稍后重试或转人工处理，不要编造知识库中不存在的内容。",
        )

    if not docs:
        return (
            f"未检索到与「{query}」相关的知识内容。建议："
            "1) 更换措辞或补充商品名称后重试；"
            "2) 若用户询问的是商品价格或库存，请使用 product_stock_lookup 工具；"
            "3) 可向用户说明该信息暂未收录。"
        )

    return "\n\n".join(
        f"【来源:{Path(doc.get('file_path', '未知')).name}】\n{doc.get('text', '')}"
        for doc in docs
    )
```

> 说明：`logger` 从 `app.core.logger` 导入（`get_logger(service="rag_tool")`），代码中补上（`_tool_error` 分支前）。

### 4.2 优化点对照

| 优化点 | 落地 |
|--------|------|
| P1 description | 已实施（2026-08-28）——分工/触发/不触发场景 |
| P2 空结果 | 返回带建议提示（三态语义 empty） |
| P3 错误兜底 | try/except → 错误提示（三态语义 error），不抛异常 |
| P4 来源溯源 | `【来源:{Path(file_path).name}】` 前缀 |

---

## 5. 文件改动清单

| 文件 | 动作 | 内容 |
|------|------|------|
| `llm_backend/app/services/rag_tool.py` | 修改 | description（已实施）+ 空结果/错误提示 + 来源前缀 + logger 导入 |
| `llm_backend/tests/test_rag_tool.py` | **新增** | pytest：mock RAGRetrieverService 验证三态返回（成功带来源/空结果建议/异常兜底） |

---

## 6. 边界情况处理表

| # | 场景 | 预期行为 |
|---|------|---------|
| 1 | 正常检索（命中） | 文档片段列表，每段带【来源:文件名】前缀 |
| 2 | 空结果（知识库无相关内容） | 提示 + 建议（换措辞/转 product_stock_lookup/说明未收录） |
| 2a | query 为空/空白（LLM 参数错误） | error/invalid_argument（retryable=true），message 引导生成完整检索问题或直接回答/澄清 |
| 3 | search 异常（DB 不可用） | 自动重试 1 次 → 仍失败 → 错误 JSON（db_timeout/db_connection，retryable=false），引导转人工，不抛异常、不编造 |
| 3a | 检索挂死/超时 | wait_for(10s) → 自动重试 1 次 → 仍超时 → error/db_timeout，不无限等待 |
| 3b | 永久错误（表不存在/认证失败） | 不重试 → error/db_config，LLM 直接转人工 |
| 4 | file_path 为空 | 来源回退「未知」 |
| 5 | 多文档命中 | 各段独立【来源】前缀，LLM 可区分多来源 |
| 6 | 查询含特殊字符（%/_） | RAG 管线内部处理（向量/BM25 参数化），tool 层透传不干预 |
| 7 | 价格/库存类问题误用本工具 | 空结果提示引导转 product_stock_lookup（description 已预防，双保险） |

---

## 7. 影响面分析

| 面 | 影响 |
|----|------|
| RAG 检索管线 | 零改动（RAGRetrieverService/混合检索/RAGAS 评测不受影响） |
| 商品 tool（product_stock_lookup） | 无直接依赖；三态语义对齐互为参照 |
| 意图识别模块 | 无影响（当前主图不调用 tool） |
| 现有调用方 | 检索 `rag_retrieval` 的现存调用点？——全项目检索确认（当前无 bind_tools 调用，纯工具定义） |
| 返回格式变化 | 成功路径增加【来源】前缀、空结果从 `""` 变提示——**任何已消费该 tool 返回的代码需同步**（当前无，后续 agent 是唯一消费方） |

---

## 8. 实施步骤

1. **修改 `rag_tool.py`**（§4.1：logger 导入 + 三态返回 + 来源前缀）→ 验证：import 通过；mock 调用三态输出正确
2. **pytest 测试**（`tests/test_rag_tool.py`）：mock `get_rag_retriever_service` 返回空/异常/正常 docs，断言三态文本
3. **全项目检索**：`rag_retrieval` 引用点核对（确认无现存消费方需要同步返回格式）
4. **agent 绑定验证**：`llm.bind_tools([rag_retrieval, product_stock_lookup])` 不报错

---

## 9. 验证方案

| 验证项 | 方法 |
|--------|------|
| 三态返回 | pytest：正常（带来源前缀）/ 空（建议文本）/ 异常（兜底文本） |
| description 正确性 | 人工核对无"价格、库存"误导；分工/触发/不触发完整 |
| bind_tools 兼容 | DeepSeek/Ollama 绑定工具定义成功 |
| 与商品 tool 协议一致性 | 后续 agent prompt 一套工具处理逻辑覆盖两 tool（成功=内容、非成功=按 message 行动） |

---

## 10. 决策记录

| # | 决策 | 结论 | 时间 |
|---|------|------|------|
| 1 | description 优化 | 先行实施——修正"含价格库存"误导、明确与 product_stock_lookup 分工、触发/不触发场景 | 2026-08-28 |
| 2 | 返回形态 | 文本形态 + 三态语义（成功=文档列表；空/错误=带建议提示），不 JSON 化（文档片段是自然语言） | 2026-08-28 |
| 3 | 来源溯源 | `【来源:{Path(file_path).name}】` 前缀——回答可追溯（福客 A4），file_path 空回退「未知」 | 2026-08-28 |
| 4 | 错误兜底 | try/except 不抛异常，返回"暂不可用+转人工"提示，LLM 不得编造 | 2026-08-28 |
| 5 | 错误处理分层（与商品 tool 对齐） | ① 超时保护（wait_for 10s）② 瞬时错误自动重试 1 次 ③ 错误分类（db_timeout/db_connection 瞬时 vs db_config 永久）+ retryable=false；错误路径统一 JSON 协议（成功/空保持文本） | 2026-08-31 |
| 6 | 入参校验（用户确认） | query 空值 → error/invalid_argument（retryable=true）返回 LLM；`_tool_error` 增加 retryable 参数（与商品 tool invalid_argument 行为一致）；空串不再直接检索 | 2026-08-31 |

---

## 11. 风险与避坑清单

| # | 风险 | 对策 |
|---|------|------|
| 1 | 来源前缀增加返回长度（~30 字符/段 × 5） | 可接受（top-5 总长 ≈2500 字符）；后续可裁剪 |
| 2 | LLM 忽略空结果建议继续编造 | 空结果提示措辞明确"未检索到"；后续 agent prompt 加约束（与商品 tool 风险 #6 一致） |
| 3 | 返回格式变化影响未知消费方 | 实施步骤 3 全项目检索确认（当前无消费方，后续 agent 是唯一消费方） |
| 4 | logger 未导入导致 NameError | 实施时补 `from app.core.logger import get_logger; logger = get_logger(service="rag_tool")` |
| 5 | 自动重试放大检索压力 | 重试仅 1 次 + 仅瞬时错误；检索管线内部已有单路降级（_safe），重试叠加可控 |
| 6 | 空串检索行为未定义 | 入参校验拦截（query 空值返回 invalid_argument），空串不再进入检索管线 |
