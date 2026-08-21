"""
统一 Embedding 提供器

支持三种后端：
    - local:  本地 SentenceTransformer（离线，速度快）
    - ollama: Ollama HTTP API（远程，需部署 Ollama）
    - qwen:   通义千问 API（远程，OpenAI-compatible，兼容其他兼容接口）

用法:
    from app.services.embedding_provider import get_embedding_provider
    provider = get_embedding_provider()
    vectors = await provider.embed(["文本1", "文本2"])
    # 或同步模式（local 后端）
    vectors = provider.embed_sync(["文本1", "文本2"])
"""

from typing import List, Optional
import asyncio

import numpy as np
from sentence_transformers import SentenceTransformer
import aiohttp
import requests

from app.core.config import settings, EmbeddingServiceType
from app.core.logger import get_logger

logger = get_logger(service="embedding_provider")


# ==================== 抽象基类 ====================


class BaseEmbeddingProvider:
    """Embedding 提供器基类"""

    @property
    def dimension(self) -> int:
        return settings.EMBEDDING_DIMENSION

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """异步获取文本向量（子类实现）"""
        raise NotImplementedError

    def embed_sync(self, texts: List[str]) -> List[List[float]]:
        """同步获取文本向量（子类实现）"""
        raise NotImplementedError


# ==================== Local SentenceTransformer ====================


class LocalEmbeddingProvider(BaseEmbeddingProvider):
    """本地 SentenceTransformer（离线）"""

    def __init__(self):
        model_name = settings.EMBEDDING_MODEL
        logger.info("加载本地 Embedding 模型: {}", model_name)
        self._model = SentenceTransformer(model_name)

    async def embed(self, texts: List[str]) -> List[List[float]]:
        return self.embed_sync(texts)

    def embed_sync(self, texts: List[str]) -> List[List[float]]:
        embeddings = self._model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()


# ==================== Ollama HTTP API ====================


class OllamaEmbeddingProvider(BaseEmbeddingProvider):
    """Ollama HTTP API"""

    def __init__(self):
        self.base_url = settings.OLLAMA_BASE_URL.rstrip("/")
        self.model = settings.OLLAMA_EMBEDDING_MODEL
        self.api_url = f"{self.base_url}/api/embed"
        logger.info("使用 Ollama Embedding: {} @ {}", self.model, self.base_url)

    def embed_sync(self, texts: List[str]) -> List[List[float]]:
        payload = {"model": self.model, "input": texts}
        try:
            resp = requests.post(self.api_url, json=payload, timeout=settings.EMBEDDING_TIMEOUT)
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except Exception as e:
            logger.error("Ollama Embedding 失败: {}", e)
            return [[0.0] * self.dimension] * len(texts)

    async def embed(self, texts: List[str]) -> List[List[float]]:
        payload = {"model": self.model, "input": texts}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload, timeout=aiohttp.ClientTimeout(total=settings.EMBEDDING_TIMEOUT)) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                    return result["embeddings"]
        except Exception as e:
            logger.error("Ollama Embedding 失败: {}", e)
            return [[0.0] * self.dimension] * len(texts)


# ==================== 通义千问 API（OpenAI-compatible）====================


class QwenEmbeddingProvider(BaseEmbeddingProvider):
    """通义千问 Embedding API（也兼容其他 OpenAI-compatible 接口）"""

    def __init__(self):
        self.api_key = settings.QWEN_EMBEDDING_API_KEY
        self.base_url = settings.QWEN_EMBEDDING_BASE_URL.rstrip("/")
        self.model = settings.QWEN_EMBEDDING_MODEL
        self.api_url = f"{self.base_url}/embeddings"
        logger.info("使用 Qwen Embedding: {} @ {}", self.model, self.base_url)

    @staticmethod
    def _normalize(vec: List[float]) -> List[float]:
        """L2 归一化：qwen v4 不保证输出单位向量，而 pgvector/hybrid 余弦检索依赖单位向量"""
        arr = np.asarray(vec, dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm == 0:
            return vec  # 全 0 兜底向量原样返回，保留失败语义
        return (arr / norm).tolist()

    async def embed(self, texts: List[str]) -> List[List[float]]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
            "dimensions": settings.EMBEDDING_DIMENSION,  # 显式锁定输出维度，与表 Vector(dim) 一致
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=settings.EMBEDDING_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                    return [self._normalize(item["embedding"]) for item in result["data"]]
        except Exception as e:
            logger.error("Qwen Embedding 失败: {}", e)
            return [[0.0] * self.dimension] * len(texts)

    def embed_sync(self, texts: List[str]) -> List[List[float]]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
            "dimensions": settings.EMBEDDING_DIMENSION,  # 显式锁定输出维度，与表 Vector(dim) 一致
        }
        try:
            resp = requests.post(self.api_url, json=payload, headers=headers, timeout=settings.EMBEDDING_TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            return [self._normalize(item["embedding"]) for item in result["data"]]
        except Exception as e:
            logger.error("Qwen Embedding 失败: {}", e)
            return [[0.0] * self.dimension] * len(texts)


# ==================== 工厂函数 ====================

_provider: Optional[BaseEmbeddingProvider] = None


def get_embedding_provider() -> BaseEmbeddingProvider:
    """根据 settings.EMBEDDING_TYPE 获取 Embedding 提供器（单例）"""
    global _provider
    if _provider is not None:
        return _provider

    if settings.EMBEDDING_TYPE == EmbeddingServiceType.LOCAL:
        _provider = LocalEmbeddingProvider()
    elif settings.EMBEDDING_TYPE == EmbeddingServiceType.QWEN:
        _provider = QwenEmbeddingProvider()
    else:  # ollama (default)
        _provider = OllamaEmbeddingProvider()

    return _provider


def reset_embedding_provider():
    """重置单例（用于测试或配置热更新）"""
    global _provider
    _provider = None


async def embed_in_batches(texts: List[str], batch_size: int = 10) -> List[List[float]]:
    """分批调用 embedding API 并拼接结果。

    text-embedding-v4 单请求文本数上限为 10 条，索引/兜底编码等批量场景
    必须分批。每批失败(返回全零向量)按指数退避重试 EMBEDDING_MAX_RETRIES 次,
    重试耗尽仍失败返回全零(不抛错),由调用方检测。
    """
    provider = get_embedding_provider()
    results: List[List[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        vecs = await _embed_with_retry(provider, batch)
        results.extend(vecs)
    return results


async def _embed_with_retry(provider, batch: List[str]) -> List[List[float]]:
    """单批嵌入 + 指数退避重试([1,2,4]s);全零视为失败。"""
    max_retries = settings.EMBEDDING_MAX_RETRIES
    for attempt in range(max_retries):
        vecs = await provider.embed(batch)
        if vecs and all(np.count_nonzero(v) > 0 for v in vecs):
            return vecs
        if attempt < max_retries - 1:
            delay = [1, 2, 4][min(attempt, 2)]
            logger.warning("Embedding 批次失败(全零),{}s 后重试({}/{})",
                           delay, attempt + 1, max_retries)
            await asyncio.sleep(delay)
    return [[0.0] * settings.EMBEDDING_DIMENSION for _ in batch]
