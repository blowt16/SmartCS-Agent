"""
对话摘要 Redis 缓存

为什么需要这一层？
    MemoryManager 每次压缩老对话都要调用 LLM，很贵（token + 延迟）。
    但同一会话中，用户连续发消息时，历史对话几乎没变——只是多了一轮。

    所以把压缩好的摘要缓存到 Redis：
        - 同一会话再次请求时，直接从 Redis 取缓存，跳过 LLM 压缩
        - 设置 TTL（默认 24 小时），过期自动清理，不占空间
        - 只有新增轮次超出缓存范围时，才增量压缩新的部分

缓存结构（按会话维度）：
    key:   memory:summary:{conversation_id}
    value: JSON {
        "high_summary": {...},      # 高层摘要（16轮以前）
        "medium_summary": {...},    # 中等摘要（6-15轮）
        "compressed_turns": 10,     # 已压缩到第几轮
        "total_turns": 12           # 缓存时的总轮数
    }
    TTL: 86400 秒（24小时）
"""

from typing import Optional, Dict, Any
import json

import redis

from app.core.config import settings
from app.core.logger import get_logger
from .memory_compressor import ConversationSummary

logger = get_logger(service="memory_cache")

# 默认 TTL：24 小时
DEFAULT_SUMMARY_TTL = settings.MEMORY_CACHE_TTL


class MemoryCache:
    """
    对话摘要的 Redis 缓存管理器。

    用法：
        cache = MemoryCache()
        # 存
        cache.save_summary("conv_123", high, medium, compressed_turns=10, total_turns=12)
        # 取
        cached = cache.load_summary("conv_123")
        if cached:
            # 直接用缓存的摘要，跳过 LLM 压缩
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        ttl: int = DEFAULT_SUMMARY_TTL,
    ):
        self.redis = redis.from_url(redis_url or settings.REDIS_URL)
        self.ttl = ttl

    def _make_key(self, conversation_id: str) -> str:
        return f"memory:summary:{conversation_id}"

    def save_summary(
        self,
        conversation_id: str,
        high_summary: Optional[ConversationSummary],
        medium_summary: Optional[ConversationSummary],
        compressed_turns: int,
        total_turns: int,
    ) -> bool:
        """
        将压缩后的摘要存入 Redis。

        Args:
            conversation_id: 会话 ID
            high_summary: 高层摘要（可为 None）
            medium_summary: 中等摘要（可为 None）
            compressed_turns: 已压缩到第几轮
            total_turns: 当前总轮数
        """
        data = {
            "high_summary": high_summary.model_dump() if high_summary else None,
            "medium_summary": medium_summary.model_dump() if medium_summary else None,
            "compressed_turns": compressed_turns,
            "total_turns": total_turns,
        }

        key = self._make_key(conversation_id)
        try:
            self.redis.set(key, json.dumps(data, ensure_ascii=False), ex=self.ttl)
            logger.info(
                "摘要已缓存: {}, 已压缩 {}/{} 轮, TTL={}s",
                conversation_id, compressed_turns, total_turns, self.ttl,
            )
            return True
        except Exception as e:
            logger.error("缓存摘要失败: {}", e)
            return False

    def load_summary(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """
        从 Redis 加载缓存的摘要。

        Returns:
            缓存数据字典，或 None（缓存不存在/过期）
        """
        key = self._make_key(conversation_id)
        try:
            raw = self.redis.get(key)
            if raw is None:
                logger.info("摘要缓存未命中: {}", conversation_id)
                return None

            data = json.loads(raw)
            logger.info(
                "摘要缓存命中: {}, 已压缩 {}/{} 轮",
                conversation_id, data.get("compressed_turns", 0), data.get("total_turns", 0),
            )
            return data
        except Exception as e:
            logger.error("加载摘要缓存失败: {}", e)
            return None

    def delete_summary(self, conversation_id: str) -> bool:
        """删除指定会话的摘要缓存"""
        key = self._make_key(conversation_id)
        try:
            self.redis.delete(key)
            logger.info("摘要缓存已删除: {}", conversation_id)
            return True
        except Exception as e:
            logger.error("删除摘要缓存失败: {}", e)
            return False

    def reconstruct_summary(
        self, data: Optional[Dict[str, Any]], key: str
    ) -> Optional[ConversationSummary]:
        """从缓存数据重建 ConversationSummary 对象"""
        if not data or not data.get(key):
            return None
        return ConversationSummary(**data[key])
