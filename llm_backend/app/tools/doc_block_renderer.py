"""doc dict → LLM 上下文文本块公共渲染(检索结果 metadata 透出层)。

@tool 通道(rag_retrieval)与硬编码通道(customer_tools 节点)共用同一渲染函数,
保证两消费通道格式零漂移(SPEC_RAG_SKU_METADATA D5——历史教训:两处手写必漂移,
现状即 @tool 有【来源】前缀而节点无)。

前缀行字段:
- 商品编码: 块覆盖商品编码多值列表;空 = 无商品归属(政策/通用,不可据此查/断言动态数据)
- 知识类型: chapter 末级标题(通常为 H4 小节名);两处例外——H3 标题行自段末级是商品标题
  (含 (SKU:) 锚点,过滤为 —),混合块为块首章节近似值(全块精确归属看商品编码区间)
- 来源: 源文件名(溯源引用)
"""
from pathlib import Path


def render_dynamic_rows(rows: dict[str, dict]) -> str:
    """商品动态区渲染(方案 A,2026-09-06):RAG 候选集 sku 的动态行一次给全。

    每行一条:【动态|商品编码:S｜商品名:N】价格/库存/更新时间——编码与静态块
    前缀行【商品编码:】同键,LLM 无需 join 即可配对(原子性呈现)。
    调用方保证 rows 为 {sku: row_dict} 有序 dict(product_dynamic_service.fetch_by_skus)。
    """
    lines = []
    for sku, r in rows.items():
        price = r.get("current_price")
        stock = r.get("stock_quantity")
        stock_txt = "无货" if stock == 0 else f"库存{stock}"
        price_txt = f"¥{price:.2f}" if price is not None else "价格未知"
        lines.append(
            f"【动态|商品编码:{sku}｜商品名:{r.get('product_name', '')}】"
            f"{price_txt}｜{stock_txt}｜更新:{r.get('updated_at', '')}"
        )
    if not lines:
        return ""
    return "【商品动态信息区】\n" + "\n".join(lines)


def render_doc_blocks(docs: list[dict]) -> str:
    """doc dict 列表 → LLM 上下文文本(每块前缀行 + 文本,块间空行分隔)。"""
    blocks = []
    for d in docs:
        skus = d.get("sku_codes") or []
        sku_part = "|".join(skus) if skus else "—（无商品归属:政策/通用,不可查动态数据）"
        chapter = d.get("chapter") or ""
        # 末级 = H4 小节名 → 知识类型;例外:H3 标题行自段的末级是商品标题(含 (SKU:) 锚点),
        # 此时显示 "—"(标题段),避免把商品名误标为知识类型
        last = chapter.split(">")[-1].strip() if chapter else ""
        if last and "(SKU:" in last:
            last = ""
        # 注:混合块的 chapter 为块首段归属——知识类型仅为块首章节近似值,非全块精确;
        #     全块商品的精确归属由 sku_codes(字符轴区间)承担,两者语义不同勿混用
        src = Path(d.get("file_path") or "未知").name
        blocks.append(
            f"【商品编码:{sku_part}｜知识类型:{last or '—'}｜来源:{src}】\n{d.get('text', '')}"
        )
    return "\n\n".join(blocks)
