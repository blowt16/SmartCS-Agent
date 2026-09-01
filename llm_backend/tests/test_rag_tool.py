"""rag_retrieval tool 三态返回测试（mock RAGRetrieverService，不连库）

覆盖 SPEC_RAG_TOOL_OPTIMIZATION §9 验证方案：
- 三态返回：正常（带来源前缀）/ 空（建议文本）/ 异常（错误 JSON 兜底）
- 异常分类映射（含 api_unavailable）
- 入参校验（query 空 → invalid_argument retryable=true）
- 瞬时错误自动重试 1 次 / 永久错误不重试
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import aiohttp
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.tools.rag_tool import _classify_error, rag_retrieval

DOC_PATH = "D:/knowledge/product_knowledge_docx/京东智能家具产品知识文档.docx"


def _doc(text: str = "测试片段", file_path: str = DOC_PATH) -> dict:
    return {"text": text, "file_path": file_path}


async def _invoke(query: str, mock_search) -> str:
    svc = AsyncMock()
    svc.search = mock_search
    with patch("app.tools.rag_tool.get_rag_retriever_service", return_value=svc):
        return await rag_retrieval.ainvoke({"query": query})


# ---------- 三态：成功 ----------


async def test_success_with_source_prefix():
    result = await _invoke(
        "沙发参数",
        AsyncMock(return_value=[_doc("参数A"), _doc("参数B", file_path="")]),
    )
    assert "【来源:京东智能家具产品知识文档.docx】" in result
    assert "参数A" in result and "参数B" in result


async def test_source_prefix_empty_path_falls_back_unknown():
    """边界 #4：file_path 空串/缺失 → 来源回退「未知」"""
    result = await _invoke("灯", AsyncMock(return_value=[_doc("无路径", file_path="")]))
    assert "【来源:未知】" in result
    result = await _invoke("灯", AsyncMock(return_value=[_doc("无路径", file_path=None)]))
    assert "【来源:未知】" in result


async def test_success_multiple_docs_separate_source():
    """边界 #5：多文档命中各段独立【来源】前缀"""
    result = await _invoke(
        "门锁",
        AsyncMock(return_value=[_doc("A", "a.docx"), _doc("B", "b.docx")]),
    )
    assert result.count("【来源:") == 2


# ---------- 三态：空结果 ----------


async def test_empty_result_advice():
    result = await _invoke("不存在的知识", AsyncMock(return_value=[]))
    assert "未检索到" in result
    assert "product_stock_lookup" in result  # 价格/库存引导
    assert "换措辞" in result


# ---------- 三态：异常兜底 ----------


async def test_error_fallback_json():
    result = await _invoke("沙发", AsyncMock(side_effect=OperationalError("s", {}, Exception("conn"))))
    body = json.loads(result)
    assert body["status"] == "error"
    assert body["error_type"] == "db_connection"
    assert body["retryable"] is False
    assert "转人工" in body["message"]


# ---------- 入参校验 ----------


async def test_empty_query_invalid_argument():
    for bad in ("", "   "):
        result = await rag_retrieval.ainvoke({"query": bad})
        body = json.loads(result)
        assert body["status"] == "error"
        assert body["error_type"] == "invalid_argument"
        assert body["retryable"] is True


# ---------- 异常分类映射 ----------


def test_classify_error_mapping():
    cases = [
        (asyncio.TimeoutError(), "db_timeout", True),
        (OperationalError("s", {}, Exception("conn")), "db_connection", True),
        (ProgrammingError("s", {}, Exception("table missing")), "db_config", False),
        (aiohttp.ClientError("embed api down"), "api_unavailable", True),
        (ValueError("x"), "unknown", False),
    ]
    for exc, etype, retryable in cases:
        assert _classify_error(exc) == (etype, retryable), f"{type(exc).__name__}"


# ---------- 重试语义 ----------


async def test_transient_error_retried_once_then_error():
    """瞬时错误：内部重试 1 次（第 2 次仍失败）→ 最终 error JSON，不抛异常"""
    mock_search = AsyncMock(side_effect=OperationalError("s", {}, Exception("conn")))
    result = await _invoke("沙发", mock_search)
    assert mock_search.call_count == 2  # 首次 + 重试 1 次
    body = json.loads(result)
    assert body["status"] == "error" and body["retryable"] is False


async def test_permanent_error_no_retry():
    mock_search = AsyncMock(side_effect=ProgrammingError("s", {}, Exception("table missing")))
    result = await _invoke("沙发", mock_search)
    assert mock_search.call_count == 1  # 永久错误不重试
    body = json.loads(result)
    assert body["error_type"] == "db_config"
