"""
RRF（Reciprocal Rank Fusion）分数融合

为什么用 RRF 而不是简单加权？
    向量检索返回的是余弦相似度（-1~1），BM25 返回的是 TF-IDF 分数（0~无穷）。
    两路检索的分数尺度完全不同，直接加权没有意义。

    RRF 的巧妙之处：不用原始分数，只用排名（rank）。
    公式：score(doc) = sum( 1 / (k + rank_i) )
    其中 k 是平滑常数（通常取 60），防止排名靠前的结果权重过大。

    这样就绕开了分数归一化的问题——只比谁在两路检索中都排得靠前。
"""

from typing import List, Dict, Any

from app.core.logger import get_logger

logger = get_logger(service="rrf_fusion")

DEFAULT_K = 60


def rrf_fuse(
    result_lists: List[List[Dict[str, Any]]],
    id_key: str = "id",
    k: int = DEFAULT_K,
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """
    RRF 分数融合：将多路检索结果按排名倒数求和融合。

    Args:
        result_lists: 多路检索的结果列表，每个元素是一路检索的结果
        id_key: 用于去重的文档唯一标识字段名
        k: RRF 平滑常数（默认 60）
        top_k: 融合后返回前 K 个结果

    Returns:
        融合后的文档列表，每个文档增加 "rrf_score" 字段
    """
    rrf_scores: Dict[str, float] = {}
    doc_store: Dict[str, Dict[str, Any]] = {}

    for result_list in result_lists:
        for rank, doc in enumerate(result_list, start=1):
            doc_id = doc.get(id_key, str(hash(str(doc))))
            doc_store[doc_id] = doc

            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = 0.0
            rrf_scores[doc_id] += 1.0 / (k + rank)

    sorted_docs = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for doc_id, score in sorted_docs[:top_k]:
        doc = dict(doc_store[doc_id])
        doc["rrf_score"] = float(score)
        results.append(doc)

    logger.info(
        f"RRF 融合完成: {len(result_lists)} 路检索, "
        f"去重后 {len(doc_store)} 个文档, 返回 {len(results)} 条"
    )

    return results
