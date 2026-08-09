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

from sentence_transformers import SentenceTransformer
import numpy as np

from app.core.logger import get_logger
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
        embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2",
    ):
        """
        Args:
            documents: 文档语料列表（如向量库中的全部文档）
            text_key: 文档中用于检索的文本字段名
            embedding_model: 向量编码模型名称
        """
        self.documents = documents
        self.text_key = text_key
        self.embedding_model_name = embedding_model

        # BM25 检索器
        self.bm25 = BM25Retriever(documents, text_key=text_key)

        # 向量检索相关（懒加载）
        self._encoder: Optional[SentenceTransformer] = None
        self._doc_embeddings: Optional[np.ndarray] = None

    @property
    def encoder(self) -> SentenceTransformer:
        """懒加载向量编码模型"""
        if self._encoder is None:
            logger.info(f"加载向量编码模型: {self.embedding_model_name}")
            self._encoder = SentenceTransformer(self.embedding_model_name)
        return self._encoder

    def _get_doc_embeddings(self) -> np.ndarray:
        """懒加载并缓存文档向量"""
        if self._doc_embeddings is None:
            texts = [
                doc.get(self.text_key, "") if isinstance(doc, dict) else str(doc)
                for doc in self.documents
            ]
            logger.info(f"编码 {len(texts)} 个文档...")
            self._doc_embeddings = self.encoder.encode(
                texts, convert_to_numpy=True, normalize_embeddings=True
            )
            logger.info("文档向量编码完成")
        return self._doc_embeddings

    def _vector_search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """
        向量语义检索。

        原理：
            1. 将 query 编码成向量
            2. 计算 query 向量和所有文档向量的余弦相似度
            3. 返回最相似的 top-K 个文档
        """
        doc_embeddings = self._get_doc_embeddings()
        query_embedding = self.encoder.encode(
            [query], convert_to_numpy=True, normalize_embeddings=True
        )

        # 余弦相似度（已归一化，直接点积即可）
        similarities = np.dot(doc_embeddings, query_embedding.T).flatten()

        top_indices = np.argsort(similarities)[::-1][:top_k]

        results = []
        for rank, idx in enumerate(top_indices, start=1):
            if similarities[idx] <= 0:
                continue
            doc = dict(self.documents[idx]) if isinstance(self.documents[idx], dict) else {"text": self.documents[idx]}
            doc["vector_score"] = float(similarities[idx])
            doc["vector_rank"] = rank
            results.append(doc)

        logger.info(f"向量检索完成: 返回 {len(results)} 条结果")
        return results

    def search(
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
        logger.info(f"开始混合检索: query='{query}', retrieval_top_n={retrieval_top_n}")

        # BM25 + 向量检索
        bm25_results = self.bm25.search(query, top_k=retrieval_top_n)
        vector_results = self._vector_search(query, top_k=retrieval_top_n)

        # RRF 融合
        fused = rrf_fuse(
            result_lists=[vector_results, bm25_results],
            id_key="id",
            top_k=top_k,
        )

        logger.info(
            f"混合检索完成: BM25 {len(bm25_results)} 条 + "
            f"向量 {len(vector_results)} 条 -> 融合后 {len(fused)} 条"
        )

        return fused
