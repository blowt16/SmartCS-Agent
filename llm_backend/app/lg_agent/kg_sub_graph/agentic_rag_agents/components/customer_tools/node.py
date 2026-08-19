from typing import Any, Callable, Coroutine, Dict, List, Optional
import threading
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.document_chunk import DocumentChunk
from app.services.embedding_provider import get_embedding_provider

# 导入混合检索模块
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval import HybridRetriever

# 导入相关性评分模块
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.relevance_grader import grade_relevance

# 导入 LLM 模块（用于相关性评分）
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import ChatOllama

# 导入配置
from app.core.config import settings, ServiceType
from app.core.logger import get_logger

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


# ==================== 向量查询封装 ====================

class VectorStoreQuery:
    """向量库查询封装（pgvector），替代原 GraphRAGAPI"""

    # 向量由统一 Provider 提供（qwen text-embedding-v4），无需本地加载模型

    @staticmethod
    def _to_doc(
        chunk: DocumentChunk,
        score: float | None = None,
        include_embedding: bool = False,
    ) -> Dict[str, Any]:
        doc = {
            "text": chunk.content,
            "id": chunk.id,
            "source": chunk.source,
            "file_path": chunk.file_path,
            "user_id": chunk.user_id,
            "chunk_index": chunk.chunk_index,
        }
        if include_embedding:
            # 供 HybridRetriever 复用库内向量，避免重复编码
            doc["embedding"] = list(chunk.embedding)
        if score is not None:
            # 归一化向量下 cosine_distance = 1 - 余弦相似度，score 取相似度（越大越相关）
            doc["score"] = 1.0 - float(score)
        return doc

    async def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """执行 pgvector 向量检索（余弦距离 Top-K），返回文档列表"""
        query_vec = (await get_embedding_provider().embed([query]))[0]
        if not any(query_vec):
            logger.warning("查询向量全零（Embedding API 失败），跳过向量检索")
            return []

        distance = DocumentChunk.embedding.cosine_distance(query_vec)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(DocumentChunk, distance.label("distance"))
                .order_by(distance)
                .limit(top_k)
            )
            rows = result.all()

        docs = [self._to_doc(chunk, score=dist) for chunk, dist in rows]

        logger.info("向量检索完成: 返回 {} 条结果", len(docs))
        return docs

    async def get_all_documents(self) -> List[Dict[str, Any]]:
        """获取表中所有文档块（用于 HybridRetriever 构建语料库），携带 embedding 供复用"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(DocumentChunk).order_by(DocumentChunk.id))
            chunks = result.scalars().all()

        return [self._to_doc(chunk, include_embedding=True) for chunk in chunks]


# ==================== 向量库客户端单例 ====================

_vector_store: Optional[VectorStoreQuery] = None
_vector_store_lock = threading.Lock()


def get_vector_store() -> VectorStoreQuery:
    """模块级懒加载单例：ChromaDB/Embedding 客户端只初始化一次。

    消除每请求重建查询客户端与全量文档拉取的开销（并行分支下放大）。
    线程安全：并发首请求只允许一次初始化（见 SPEC_ENTITY_PARALLEL_RAG.md 风险 #4）。
    """
    global _vector_store
    if _vector_store is None:
        with _vector_store_lock:
            if _vector_store is None:
                _vector_store = VectorStoreQuery()
    return _vector_store


# ==================== LangGraph 节点工厂 ====================

def create_vector_search_query_node(
) -> Callable[
    [Dict[str, Any]],
    Coroutine[Any, Any, Dict[str, List[VectorSearchOutputState] | List[str]]],
]:
    """
    创建向量检索查询节点，用于 LangGraph 工作流。

    返回
    -------
    Callable
        名为 vector_search_query 的 LangGraph 节点。
    """

    async def vector_search_query(
        state: Dict[str, Any],
    ) -> Dict[str, List[VectorSearchOutputState] | List[str]]:
        """
        执行向量检索 + 混合检索（BM25+向量 RRF 融合），合并结果返回。
        """
        errors = list()
        hybrid_results = []

        query = state.get("task", "")
        if not query:
            errors.append("未提供查询文本")
        else:
            vector_store = get_vector_store()

            # 1. 向量检索
            vector_results = await vector_store.search(query, top_k=settings.VECTOR_SEARCH_TOP_K)

            # 2. 混合检索：用向量库的全部文档构建 HybridRetriever 语料库
            try:
                all_docs = await vector_store.get_all_documents()
                if all_docs and len(all_docs) > 0:
                    retriever = HybridRetriever(
                        documents=all_docs,
                        text_key="text",
                    )
                    hybrid_results = await retriever.search(query, top_k=settings.HYBRID_RETRIEVAL_TOP_K, retrieval_top_n=settings.HYBRID_RETRIEVAL_TOP_N)
                    logger.info("混合检索补充了 {} 条文档", len(hybrid_results))
            except Exception as e:
                logger.warning("混合检索失败，跳过: {}", e)

            # 3. 合并向量检索结果到 hybrid_results（补充纯语义匹配结果）
            if vector_results:
                merged = {doc.get("id", doc.get("text", "")[:50]): doc for doc in hybrid_results}
                for doc in vector_results:
                    key = doc.get("id", doc.get("text", "")[:50])
                    if key not in merged:
                        merged[key] = doc
                hybrid_results = list(merged.values())

            # 4. 相关性评分
            if hybrid_results and settings.RELEVANCE_GRADING_ENABLED:
                if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
                    grader_llm = ChatDeepSeek(
                        api_key=settings.DEEPSEEK_API_KEY,
                        model=settings.DEEPSEEK_MODEL,
                        temperature=settings.LLM_GRADER_TEMPERATURE,
                    )
                else:
                    grader_llm = ChatOllama(
                        model=settings.OLLAMA_AGENT_MODEL,
                        base_url=settings.OLLAMA_BASE_URL,
                        temperature=settings.LLM_GRADER_TEMPERATURE,
                    )

                hybrid_results = await grade_relevance(
                    llm=grader_llm,
                    query=query,
                    documents=hybrid_results,
                    content_key="text",
                )

        # 构建 LLM 可用的文本上下文
        response_text = ""
        if hybrid_results:
            response_text = "\n\n".join(
                d.get("text", "") for d in hybrid_results
            )

        return {
            "searches": [
                VectorSearchOutputState(
                    **{
                        "task": state.get("task", ""),
                        "query": query,
                        "errors": errors,
                        "records": {
                            "result": response_text,
                            "hybrid_docs": hybrid_results,
                        },
                        "steps": ["execute_vector_search"],
                    }
                )
            ],
            "steps": ["execute_vector_search"],
        }

    return vector_search_query
