"""customer_tools 节点 RAG 门控动态补全测试（mock 检索与动态服务，不连库）——方案 A。

覆盖:
- 命中块含 sku → 收集候选 → 动态区置前 + 静态块同码配对
- 零文档/无 sku 块 → 不查动态服务,结果仅静态渲染
- 动态查询降级(异常) → 仅静态,不阻塞
"""
import json
from unittest.mock import AsyncMock, patch

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import (
    create_vector_search_query_node,
)


def _doc(sku_codes=None, text="静态文本"):
    return {"text": text, "file_path": "D:/kb/京东智能家具产品知识文档.docx",
            "sku_codes": sku_codes or [], "chapter": "京东智能家具产品知识文档 > 规格参数"}


def _row(sku="JD-DRY-003", price=783.33):
    return {"sku": sku, "product_name": "米家智能晾衣机2", "category": "智能晾衣架",
            "current_price": price, "stock_quantity": 50, "updated_at": "2026-09-06T10:00:00"}


async def _run(docs, rows_by_sku=None):
    retriever = AsyncMock()
    retriever.search = AsyncMock(return_value=docs)
    node = create_vector_search_query_node()
    with patch("app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node."
               "get_rag_retriever_service", return_value=retriever):
        with patch("app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node."
                   "fetch_by_skus", AsyncMock(return_value=rows_by_sku or {})) as m:
            out = await node({"task": "米家智能晾衣机2 多少钱"})
            return out, m


async def test_hit_with_sku_fetches_dynamic_and_prepends():
    docs = [_doc(sku_codes=["JD-DRY-003"])]
    out, fetch_mock = await _run(docs, {"JD-DRY-003": _row()})
    assert fetch_mock.await_args.args[0] == ["JD-DRY-003"]
    records = out["searches"][0].records
    text = records["result"]
    assert text.startswith("【商品动态信息区】")          # 动态区置前
    assert "【动态|商品编码:JD-DRY-003｜商品名:米家智能晾衣机2】¥783.33" in text
    assert "【商品编码:JD-DRY-003｜知识类型:规格参数" in text   # 静态块随后(同码配对)
    assert records["dynamic_rows"]["JD-DRY-003"]["current_price"] == 783.33


async def test_mixed_chunk_dedup_collect_all_skus():
    docs = [
        _doc(sku_codes=["JD-DRY-003", "JD-DRY-002"], text="混合块A"),
        _doc(sku_codes=["JD-DRY-002"], text="块B"),
    ]
    rows = {"JD-DRY-003": _row(), "JD-DRY-002": _row(sku="JD-DRY-002", price=899.0)}
    out, fetch_mock = await _run(docs, rows)
    assert fetch_mock.await_args.args[0] == ["JD-DRY-003", "JD-DRY-002"]  # 去重保序
    assert "JD-DRY-003" in out["searches"][0].records["result"]
    assert "JD-DRY-002" in out["searches"][0].records["result"]


async def test_no_docs_no_dynamic_call():
    out, fetch_mock = await _run([])
    assert not fetch_mock.await_args                        # 零文档不查动态
    assert out["searches"][0].records["result"] == ""       # 仅静态渲染(空)


async def test_policy_chunk_no_sku_no_dynamic_call():
    docs = [_doc(sku_codes=[], text="七天无理由退货政策...")]
    out, fetch_mock = await _run(docs)
    assert not fetch_mock.await_args
    assert "无商品归属" in out["searches"][0].records["result"]  # 政策块渲染保留


async def test_dynamic_fetch_empty_degrades_to_static_only():
    """动态服务契约=异常内兜底返回 {}(见 fetch_by_skus);节点侧空结果=仅静态,不阻塞。"""
    docs = [_doc(sku_codes=["JD-DRY-003"])]
    out, _ = await _run(docs, {})
    records = out["searches"][0].records
    text = records["result"]
    assert "【商品动态信息区】" not in text           # 无动态行则不渲染动态区
    assert "【商品编码:JD-DRY-003" in text           # 静态块正常
    assert records["dynamic_rows"] == {}
    assert not out["searches"][0].errors


async def test_dynamic_fetch_exception_not_crash_node():
    """节点层独立兜底:fetch_by_skus 抛异常(service 兜底之外)不得崩溃子任务——
    降级为仅静态 + errors 记录(2026-09-06 场景2 实测缺陷修复)。"""
    docs = [_doc(sku_codes=["JD-DRY-003"])]
    node = create_vector_search_query_node()
    retriever = AsyncMock()
    retriever.search = AsyncMock(return_value=docs)
    with patch("app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node."
               "get_rag_retriever_service", return_value=retriever):
        with patch("app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node."
                   "fetch_by_skus", AsyncMock(side_effect=RuntimeError("sim db timeout"))):
            out = await node({"task": "米家智能晾衣机2 多少钱"})
    search = out["searches"][0]
    assert "dynamic_fetch_failed" in search.errors          # 异常被记录
    assert search.records["dynamic_rows"] == {}
    assert "【商品编码:JD-DRY-003" in search.records["result"]  # 静态正常
    assert "【商品动态信息区】" not in search.records["result"]   # 无动态区
