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
    从企业知识库中检索与用户问题相关的知识文档片段。

    知识库内容（商品静态信息）：商品参数与规格、功能特点、使用指导与故障排查、
    保修与售后政策（含《京东自营售后政策》独立文档）等。
    【动态数据不在本工具】商品价格与库存存储在数据库，请使用 product_stock_lookup 工具查询。

    何时使用本工具：
    - 用户询问商品参数/规格/功能特点（如"这款灯的亮度调节范围是多少"）
    - 用户询问使用方法或故障解决（如"扫地机器人一直报错怎么办"）
    - 用户询问保修、退换货、售后政策条款（如"这个锁保修多久"）
    - 用户询问某类商品的功能/推荐/对比（如"电动沙发和普通沙发有什么区别"）

    何时不要使用本工具：
    - 询问商品价格或是否有货 → 使用 product_stock_lookup（数据库动态数据）
    - 与业务无关的闲聊 → 直接回答，无需检索
    - 违规/高风险内容 → 按风险规则处理，不检索

    Args:
        query: 用户的问题（建议为补全指代后的完整问题）

    Returns:
        相关文档片段的文本列表（每段以换行分隔，含来源 source）；
        未检索到相关内容时返回空字符串。
    """
    docs = await get_rag_retriever_service().search(query)
    if not docs:
        return ""
    return "\n\n".join(doc.get("text", "") for doc in docs)
