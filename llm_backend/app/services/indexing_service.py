import os
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any
import mimetypes
import shutil
import uuid

import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from PyPDF2 import PdfReader
from docx import Document
from sentence_transformers import SentenceTransformer

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="indexing")


class IndexingService:
    """标准 RAG 索引服务：解析 → 清洗 → 分块 → Embedding → ChromaDB 入库"""

    def __init__(self):
        # 文本分割器 — 复用 P0 智能分块的参数
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
        )

        # Embedding 模型 — 优先用 settings 中的配置，兜底本地 bge-m3
        embedding_model_name = getattr(settings, "EMBEDDING_MODEL", "bge-m3")
        self.embedding_model = SentenceTransformer(embedding_model_name)

        # Embedding 函数 — 适配 ChromaDB 接口
        def embedding_function(texts):
            return self.embedding_model.encode(texts, normalize_embeddings=True).tolist()

        self.embedding_fn = embedding_function

        # ChromaDB 客户端
        os.makedirs(settings.VECTOR_DB_PATH, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=settings.VECTOR_DB_PATH,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name=settings.VECTOR_DB_COLLECTION,
            embedding_function=self.embedding_fn,
        )

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
        """处理单个文件：解析 → 清洗 → 分块 → Embedding → ChromaDB 入库"""
        try:
            file_path = file_info['path']
            user_id = file_info.get('user_id', 0)
            original_name = file_info.get('original_name', os.path.basename(file_path))

            logger.info(f"开始处理文件: {file_path}, 用户ID: {user_id}")

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
            logger.info(f"分块完成: {len(chunks)} 个文本块")

            # 4. 入库 ChromaDB（带元数据）
            chunk_ids = [
                f"user_{user_id}_{uuid.uuid4().hex[:8]}_{i}"
                for i in range(len(chunks))
            ]
            metadatas = [
                {
                    "source": original_name,
                    "file_path": file_path,
                    "user_id": str(user_id),
                    "chunk_index": i,
                }
                for i in range(len(chunks))
            ]

            self.collection.add(
                ids=chunk_ids,
                documents=chunks,
                metadatas=metadatas,
            )

            logger.info(f"入库完成: {len(chunks)} 个文本块已写入 ChromaDB")

            return {
                'original_file_path': file_path,
                'status': 'success',
                'chunks': len(chunks),
                'user_id': user_id,
            }

        except Exception as e:
            logger.error(f"处理文件时发生错误: {str(e)}", exc_info=True)
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
            logger.error(f"处理目录时发生错误: {str(e)}", exc_info=True)
            return {
                'status': 'error',
                'error': str(e),
            }
