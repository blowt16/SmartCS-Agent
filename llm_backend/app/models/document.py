from sqlalchemy import Column, DateTime, Integer, String, Text, func, UniqueConstraint
from app.core.database import Base


class Document(Base):
    """RAG 文件级记录表(索引链路原子写入的载体)。md5 全平台唯一。"""

    __tablename__ = "documents"
    # md5 全局唯一:一份知识文档 = 一个文件指纹(见 SPEC_DOCUMENT_STATUS_FILTER D4/D5)。
    # 旧约束 (user_id, md5) 只防"同一上传者重复提交",不防"平台里同一文件存两份",
    # 正是"停用后仍能检索到"的成因 —— 停用只改了一行,另一行的块照常被召回。
    __table_args__ = (UniqueConstraint("md5", name="uq_documents_md5"),)

    id = Column(Integer, primary_key=True, index=True)
    md5 = Column(String(32), nullable=False)            # 文件指纹(去重键)
    original_filename = Column(String(255), nullable=False)
    user_id = Column(String(50), nullable=False, index=True)
    file_type = Column(String(20), nullable=False)      # txt/md/pdf/docx
    file_size = Column(Integer, nullable=False)
    page_count = Column(Integer, nullable=True)         # MVP 全 null,演进从 MinerU layout.json 解析
    chunk_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    description = Column(Text, nullable=True)        # 文件描述(参考图"内容"列 → 改为"文件描述"列)
    status = Column(String(20), nullable=False, default="enabled", server_default="enabled")  # enabled/disabled
