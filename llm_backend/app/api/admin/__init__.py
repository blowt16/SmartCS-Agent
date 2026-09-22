from fastapi import APIRouter, Depends

from app.core.security import require_admin
from app.api.admin import console, products, orders, knowledge, tickets

# 组级依赖:一条声明覆盖全组,避免每个端点各写一遍(漏写即越权的风险点)
admin_router = APIRouter(dependencies=[Depends(require_admin)])

admin_router.include_router(console.router, prefix="/console", tags=["admin-console"])
admin_router.include_router(products.router, prefix="/products", tags=["admin-products"])
admin_router.include_router(orders.router, prefix="/orders", tags=["admin-orders"])
admin_router.include_router(knowledge.router, prefix="/knowledge", tags=["admin-knowledge"])
admin_router.include_router(tickets.router, prefix="/tickets", tags=["admin-tickets"])
