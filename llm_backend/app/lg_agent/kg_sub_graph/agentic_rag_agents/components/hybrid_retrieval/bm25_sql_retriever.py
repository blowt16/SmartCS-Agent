"""
BM25 数据库检索器（pg_jieba + ts_rank_cd）

替代原内存 BM25（jieba + rank_bm25）：
    1. 索引与查询统一走 PostgreSQL 全文检索（jiebacfg 精确模式，cppjieba 分词）
    2. GIN 倒排索引在 DB 内增量维护，无应用内存驻留、无重建窗口
    3. ts_rank_cd 为 BM25 变体排名（词类权重 D/C/B/A），排名行为需与旧实现回归对比

整体流程：
    1. plainto_tsquery('jiebacfg', query) 分词查询词（AND 连接）
    2. content_tsv @@ query 走 GIN 索引筛候选
    3. ts_rank_cd 排名，LIMIT top_k
"""

from typing import Any, Dict, List

from sqlalchemy import desc, func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.models.document_chunk import DocumentChunk

logger = get_logger(service="bm25_sql_retriever")


class BM25SQLRetriever:
    """pg_jieba + ts_rank_cd 的 BM25 检索器（无状态，可复用）"""

    async def search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """
        执行 BM25 全文检索。

        Args:
            query: 用户查询文本
            top_k: 返回前 K 个结果

        Returns:
            检索结果列表，每个文档包含 id/source/text/file_path/user_id/chunk_index/bm25_score
        """
        # jiebacfg 精确模式分词查询词（与索引生成列同一配置，保证 token 对齐）
        query_tsv = func.plainto_tsquery("jiebacfg", query)

        stmt = (
            select(DocumentChunk, func.ts_rank_cd(DocumentChunk.content_tsv, query_tsv).label("bm25_score"))
            .where(DocumentChunk.content_tsv.op("@@")(query_tsv))
            .order_by(desc("bm25_score"))
            .limit(top_k)
        )

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(stmt)).all()

        results = []
        for chunk, score in rows:
            results.append(
                {
                    "id": chunk.id,
                    "chunk_id": chunk.chunk_id,
                    "source": chunk.source,
                    "file_path": chunk.file_path,
                    "user_id": chunk.user_id,
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.content,
                    "bm25_score": float(score),
                }
            )

        logger.info("BM25 SQL 检索完成: query='{}', 返回 {} 条结果", query, len(results))
        return results
