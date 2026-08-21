from sqlalchemy import Column, DateTime, Integer, String, func, UniqueConstraint
from app.core.database import Base


class Document(Base):
    """RAG 文件级记录表(索引链路原子写入的载体)"""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("user_id", "md5", name="uq_documents_user_md5"),)

    id = Column(Integer, primary_key=True, index=True)
    md5 = Column(String(32), nullable=False)            # 文件指纹(去重键)
    original_filename = Column(String(255), nullable=False)
    user_id = Column(String(50), nullable=False, index=True)
    file_type = Column(String(20), nullable=False)      # txt/md/pdf/docx
    file_size = Column(Integer, nullable=False)
    page_count = Column(Integer, nullable=True)         # MVP 全 null,演进从 MinerU layout.json 解析
    chunk_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
