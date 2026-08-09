from typing import Any, Callable, Coroutine, Dict, List
import chromadb
from chromadb.config import Settings as ChromaSettings
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

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
    """向量库查询封装，替代原 GraphRAGAPI"""

    def __init__(self):
        self.client = chromadb.PersistentClient(
            path=settings.VECTOR_DB_PATH,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

        # Embedding 模型
        model_name = getattr(settings, "EMBEDDING_MODEL", "bge-m3")
        self.encoder = SentenceTransformer(model_name)

        def embedding_fn(texts):
            return self.encoder.encode(texts, normalize_embeddings=True).tolist()

        self.collection = self.client.get_or_create_collection(
            name=settings.VECTOR_DB_COLLECTION,
            embedding_function=embedding_fn,
        )

    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """执行向量检索，返回文档列表"""
        results = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        docs = []
        if results["documents"] and results["documents"][0]:
            for i, text in enumerate(results["documents"][0]):
                doc = {"text": text, "id": results["ids"][0][i]}
                if results["metadatas"] and results["metadatas"][0]:
                    doc.update(results["metadatas"][0][i] or {})
                if results["distances"] and results["distances"][0]:
                    doc["score"] = results["distances"][0][i]
                docs.append(doc)

        logger.info(f"向量检索完成: 返回 {len(docs)} 条结果")
        return docs

    def get_all_documents(self) -> List[Dict[str, Any]]:
        """获取集合中所有文档（用于 HybridRetriever 构建语料库）"""
        results = self.collection.get(include=["documents", "metadatas"])
        docs = []
        if results["documents"]:
            for i, text in enumerate(results["documents"]):
                doc = {"text": text, "id": results["ids"][i]}
                if results["metadatas"] and results["metadatas"][i]:
                    doc.update(results["metadatas"][i] or {})
                docs.append(doc)
        return docs


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
            vector_store = VectorStoreQuery()

            # 1. 向量检索
            vector_results = vector_store.search(query, top_k=10)

            # 2. 混合检索：用向量库的全部文档构建 HybridRetriever 语料库
            try:
                all_docs = vector_store.get_all_documents()
                if all_docs and len(all_docs) > 0:
                    retriever = HybridRetriever(
                        documents=all_docs,
                        text_key="text",
                    )
                    hybrid_results = retriever.search(query, top_k=5, retrieval_top_n=20)
                    logger.info(f"混合检索补充了 {len(hybrid_results)} 条文档")
            except Exception as e:
                logger.warning(f"混合检索失败，跳过: {e}")

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
                        temperature=0,
                    )
                else:
                    grader_llm = ChatOllama(
                        model=settings.OLLAMA_AGENT_MODEL,
                        base_url=settings.OLLAMA_BASE_URL,
                        temperature=0,
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
            "cyphers": [
                VectorSearchOutputState(
                    **{
                        "task": state.get("task", ""),
                        "query": query,
                        "statement": "",
                        "parameters": "",
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
