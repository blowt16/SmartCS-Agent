import sys
from pathlib import Path

# 添加项目根目录到 PYTHONPATH
ROOT_DIR = Path(__file__).parent.parent
sys.path.append(str(ROOT_DIR))

from app.core.logger import get_logger

logger = get_logger(service="init_db")

# 确保能找到 app 模块
logger.info("Python path: {}", sys.path)
logger.info("Current directory: {}", Path.cwd())
logger.info("Root directory: {}", ROOT_DIR)

import asyncio
from sqlalchemy import text
from app.core.database import engine, Base
from app.models import User, Conversation, Message, DocumentChunk

async def init_db():
    try:
        logger.info("Initializing database...")
        async with engine.begin() as conn:
            # 启用 pgvector 扩展（需先于建表，document_chunks 使用 vector 类型）
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            # 启用 pg_jieba 中文分词扩展（BM25 全文检索，需自定义镜像构建，见 docker/postgres/Dockerfile）
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_jieba"))
            # 删除所有表（如果存在）
            await conn.run_sync(Base.metadata.drop_all)
            # 创建所有表
            await conn.run_sync(Base.metadata.create_all)
            # 向量检索索引（HNSW，余弦距离）
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding "
                "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
            ))
            # BM25 全文检索：content_tsv 生成列（jiebacfg 精确模式分词）+ GIN 倒排索引
            # （Base.metadata.create_all 不覆盖生成列，须显式执行；存量数据 ALTER 时自动回填）
            await conn.execute(text(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('jiebacfg', content)) STORED"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_tsv "
                "ON document_chunks USING GIN (content_tsv)"
            ))
        logger.info("Database initialization completed successfully!")
    except Exception as e:
        logger.error("Database initialization failed: {}", str(e))
        raise
    finally:
        # 在事件循环关闭前显式释放引擎，避免连接池在 Windows 上的清理报错
        await engine.dispose()

def main():
    try:
        asyncio.run(init_db())
    except Exception as e:
        logger.error("An error occurred: {}", str(e))

if __name__ == "__main__":
    main()
