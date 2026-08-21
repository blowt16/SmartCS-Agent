from typing import Any, Callable, Coroutine, Dict, List

from pydantic import BaseModel, Field

from app.core.logger import get_logger
from app.services.rag_retriever_service import get_rag_retriever_service

logger = get_logger(service="customer_tools")


# ==================== 数据模型 ====================

class VectorSearchInputState(BaseModel):
    task: str
    query: str
    steps: List[str]


class VectorSearchOutputState(BaseModel):
    task: str
    query: str
    errors: List[str]
    records: Dict[str, Any]
    steps: List[str]


# ==================== LangGraph 节点工厂 ====================

def create_vector_search_query_node() -> Callable[
    [Dict[str, Any]],
    Coroutine[Any, Any, Dict[str, List[VectorSearchOutputState] | List[str]]],
]:
    """
    创建向量检索查询节点，用于 LangGraph 工作流。

    检索链路收敛于 RAGRetrieverService（HNSW ∥ BM25 并行 → RRF → Reranker 精排），
    应用层不再持有语料与索引。

    返回
    -------
    Callable
        名为 vector_search_query 的 LangGraph 节点。
    """

    async def vector_search_query(
        state: Dict[str, Any],
    ) -> Dict[str, List[VectorSearchOutputState] | List[str]]:
        """
        执行混合检索（向量 ∥ BM25 → RRF → 精排），返回检索结果供 summarize 消费。
        """
        errors = list()
        docs = []

        query = state.get("task", "")
        if not query:
            errors.append("未提供查询文本")
        else:
            retriever = get_rag_retriever_service()
            docs = await retriever.search(query)
            logger.info("检索节点返回 {} 条文档", len(docs))

        # 构建 LLM 可用的文本上下文
        response_text = "\n\n".join(d.get("text", "") for d in docs)

        return {
            "searches": [
                VectorSearchOutputState(
                    **{
                        "task": state.get("task", ""),
                        "query": query,
                        "errors": errors,
                        "records": {
                            "result": response_text,
                            "hybrid_docs": docs,
                        },
                        "steps": ["execute_vector_search"],
                    }
                )
            ],
            "steps": ["execute_vector_search"],
        }

    return vector_search_query
