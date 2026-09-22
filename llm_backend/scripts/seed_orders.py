"""订单种子数据(18 条,幂等 upsert,可重复执行)。

用法:
  python scripts/seed_orders.py

幂等策略是【upsert 覆盖】而不是"已存在则跳过":order_date 是"运行日往前推 index%14 天",
若跳过则已有 18 行的日期永不刷新——一周后控制台"近 7 日趋势"的订单折线会全是 0,
而统计卡片仍写着"订单 18"。upsert 让重跑同时起到"刷新演示数据"的作用。
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent          # llm_backend
sys.path.insert(0, str(ROOT_DIR))
import app.core.database  # noqa: E402 —— Windows Selector 事件循环补丁

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.product_price_stock import ProductPriceStock  # noqa: E402
from app.models.user import User  # noqa: E402

logger = get_logger(service="seed_orders")

ORDER_COUNT = 18

# 买家名单(轮转取用);买家编码 P{序号:03d} 与之一一对应
BUYER_NAMES = [
    "沈七", "蒋六", "卫五", "楚四", "陈三", "冯二",
    "郑一", "吴十", "周九", "孙八", "钱七", "赵六",
    "李四", "王五", "张伟", "刘敏", "陈静", "杨帆",
]

# 三个状态均匀分布,保证控制台环形图三色都有
STATUSES = ["处理中", "已发货", "已送达"]


async def main() -> int:
    async with AsyncSessionLocal() as s:
        products = (await s.execute(
            select(
                ProductPriceStock.sku,
                ProductPriceStock.product_name,
                ProductPriceStock.category,
                ProductPriceStock.current_price,
            ).order_by(ProductPriceStock.sku).limit(ORDER_COUNT)
        )).all()

        if not products:
            logger.error("product_price_stock 无数据,请先跑 import_product_price_stock.py")
            return 1

        # user_id 现查轮转,不写死 [3,4,5,6] —— 库一旦重建/清过,写死会让外键直接报错
        user_ids = (await s.execute(
            select(User.id).order_by(User.id).limit(4)
        )).scalars().all()

        # 日期基准必须与图表日期轴完全一致(都用 UTC),否则折线错位一天;
        # index % 14 铺开到近 14 天,保证近 7 天每天至少 1 条、折线有起伏
        today = datetime.now(timezone.utc).date()

        rows = []
        for index, (sku, product_name, category, current_price) in enumerate(products):
            rows.append({
                "order_no": f"ORD-{index + 1:03d}",
                "sku": sku,
                "product_name": product_name,       # 下单时快照
                "category": category,
                "buyer_name": BUYER_NAMES[index % len(BUYER_NAMES)],
                "buyer_code": f"P{index + 1:03d}",
                "user_id": user_ids[index % len(user_ids)] if user_ids else None,
                "amount": current_price,            # 下单金额快照
                "status": STATUSES[index % len(STATUSES)],
                "order_date": today - timedelta(days=index % 14),
            })

        stmt = pg_insert(Order).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Order.order_no],
            set_={
                "order_date": stmt.excluded.order_date,
                "status": stmt.excluded.status,
            },
        )
        await s.execute(stmt)
        await s.commit()

        total = (await s.execute(select(Order.order_no))).scalars().all()
        logger.info("订单种子完成: 写入/更新 {} 条,表内共 {} 条", len(rows), len(total))
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
