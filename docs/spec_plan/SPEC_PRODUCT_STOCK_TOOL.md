# 商品动态数据检索 Tool 实施规格（langchain 格式）

> **用途**: 将数据库 `product_price_stock` 表（商品价格/库存动态信息）检索封装为 langchain `@tool`，供后续业务子 agent（售前/售后）通过 tool calling 检索商品动态数据；与现有 `rag_retrieval`（docx 知识库检索）互补——动态数据查库、静态知识查向量
> **技术栈**: langchain-core `@tool`（与 `rag_tool.py` 同模式）+ SQLAlchemy async（AsyncSessionLocal）+ PostgreSQL `product_price_stock` 表（模型/导入脚本已就绪）
> **状态**: 待实施（2026-08-28 设计定稿，用户确认：检索范围=仅价格/库存动态数据；输入=名称模糊 + 品类过滤）
> **关联文档**: [[SPEC_INTENT_RECOGNITION_OPTIMIZATION.md]]（§8.3 售后 agent 方案 C：RAG tool + 信息确认骨架，本 tool 为其前置）[[CLAUDE.md]]（知识分层原则：动态信息只入 product_price_stock 表）

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

1. 知识分层落地后（CLAUDE.md）：**价格/库存为动态信息，只存 `product_price_stock` 表，禁止写入 docx**——现有 `rag_retrieval` tool（`rag_tool.py`）检索的是 docx 知识块，**已检索不到价格/库存**（docstring 仍写"含价格、库存"，过时）
2. 后续业务子 agent 方案（SPEC_INTENT_RECOGNITION_OPTIMIZATION §8.3）：售前 agent 需要查价格/库存回答"这款灯多少钱""有货吗"，售后 agent 信息确认后可能查订单关联商品——**都需要结构化商品数据检索能力**
3. 项目已有 langchain tool 封装先例（`rag_tool.py` 的 `rag_retrieval`），新 tool 复用同模式，后续 `llm.bind_tools([...])` 直接可用

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 商品动态数据检索 | `product_stock_lookup` tool：按商品名模糊 + 品类过滤查价格/库存，返回 JSON 记录列表 |
| langchain 格式 | `@tool` 装饰器 + pydantic 参数 schema，`bind_tools` 可直接使用 |
| 与 RAG 互补 | 动态数据（价格/库存）查本 tool；静态知识（参数/规格/政策）查 `rag_retrieval`——职责清晰 |
| 可测试 | pytest 覆盖查询逻辑（真实 DB 或 mock 会话） |

### 1.3 明确不做（MVP 边界）

- TSV 静态信息（品类/品牌/参数/规格/售后）入库——不在本 spec 范围（现有 docx→RAG 路径已承载）
- 价格历史/变动记录表——只有当前价
- 排序/分页的高级查询（按价格排序、翻页）——YAGNI，`limit` 即可
- 缓存层——MVP 直接查库（表小，50 行级）

---

## 2. 现状与问题

| 现状 | 说明 |
|------|------|
| `product_price_stock` 表 | 模型（`app/models/product_price_stock.py`）+ 导入脚本（`scripts/import_product_price_stock.py` 幂等 upsert）已就绪 |
| **无检索 service** | 表建好后没有任何查询封装（grep 全项目 0 命中） |
| `rag_tool.py` 过时 | docstring 声称检索"价格、库存"，实际已检不到（动态信息移出 docx） |
| `function_tools.py` | 自研 ToolRegistry（OpenAI function calling 格式），**非 langchain tool**——后续 agent 用 langchain `bind_tools`，需 langchain 格式 |

**问题**：子 agent 要回答"XX 多少钱/有货吗"时，无任何可调用的商品数据检索能力。

---

## 3. 设计决策

### 3.1 检索范围

| 候选 | 结论 | 理由 |
|---|---|---|
| **仅 product_price_stock 动态数据** | ✅ 采纳（用户确认） | 与 rag_retrieval 互补、职责清晰；TSV 静态信息已 docx 化走 RAG |
| 动态+静态都查 | 否决 | 需先把 TSV 静态信息入库（新表），工作量大，超出本 spec |
| 合并到 rag_retrieval | 否决 | 耦合两种检索（向量 vs 结构化），调用方无法区分 |

### 3.2 输入参数

| 候选 | 结论 | 理由 |
|---|---|---|
| **product_name 模糊（ILIKE）+ category 过滤 + limit** | ✅ 采纳（用户确认） | TSV 商品名是完整长名（如"京东京造 智能电动沙发 多功能可躺可摇真皮电动沙发"），LLM 从对话抽取完整名精确匹配几乎不可能；模糊匹配传简称（"门锁"）即可命中；品类过滤覆盖"智能门锁有哪些多少钱"类问法 |
| 仅精确匹配 | 否决 | 实际几乎查不到（长名抽取失败率高） |
| 自由文本 query | 否决 | 内部黑盒，不好调试；结构化参数更利于 LLM 生成与评测 |

### 3.3 返回格式

| 候选 | 结论 | 理由 |
|---|---|---|
| **结构化三态 JSON**（status=ok / empty / error + message + data） | ✅ 采纳（用户补充） | langchain tool 返回 str；统一 JSON 协议，**错误/空结果携带详细 message 与可执行建议**——LLM 读错误信息自主决策（换关键词/转 rag_retrieval/向用户澄清），不抛异常终止 agent 循环；三态区分"成功有数据/正常无匹配/调用失败" |
| 纯 JSON 数组（空结果 `[]`） | 否决 | 无匹配与调用失败无法区分，LLM 无法判断"没找到"还是"查询出错了" |
| 抛异常 | 否决 | langchain 工具异常会中断工具调用流程，错误信息不经 LLM 判断，恢复能力差 |

---

## 4. 目标设计

### 4.1 Tool 定义（`llm_backend/app/services/product_stock_tool.py`）

与 `rag_tool.py` 同模式：

```python
"""商品动态数据检索工具（langchain @tool 薄封装）

将 product_price_stock 表（价格/库存动态信息）检索封装为 agent 可调用的工具。
与 rag_retrieval 互补：动态数据（价格/库存）查本工具，静态知识（参数/政策）查知识库。
所有结果（成功/空/错误）均返回结构化 JSON，错误信息供 LLM 自主判断下一步。

用法（后续 agent 接入）：
    from app.services.product_stock_tool import product_stock_lookup
    llm.bind_tools([product_stock_lookup, rag_retrieval])
"""
import asyncio
import json
from typing import Optional

from langchain_core.tools import tool
from sqlalchemy import func, select
from sqlalchemy.exc import InvalidPasswordError, OperationalError, ProgrammingError

from app.core.database import AsyncSessionLocal
from app.models.product_price_stock import ProductPriceStock

# 查询超时与重试配置
DB_TIMEOUT_SECONDS = 10.0   # 超时保护：DB 挂死时 tool 调用不无限等待
DB_RETRY_TIMES = 1          # 瞬时错误自动重试次数（SELECT 幂等，重试安全）
DB_RETRY_INTERVAL = 0.5     # 重试间隔（秒）


def _normalize_keyword(name: str) -> str:
    """参数预处理：去全部空格 + 转义 ILIKE 通配符（%/_），防止注入式全表匹配。

    空格去除后与表内名称（同样压缩空格）连续匹配——"京东京造 智能门锁"
    可命中表内"京东京造 智能门锁 全自动3D人脸识别"（保留词序，精确度高）。
    """
    return name.replace(" ", "").replace("%", r"\%").replace("_", r"\_")


def _classify_error(e: Exception) -> tuple[str, bool]:
    """异常 → (error_type, retryable)。

    瞬时类（超时/连接）可自动重试；永久类（配置/认证/SQL）重试无意义。
    """
    if isinstance(e, asyncio.TimeoutError):
        return "db_timeout", True
    if isinstance(e, OperationalError):          # 连接失败/断连/连接池耗尽
        return "db_connection", True
    if isinstance(e, (ProgrammingError, InvalidPasswordError)):
        return "db_config", False                # 表不存在/认证失败/SQL 错误
    return "unknown", False


async def _query_with_retry(stmt) -> list:
    """超时保护 + 瞬时错误自动重试。

    asyncio.wait_for 防 DB 挂死；瞬时错误（timeout/connection）自动重试
    DB_RETRY_TIMES 次；永久错误不重试直接抛出。重试后仍失败由调用方
    返回 error（retryable=false，LLM 不再盲目重试）。
    """
    for attempt in range(DB_RETRY_TIMES + 1):
        try:
            async with AsyncSessionLocal() as session:
                return (await asyncio.wait_for(
                    session.execute(stmt), timeout=DB_TIMEOUT_SECONDS
                )).scalars().all()
        except Exception as e:
            _, retryable = _classify_error(e)
            if attempt < DB_RETRY_TIMES and retryable:
                await asyncio.sleep(DB_RETRY_INTERVAL)
                continue
            raise


def _ok(records: list[dict]) -> str:
    return json.dumps({"status": "ok", "count": len(records), "data": records}, ensure_ascii=False)


def _empty(product_name: str, category: Optional[str]) -> str:
    msg = (
        f"未找到名称包含「{product_name}」的商品"
        + (f"（品类：{category}）" if category else "")
        + "。建议：1) 缩短或更换商品名关键词重试；"
        + "2) 若用户询问的是商品参数/规格/售后政策等静态信息，请改用 rag_retrieval 工具；"
        + "3) 若用户提供的商品名过于模糊，可向用户澄清具体商品。"
    )
    return json.dumps({"status": "empty", "count": 0, "data": [], "message": msg}, ensure_ascii=False)


def _error(error_type: str, retryable: bool, message: str) -> str:
    """错误信息（统一协议）：error_type 分类 + retryable 标志供 LLM 决策。

    注意：tool 内部已自动重试过瞬时错误，返回的 error 一律 retryable=false
    （LLM 不再盲目重试）；仅 invalid_argument 为 true（修正参数后重试）。
    """
    return json.dumps(
        {"status": "error", "error_type": error_type, "retryable": retryable,
         "count": 0, "data": [], "message": message},
        ensure_ascii=False,
    )


@tool
async def product_stock_lookup(
    product_name: str,
    category: Optional[str] = None,
    limit: int = 5,
) -> str:
    """查询商品实时价格与库存。

    当用户询问商品价格、是否有货、库存情况时使用。动态数据（价格/库存）存储在
    数据库中，商品参数/规格/售后政策等静态信息请使用 rag_retrieval。

    Args:
        product_name: 商品名称关键词（支持模糊匹配，传简称即可，如"门锁"）
        category: 品类过滤（可选，如"智能门锁"）；不传则按名称模糊全表匹配
        limit: 返回条数上限（默认 5，最大 20）

    Returns:
        结构化 JSON 字符串（status=ok/empty/error）：
        - ok: {"status":"ok","count":N,"data":[{product_name,category,current_price,stock_quantity,updated_at}]}
        - empty: 无匹配，message 含建议
        - error: 入参/数据库异常，error_type + message 供 LLM 判断
    """
    # 入参校验：product_name 为空 → 明确错误，引导 LLM 提取商品名重试（retryable=true）
    if not product_name or not product_name.strip():
        return _error(
            "invalid_argument", True,
            "参数错误：product_name 不能为空。请从用户消息中提取商品名称关键词（可传简称）后重试，"
            "若确实无法提取，请向用户询问具体想查询哪款商品。",
        )
    limit = max(1, min(int(limit), 20))  # 钳制 1~20，防超量
    kw = _normalize_keyword(product_name)  # 去空格 + 通配符转义

    stmt = (
        select(ProductPriceStock)
        .where(
            func.replace(ProductPriceStock.product_name, " ", "").ilike(
                f"%{kw}%", escape="\\"
            )
        )
        .limit(limit)
    )
    if category:
        stmt = stmt.where(ProductPriceStock.category == category)

    try:
        # 超时保护（10s）+ 瞬时错误自动重试 1 次；重试后仍失败 → 返回 error
        rows = await _query_with_retry(stmt)
    except Exception as e:
        error_type, _ = _classify_error(e)
        return _error(
            error_type, False,
            f"商品数据查询失败（{error_type}），已自动重试仍未恢复。"
            "请告知用户当前价格查询暂不可用，稍后重试或转人工，不要编造价格。",
        )

    if not rows:
        return _empty(product_name, category)

    records = [
        {
            "product_name": r.product_name,
            "category": r.category,
            "current_price": float(r.current_price),
            "stock_quantity": r.stock_quantity,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
    return _ok(records)
```

### 4.2 查询逻辑与返回协议要点

**返回协议（三态 JSON）**——所有路径都返回结构化 JSON，不抛异常：

| 状态 | 触发 | 返回 | LLM 可执行的建议（message） |
|------|------|------|---------------------------|
| `ok` | 查到 ≥1 条 | count + data 数组 | — |
| `empty` | 无匹配（正常查询结果） | count=0 + message | 换关键词重试 / 转 rag_retrieval 查静态信息 / 向用户澄清 |
| `error/invalid_argument` | product_name 空 | error_type + **retryable=true** + message | 重新提取商品名重试 / 询问用户具体商品 |
| `error/db_timeout` | 查询超时（10s，自动重试后） | error_type + **retryable=false** + message | 告知用户暂不可用稍后重试 / 转人工 |
| `error/db_connection` | 连接失败/断连（自动重试后） | error_type + **retryable=false** + message | 同上 |
| `error/db_config` | 表不存在/认证失败/SQL 错误（不重试） | error_type + **retryable=false** + message | 告知服务不可用，转人工 |
| `error/unknown` | 其他异常 | error_type + **retryable=false** + message | 同上 |

**错误处理分层**（解决 DB 连接等问题的三道防线）：

1. **超时保护**：查询包 `asyncio.wait_for(10s)`——DB 挂死时 tool 调用不无限等待，变成"可分类的错误"
2. **自动重试**：瞬时错误（`db_timeout`/`db_connection`）tool 内部自动重试 1 次（0.5s 间隔）——SELECT 幂等，重试安全；大部分瞬时故障自愈，根本不返回错误
3. **错误分类 + retryable**：`_classify_error` 将异常映射为 `error_type` + `retryable`（瞬时可重试 / 永久不重试）；**tool 已自动重试过，返回给 LLM 的 error 一律 `retryable=false`**——避免 LLM 盲目重试循环；仅 `invalid_argument` 为 true（改参数后重试有意义）

其他要点：

- **去空格容错匹配**（用户确认方案）：参数与表内名称均压缩空格后连续匹配——`func.replace(列, ' ', '') ILIKE '%压缩后关键词%'`。"京东京造 智能门锁"（参数含空格）可命中表内"京东京造 智能门锁 全自动3D人脸识别"；**保留词序约束**（精确度高，优于 AND 多词拆分）；参数侧同时转义 `%`/`_` 通配符（防注入式全表匹配）
- **品类过滤**：`category = '智能门锁'` 精确等于（品类名来自 TSV 固定集合）
- **排序**：不排序（按表内 id 顺序），LLM 从结果中自行选择——MVP 简单化
- **limit 钳制**：1~20（`int()` 转换 + 钳制，防 LLM 传非数字/超大值）
- **库存语义**：`stock_quantity=0` 表示无货，输出中保留原始值，由 LLM 理解（"无货"）

---

## 5. 文件改动清单

| 文件 | 动作 | 内容 |
|------|------|------|
| `llm_backend/app/services/product_stock_tool.py` | **新增** | `product_stock_lookup` @tool（§4.1） |
| `llm_backend/app/services/rag_tool.py` | 修改 | **docstring 修正**：删除"含价格、库存"表述，改为"动态数据（价格/库存）请用 product_stock_lookup" |
| `llm_backend/tests/test_product_stock_tool.py` | **新增** | pytest：查询逻辑（mock 或真实 DB） |

---

## 6. 边界情况处理表

| # | 场景 | 预期行为 |
|---|------|---------|
| 1 | 精确名称（"京东京造智能门锁…完整名"） | status=ok，ILIKE 命中 1 条 |
| 2 | 简称（"门锁"） | status=ok，ILIKE 命中多条，limit 截断 |
| 3 | 名称 + 品类组合（"门锁" + 智能门锁） | status=ok，双重过滤，结果收窄 |
| 4 | 无匹配（"冰箱"——不在商品集） | status=empty，message 建议换关键词/转 rag_retrieval/向用户澄清 |
| 5 | product_name 为空（LLM 参数错误） | status=error/invalid_argument，message 引导重新提取商品名重试 |
| 6 | LLM 传 limit=100 或非数字 | 钳制为 20（int 转换 + 钳制，非数字按 5） |
| 7 | 库存 0 商品 | status=ok，返回 stock_quantity=0，LLM 回答"无货" |
| 8 | 价格范围格式（"1999-2999 元"已均值入库） | 返回单值 current_price（导入脚本已处理） |
| 9 | DB 连接异常 | 自动重试 1 次 → 仍失败 → status=error/db_connection（retryable=false），message 告知暂不可用/转人工（**不抛异常**，错误信息给 LLM 判断） |
| 10 | 通配符注入（LLM 传 "%" 或 "_"） | `_normalize_keyword` 转义为 `\%`/`\_` + `escape="\\"`——匹配不到任何商品返回 empty（而非全表返回） |
| 11 | DB 挂死/查询超时 | `asyncio.wait_for(10s)` 触发 → 自动重试 1 次 → 仍超时 → error/db_timeout（retryable=false），不无限等待 |
| 12 | 表不存在/认证失败（永久错误） | 不重试（`_classify_error` 判 db_config）→ error/db_config（retryable=false），LLM 直接转人工 |

---

## 7. 影响面分析

| 面 | 影响 |
|----|------|
| 现有 RAG 链路 | 无影响（新 tool 独立，不改检索管线） |
| `rag_retrieval` | 仅 docstring 修正，行为不变 |
| 意图识别模块 | 无影响（tool 供后续 agent 用，当前主图不调用） |
| `product_price_stock` 表/导入脚本 | 无影响（只读查询） |
| 前端/API | 无影响（无新端点） |

---

## 8. 实施步骤

1. **新增 `product_stock_tool.py`**（§4.1）→ 验证：`uv run python -c "from app.services.product_stock_tool import product_stock_lookup; print(product_stock_lookup.name)"` 输出 `product_stock_lookup`
2. **修正 `rag_tool.py` docstring** → 验证：无"价格、库存"误导表述
3. **pytest 测试**（`tests/test_product_stock_tool.py`）：mock 会话验证查询参数拼接（ILIKE/category/limit 钳制）+ 真实 DB 集成用例（若有 Postgres）
4. **验证 tool calling 可用性**：`llm.bind_tools([product_stock_lookup])` 绑定不报错

---

## 9. 验证方案

| 验证项 | 方法 |
|--------|------|
| 查询正确性 | pytest：mock 数据验证 ILIKE/category/limit/空结果/钳制逻辑 |
| 真实数据（可选） | Postgres 起库 + 导入脚本后，`product_stock_lookup.ainvoke({"product_name": "门锁"})` 返回真实记录 |
| bind_tools 兼容 | DeepSeek/Ollama 绑定 tool 定义不报错（工具 schema 生成成功） |
| 后续 agent 预留 | spec 记录：售前/售后 agent 方案中 `bind_tools([product_stock_lookup, rag_retrieval])` |

---

## 10. 决策记录

| # | 决策 | 结论 | 时间 |
|---|------|------|------|
| 1 | 检索范围 | 仅 product_price_stock 动态数据（价格/库存）；静态知识继续 rag_retrieval | 2026-08-28 |
| 2 | 输入参数 | product_name 模糊（ILIKE）+ category 可选 + limit（1~20 钳制） | 2026-08-28 |
| 3 | 返回格式 | JSON 数组字符串，空结果 `[]` | 2026-08-28 |
| 4 | 封装模式 | langchain `@tool` 薄封装（复用 rag_tool.py 模式），内部直接 SQLAlchemy 查询 | 2026-08-28 |
| 5 | 错误处理（用户补充） | **不抛异常**——所有路径（成功/空/入参错误/DB 异常）返回结构化三态 JSON，error/empty 携带详细 message 与可执行建议，供 LLM 自主判断下一步 | 2026-08-28 |
| 6 | 空格差异匹配（用户方案） | 参数与表内名称**两侧去空格**后连续匹配（func.replace 列 + 参数预处理），保留词序、精确度高；替代 AND 多词拆分 | 2026-08-28 |
| 7 | 通配符转义（用户采纳） | `_normalize_keyword` 转义 `%`/`_` + `ilike(..., escape="\\")`——防注入式全表匹配（边界 #10） | 2026-08-28 |
| 8 | 错误处理分层（DB 连接问题） | ① 超时保护（wait_for 10s）② 瞬时错误自动重试 1 次 ③ `_classify_error` 错误分类 + retryable 标志（error 一律 false，防 LLM 盲目重试） | 2026-08-31 |

---

## 11. 风险与避坑清单

| # | 风险 | 对策 |
|---|------|------|
| 1 | 去空格连续匹配仍会命中噪声（"灯"匹配灯带/灯泡/床头灯） | limit 截断 + LLM 从结果选；需要时后续可加名称相关度排序（LENGTH 升序）；通配符转义已在参数预处理中（_normalize_keyword） |
| 2 | LLM 抽取商品名失败（长名） | 模糊匹配已缓解；prompt 引导传简称；边界 #4 空结果转 rag_retrieval |
| 3 | tool 与 rag_retrieval 混用混乱 | docstring 明确分工；empty 状态 message 内引导转 rag_retrieval；后续 agent prompt 注明"价格库存→product_stock_lookup，参数政策→rag_retrieval" |
| 4 | AsyncSessionLocal 生命周期（tool 内建会话） | 每次调用独立 `async with`，无跨请求会话泄漏 |
| 5 | Decimal 序列化 | `float()` 转换后进 JSON（价格精度 2 位小数，float 足够） |
| 6 | 错误信息被 LLM 忽略（继续编造价格） | message 措辞明确"未找到/查询失败"；后续 agent prompt 加约束"tool 返回 error/empty 时不得编造价格，按 message 建议执行" |
| 7 | 自动重试放大 DB 压力 | 重试仅限 1 次 + 仅瞬时错误（timeout/connection），永久错误不重试；配置常量（DB_RETRY_TIMES）可调 |
