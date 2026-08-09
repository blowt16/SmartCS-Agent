"""
重排模块：BGE-Reranker (Cross-Encoder)

为什么需要重排？
    Embedding 检索（Bi-Encoder）中，Query 和 Doc 分别编码成向量再算余弦相似度，
    两者之间没有"交互"，理解深度有限，可能把不太相关的文档排在前面。

    Cross-Encoder 把 Query 和 Doc 拼在一起送入模型，能捕捉细粒度语义关系，
    准确率更高。但速度慢，所以标准做法是：
        Bi-Encoder 粗筛 top-20 -> Cross-Encoder 精排取 top-5

使用的模型：
    BAAI/bge-reranker-v2-m3 -- BGE 系列 Reranker 的多语言版本
    - 支持中文，效果好
    - 基于 XLM-RoBERTa 架构的 Cross-Encoder
    - 使用 sentence_transformers.CrossEncoder 加载
"""

from typing import List, Dict, Any

from sentence_transformers import CrossEncoder

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="reranker")

# ==================== 默认配置 ====================

DEFAULT_MODEL_NAME = settings.RERANKER_MODEL
DEFAULT_TOP_K = settings.RERANKER_TOP_K
DEFAULT_MAX_LENGTH = settings.RERANKER_MAX_LENGTH


# ==================== 核心类 ====================


class BGEReranker:
    """
    基于 BGE-Reranker 的 Cross-Encoder 重排器。

    工作原理：
        1. 把 query 和每个 document 拼成一对: (query, doc)
        2. 送入 Cross-Encoder 模型打分
        3. 按分数降序排列，取 top-K

    为什么用 Cross-Encoder 而非 Bi-Encoder？
        Bi-Encoder:  query -> [向量A],  doc -> [向量B],  score = cosine(A, B)
                     两者独立编码，没有交互，速度快但不够精确

        Cross-Encoder: (query, doc) -> score
                     两者拼接后一起编码，有深度交互，精确但速度慢

        所以用 Bi-Encoder 做粗筛（快），Cross-Encoder 做精排（准）。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        max_length: int = DEFAULT_MAX_LENGTH,
    ):
        """
        Args:
            model_name: HuggingFace 模型名称，默认 bge-reranker-v2-m3
            max_length: 输入文本最大 token 长度
        """
        self.model_name = model_name
        self.max_length = max_length
        self._model: CrossEncoder | None = None

    @property
    def model(self) -> CrossEncoder:
        """
        懒加载模型：第一次调用时才加载，避免启动时阻塞。

        为什么懒加载？
            Cross-Encoder 模型较大（~560M 参数），加载需要几秒。
            如果启动时就加载，会拖慢服务启动速度。
            懒加载让服务先启动，第一次请求时再加载模型。
        """
        if self._model is None:
            logger.info(f"加载 BGE-Reranker 模型: {self.model_name}")
            self._model = CrossEncoder(
                self.model_name,
                max_length=self.max_length,
            )
            logger.info("BGE-Reranker 模型加载完成")
        return self._model

    def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = DEFAULT_TOP_K,
        content_key: str = "text",
    ) -> List[Dict[str, Any]]:
        """
        对文档列表进行重排。

        Args:
            query: 用户查询文本
            documents: 待重排的文档列表，每个文档是字典
            top_k: 返回前 K 个结果
            content_key: 文档中用于重排的文本字段名

        Returns:
            重排后的文档列表（按相关性降序），每个文档增加 "rerank_score" 字段
        """
        if not documents:
            return []

        # 提取文档文本
        doc_texts = []
        for doc in documents:
            if isinstance(doc, dict):
                text = doc.get(content_key, str(doc))
            else:
                text = str(doc)
            doc_texts.append(text)

        # 构造 (query, doc) 对
        pairs = [(query, text) for text in doc_texts]

        # Cross-Encoder 打分
        scores = self.model.predict(pairs)

        # 按分数降序排列
        scored_docs = list(zip(documents, scores))
        scored_docs.sort(key=lambda x: x[1], reverse=True)

        # 取 top-K，添加分数
        results = []
        for doc, score in scored_docs[:top_k]:
            ranked_doc = dict(doc) if isinstance(doc, dict) else {"text": doc}
            ranked_doc["rerank_score"] = float(score)
            results.append(ranked_doc)

        logger.info(
            f"重排完成: {len(documents)} 条 -> {len(results)} 条, "
            f"最高分: {results[0]['rerank_score']:.4f}"
        )

        return results

    def rerank_cypher_records(
        self,
        query: str,
        records: List[Dict[str, Any]],
        top_k: int = DEFAULT_TOP_K,
    ) -> List[Dict[str, Any]]:
        """
        对 Cypher 查询返回的结构化记录进行重排。

        Cypher 返回的是结构化字典（如 {"ProductName": "X1", "UnitPrice": 2999}），
        需要先把字典序列化为文本，再用 Cross-Encoder 打分。

        序列化策略：
            把每个 key-value 对拼成 "key: value" 格式，
            用逗号连接成一段文本。
            例如: "ProductName: 扫地机器人X1, UnitPrice: 2999, UnitsInStock: 50"

        Args:
            query: 用户查询文本
            records: Cypher 查询返回的记录列表
            top_k: 返回前 K 条

        Returns:
            重排后的记录列表（带 rerank_score）
        """
        if not records:
            return []

        # 将结构化记录序列化为文本
        serialized_records = []
        for record in records:
            if isinstance(record, dict):
                text = ", ".join(f"{k}: {v}" for k, v in record.items())
            else:
                text = str(record)
            serialized_records.append(text)

        # 构造 pairs 并打分
        pairs = [(query, text) for text in serialized_records]
        scores = self.model.predict(pairs)

        # 按分数排序，取 top-K
        scored = list(zip(records, scores))
        scored.sort(key=lambda x: x[1], reverse=True)

        results = []
        for record, score in scored[:top_k]:
            ranked_record = dict(record) if isinstance(record, dict) else {"data": record}
            ranked_record["rerank_score"] = float(score)
            results.append(ranked_record)

        logger.info(
            f"Cypher 记录重排: {len(records)} 条 -> {len(results)} 条, "
            f"最高分: {results[0]['rerank_score']:.4f}"
        )

        return results
