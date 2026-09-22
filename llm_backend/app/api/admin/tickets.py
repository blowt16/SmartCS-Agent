"""管理端工单接口(2 个端点)。"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models.ticket import Ticket
# ⚠️ 必须导入:PUT /{id} 的 current_user 注解在 Python 3.13 定义时求值,缺它 NameError
from app.models.user import User
from app.schemas.admin import TicketUpdate

router = APIRouter()


def _serialize(t: Ticket) -> dict:
    """工单行 -> 响应 dict(列表与保存结果同形状,均为全字段)。"""
    return {
        "id": t.id,
        "ticket_no": t.ticket_no,
        "summary": t.summary,
        "category": t.category,
        "urgency": t.urgency,
        "user_query": t.user_query,
        "status": t.status,
        "detail": t.detail,
        "suggestion": t.suggestion,
        "handler": t.handler,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
    }


@router.get("")
async def list_tickets(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: str = Query(""),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """工单列表(分页对象,items 带全字段,处理弹窗无需二次请求)。"""
    conds = []
    kw = (keyword or "").strip()
    if kw:
        conds.append(or_(
            Ticket.ticket_no.ilike(f"%{kw}%"),
            Ticket.summary.ilike(f"%{kw}%"),
            Ticket.user_query.ilike(f"%{kw}%"),
        ))
    if status:
        conds.append(Ticket.status == status)

    total = (
        await db.execute(select(func.count()).select_from(Ticket).where(*conds))
    ).scalar()
    rows = (
        await db.execute(
            select(Ticket)
            .where(*conds)
            .order_by(Ticket.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(t) for t in rows],
    }


@router.put("/{ticket_id}")
async def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """保存处理结果。ticket_no / user_query / conversation_id 不可改(schema 里就没有)。"""
    ticket = (
        await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"工单不存在: {ticket_id}")

    old_status = ticket.status
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ticket, field, value)

    if ticket.status == "已解决" and old_status != "已解决":
        # 朴素 UTC:列是 DateTime(不带时区),全库按 UTC 读
        ticket.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
        ticket.handler = current_user.username
    elif ticket.status == "待处理" and old_status == "已解决":
        ticket.resolved_at = None  # 重开单
        ticket.handler = None

    return _serialize(ticket)
