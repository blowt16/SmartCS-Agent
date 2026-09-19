"""商品静态/动态信息对齐守护测试（零 LLM，直连库）。

背景：静态块（文档 chunk，带 sku_codes 标签）与动态行（product_price_stock）
是两条独立通道，同一个商品靠【商品编码】在两侧对齐。本测试守护三条错配通道：

  A. 同一个编码在两侧指向不同商品名（商品名漂移）—— 真正的错配风险
  B. 同一个商品名对应多个编码（1:1 约束被破坏）
  C. 文档覆盖的商品在动态表缺失（信息分裂：文档有、价格无）

C 不必然是错误：TSV 价格列为"—"（无公开报价）的商品会被导入脚本整行跳过
（`import_product_price_stock.py:93-96`），其动态信息本就不存在。但缺口必须
显式登记——静态块能查到的编码在动态表缺失时，LLM 侧表现为"该商品动态信息
暂未收录"（prompt 规则已兜底），不会被误当成"检索失败"。
"""
import re

import pytest
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.models.product_price_stock import ProductPriceStock
from app.services.rag_retriever_service import get_rag_retriever_service
from app.tools.doc_block_renderer import render_dynamic_rows

SKU_IN_HEADING = re.compile(r"\(SKU:(JD-[A-Z]+-\d+)\)")


async def _product_map() -> dict[str, str]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(ProductPriceStock.sku, ProductPriceStock.product_name)
        )).all()
    return {r[0].upper(): r[1] for r in rows}


@pytest.mark.asyncio
async def test_product_name_is_unique_per_sku():
    """B：编码 ↔ 商品名必须 1:1（库约束 uq_product_price_stock_name 的运行时守护）。"""
    prod = await _product_map()
    seen: dict[str, str] = {}
    dups = []
    for sku, name in prod.items():
        if name in seen:
            dups.append((name, seen[name], sku))
        seen[name] = sku
    assert not dups, f"同一商品名对应多个编码，动态行将无法定位商品: {dups}"


@pytest.mark.asyncio
async def test_doc_heading_name_matches_product_table():
    """A：文档标题里的商品名与动态表商品名必须同源（防商品名漂移）。"""
    prod = await _product_map()
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text("SELECT content FROM document_chunks"))).all()

    mismatches = []
    checked = 0
    for (content,) in rows:
        for sku in SKU_IN_HEADING.findall(content or ""):
            if sku not in prod:
                continue  # C 类，由下一个测试负责
            checked += 1
            # 标题行形如 "商品名 (SKU:XXX)"，取该行 SKU 锚点之前的文本
            for line in content.split("\n"):
                if f"(SKU:{sku})" in line:
                    heading_name = line.split("(SKU:")[0].strip().strip("#").strip()
                    if heading_name and heading_name != prod[sku]:
                        mismatches.append((sku, heading_name, prod[sku]))
                    break

    assert checked > 0, "未在文档中解析到任何 SKU 标题，解析口径可能失效"
    assert not mismatches, (
        f"文档标题商品名与动态表不一致（静态/动态将指向不同商品）: {mismatches}"
    )


@pytest.mark.asyncio
async def test_doc_covered_skus_present_in_dynamic_table():
    """C：文档覆盖的编码应都能在动态表查到；只允许已登记的无报价商品缺失。

    缺口有增减都会失败以提示复核——正是不许静默缺失的意义：
    新增缺口可能是导入漏行（真问题），缺口消失说明该商品补上了报价（应同步更新清单）。
    """
    # TSV 价格列为 "—"（无公开报价）→ 导入脚本整行跳过，动态信息本就不存在
    EXPECTED_MISSING = {"JD-MAS-001", "JD-MAS-002", "JD-MAS-004"}

    prod = await _product_map()
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            text("SELECT DISTINCT jsonb_array_elements_text(sku_codes) FROM document_chunks "
                 "WHERE sku_codes IS NOT NULL")
        )).all()
    doc_skus = {r[0].upper() for r in rows}
    missing = set(doc_skus) - set(prod)

    unexpected = sorted(missing - EXPECTED_MISSING)
    assert not unexpected, (
        f"文档中有 {len(unexpected)} 个商品在 product_price_stock 缺失且未登记，"
        f"这些商品的价格/库存无法提供（可能是导入漏行）: {unexpected}"
    )
    resolved = sorted(EXPECTED_MISSING - missing)
    assert not resolved, (
        f"以下商品已补入动态表，请从 EXPECTED_MISSING 移除: {resolved}"
    )


@pytest.mark.asyncio
async def test_dynamic_rows_always_carry_product_name():
    """防错配的最后一环：动态区每行必须带完整商品名。

    LLM 在动态区是按【商品名 + 商品编码】配对取值的（实测错配率为零的前提）。
    若渲染格式改为只留编码，跨商品噪音下的错配风险会立即回归。
    """
    prod = await _product_map()
    sample = list(prod)[:5]
    from app.services.product_dynamic_service import fetch_by_skus

    rows = await fetch_by_skus(sample)
    assert rows, "动态查询返回为空，无法校验渲染格式"
    rendered = render_dynamic_rows(rows)
    for sku, row in rows.items():
        assert f"商品编码:{sku}" in rendered, f"动态行缺编码: {sku}"
        name = row["product_name"]
        assert f"商品名:{name}" in rendered, f"动态行缺商品名: {sku}（错配防线失效）"
