from sqlalchemy import Column, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from pgvector.sqlalchemy import Vector
from app.core.database import Base
from app.core.config import settings


class DocumentChunk(Base):
    """RAG 文档块表（pgvector 向量存储，替代原 ChromaDB collection）"""

    __tablename__ = settings.VECTOR_TABLE_NAME

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String(255), nullable=False)      # 原始文件名
    file_path = Column(String(500), nullable=False)   # 上传路径
    user_id = Column(String(50), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)     # 块序号
    content = Column(Text, nullable=False)            # 文本块内容
    embedding = Column(Vector(settings.EMBEDDING_DIMENSION), nullable=False)
    # BM25 全文检索生成列（jiebacfg 分词，DB 端自动维护，ORM 只读使用）
    content_tsv = Column(TSVECTOR, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
