"""
BM25 数据库检索器（pg_jieba + ts_rank_cd）

替代原内存 BM25（jieba + rank_bm25）：
    1. 索引与查询统一走 PostgreSQL 全文检索（jiebacfg 精确模式，cppjieba 分词）
    2. GIN 倒排索引在 DB 内增量维护，无应用内存驻留、无重建窗口
    3. ts_rank_cd 为 BM25 变体排名（词类权重 D/C/B/A），排名行为需与旧实现回归对比

整体流程：
    1. DB 内双配置分词（jiebacfg 精确模式 ∪ jiebamp 单字兜底）→ token 过滤空白 junk
       → OR 连接构建 tsquery（部分命中即入选，"宁可多召回"）
    2. content_tsv @@ query 走 GIN 索引筛候选
    3. ts_rank_cd 排名（整词命中排前、高频单字 IDF 衰减），LIMIT top_k
"""

from typing import Any, Dict, List

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger

logger = get_logger(service="bm25_sql_retriever")


class BM25SQLRetriever:
    """pg_jieba + ts_rank_cd 的 BM25 检索器（无状态，可复用）"""

    # 双配置分词并集后 OR 连接（plainto_tsquery 的 AND 全词语义在微块语料上
    # 恒 0 命中，见 spec_plan/SPEC_BM25_QUERY_SEMANTICS_FIX.md）：
    #   jiebacfg（精确模式）保护整词命中；jiebamp（MP 单字模式）兜底文档侧
    #   被拆散的未登录词/品牌词（如 "品牌：芝华仕" 在生成列中被拆为 芝/华/仕）。
    _OR_QUERY_SQL_TEXT = text(
        """
        SELECT document_chunks.*,
               ts_rank_cd(document_chunks.content_tsv, tsq.q) AS bm25_score
        FROM document_chunks,
             (SELECT to_tsquery('jiebacfg',
                      string_agg(quote_literal(tok), '|' ORDER BY tok)) AS q
              FROM (
                  SELECT unnest(tsvector_to_array(to_tsvector('jiebacfg', :query))) AS tok
                  UNION
                  SELECT unnest(tsvector_to_array(to_tsvector('jiebamp', :query))) AS tok
              ) t
              WHERE tok ~ '\\S') tsq
        WHERE document_chunks.content_tsv @@ tsq.q
        ORDER BY bm25_score DESC, document_chunks.id
        LIMIT :top_k
        """
    )

    async def search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """
        执行 BM25 全文检索（双分词并集 OR 语义）。

        查询词条由 jiebacfg（精确模式，整词）与 jiebamp（MP 模式，单字兜底）并集，
        过滤空白 junk 后 OR 连接：文档只要命中部分查询词即可进入排名，
        由 ts_rank_cd 打分排序（整词命中排前、高频单字 IDF 衰减）。

        Args:
            query: 用户查询文本
            top_k: 返回前 K 个结果

        Returns:
            检索结果列表，每个文档包含 id/source/text/file_path/user_id/chunk_index/bm25_score
        """
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    self._OR_QUERY_SQL_TEXT,
                    {"query": query, "top_k": top_k},
                )
            ).all()

        results = []
        for row in rows:
            r = row._mapping  # text() 查询返回 Row，按列名取
            results.append(
                {
                    "id": r["id"],
                    "chunk_id": r["chunk_id"],
                    "source": r["source"],
                    "file_path": r["file_path"],
                    "user_id": r["user_id"],
                    "chunk_index": r["chunk_index"],
                    "text": r["content"],
                    "bm25_score": float(r["bm25_score"]),
                }
            )

        logger.info("BM25 SQL 检索完成: query='{}', 返回 {} 条结果", query, len(results))
        return results
