"""商品动态数据补全工具（langchain @tool 薄封装，sku 精确通道）

职责定位（2026-09-06 方案 A 整改，见对话决策"RAG 门控 + sku-only"）：
- 本工具是 rag_retrieval 的**辅助工具**：为 RAG 已检索命中的商品补全动态信息（价格/库存）
- **商品检索的召回与准确性由 rag_retrieval 负责**：rag 未命中商品 → 不调用本工具，
  回答口径为"该商品动态信息暂未收录"，禁止按名称猜测检索
- 入参仅 sku（必填，来自 rag 返回段的【商品编码:】前缀）——名称/品类模糊通道已移除
- 返回的 product_name/category 等为**展示与身份核对字段**（供 LLM 向用户展示与比对），
  不作为检索键

用法（后续 agent 接入）：
    from app.tools.product_stock_tool import product_stock_lookup
    from app.tools.rag_tool import rag_retrieval
    llm.bind_tools([rag_retrieval, product_stock_lookup])
"""
import asyncio
import json
from typing import List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from psycopg.errors import InvalidPassword  # psycopg3 驱动层认证异常（SQLAlchemy 不暴露）
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.product_price_stock import ProductPriceStock

# 超时与重试配置统一入 env（settings.TOOL_*，与 rag_retrieval 共用）


def _classify_error(e: Exception) -> tuple[str, bool]:
    """异常 → (error_type, retryable)。

    瞬时类（超时/连接）可自动重试；永久类（配置/认证/SQL）重试无意义。
    """
    if isinstance(e, asyncio.TimeoutError):
        return "db_timeout", True
    if isinstance(e, OperationalError):          # 连接失败/断连/连接池耗尽
        return "db_connection", True
    if isinstance(e, (ProgrammingError, InvalidPassword)):
        return "db_config", False                # 表不存在/认证失败/SQL 错误
    return "unknown", False


async def _query_with_retry(stmt) -> list:
    """超时保护 + 瞬时错误自动重试。

    asyncio.wait_for 防 DB 挂死；瞬时错误（timeout/connection）自动重试
    settings.TOOL_RETRY_TIMES 次；永久错误不重试直接抛出。重试后仍失败由调用方
    返回 error（retryable=false，LLM 不再盲目重试）。
    """
    for attempt in range(settings.TOOL_RETRY_TIMES + 1):
        try:
            async with AsyncSessionLocal() as session:
                return (await asyncio.wait_for(
                    session.execute(stmt), timeout=settings.TOOL_DB_TIMEOUT_SECONDS
                )).scalars().all()
        except Exception as e:
            _, retryable = _classify_error(e)
            if attempt < settings.TOOL_RETRY_TIMES and retryable:
                await asyncio.sleep(settings.TOOL_RETRY_INTERVAL)
                continue
            raise


def _ok(records: list[dict]) -> str:
    return json.dumps({"status": "ok", "count": len(records), "data": records}, ensure_ascii=False)


def _empty(codes: list[str]) -> str:
    """全缺失 empty：单码文案与方案 A 原文一致(回归锚点)；批量文案列全部入参码。"""
    if len(codes) == 1:
        sku = codes[0]
        msg = (
            f"商品编码 {sku} 未找到：该商品不在在售商品库中（编码不存在或已下线）。建议："
            "1) 核对编码是否与 rag_retrieval 返回的【商品编码:】前缀一致（含大小写）；"
            "2) 若用户询问的是参数/规格/售后政策等静态信息，请改用 rag_retrieval 工具；"
            "3) 若确实无此商品动态数据，如实告知用户『该商品动态信息暂未收录』，不要编造价格。"
        )
    else:
        codes_txt = "、".join(codes)
        msg = (
            f"商品编码 {codes_txt} 均未找到：均不在在售商品库中（编码不存在或已下线）。建议："
            "1) 核对编码是否与 rag_retrieval 返回的【商品编码:】前缀一致（含大小写）；"
            "2) 若用户询问的是参数/规格/售后政策等静态信息，请改用 rag_retrieval 工具；"
            "3) 若确实无这些商品动态数据，如实告知用户『该商品动态信息暂未收录』，不要编造价格。"
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


class ProductStockLookupInput(BaseModel):
    """商品动态数据补全参数（Pydantic args_schema——描述进 JSON schema）。

    sku 与 skus 至少提供一个：单码（sku）/ 批量（skus）一次精确取齐；
    编码一律大写归一去重。SPEC_PRODUCT_TOOL_SKU_BATCH D1~D7。
    """

    sku: Optional[str] = Field(
        default=None,
        description="商品编码（单码，来自 rag_retrieval 返回段的【商品编码:】前缀，"
        "如 JD-LCK-001）。已定位单一目标商品时使用；sku 与 skus 至少提供一个。"
        "不用于按名称猜测检索；rag 未命中商品时不要调用本工具",
    )
    skus: Optional[List[str]] = Field(
        default=None,
        description="商品编码列表（批量，一次检索全部候选的动态信息，单条查询同一时刻快照）。"
        "rag 返回段含多个编码且需一次全查（清单/对比/混合块候选）时使用；"
        "最多 20 个；与 sku 同时提供则合并去重；返回 data 顺序与入参一致",
    )


@tool(args_schema=ProductStockLookupInput)
async def product_stock_lookup(
    sku: Optional[str] = None,
    skus: Optional[List[str]] = None,
) -> str:
    """为商品补全实时价格与库存（sku 精确，单码或批量一次取齐）。

    本工具是 rag_retrieval 的辅助工具：商品检索准确性由 rag_retrieval 负责，
    本工具仅为 RAG 已命中（返回段含【商品编码:】前缀）的商品补全动态数据。

    何时使用本工具：
    - 已定位单一目标商品（rag 前缀为单码，或混合块中已按正文/用户问题定位）→ 传 sku=
    - rag 返回多个候选编码且需一次全查（清单/对比/混合块多码候选）→ 传 skus=[...]，
      单条查询同一时刻快照，返回按入参顺序；缺失编码不报错（部分命中 ok，
      全部缺失 empty），勿据缺失码推断除"未收录"外的结论

    何时不要使用本工具：
    - rag_retrieval 未检索到用户所指商品 → 不调用，如实告知"该商品动态信息暂未收录"
      （静态知识库无该商品时亦然），禁止按名称猜测检索
    - 询问商品参数/规格/功能特点/售后政策 → 使用 rag_retrieval（静态知识库检索）
    - 与业务无关的闲聊 → 直接回答，无需查询

    Returns:
        结构化 JSON 字符串（status=ok/empty/error）：
        - ok: {"status":"ok","count":N,"data":[{sku,product_name,category,current_price,stock_quantity,updated_at}]}（入参序）
        - empty: 全部编码不存在/已下线（单码文案与批量文案均含不编造指引）
        - error: 入参/数据库异常，error_type + message 供 LLM 判断
    """
    # 入参归一：合并(sku+skus) → strip+upper → 去重保序（D2/D4/D7）
    raw: list[str] = []
    if sku:
        raw.append(sku)
    if skus:
        raw.extend(skus)
    codes = list(dict.fromkeys(c.strip().upper() for c in raw if c and c.strip()))

    # 入参校验：至少一个有效编码 → 明确错误，引导先 rag 检索拿编码（retryable=true）
    if not codes:
        return _error(
            "invalid_argument", True,
            "参数错误：sku 与 skus 至少提供一个有效商品编码。请先调用 rag_retrieval 检索商品，"
            "从返回段的【商品编码:】前缀取得编码后，再以 sku=（单码）或 skus=[...]（批量）"
            "调用本工具补全动态信息。",
        )
    if len(codes) > 20:  # D6：防全库扫描式滥用
        return _error(
            "invalid_argument", True,
            f"参数错误：商品编码数量 {len(codes)} 超过上限 20。"
            "请按 rag 返回的候选范围收窄后重试（单商品用 sku=）。",
        )

    stmt = select(ProductPriceStock).where(ProductPriceStock.sku.in_(codes))

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
        return _empty(codes)

    # 部分命中：按入参序重排，缺失码静默（D5）
    by_sku = {r.sku: r for r in rows}
    records = [
        {
            "sku": r.sku,
            "product_name": r.product_name,   # 展示与身份核对字段（非检索键）
            "category": r.category,
            "current_price": float(r.current_price),
            "stock_quantity": r.stock_quantity,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for c in codes
        if (r := by_sku.get(c)) is not None
    ]
    return _ok(records)
