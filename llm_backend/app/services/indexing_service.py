import os
from typing import Dict, Any

from langchain_text_splitters import RecursiveCharacterTextSplitter
from PyPDF2 import PdfReader
from docx import Document
from sentence_transformers import SentenceTransformer

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.models.document_chunk import DocumentChunk

logger = get_logger(service="indexing")


class IndexingService:
    """标准 RAG 索引服务：解析 → 清洗 → 分块 → Embedding → pgvector 入库"""

    def __init__(self):
        # 文本分割器 — 复用 P0 智能分块的参数
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
            separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
        )

        # Embedding 模型 — 优先用 settings 中的配置，兜底本地 bge-m3
        embedding_model_name = getattr(settings, "EMBEDDING_MODEL", "bge-m3")
        self.embedding_model = SentenceTransformer(embedding_model_name)

    # ==================== 文档解析 ====================

    def _parse_document(self, file_path: str) -> str:
        """根据文件类型解析文档为纯文本"""
        if file_path.endswith('.pdf'):
            reader = PdfReader(file_path)
            return "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
        elif file_path.endswith('.docx'):
            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
        else:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()

    def _clean_text(self, text: str) -> str:
        """清洗文本：去掉多余空行、统一换行符、去掉噪声字符"""
        # 统一换行符
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # 去掉 3 个以上连续换行，压缩为 2 个
        import re
        text = re.sub(r'\n{3,}', '\n\n', text)
        # 去掉首尾空白
        text = text.strip()
        return text

    # ==================== 核心流程 ====================

    async def process_file(self, file_info: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个文件：解析 → 清洗 → 分块 → Embedding → pgvector 入库"""
        try:
            file_path = file_info['path']
            user_id = file_info.get('user_id', 0)
            original_name = file_info.get('original_name', os.path.basename(file_path))

            logger.info("开始处理文件: {}, 用户ID: {}", file_path, user_id)

            # 1. 解析文档
            text = self._parse_document(file_path)
            if not text.strip():
                return {
                    'original_file_path': file_path,
                    'status': 'error',
                    'error': '文档解析后为空'
                }

            # 2. 清洗文本
            text = self._clean_text(text)

            # 3. 分块
            chunks = self.text_splitter.split_text(text)
            logger.info("分块完成: {} 个文本块", len(chunks))

            # 4. Embedding
            embeddings = self.embedding_model.encode(
                chunks, normalize_embeddings=True
            ).tolist()

            # 5. 入库 pgvector（document_chunks 表）
            rows = [
                DocumentChunk(
                    source=original_name,
                    file_path=file_path,
                    user_id=str(user_id),
                    chunk_index=i,
                    content=chunk,
                    embedding=embedding,
                )
                for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
            ]
            async with AsyncSessionLocal() as session:
                session.add_all(rows)
                await session.commit()

            logger.info("入库完成: {} 个文本块已写入 pgvector", len(chunks))

            return {
                'original_file_path': file_path,
                'status': 'success',
                'chunks': len(chunks),
                'user_id': user_id,
            }

        except Exception as e:
            logger.exception("处理文件时发生错误: {}", str(e))
            return {
                'original_file_path': file_path,
                'status': 'error',
                'error': str(e),
            }

    async def process_directory(self, directory_path: str, user_id: int = 0) -> Dict[str, Any]:
        """批量处理整个目录"""
        try:
            results = []
            for root, _, files in os.walk(directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    file_info = {
                        'path': file_path,
                        'original_name': file,
                        'user_id': user_id,
                    }
                    result = await self.process_file(file_info)
                    results.append(result)

            return {
                'status': 'success',
                'processed_files': len(results),
                'results': results,
            }

        except Exception as e:
            logger.exception("处理目录时发生错误: {}", str(e))
            return {
                'status': 'error',
                'error': str(e),
            }
