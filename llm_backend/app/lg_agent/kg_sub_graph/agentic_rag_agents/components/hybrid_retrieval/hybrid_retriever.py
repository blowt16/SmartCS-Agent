"""
混合检索器：BM25 + 向量检索 + RRF 融合

整体流程：
    1. BM25 关键词检索（精确匹配）
    2. 向量语义检索（语义匹配）
    3. RRF 融合两路结果（排名倒数求和）
    4. 返回融合后的 top-K 文档

为什么需要混合检索？
    纯向量检索的短板：
        - 精确型号匹配差（"X1-Pro" 不一定能匹配到 "X1-Pro"）
        - 编号类信息检索差（"SN-2024-089"）
        - 罕见专有名词匹配差

    BM25 的互补：
        - 基于词频，对精确匹配非常有效
        - 但不理解语义（"灯泡" 不等于 "LED灯"）

    两者结合后，语义匹配和精确匹配都能覆盖，检索质量显著提升。
"""

from typing import List, Dict, Any, Optional

import numpy as np

from app.core.config import settings
from app.core.logger import get_logger
from app.services.embedding_provider import get_embedding_provider, embed_in_batches
from .bm25_retriever import BM25Retriever
from .rrf_fusion import rrf_fuse

logger = get_logger(service="hybrid_retriever")


class HybridRetriever:
    """
    混合检索器：BM25 + 向量检索，RRF 融合。

    用法：
        retriever = HybridRetriever(documents=text_units)
        results = retriever.search("扫地机器人X1故障排查", top_k=5)
    """

    def __init__(
        self,
        documents: List[Dict[str, Any]],
        text_key: str = "text",
        embedding_model: str = None,
    ):
        """
        Args:
            documents: 文档语料列表（如向量库中的全部文档）
            text_key: 文档中用于检索的文本字段名
            embedding_model: 兼容参数（已废弃，向量统一由 Provider 提供）
        """
        self.documents = documents
        self.text_key = text_key

        # BM25 检索器
        self.bm25 = BM25Retriever(documents, text_key=text_key)

        # 向量检索相关（懒加载）
        self._doc_embeddings: Optional[np.ndarray] = None

    async def _get_doc_embeddings(self) -> np.ndarray:
        """懒加载并缓存文档向量：优先复用文档自带的库内向量，缺失时走 Provider 兜底编码"""
        if self._doc_embeddings is None:
            arrays: List[np.ndarray] = []
            missing_texts: List[str] = []
            for doc in self.documents:
                emb = doc.get("embedding") if isinstance(doc, dict) else None
                if emb is not None:
                    arrays.append(np.asarray(emb, dtype=np.float32))
                else:
                    missing_texts.append(
                        doc.get(self.text_key, "") if isinstance(doc, dict) else str(doc)
                    )
            if missing_texts:
                logger.info("兜底编码 {} 个文档向量...", len(missing_texts))
                vectors = await embed_in_batches(missing_texts)
                arrays.extend(np.asarray(v, dtype=np.float32) for v in vectors)
            self._doc_embeddings = (
                np.vstack(arrays) if arrays else np.zeros((0, settings.EMBEDDING_DIMENSION))
            )
            logger.info("文档向量就绪: 复用 {} 个 + 兜底编码 {} 个", len(arrays) - len(missing_texts), len(missing_texts))
        return self._doc_embeddings

    async def _vector_search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """
        向量语义检索。

        原理：
            1. 将 query 编码成向量
            2. 计算 query 向量和所有文档向量的余弦相似度
            3. 返回最相似的 top-K 个文档
        """
        doc_embeddings = await self._get_doc_embeddings()
        query_embedding = np.asarray(
            (await get_embedding_provider().embed([query]))[0], dtype=np.float32
        ).reshape(1, -1)

        # 余弦相似度（已归一化，直接点积即可）
        similarities = np.dot(doc_embeddings, query_embedding.T).flatten()

        top_indices = np.argsort(similarities)[::-1][:top_k]

        results = []
        for rank, idx in enumerate(top_indices, start=1):
            if similarities[idx] <= 0:
                continue
            doc = dict(self.documents[idx]) if isinstance(self.documents[idx], dict) else {"text": self.documents[idx]}
            doc.pop("embedding", None)  # 不把 1024 维向量带进下游响应
            doc["vector_score"] = float(similarities[idx])
            doc["vector_rank"] = rank
            results.append(doc)

        logger.info("向量检索完成: 返回 {} 条结果", len(results))
        return results

    async def search(
        self,
        query: str,
        top_k: int = 10,
        retrieval_top_n: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        混合检索主入口：BM25 + 向量检索 + RRF 融合。

        流程：
            1. BM25 检索 top-N（关键词匹配）
            2. 向量检索 top-N（语义匹配）
            3. RRF 融合两路结果
            4. 返回融合后的 top-K

        Args:
            query: 用户查询文本
            top_k: 最终返回的结果数
            retrieval_top_n: 每路检索的候选数量（融合前）

        Returns:
            融合后的文档列表（带 rrf_score）
        """
        logger.info("开始混合检索: query='{}', retrieval_top_n={}", query, retrieval_top_n)

        # BM25 + 向量检索
        bm25_results = self.bm25.search(query, top_k=retrieval_top_n)
        vector_results = await self._vector_search(query, top_k=retrieval_top_n)

        # RRF 融合
        fused = rrf_fuse(
            result_lists=[vector_results, bm25_results],
            id_key="id",
            top_k=top_k,
        )

        logger.info(
            "混合检索完成: BM25 {} 条 + 向量 {} 条 -> 融合后 {} 条",
            len(bm25_results), len(vector_results), len(fused),
        )

        return fused
