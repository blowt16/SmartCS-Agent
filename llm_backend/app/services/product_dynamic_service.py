"""商品动态信息批量查询服务(节点链路确定性消费,非 @tool)。

方案 A(2026-09-06):product_tool 收窄为 sku-only 辅助通道后,售前 customer_tools
节点按"RAG 命中 → sku 动态补全"门控取数:本服务一次 WHERE sku IN 批量取回 RAG
候选集的全部动态行,供节点按 sku 组装"动态区"(与静态块前缀编码同键配对)。
"""
import asyncio
from typing import Dict, List

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.product_price_stock import ProductPriceStock


def _row_to_dict(r: ProductPriceStock) -> dict:
    return {
        "sku": r.sku,
        "product_name": r.product_name,   # 展示与身份核对字段
        "category": r.category,
        "current_price": float(r.current_price),
        "stock_quantity": r.stock_quantity,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


async def fetch_by_skus(skus: List[str]) -> Dict[str, dict]:
    """按 sku 列表批量精确查询(去重保序),返回 {sku: row_dict}。

    - sku 大写归一后等值匹配;未知编码不入结果(消费侧按缺失处理:该商品动态暂缺)
    - 单次 IN 查询,与 product_stock_lookup 单查共用同一张表与键语义
    """
    if not skus:
        return {}
    normalized = list(dict.fromkeys(s.upper() for s in skus if s and s.strip()))
    if not normalized:
        return {}

    stmt = select(ProductPriceStock).where(ProductPriceStock.sku.in_(normalized))
    try:
        async with AsyncSessionLocal() as session:
            rows = (await asyncio.wait_for(
                session.execute(stmt), timeout=settings.TOOL_DB_TIMEOUT_SECONDS
            )).scalars().all()
    except Exception:
        # 动态查询失败不阻塞静态检索链路:上层降级为"仅静态事实"由 summarize 口径兜底
        return {}

    by_sku = {}
    for r in rows:
        by_sku[r.sku] = _row_to_dict(r)
    # 保持入参顺序(节点侧与块编码顺序一致便于 LLM 对应)
    ordered = {s: by_sku[s] for s in normalized if s in by_sku}
    return ordered
