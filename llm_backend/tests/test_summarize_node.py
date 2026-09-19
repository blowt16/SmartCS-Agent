"""summarize 跨分支证据装配测试（SPEC_PLANNER_ENTITY_SPLIT_AND_RETRIEVAL §4.5）。

_assemble_evidence 是取消实体约束后的唯一过滤层——只做去重与合并，不做相关性过滤。
覆盖：chunk_id 跨分支去重 / 动态行按 sku 合并 / 无键块不被误丢 / 空装配兜底 /
records 来源兼容（dict 与 pydantic 模型两种入参形态）。
"""
import pytest
from langchain_core.runnables import RunnableLambda

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import (
    VectorSearchOutputState,
)
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.summarize.node import (
    _assemble_evidence,
    create_summarization_node,
)


def _doc(chunk_id, sku_codes=None, text="正文"):
    return {
        "chunk_id": chunk_id,
        "id": hash(chunk_id) % 10000,
        "text": text,
        "sku_codes": sku_codes or [],
        "chapter": "文档 > 智能家居 > 商品 > 规格参数",
        "file_path": "京东智能家具产品知识文档.docx",
    }


def _search(task, docs, dynamic_rows=None):
    return VectorSearchOutputState(
        task=task, query=task, errors=[],
        records={"result": "", "hybrid_docs": docs, "dynamic_rows": dynamic_rows or {}},
        steps=["execute_vector_search"],
    )


def _row(sku, name, price):
    return {"sku": sku, "product_name": name, "category": "测试",
            "current_price": price, "stock_quantity": 50, "updated_at": None}


# ---------- 去重 ----------

def test_dedup_same_chunk_across_branches():
    """同一混合块被两条子 query 各召回一次 → 证据里只出现一次。"""
    shared = _doc("c-shared", ["JD-DRY-004", "JD-DRY-003"], text="混合块正文")
    searches = [
        _search("子问A", [shared, _doc("c-a", ["JD-DRY-004"])]),
        _search("子问B", [shared, _doc("c-b", ["JD-DRY-003"])]),
    ]
    ev = _assemble_evidence(searches)
    assert ev.count("混合块正文") == 1
    assert "c-a" not in ev and "c-b" not in ev        # chunk_id 不进渲染文本，仅作去重键
    assert ev.count("【商品编码:JD-DRY-004｜") == 1
    assert ev.count("【商品编码:JD-DRY-003｜") == 1


def test_keeps_distinct_chunks_from_all_branches():
    """不同块全部保留（本层不做相关性过滤，不丢任何分支的证据）。"""
    searches = [
        _search("子问A", [_doc("c-1", ["SKU-A"], text="A的内容")]),
        _search("子问B", [_doc("c-2", ["SKU-B"], text="B的内容")]),
    ]
    ev = _assemble_evidence(searches)
    assert "A的内容" in ev and "B的内容" in ev


def test_doc_without_key_is_not_swallowed():
    """无 chunk_id/id 的块不参与去重——多个无键块必须全部保留（防静默丢证据）。"""
    no_key = {"text": "无键块", "sku_codes": [], "chapter": "", "file_path": "x.docx"}
    searches = [_search("子问A", [dict(no_key), dict(no_key), dict(no_key)])]
    ev = _assemble_evidence(searches)
    assert ev.count("无键块") == 3


# ---------- 动态区合并 ----------

def test_dynamic_rows_merged_by_sku():
    """两分支各自补全的动态行按 sku 合并成全局一份。"""
    searches = [
        _search("子问A", [_doc("c-1", ["JD-DRY-004"])],
                {"JD-DRY-004": _row("JD-DRY-004", "晾衣机3", 1194.5)}),
        _search("子问B", [_doc("c-2", ["JD-DRY-005"])],
                {"JD-DRY-005": _row("JD-DRY-005", "晾衣机Pro", 831.5)}),
    ]
    ev = _assemble_evidence(searches)
    assert "商品编码:JD-DRY-004" in ev and "商品编码:JD-DRY-005" in ev
    assert "¥1194.50" in ev and "¥831.50" in ev
    assert ev.count("【商品动态信息区】") == 1          # 全局一份


def test_dynamic_region_precedes_static():
    """动态区置前、静态块随后（与 customer_tools 单分支渲染顺序一致）。"""
    searches = [_search("子问A", [_doc("c-1", ["SKU-A"], text="静态正文")],
                        {"SKU-A": _row("SKU-A", "商品A", 100.0)})]
    ev = _assemble_evidence(searches)
    assert ev.index("【商品动态信息区】") < ev.index("静态正文")


# ---------- 空与异常 ----------

def test_empty_searches_returns_empty_string():
    assert _assemble_evidence([]) == ""


def test_searches_without_records_skipped():
    """records 为空的搜索记录被跳过，不报错。"""
    assert _assemble_evidence([{"task": "x", "records": None}]) == ""


def test_dict_records_supported():
    """records 以 dict 形式给出（非 pydantic）同样支持。"""
    searches = [{"records": {"hybrid_docs": [_doc("c-1", ["SKU-A"], text="字典形态")],
                             "dynamic_rows": {}}}]
    assert "字典形态" in _assemble_evidence(searches)


# ---------- 节点兜底 ----------

async def test_node_falls_back_when_no_evidence():
    """装配为空 → "No data to summarize."，不调用 LLM。"""
    def _explode(_messages):
        raise AssertionError("证据为空时不得调用 LLM")

    node = create_summarization_node(llm=RunnableLambda(_explode))
    out = await node({"question": "测试", "searches": []})
    assert out["summary"] == "No data to summarize."
