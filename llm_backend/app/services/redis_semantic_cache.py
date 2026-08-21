from typing import Dict, List, Optional, Tuple
import redis.asyncio as aioredis
import hashlib
import numpy as np
import json
import time
from app.core.config import settings
from app.core.logger import get_logger
from app.services.embedding_provider import get_embedding_provider
from app.services.pronoun_detector import detect_pronoun, DetectionDecision
from app.services.pronoun_resolver import resolve_pronouns
import asyncio
import threading
from datetime import datetime

logger = get_logger(service="redis_cache")

class RedisSemanticCache:
    """基于语义的 Redis 缓存实现

    生命周期说明：
        __init__ 不启动后台任务；清理任务由 start_cleanup() 幂等启动。
        实例应通过 get_instance() 获取（按 prefix+user_id 池化），
        保证每个用户维度仅一个实例、一个清理任务，避免任务泄漏。
    """

    # 实例池：按 (prefix, user_id) 复用，避免每次请求重建连接与清理任务
    _instances: Dict[Tuple[str, Optional[int]], "RedisSemanticCache"] = {}
    _instances_lock = threading.Lock()

    def __init__(
        self,
        redis_url: str = None,
        score_threshold: float = None,
        prefix: str = None,
        user_id: Optional[int] = None,
        max_cache_size: int = None,
        cleanup_interval: int = None
    ):
        self.redis = aioredis.from_url(redis_url or settings.REDIS_URL)
        self.score_threshold = score_threshold or settings.REDIS_CACHE_THRESHOLD
        self.prefix = prefix if prefix is not None else settings.REDIS_CACHE_PREFIX
        self.max_cache_size = max_cache_size if max_cache_size is not None else settings.REDIS_CACHE_MAX_SIZE
        self.cleanup_interval = cleanup_interval if cleanup_interval is not None else settings.REDIS_CACHE_CLEANUP_INTERVAL
        self.prefix = f"{self.prefix}:{user_id}" if user_id else self.prefix
        self._embedding_provider = get_embedding_provider()

        # 清理任务生命周期：由 start_cleanup() 显式启动，__init__ 不创建任务（防泄漏）
        self._cleanup_started = False
        self._cleanup_lock = threading.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        # 有序索引：{prefix}:index → ZSET(member=hash_id, score=last_access)
        # 替代 keys() 全库扫描，lookup/cleanup 均通过该索引访问缓存条目
        self._index_key = f"{self.prefix}:index"

    # ==================== 实例池与生命周期 ====================

    @classmethod
    def get_instance(cls, prefix: str = None, user_id: Optional[int] = None) -> "RedisSemanticCache":
        """按 (prefix, user_id) 获取/创建缓存实例（池化复用）。

        每次请求 new 实例会反复重建 Redis 连接池并泄漏清理任务，
        统一走池保证每个用户维度仅一个实例、一个清理任务。
        """
        key = (prefix or settings.REDIS_CACHE_PREFIX, user_id)
        with cls._instances_lock:
            if key not in cls._instances:
                inst = cls(prefix=prefix, user_id=user_id)
                inst.start_cleanup()
                cls._instances[key] = inst
            return cls._instances[key]

    def start_cleanup(self):
        """幂等启动自动清理任务（需在运行的事件循环内调用）"""
        if self._cleanup_started:
            return
        with self._cleanup_lock:
            if self._cleanup_started:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("无运行中的事件循环，清理任务延迟启动")
                return
            self._cleanup_task = loop.create_task(self._auto_cleanup())
            self._cleanup_started = True
            logger.info("自动清理任务已启动，prefix={}", self.prefix)

    async def _get_embedding(self, text: str) -> List[float]:
        """使用统一 Embedding Provider 获取文本向量"""
        try:
            embeddings = await self._embedding_provider.embed([text])
            if not embeddings or not embeddings[0]:
                raise ValueError("Failed to get embedding")
            return embeddings[0]
        except Exception as e:
            logger.exception("Error in get_embedding: {}", str(e))
            raise

    # ==================== Key 生成 ====================

    def _get_hash_id(self, message: str) -> str:
        """根据消息生成缓存条目 hash_id"""
        return hashlib.md5(message.encode()).hexdigest()

    def _get_vector_key(self, message: str) -> str:
        """生成向量存储的键名"""
        return f"{self.prefix}:vec:{self._get_hash_id(message)}"

    def _get_response_key(self, message: str) -> str:
        """生成响应存储的键名"""
        return f"{self.prefix}:resp:{self._get_hash_id(message)}"

    def _get_metadata_key(self, message: str) -> str:
        """生成元数据存储的键名"""
        return f"{self.prefix}:meta:{self._get_hash_id(message)}"

    def _get_last_user_message(self, messages: List[Dict]) -> str:
        """获取最后一条用户消息"""
        for msg in reversed(messages):
            if msg["role"] == "user":
                return msg["content"]
        return ""

    # ==================== 分级指代消解 ====================

    async def _resolve_message(self, messages: List[Dict], raw: str, resolve_llm) -> Optional[str]:
        """分级指代消解：检测 → LLM 消解 → 降级透传。

        lookup 和 update 必须走同一套逻辑（SPEC §原则三），
        保证消解后的消息在写入和查询时生成相同向量，从而能命中。

        Returns:
            - None: 纯语气词，调用方跳过缓存（不查不写）
            - str:  用于缓存查找/写入的完整消息（消解后或原样透传）
        """
        if not settings.RESOLVE_ENABLED:
            return raw  # 总开关关闭，完全退化为现有行为

        decision = detect_pronoun(raw, skip_filler=settings.RESOLVE_SKIP_FILLER)
        if decision == DetectionDecision.SKIP_CACHE:
            logger.info("纯语气词，跳过缓存: '{}'", raw)
            return None
        if decision == DetectionDecision.NEED_RESOLVE:
            if resolve_llm is None:
                logger.warning("检测到指代但未注入 LLM，降级为原始消息: '{}'", raw)
                return raw
            return await resolve_pronouns(resolve_llm, messages, raw)
        return raw  # PASS_THROUGH，零额外开销

    # ==================== 索引维护 ====================

    async def _rebuild_index(self):
        """索引为空时用 scan_iter 重建（兼容升级前的存量键，一次性兜底）"""
        count = 0
        async for vec_key in self.redis.scan_iter(match=f"{self.prefix}:vec:*"):
            key = vec_key.decode("utf-8") if isinstance(vec_key, bytes) else vec_key
            hash_id = key.split(":")[-1]
            await self.redis.zadd(self._index_key, {hash_id: datetime.now().timestamp()})
            count += 1
        if count:
            logger.info("重建缓存索引完成，prefix={}，条目数={}", self.prefix, count)

    async def _auto_cleanup(self):
        """自动清理过期和超量的缓存（基于 ZSET 索引，非 keys 全库扫描）"""
        while True:
            try:
                # 取当前前缀下所有条目（member=hash_id, score=last_access）
                # 注：redis 默认 decode_responses=False，member 返回 bytes，需解码
                items = await self.redis.zrange(self._index_key, 0, -1, withscores=True)

                if len(items) > self.max_cache_size:
                    # 按最近访问时间升序，删除最旧的条目直到达到限制
                    items.sort(key=lambda x: x[1])
                    items_to_remove = len(items) - self.max_cache_size
                    for hash_id, _ in items[:items_to_remove]:
                        if isinstance(hash_id, bytes):
                            hash_id = hash_id.decode("utf-8")
                        await self._remove_cache_item(hash_id)

                logger.info("Cache cleanup completed for prefix {}", self.prefix)

            except Exception as e:
                logger.exception("Error in cache cleanup: {}", str(e))

            await asyncio.sleep(self.cleanup_interval)

    async def _remove_cache_item(self, hash_id: str):
        """删除一个缓存项的所有相关键（含索引条目）"""
        try:
            # 防御：ZSET 成员读出可能是 bytes（decode_responses=False）
            if isinstance(hash_id, bytes):
                hash_id = hash_id.decode("utf-8")
            await self.redis.delete(
                f"{self.prefix}:vec:{hash_id}",
                f"{self.prefix}:resp:{hash_id}",
                f"{self.prefix}:meta:{hash_id}"
            )
            await self.redis.zrem(self._index_key, hash_id)
        except Exception as e:
            logger.exception("Error removing cache item: {}", str(e))

    async def _update_metadata(self, message: str):
        """更新缓存项的元数据与索引访问时间"""
        try:
            hash_id = self._get_hash_id(message)
            meta_key = self._get_metadata_key(message)
            # 从Redis读取的是bytes,需要解码
            current_meta = await self.redis.get(meta_key)
            if current_meta:
                current_meta = json.loads(current_meta.decode('utf-8'))
            else:
                current_meta = {"access_count": 0}

            metadata = {
                "last_access": datetime.now().timestamp(),
                "access_count": current_meta["access_count"] + 1
            }
            await self.redis.set(meta_key, json.dumps(metadata), ex=settings.REDIS_CACHE_EXPIRE)
            # 同步更新索引 score（按最近访问排序，供清理淘汰）
            await self.redis.zadd(self._index_key, {hash_id: metadata["last_access"]})
        except Exception as e:
            logger.exception("Error updating metadata: {}", str(e))

    async def lookup(self, messages: List[Dict], resolve_llm=None) -> Optional[str]:
        """查找缓存的响应

        Args:
            messages: 完整对话消息列表（最后一条为当前用户消息）
            resolve_llm: 指代消解用的 LLM 服务（具备 generate(messages, ...) 方法）；
                         由调用方注入以避免循环依赖
        """
        try:
            user_message = self._get_last_user_message(messages)
            if not user_message:
                return None

            # 分级指代消解：SKIP_CACHE → None（不查）；其余得到用于查找的完整消息
            resolved_message = await self._resolve_message(messages, user_message, resolve_llm)
            if resolved_message is None:
                return None

            current_vector = await self._get_embedding(resolved_message)

            # 索引为空时先重建（兼容升级前直接 set 的存量键）
            if await self.redis.zcard(self._index_key) == 0:
                await self._rebuild_index()

            # 通过 ZSET 索引取当前前缀下的全部 hash_id（替代 keys() 全库扫描）
            # 注：redis 默认 decode_responses=False，member 返回 bytes，需解码
            members = await self.redis.zrange(self._index_key, 0, -1)
            max_similarity = 0
            most_similar_hash = None

            for hash_id in members:
                if isinstance(hash_id, bytes):
                    hash_id = hash_id.decode("utf-8")
                vec_raw = await self.redis.get(f"{self.prefix}:vec:{hash_id}")
                if not vec_raw:
                    continue
                cached_vector = json.loads(vec_raw.decode('utf-8'))
                similarity = np.dot(current_vector, cached_vector) / (
                    np.linalg.norm(current_vector) * np.linalg.norm(cached_vector)
                )

                if similarity > max_similarity:
                    max_similarity = similarity
                    most_similar_hash = hash_id

            if max_similarity >= self.score_threshold and most_similar_hash:
                resp_key = f"{self.prefix}:resp:{most_similar_hash}"
                cached_response = await self.redis.get(resp_key)

                if cached_response:
                    # 更新访问元数据（基于消解后消息，与写入时的 key 一致）
                    await self._update_metadata(resolved_message)
                    logger.info("Cache hit with similarity: {:.4f}", max_similarity)
                    return cached_response.decode('utf-8')

            return None

        except Exception as e:
            logger.exception("Error in lookup: {}", str(e))
            return None

    async def update(self, messages: List[Dict], response: str, expire: int = None, resolve_llm=None):
        """更新缓存

        Args:
            messages: 完整对话消息列表（最后一条为当前用户消息）
            response: 生成的完整响应
            expire: 过期时间（秒），默认 settings.REDIS_CACHE_EXPIRE
            resolve_llm: 指代消解用的 LLM 服务（同 lookup，注入避免循环依赖）
        """
        try:
            user_message = self._get_last_user_message(messages)
            if not user_message:
                return

            # 分级指代消解：SKIP_CACHE → None（不写，避免缓存污染）
            resolved_message = await self._resolve_message(messages, user_message, resolve_llm)
            if resolved_message is None:
                return

            vector = await self._get_embedding(resolved_message)

            # 缓存 key 基于消解后消息生成（SPEC §6.4），保证消解后能命中已有缓存
            hash_id = self._get_hash_id(resolved_message)
            vec_key = self._get_vector_key(resolved_message)
            resp_key = self._get_response_key(resolved_message)
            meta_key = self._get_metadata_key(resolved_message)

            expire = expire or settings.REDIS_CACHE_EXPIRE

            # 存储向量、响应和元数据 - 确保存储为字符串
            await self.redis.set(vec_key, json.dumps(vector), ex=expire)
            await self.redis.set(resp_key, response.encode('utf-8'), ex=expire)

            metadata = {
                "created_at": datetime.now().timestamp(),
                "last_access": datetime.now().timestamp(),
                "access_count": 1
            }
            await self.redis.set(meta_key, json.dumps(metadata), ex=expire)
            # 维护有序索引（member=hash_id, score=last_access）
            await self.redis.zadd(self._index_key, {hash_id: metadata["last_access"]})

            logger.info("Cache updated for message: {}...", resolved_message[:50])

        except Exception as e:
            logger.exception("Error in update: {}", str(e))
