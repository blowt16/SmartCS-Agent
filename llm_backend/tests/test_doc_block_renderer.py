"""render_doc_blocks 三态渲染测试(纯函数,不连库)——SPEC_RAG_SKU_METADATA A8。

覆盖:有 sku 块 / 无 sku 块(无商品归属语义) / 无 chapter 块 / H3 标题段(含 (SKU:)
锚点)不误标为知识类型 / 多值 sku 与混合块近似注记的渲染行为。
"""
from app.tools.doc_block_renderer import render_doc_blocks, render_dynamic_rows


def _doc(**kw) -> dict:
    base = {"text": "块内容", "file_path": "D:/kb/京东智能家具产品知识文档.docx", "sku_codes": [], "chapter": ""}
    base.update(kw)
    return base


def test_single_sku_chunk():
    out = render_doc_blocks([_doc(sku_codes=["JD-SOF-001"], chapter="京东智能家具产品知识文档 > 一、电动智能沙发 > 芝华仕 XX (SKU:JD-SOF-001) > 规格参数")])
    assert "【商品编码:JD-SOF-001｜知识类型:规格参数｜来源:京东智能家具产品知识文档.docx】" in out
    assert "块内容" in out


def test_multi_sku_chunk_pipe_joined():
    out = render_doc_blocks([_doc(sku_codes=["JD-SOF-001", "JD-SOF-002"])])
    assert "商品编码:JD-SOF-001|JD-SOF-002" in out


def test_no_sku_chunk_explicit_marker():
    """政策/通用块:显式"无商品归属"标记,防 LLM 据此查/断言单一商品动态数据。"""
    out = render_doc_blocks([_doc(chapter="京东自营售后政策 > 七天无理由退货")])
    assert "无商品归属" in out
    assert "不可查动态数据" in out
    assert "商品编码:—" in out


def test_no_chapter_chunk_type_dash():
    out = render_doc_blocks([_doc(sku_codes=["JD-CUR-001"])])
    assert "知识类型:—" in out


def test_h3_title_segment_sku_suffix_not_type():
    """H3 标题行自段:末级含 (SKU:) 锚点 → 知识类型显示 —(防商品名误标为类型)。"""
    out = render_doc_blocks([_doc(sku_codes=["JD-SOF-001"], chapter="京东智能家具产品知识文档 > 一、电动智能沙发 > 芝华仕 XX (SKU:JD-SOF-001)")])
    assert "知识类型:—" in out
    assert "芝华仕" not in out.split("｜知识类型:")[1][:20]  # 类型位不含商品名


def test_empty_docs_returns_empty():
    assert render_doc_blocks([]) == ""


# ==================== 动态区渲染(方案 A) ====================


def _row(sku="JD-DRY-003", name="米家智能晾衣机2", price=783.33, stock=50):
    return {"sku": sku, "product_name": name, "category": "智能晾衣架",
            "current_price": price, "stock_quantity": stock, "updated_at": "2026-09-06T10:00:00"}


def test_dynamic_rows_rendered():
    out = render_dynamic_rows({"JD-DRY-003": _row()})
    assert "【商品动态信息区】" in out
    assert "【动态|商品编码:JD-DRY-003｜商品名:米家智能晾衣机2】¥783.33｜库存50｜更新:2026-09-06T10:00:00" in out


def test_dynamic_rows_zero_stock_wording():
    out = render_dynamic_rows({"JD-LCK-001": _row(sku="JD-LCK-001", stock=0)})
    assert "无货" in out


def test_dynamic_rows_empty_dict():
    assert render_dynamic_rows({}) == ""
