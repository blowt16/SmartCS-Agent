"""生成商品占位图 SVG(每个 sku 一张,输出到 frontend/public/products/)。

用法(在项目根执行):
  python scripts/build_product_placeholders.py

产物是【由 DB 数据派生的生成物】,与 knowledge_data/ 下的 docx 同性质,已在
frontend/.gitignore 里忽略(public/products/),只提交本脚本。新克隆的仓库没有这些
SVG,跑 npm run build 前必须先跑本脚本,否则页面图片全走 onerror 兜底(灰块+箱子图标)。
"""
import asyncio
import sys
from html import escape
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent          # = 项目根
sys.path.insert(0, str(PROJECT_ROOT / "llm_backend"))          # ⚠️ app 包在 llm_backend 下,不是项目根
import app.core.database  # noqa: E402,F401 —— 触发 Windows SelectorEventLoop 补丁

from sqlalchemy import select  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.product_price_stock import ProductPriceStock  # noqa: E402

logger = get_logger(service="build_placeholders")

OUTPUT_DIR = PROJECT_ROOT / "frontend" / "public" / "products"

# 品类 → 渐变起止色;未命中回退灰
CATEGORY_GRADIENTS = {
    "智能门锁": ("#2563eb", "#1d4ed8"),
    "电动智能沙发": ("#b45309", "#92400e"),
    "智能窗帘": ("#7c3aed", "#5b21b6"),
    "电动升降桌": ("#0891b2", "#0e7490"),
    "智能晾衣架": ("#059669", "#047857"),
    "智能电动床": ("#4f46e5", "#4338ca"),
    "智能床垫": ("#db2777", "#be185d"),
    "智能床头柜": ("#ea580c", "#c2410c"),
    "按摩椅": ("#dc2626", "#b91c1c"),
}
FALLBACK_GRADIENT = ("#64748b", "#475569")

SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400" role="img" aria-label="{label}">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{start}"/>
      <stop offset="100%" stop-color="{end}"/>
    </linearGradient>
  </defs>
  <rect width="400" height="400" rx="32" fill="url(#g)"/>
  <text x="200" y="205" text-anchor="middle" fill="#ffffff"
        font-family="Inter, system-ui, -apple-system, 'Microsoft YaHei', sans-serif"
        font-size="120" font-weight="700">{initial}</text>
  <text x="200" y="285" text-anchor="middle" fill="#ffffff" fill-opacity="0.7"
        font-family="Inter, system-ui, -apple-system, 'Microsoft YaHei', sans-serif"
        font-size="30" font-weight="500">{category}</text>
</svg>
"""


def pick_initial(product_name: str) -> str:
    """取首字符;若首字符是数字/字母(如 8H 智能电动床),取其前 2 个字符(单字视觉太单薄)。"""
    name = product_name.strip()
    if not name:
        return "?"
    first = name[0]
    if first.isascii() and first.isalnum():
        return name[:2]
    return first


def build_svg(sku: str, product_name: str, category: str) -> str:
    start, end = CATEGORY_GRADIENTS.get(category, FALLBACK_GRADIENT)
    return SVG_TEMPLATE.format(
        label=escape(f"{product_name} {category}"),
        start=start,
        end=end,
        initial=escape(pick_initial(product_name)),
        category=escape(category),
    )


async def main() -> int:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(
                ProductPriceStock.sku,
                ProductPriceStock.product_name,
                ProductPriceStock.category,
            ).order_by(ProductPriceStock.sku)
        )).all()

    if not rows:
        logger.error("product_price_stock 无数据,请先跑 llm_backend/scripts/import_product_price_stock.py")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for sku, product_name, category in rows:
        # 覆盖写,可重复执行(幂等)
        (OUTPUT_DIR / f"{sku}.svg").write_text(
            build_svg(sku, product_name, category), encoding="utf-8"
        )

    logger.info("占位图生成完成: {} 张", len(rows))
    logger.info("输出目录: {}", OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
