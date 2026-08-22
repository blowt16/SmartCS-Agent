"""
RAG 检索核心服务（唯一检索入口）

结构：HNSW 向量检索 ∥ pg_jieba BM25 检索（并行）→ RRF 融合 → Reranker 精排

    RAGRetrieverService.search(query)
        ├─ asyncio.gather 并行：
        │   ├─ pgvector HNSW 余弦检索（DB 内近似最近邻）  → top-N
        │   └─ pg_jieba BM25 全文检索（GIN 倒排索引）    → top-N
        ├─ rrf_fuse 排名融合 → 候选 top-RERANKER_INPUT_TOP_K
        ├─ RerankerService 精排 → top-RERANKER_TOP_K（RERANKER_ENABLED=false 或失败时跳过）
        └─ 返回 docs（id/text/source/rrf_score[/rerank_score]）

调用方：
    - LangGraph 节点 vector_search_query（records.hybrid_docs / records.result）
    - langchain @tool rag_retrieval（供 agent bind_tools）

降级设计：
    - 任一路检索失败 → 该路返回空，融合仅用成功路
    - 精排失败/关闭 → 直接用融合 top-K
"""

import asyncio
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.models.document_chunk import DocumentChunk
from app.services.embedding_provider import get_embedding_provider
from app.services.reranker_service import get_reranker_service

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval.bm25_sql_retriever import (
    BM25SQLRetriever,
)
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.hybrid_retrieval.rrf_fusion import (
    rrf_fuse,
)

logger = get_logger(service="rag_retriever")


class RAGRetrieverService:
    """RAG 混合检索核心服务（无状态，模块级单例复用）"""

    def __init__(self):
        self.bm25 = BM25SQLRetriever()

    # ==================== 向量检索（pgvector HNSW） ====================

    @staticmethod
    def _to_doc(chunk: DocumentChunk, score: float | None = None) -> Dict[str, Any]:
        """chunk → 检索结果文档（与下游消费结构对齐：id 必在，RRF 按 chunk_id 去重）"""
        doc = {
            "text": chunk.content,
            "id": chunk.id,
            "chunk_id": chunk.chunk_id,
            "source": chunk.source,
            "file_path": chunk.file_path,
            "user_id": chunk.user_id,
            "chunk_index": chunk.chunk_index,
        }
        if score is not None:
            # 归一化向量下 cosine_distance = 1 - 余弦相似度，score 取相似度（越大越相关）
            doc["score"] = 1.0 - float(score)
        return doc

    async def _vector_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """pgvector HNSW 余弦检索（ORDER BY 距离 + LIMIT 触发 ANN 索引）"""
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
        logger.info("HNSW 向量检索完成: 返回 {} 条结果", len(docs))
        return docs

    # ==================== 单路安全包装（失败降级为空） ====================

    async def _safe(self, coro) -> List[Dict[str, Any]]:
        """执行一路检索，失败返回空列表（不阻塞融合）"""
        try:
            return await coro
        except Exception as e:
            logger.warning("检索路失败，降级为空: {}", e)
            return []

    # ==================== 主入口 ====================

    async def search(
        self,
        query: str,
        top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        混合检索主入口：HNSW ∥ BM25 并行 → RRF 融合 → Reranker 精排。

        Args:
            query: 用户查询文本
            top_k: 最终返回数（默认 RERANKER_TOP_K / HYBRID_RETRIEVAL_TOP_K）

        Returns:
            精排后的文档列表（带 rrf_score；精排启用时含 rerank_score）
        """
        retrieval_top_n = settings.HYBRID_RETRIEVAL_TOP_N
        if not top_k:
            top_k = settings.RERANKER_TOP_K if settings.RERANKER_ENABLED else settings.HYBRID_RETRIEVAL_TOP_K

        logger.info("开始混合检索: query='{}', retrieval_top_n={}, top_k={}", query, retrieval_top_n, top_k)

        # ① 两路并行（互不依赖，耗时 = max 非 sum；每路候选数独立配置）
        vector_results, bm25_results = await asyncio.gather(
            self._safe(self._vector_search(query, retrieval_top_n)),
            self._safe(self.bm25.search(query, settings.BM25_TOP_K)),
        )

        # ② RRF 融合（只消费排名，输出候选数 = RRF_TOP_K）
        # 去重键用 chunk_id(内容确定性):PK 是 DB 行号,删除重传后漂移/复用,
        # chunk_id 同文件同键、跨环境稳定(见 spec §4 RRF 去重键决策)
        fused = rrf_fuse(
            result_lists=[vector_results, bm25_results],
            id_key="chunk_id",
            top_k=settings.RRF_TOP_K,
        )

        # ③ 精排（失败/关闭 → 直接用融合 top-K）
        if settings.RERANKER_ENABLED and fused:
            reranker = get_reranker_service()
            # CrossEncoder 为同步计算，放线程池避免阻塞事件循环
            reranked = await asyncio.to_thread(reranker.rerank, query, fused, top_k)
            if reranked is not None:
                fused = reranked
            else:
                fused = fused[:top_k]
        else:
            fused = fused[:top_k]

        logger.info("混合检索完成: 向量 {} 条 + BM25 {} 条 -> 融合 {} 条 -> 最终 {} 条",
                    len(vector_results), len(bm25_results), len(fused), len(fused))

        # rerank 后最终结果内容预览: 每条 chunk 的 content 前 50 字符（列表格式, 调试用）
        final_preview = [str(d.get("text", ""))[:50] for d in fused]
        logger.info("最终检索结果内容预览(前50字符): {}", final_preview)
        return fused


# ==================== 单例 ====================

_service: Optional[RAGRetrieverService] = None
_service_lock = threading.Lock()


def get_rag_retriever_service() -> RAGRetrieverService:
    """模块级懒加载单例（沿用 lock + double-check 模式）"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = RAGRetrieverService()
    return _service
