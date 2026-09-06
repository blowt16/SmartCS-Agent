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


def _empty(sku: str) -> str:
    msg = (
        f"商品编码 {sku} 未找到：该商品不在在售商品库中（编码不存在或已下线）。建议："
        "1) 核对编码是否与 rag_retrieval 返回的【商品编码:】前缀一致（含大小写）；"
        "2) 若用户询问的是参数/规格/售后政策等静态信息，请改用 rag_retrieval 工具；"
        "3) 若确实无此商品动态数据，如实告知用户『该商品动态信息暂未收录』，不要编造价格。"
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

    sku 为唯一入参（必填）：精确等值查询，一次一个商品；编码一律大写归一。
    """

    sku: str = Field(
        description="商品编码（必填，来自 rag_retrieval 返回段的【商品编码:】前缀，"
        "如 JD-LCK-001）。为 RAG 已命中商品补全价格/库存动态信息，不用于按名称猜测检索；"
        "rag 未命中商品时不要调用本工具，如实说明动态信息暂未收录。"
    )


@tool(args_schema=ProductStockLookupInput)
async def product_stock_lookup(sku: str) -> str:
    """为商品补全实时价格与库存（sku 精确，一次一个商品）。

    本工具是 rag_retrieval 的辅助工具：商品检索准确性由 rag_retrieval 负责，
    本工具仅为 RAG 已命中（返回段含【商品编码:】前缀）的商品补全动态数据。

    何时使用本工具：
    - rag_retrieval 返回段含商品编码前缀，且用户询问该商品价格/是否有货/库存时，
      以对应编码调用（rag 前缀含多个编码的混合块：先按块正文/用户问题定位目标商品
      再传对应编码，勿取首码）

    何时不要使用本工具：
    - rag_retrieval 未检索到用户所指商品 → 不调用，如实告知"该商品动态信息暂未收录"
      （静态知识库无该商品时亦然），禁止按名称猜测检索
    - 询问商品参数/规格/功能特点/售后政策 → 使用 rag_retrieval（静态知识库检索）
    - 与业务无关的闲聊 → 直接回答，无需查询

    Returns:
        结构化 JSON 字符串（status=ok/empty/error）：
        - ok: {"status":"ok","count":N,"data":[{sku,product_name,category,current_price,stock_quantity,updated_at}]}
        - empty: 编码不存在/已下线，message 含建议与不编造指引
        - error: 入参/数据库异常，error_type + message 供 LLM 判断
    """
    # 入参校验：sku 为空 → 明确错误，引导先 rag 检索拿编码（retryable=true）
    sku = (sku or "").strip().upper()  # 大写归一，防大小写差异稳定 empty
    if not sku:
        return _error(
            "invalid_argument", True,
            "参数错误：sku 不能为空。请先调用 rag_retrieval 检索商品，从返回段的"
            "【商品编码:】前缀取得编码后，再以 sku= 调用本工具补全动态信息。",
        )

    stmt = select(ProductPriceStock).where(ProductPriceStock.sku == sku)

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
        return _empty(sku)

    records = [
        {
            "sku": r.sku,
            "product_name": r.product_name,   # 展示与身份核对字段（非检索键）
            "category": r.category,
            "current_price": float(r.current_price),
            "stock_quantity": r.stock_quantity,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
    return _ok(records)
