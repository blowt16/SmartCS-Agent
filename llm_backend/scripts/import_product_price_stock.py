"""将 TSV 商品价格/库存导入 product_price_stock 表(按 product_name 幂等 upsert,可重复执行)。

用法:
  python -m scripts.import_product_price_stock
"""
import asyncio
import csv
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent          # llm_backend
PROJECT_ROOT = ROOT_DIR.parent                             # 项目根
TSV_PATH = PROJECT_ROOT / "scripts" / "data" / "jd_smart_furniture.tsv"

sys.path.insert(0, str(ROOT_DIR))
import app.core.database  # noqa: E402 —— Windows Selector 事件循环补丁

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.product_price_stock import ProductPriceStock  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

logger = get_logger(service="import_price_stock")

COL_NAME, COL_CATEGORY, COL_PRICE, COL_AFTERSALES = "商品名称", "品类", "参考价格(元)", "售后服务"

# 测试库存填充规则(固定选取保证可复现):
# 品类行序第一款商品填 0 库存/少量,其余 50
ZERO_STOCK_CATEGORIES = ["智能晾衣架", "智能门锁"]
LOW_STOCK_CATEGORIES = {"电动智能沙发": 2, "智能床垫": 3, "电动升降桌": 5}


def parse_price(text: str) -> Decimal | None:
    """提取数字(含范围/券后等格式),多值取均值;无法解析返回 None。"""
    numbers = [float(m) for m in re.findall(r"\d+(?:\.\d+)?", text or "")]
    if not numbers:
        return None
    return Decimal(str(sum(numbers) / len(numbers))).quantize(Decimal("0.01"))


def assign_stock(category: str, is_first_in_category: bool) -> int:
    """库存填充规则:目标品类行序第一款填 0/少量,其余 50。"""
    if is_first_in_category and category in ZERO_STOCK_CATEGORIES:
        return 0
    if is_first_in_category and category in LOW_STOCK_CATEGORIES:
        return LOW_STOCK_CATEGORIES[category]
    return 50


def read_tsv_rows(path: Path) -> list[dict]:
    """读 TSV 并补充库存/价格字段;价格解析失败的行整行跳过(记警告)。"""
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    seen_categories: set[str] = set()
    result: list[dict] = []
    for idx, row in enumerate(rows, start=2):  # 2 起为 TSV 真实行号(表头占 1)
        category = row[COL_CATEGORY].strip()
        is_first = category not in seen_categories
        seen_categories.add(category)

        price = parse_price(row[COL_PRICE])
        if price is None:
            logger.warning("第 {} 行价格解析失败,整行跳过: {} ({})", idx, row[COL_NAME], row[COL_PRICE])
            continue
        result.append({
            "product_name": row[COL_NAME].strip(),
            "category": category,
            "current_price": price,
            "stock_quantity": assign_stock(category, is_first),
        })
    return result


async def upsert_rows(rows: list[dict]) -> int:
    """按 product_name 幂等 upsert(存在则更新价格/库存,不存在则插入)。"""
    async with AsyncSessionLocal() as s:
        for r in rows:
            stmt = pg_insert(ProductPriceStock).values(**r)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ProductPriceStock.product_name],
                set_={
                    "category": stmt.excluded.category,
                    "current_price": stmt.excluded.current_price,
                    "stock_quantity": stmt.excluded.stock_quantity,
                },
            )
            await s.execute(stmt)
        await s.commit()
    return len(rows)


async def main():
    rows = read_tsv_rows(TSV_PATH)
    if not rows:
        logger.error("无可入库行(价格解析全部失败),终止")
        return 1
    count = await upsert_rows(rows)
    zero = [r["product_name"] for r in rows if r["stock_quantity"] == 0]
    low = [f"{r['product_name']}({r['stock_quantity']})" for r in rows if 0 < r["stock_quantity"] < 50]
    logger.info("入库完成: {} 行 | 零库存 {} 款 | 少量 {} 款 | 其余 50", count, len(zero), len(low))
    logger.info("零库存: {}", zero)
    logger.info("少量: {}", low)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
