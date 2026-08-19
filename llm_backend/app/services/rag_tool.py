"""
RAG 检索工具（langchain @tool 薄封装）

将整个 RAG 检索管线（HNSW ∥ BM25 → RRF → Reranker 精排）封装为 agent 可调用的工具。
核心逻辑全部在 RAGRetrieverService，此处仅为薄适配层。

用法（后续 agent 接入）：
    from app.services.rag_tool import rag_retrieval
    llm.bind_tools([rag_retrieval])
"""

from langchain_core.tools import tool

from app.services.rag_retriever_service import get_rag_retriever_service


@tool
async def rag_retrieval(query: str) -> str:
    """
    从企业知识库中检索与用户问题相关的文档片段。

    知识库包含商品信息（产品参数、价格、库存）、常见故障排查、保修与售后政策等。
    当用户询问产品参数、价格、故障处理或政策条款时使用本工具。

    Args:
        query: 用户的问题（建议为补全指代后的完整问题）

    Returns:
        相关文档片段的文本列表，每段以换行分隔；无结果时返回空字符串。
    """
    docs = await get_rag_retriever_service().search(query)
    if not docs:
        return ""
    return "\n\n".join(doc.get("text", "") for doc in docs)
