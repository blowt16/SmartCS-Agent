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
from app.models import User, Conversation, Message, DocumentChunk, Document, ProductPriceStock

async def init_db():
    try:
        logger.info("Initializing database...")
        async with engine.begin() as conn:
            # 启用 pgvector 扩展（需先于建表，document_chunks 使用 vector 类型）
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            # 启用 pg_jieba 中文分词扩展（BM25 全文检索，需自定义镜像构建，见 docker/postgres/Dockerfile）
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_jieba"))
            # 幂等建表:不 drop_all,保留存量数据(修复 P0-2)
            await conn.run_sync(Base.metadata.create_all)
            # 存量表增量加列(幂等)
            await conn.execute(text(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunk_id VARCHAR(64)"
            ))
            await conn.execute(text(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS md5 VARCHAR(32)"
            ))
            await conn.execute(text(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS file_type VARCHAR(20)"
            ))
            await conn.execute(text(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS page INTEGER"
            ))
            await conn.execute(text(
                "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chapter VARCHAR(255)"
            ))
            # 唯一约束幂等(存量 null 允许多值)
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_user_md5 ON documents (user_id, md5)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_chunk_id ON document_chunks (chunk_id)"
            ))
            # 向量/全文索引(原有)
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding "
                "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
            ))
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
