"""管理端商品管理(5 个端点):列表 / 新增 / 编辑 / 删除 / 品类下拉。

对齐 SPEC_ADMIN_CONSOLE §5.5、§5.3(分页与序列化约定)。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.product_price_stock import ProductPriceStock
from app.schemas.admin import ProductCreate, ProductUpdate

router = APIRouter()


def _serialize(row: ProductPriceStock) -> dict:
    """列表元素结构(§5.5):新增/编辑的响应体与列表元素同结构。"""
    return {
        "sku": row.sku,
        "product_name": row.product_name,
        "category": row.category,
        # Numeric 列取出来是 Decimal,不转 float 前端会拿到字符串
        "current_price": float(row.current_price),
        "stock_quantity": row.stock_quantity,
        # 不查文件系统是否存在,前端 onerror 兜底(§6.5)
        "image": f"/products/{row.sku}.svg",
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ⚠️ 路由顺序纪律(§5.5):/categories 必须注册在任何 GET /{sku} 形态路由之前。
# Starlette 按"路径+方法"匹配,若日后新增 GET /{sku},/categories 会被 {sku}="categories"
# 抢先命中。当前设计里没有 GET /{sku}(只有 PUT/DELETE),故不存在冲突,但仍排在前面。
@router.get("/categories")
async def list_categories(db: AsyncSession = Depends(get_db)):
    """品类下拉选项:SELECT DISTINCT category ORDER BY category。"""
    rows = (await db.execute(
        select(ProductPriceStock.category)
        .distinct()
        .order_by(ProductPriceStock.category)
    )).scalars().all()
    return list(rows)


@router.get("")
async def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: str = Query(""),
    category: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """商品分页列表:keyword 模糊匹配 product_name/sku,category 精确,按 sku 升序。"""
    keyword = keyword.strip()
    conds = []
    if keyword:
        conds.append(or_(
            ProductPriceStock.product_name.ilike(f"%{keyword}%"),
            ProductPriceStock.sku.ilike(f"%{keyword}%"),
        ))
    if category:
        conds.append(ProductPriceStock.category == category)

    # total 必须是过滤后的全量条数:用同一组 where 另跑一次 count。
    # 不能取 len(items)——那样分页下 total 恒等于 page_size,且不会报错。
    total = (await db.execute(
        select(func.count()).select_from(ProductPriceStock).where(*conds)
    )).scalar_one()

    rows = (await db.execute(
        select(ProductPriceStock)
        .where(*conds)
        .order_by(ProductPriceStock.sku.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(r) for r in rows],
    }


@router.post("")
async def create_product(payload: ProductCreate, db: AsyncSession = Depends(get_db)):
    """新增商品。先查重返回可读 400:表上有 uq_product_price_stock_name,
    不先查 product_name 命中唯一约束会抛 IntegrityError → 500。"""
    dup_sku = (await db.execute(
        select(ProductPriceStock.id).where(ProductPriceStock.sku == payload.sku)
    )).scalar_one_or_none()
    if dup_sku is not None:
        raise HTTPException(status_code=400, detail=f"商品编码已存在: {payload.sku}")

    dup_name = (await db.execute(
        select(ProductPriceStock.id).where(ProductPriceStock.product_name == payload.product_name)
    )).scalar_one_or_none()
    if dup_name is not None:
        raise HTTPException(status_code=400, detail=f"商品名称已存在: {payload.product_name}")

    row = ProductPriceStock(**payload.model_dump())
    db.add(row)
    # flush 让 DB 的 server_default=func.now() 落库,再 refresh 取回 updated_at
    await db.flush()
    await db.refresh(row)
    return _serialize(row)


@router.put("/{sku}")
async def update_product(
    sku: str,
    payload: ProductUpdate,
    db: AsyncSession = Depends(get_db),
):
    """编辑商品。sku 不可改(它是 document_chunks.sku_codes 的对齐键),仅在路径参数定位。"""
    row = (await db.execute(
        select(ProductPriceStock).where(ProductPriceStock.sku == sku)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"商品不存在: {sku}")

    # product_name 是唯一键:改名撞既有名称与新增撞名同理,不先查会抛 IntegrityError → 500
    # (spec §5.5 对 POST 有此要求,§5.3 规定校验类错误一律 400)
    if payload.product_name is not None and payload.product_name != row.product_name:
        dup_name = (await db.execute(
            select(ProductPriceStock.id).where(
                ProductPriceStock.product_name == payload.product_name,
                ProductPriceStock.sku != sku,
            )
        )).scalar_one_or_none()
        if dup_name is not None:
            raise HTTPException(status_code=400, detail=f"商品名称已存在: {payload.product_name}")

    # exclude_unset 区分"没传"与"传了 null";全 None 时不更新任何列。
    # 显式 null 对 NOT NULL 列(商品名/品类/价格/库存)是非法输入,跳过而非 setattr(None) → 500
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(row, field, value)

    await db.flush()
    await db.refresh(row)
    return _serialize(row)


@router.delete("/{sku}")
async def delete_product(sku: str, db: AsyncSession = Depends(get_db)):
    """删除商品。不做级联:document_chunks 里该商品的静态知识块保留(§12-1)。"""
    row = (await db.execute(
        select(ProductPriceStock).where(ProductPriceStock.sku == sku)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"商品不存在: {sku}")

    await db.delete(row)
    return {"sku": sku, "deleted": True}
