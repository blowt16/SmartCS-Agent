from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.message import Message
from app.models.order import Order
from app.models.product_price_stock import ProductPriceStock
from app.models.ticket import Ticket
from app.models.user import User

router = APIRouter()

ORDER_STATUSES = ["处理中", "已发货", "已送达", "已签收"]
TICKET_STATUSES = ["待处理", "已解决"]


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """六张统计卡片的数字(全部实时 COUNT)。"""

    async def count(model, *conds):
        stmt = select(func.count()).select_from(model)
        if conds:
            stmt = stmt.where(*conds)
        return (await db.execute(stmt)).scalar()

    return {
        "products": {
            "total": await count(ProductPriceStock),
            "in_stock": await count(ProductPriceStock, ProductPriceStock.stock_quantity > 0),
        },
        "orders": {"total": await count(Order)},
        "knowledge": {"total": await count(Document)},  # 不按 user_id 过滤(D6)
        "tickets": {
            "total": await count(Ticket),
            "pending": await count(Ticket, Ticket.status == "待处理"),
        },
        "users": {"total": await count(User)},
        "conversations": {
            "total": await count(Conversation),
            "messages": await count(Message),
        },
    }


@router.get("/charts")
async def get_charts(days: int = Query(7, ge=1, le=30), db: AsyncSession = Depends(get_db)):
    """四张图的数据(days 默认 7,范围 1-30)。"""
    # (a) 日期轴统一 UTC:库时区 Etc/UTC,orders.order_date 也由种子按 UTC 写
    today = datetime.now(timezone.utc).date()
    days_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    labels = [d.strftime("%m-%d") for d in days_list]
    start_date = days_list[0]

    # (b) 分组计数后左连接到日期轴,缺失日补 0(否则 X 轴跳日)
    order_rows = (
        await db.execute(
            select(Order.order_date, func.count())
            .where(Order.order_date >= start_date)
            .group_by(Order.order_date)
        )
    ).all()
    order_map = {r[0]: r[1] for r in order_rows}

    conv_rows = (
        await db.execute(
            select(func.date(Conversation.created_at), func.count())
            .where(Conversation.created_at >= datetime.combine(start_date, time.min))
            .group_by(func.date(Conversation.created_at))
        )
    ).all()
    conv_map = {r[0]: r[1] for r in conv_rows}

    order_status_map = dict(
        (await db.execute(select(Order.status, func.count()).group_by(Order.status))).all()
    )
    ticket_status_map = dict(
        (await db.execute(select(Ticket.status, func.count()).group_by(Ticket.status))).all()
    )

    return {
        "trend": {
            "days": labels,
            "orders": [order_map.get(d, 0) for d in days_list],
            "conversations": [conv_map.get(d, 0) for d in days_list],
        },
        # 固定顺序,与前端图例颜色绑定(§6.6);计数为 0 也要返回该项
        "order_status": [{"name": s, "value": order_status_map.get(s, 0)} for s in ORDER_STATUSES],
        "ticket_status": [{"name": s, "value": ticket_status_map.get(s, 0)} for s in TICKET_STATUSES],
        "product_category": [
            {"name": r[0], "value": r[1]}
            for r in (
                await db.execute(
                    select(ProductPriceStock.category, func.count())
                    .group_by(ProductPriceStock.category)
                    .order_by(func.count().desc())
                )
            ).all()
        ],
    }
