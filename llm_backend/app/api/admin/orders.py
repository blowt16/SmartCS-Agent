"""管理端订单管理(4 个端点):列表 / 新增 / 编辑 / 删除。

对齐 SPEC_ADMIN_CONSOLE §5.6、§5.3(分页与序列化约定)。
"""
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.order import Order
from app.models.product_price_stock import ProductPriceStock
from app.models.user import User
from app.schemas.admin import OrderCreate, OrderUpdate

router = APIRouter()


def _serialize(o: Order) -> dict:
    """列表元素结构(§5.6):新增/编辑的响应体与列表元素同结构。"""
    return {
        "id": o.id,
        "order_no": o.order_no,
        "sku": o.sku,
        "product_name": o.product_name,
        "category": o.category,
        "buyer_name": o.buyer_name,
        "buyer_code": o.buyer_code,
        # Numeric 列取出来是 Decimal,不转 float 前端会拿到字符串
        "amount": float(o.amount),
        "status": o.status,
        # Date 列无时间部分,isoformat() 直接是 YYYY-MM-DD,不经 new Date() 无时区风险
        "order_date": o.order_date.isoformat(),
        # 不查文件系统是否存在,前端 onerror 兜底(§6.5)
        "image": f"/products/{o.sku}.svg",
    }


async def _next_order_no(db: AsyncSession) -> str:
    """ORD-{max(现有序号)+1:03d}。

    序号从现有 order_no 用 ORD-(\\d+) 解析取最大值,**不是 COUNT(*)+1**:
    删过订单后 COUNT+1 会撞 order_no 唯一约束。
    """
    order_nos = (await db.execute(select(Order.order_no))).scalars().all()
    nums = [int(m.group(1)) for n in order_nos if (m := re.match(r"ORD-(\d+)", n))]
    return f"ORD-{max(nums) + 1 if nums else 1:03d}"


@router.get("")
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: str = Query(""),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """订单分页列表:keyword 模糊匹配 order_no/product_name/buyer_name,status 精确。"""
    conds = []
    kw = (keyword or "").strip()
    if kw:
        conds.append(or_(
            Order.order_no.ilike(f"%{kw}%"),
            Order.product_name.ilike(f"%{kw}%"),
            Order.buyer_name.ilike(f"%{kw}%"),
        ))
    if status:
        conds.append(Order.status == status)

    # total 必须是过滤后的全量条数:用同一组 where 另跑一次 count。
    # 不能取 len(items)——那样分页下 total 恒等于 page_size,且不会报错。
    total = (await db.execute(
        select(func.count()).select_from(Order).where(*conds)
    )).scalar_one()

    rows = (await db.execute(
        select(Order)
        .where(*conds)
        .order_by(Order.order_date.desc(), Order.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(o) for o in rows],
    }


@router.post("")
async def create_order(payload: OrderCreate, db: AsyncSession = Depends(get_db)):
    """新增订单(§5.6 的 7 步):商品快照回填 + order_no 生成。"""
    product = (await db.execute(
        select(ProductPriceStock).where(ProductPriceStock.sku == payload.product_sku)
    )).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=400, detail=f"商品不存在: {payload.product_sku}")

    if payload.user_id is not None:
        # 不能省:orders.user_id 有外键,传不存在的 id 会抛 IntegrityError → 500(而非可读的 400)
        exists = (await db.execute(
            select(User.id).where(User.id == payload.user_id)
        )).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=400, detail=f"用户不存在: {payload.user_id}")

    # 先取成局部变量:撞号重试里 rollback 会让 ORM 对象过期,再读属性会触发刷新(异步下报错)
    product_name = product.product_name
    category = product.category
    amount = payload.amount if payload.amount is not None else product.current_price
    # UTC 日期,与 §5.4(a) 图表日期轴同基准;不用 date.today()——本地日期在早 8 小时窗口内错位一天
    order_date = payload.order_date or datetime.now(timezone.utc).date()

    for attempt in range(3):  # 并发撞号:重算 max 再试,最多 3 次
        try:
            order = Order(
                order_no=await _next_order_no(db),
                sku=payload.product_sku,
                product_name=product_name,
                category=category,
                buyer_name=payload.buyer_name,
                buyer_code=payload.buyer_code,
                user_id=payload.user_id,
                amount=amount,
                status=payload.status,
                order_date=order_date,
            )
            db.add(order)
            await db.flush()
            break
        except IntegrityError:
            # rollback 必须显式写(get_db 在 yield 后才 rollback,这里要立刻回滚才能重算 max)
            await db.rollback()
            if attempt == 2:
                raise HTTPException(status_code=409, detail="订单号生成冲突，请重试")

    return _serialize(order)


@router.put("/{order_id}")
async def update_order(
    order_id: int,
    payload: OrderUpdate,
    db: AsyncSession = Depends(get_db),
):
    """编辑订单。sku/product_name/category 不可改(schema 里就没有,换商品应删除后重建)。"""
    order = (await db.execute(
        select(Order).where(Order.id == order_id)
    )).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单不存在: {order_id}")

    # exclude_unset 区分"没传"与"传了 null";buyer_code/user_id 需要能清空
    data = payload.model_dump(exclude_unset=True)
    if data.get("user_id") is not None:
        # 同新增:外键不存在的 id 会抛 IntegrityError → 500
        exists = (await db.execute(
            select(User.id).where(User.id == data["user_id"])
        )).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=400, detail=f"用户不存在: {data['user_id']}")

    for field, value in data.items():
        setattr(order, field, value)

    return _serialize(order)


@router.delete("/{order_id}")
async def delete_order(order_id: int, db: AsyncSession = Depends(get_db)):
    """删除订单。"""
    order = (await db.execute(
        select(Order).where(Order.id == order_id)
    )).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail=f"订单不存在: {order_id}")

    await db.delete(order)
    return {"id": order_id, "deleted": True}
