"""商品动态数据检索工具（langchain @tool 薄封装）

将 product_price_stock 表（价格/库存动态信息）检索封装为 agent 可调用的工具。
与 rag_retrieval 互补：动态数据（价格/库存）查本工具，静态知识（参数/政策）查知识库。
所有结果（成功/空/错误）均返回结构化 JSON，错误信息供 LLM 自主判断下一步。

用法（后续 agent 接入）：
    from app.tools.product_stock_tool import product_stock_lookup
    llm.bind_tools([product_stock_lookup, rag_retrieval])
"""
import asyncio
import json
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from psycopg.errors import InvalidPassword  # psycopg3 驱动层认证异常（SQLAlchemy 不暴露）
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.product_price_stock import ProductPriceStock

# 超时与重试配置统一入 env（settings.TOOL_*，与 rag_retrieval 共用）


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


class ProductStockLookupInput(BaseModel):
    """商品动态数据查询参数（Pydantic args_schema——描述与范围约束进 JSON schema）。

    默认 @tool 的 parse_docstring=False：docstring 的 Args 段不进参数 schema，
    模型只能看到参数类型看不到说明。args_schema 将参数描述/示例/范围显式暴露给模型。
    """

    product_name: str = Field(
        description='商品名称关键词（模糊匹配，传简称即可，如"门锁"可命中'
        '"京东京造 智能门锁 全自动3D人脸识别"）'
    )
    category: Optional[str] = Field(
        default=None,
        description='品类过滤（可选），品类为固定集合（如"智能门锁""智能晾衣机"）；'
        "不传则按名称模糊全表匹配",
    )
    limit: int = Field(
        default=5, ge=1, le=20,
        description="返回条数上限（1~20）。泛查询（用户问'有哪些 XX/卖什么'，无指定商品）"
        "传 20 获取全量清单；单商品详情查询保持默认 5",
    )


@tool(args_schema=ProductStockLookupInput)
async def product_stock_lookup(
    product_name: str,
    category: Optional[str] = None,
    limit: int = 5,
) -> str:
    """查询商品实时价格与库存。

    当用户询问商品价格、是否有货、库存情况时使用。动态数据（价格/库存）存储在
    数据库中，商品参数/规格/售后政策等静态信息请使用 rag_retrieval。

    泛查询（用户问"有哪些沙发/卖什么"，未指定具体商品）：
    - 以品类词作为 product_name 传入（如"沙发"），并传 limit=20 获取全量清单；
      动态清单（名称/价格/库存）配合 rag_retrieval 的静态概述组装回答

    何时不要使用本工具：
    - 询问商品参数/规格/功能特点/售后政策 → 使用 rag_retrieval（静态知识库检索）
    - 与业务无关的闲聊 → 直接回答，无需查询

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
    try:
        limit = max(1, min(int(limit), 20))  # 钳制 1~20，防超量
    except (TypeError, ValueError):
        limit = 5  # 非数字按默认值（边界 #6）
    kw = _normalize_keyword(product_name)  # 去空格 + 通配符转义

    stmt = (
        select(ProductPriceStock)
        .where(
            func.replace(ProductPriceStock.product_name, " ", "").ilike(
                f"%{kw}%", escape="\\"
            )
        )
        .order_by(ProductPriceStock.updated_at.desc())  # 同名前多商品：最新更新时间在前（结果序确定）
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
