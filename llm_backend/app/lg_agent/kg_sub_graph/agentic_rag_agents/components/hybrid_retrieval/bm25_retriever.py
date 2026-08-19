"""
BM25 关键词检索器

BM25（Best Matching 25）是基于词频统计的检索算法。
核心思想：一个词在文档中出现次数越多、在整个语料库中出现次数越少，
说明这个词对这个文档越重要。

为什么需要 BM25？
    向量检索擅长语义匹配（"灯泡" ≈ "LED灯"），
    但不擅长精确词匹配（"X1-Pro" 就必须精确匹配 "X1-Pro"）。
    BM25 恰好互补，它基于词频和逆文档频率，对精确匹配非常有效。

中文分词：
    使用 jieba 分词器，将中文文本切分为词语。
    例如 "扫地机器人X1多少钱" -> ["扫地", "机器人", "X1", "多少", "钱"]
"""

from typing import List, Dict, Any, Optional

from app.core.logger import get_logger

logger = get_logger(service="bm25_retriever")

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    logger.warning("jieba 未安装，BM25 分词将退化为字符级切分")

try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    logger.warning("rank_bm25 未安装，请运行 pip install rank_bm25")


def tokenize(text: str) -> List[str]:
    """
    中文分词：jieba 分词 + 过滤空字符串。

    Args:
        text: 待分词的中文文本

    Returns:
        分词结果列表
    """
    if JIEBA_AVAILABLE:
        words = list(jieba.cut(text))
    else:
        words = list(text)

    return [w.strip() for w in words if w.strip()]


class BM25Retriever:
    """
    BM25 关键词检索器。

    工作流程：
        1. 初始化时传入语料（文档列表），构建 BM25 索引
        2. 查询时对 query 分词，用 BM25 算法计算相关性
        3. 返回 top-K 个最相关的文档

    BM25Okapi 的核心公式：
        score(D, Q) = sum( IDF(qi) * (f(qi,D)*(k1+1)) / (f(qi,D)+k1*(1-b+b*|D|/avgdl)) )
        其中：
        - f(qi,D): 词 qi 在文档 D 中的词频
        - |D|: 文档长度
        - avgdl: 平均文档长度
        - k1, b: 调节参数（默认 k1=1.5, b=0.75）
    """

    def __init__(self, documents: List[Dict[str, Any]], text_key: str = "text"):
        """
        Args:
            documents: 文档列表，每个文档是字典
            text_key: 文档中用于检索的文本字段名
        """
        self.documents = documents
        self.text_key = text_key
        self._bm25: Optional[Any] = None
        self._tokenized_corpus: List[List[str]] = []

        if BM25_AVAILABLE and documents:
            self._build_index()

    def _build_index(self):
        """构建 BM25 索引：对所有文档分词，创建 BM25Okapi 实例。"""
        self._tokenized_corpus = []
        for doc in self.documents:
            text = doc.get(self.text_key, "") if isinstance(doc, dict) else str(doc)
            tokens = tokenize(text)
            self._tokenized_corpus.append(tokens)

        self._bm25 = BM25Okapi(self._tokenized_corpus)
        logger.info("BM25 索引构建完成，共 {} 个文档", len(self.documents))

    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """
        执行 BM25 检索。

        Args:
            query: 用户查询文本
            top_k: 返回前 K 个结果

        Returns:
            检索结果列表，每个文档增加 "bm25_score" 和 "bm25_rank" 字段
        """
        if not BM25_AVAILABLE or self._bm25 is None:
            logger.warning("BM25 不可用，返回空结果")
            return []

        query_tokens = tokenize(query)
        scores = self._bm25.get_scores(query_tokens)

        # 按分数降序排列
        scored_docs = list(zip(range(len(self.documents)), scores))
        scored_docs.sort(key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in scored_docs[:top_k]:
            if score <= 0:
                continue
            doc = dict(self.documents[idx]) if isinstance(self.documents[idx], dict) else {"text": self.documents[idx]}
            doc["bm25_score"] = float(score)
            doc["bm25_rank"] = len(results) + 1
            results.append(doc)

        logger.info("BM25 检索完成: query='{}', 返回 {} 条结果", query, len(results))
        return results
