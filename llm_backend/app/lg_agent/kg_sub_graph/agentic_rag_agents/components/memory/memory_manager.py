"""
三层记忆管理器（主入口）

架构：
    第一层：最近 N 轮完整对话（滑动窗口，保留原文）
    第二层：中等摘要（窗口外的 6-15 轮，LLM 压缩为 ~200 字）
    第三层：高层摘要（16 轮以前，进一步压缩为 ~100 字）

工作流程：
    1. 输入：完整的 state.messages
    2. 分层：按轮次切分为三层
    3. 压缩：对老消息调用 LLM 生成摘要
    4. 预算检查：TokenBudgetManager 检查总 token 是否超限
    5. 输出：[系统提示] + [高层摘要] + [中等摘要] + [最近对话]
"""

from typing import List, Any, Optional, Dict, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from app.core.logger import get_logger
from .token_budget import TokenBudgetManager
from .memory_compressor import compress_medium, compress_high, ConversationSummary
from .memory_cache import MemoryCache

logger = get_logger(service="memory_manager")

# 默认配置
DEFAULT_RECENT_WINDOW = 5      # 保留最近 N 轮完整对话（1 轮 = 1 user + 1 assistant）
DEFAULT_MEDIUM_TURNS = 10      # 中等摘要覆盖的轮数（6-15 轮 → 10 轮）


class MemoryManager:
    """
    三层记忆管理器。

    用法：
        manager = MemoryManager(llm=chat_model)
        managed = await manager.manage(messages)
        # managed = [SystemMessage(摘要), ...recent Human/AIMessages]
    """

    def __init__(
        self,
        llm: BaseChatModel,
        total_budget: int = 8000,
        recent_window: int = DEFAULT_RECENT_WINDOW,
        medium_turns: int = DEFAULT_MEDIUM_TURNS,
        budgets: Optional[Dict[str, int]] = None,
        cache: Optional[MemoryCache] = None,
    ):
        """
        Args:
            llm: 用于压缩摘要的 LLM
            total_budget: 总 token 预算
            recent_window: 保留最近几轮完整对话
            medium_turns: 中等摘要覆盖的轮数
            budgets: 自定义各部分预算分配
            cache: Redis 缓存实例（可选，不传则不使用缓存）
        """
        self.llm = llm
        self.recent_window = recent_window
        self.medium_turns = medium_turns
        self.budget = TokenBudgetManager(total_budget=total_budget, budgets=budgets)
        self.cache = cache

        # 内存中的摘要缓存（无 Redis 时的降级方案）
        self._high_summary: Optional[ConversationSummary] = None
        self._medium_summary: Optional[ConversationSummary] = None
        self._last_processed_count: int = 0

    def _split_messages_into_pairs(self, messages: List[Any]) -> List[Tuple[Any, Any]]:
        """
        将消息列表按 (Human, AI) 配对为轮次。

        处理不规则情况：
            - 连续多条 Human/AI 消息
            - 以 System 消息开头
        """
        pairs = []
        buffer = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                continue  # 跳过系统消息
            buffer.append(msg)
            if len(buffer) >= 2:
                pairs.append((buffer[0], buffer[1]))
                buffer = buffer[2:]

        # 剩余的不完整轮次也保留
        if buffer:
            pairs.append(tuple(buffer))

        return pairs

    async def manage(
        self,
        messages: List[Any],
        system_prompt: str = "",
        documents_text: str = "",
        conversation_id: Optional[str] = None,
    ) -> List[Any]:
        """
        管理对话历史，返回符合 token 预算的消息列表。

        Args:
            messages: 完整的对话历史消息列表
            system_prompt: 系统提示词（固定占用预算）
            documents_text: 检索到的文档文本（固定占用预算）
            conversation_id: 会话 ID（可选，传入则启用 Redis 缓存）

        Returns:
            管理后的消息列表，格式：
            [SystemMessage(摘要上下文), ...最近 Human/AIMessages]
        """
        if not messages:
            return []

        # 1. 按轮次配对
        pairs = self._split_messages_into_pairs(messages)
        total_turns = len(pairs)

        logger.info(f"记忆管理: {len(messages)} 条消息, {total_turns} 轮对话")

        # 2. 尝试从 Redis 缓存加载已有摘要
        cached_data = None
        if conversation_id and self.cache:
            cached_data = self.cache.load_summary(conversation_id)
            if cached_data:
                self._high_summary = self.cache.reconstruct_summary(cached_data, "high_summary")
                self._medium_summary = self.cache.reconstruct_summary(cached_data, "medium_summary")
                cached_turns = cached_data.get("compressed_turns", 0)
                logger.info(f"Redis 缓存命中: 已压缩 {cached_turns} 轮")

        # 3. 分层切分
        recent_pairs = pairs[-self.recent_window:] if total_turns > self.recent_window else pairs
        older_pairs = pairs[:-self.recent_window] if total_turns > self.recent_window else []

        # 4. 增量压缩：只压缩缓存中没有的新轮次
        summary_parts = []
        need_compress_from = 0  # 从第几轮开始需要压缩

        if cached_data:
            need_compress_from = cached_data.get("compressed_turns", 0)

        if older_pairs:
            # 只取缓存中没有的轮次（增量部分）
            pairs_to_compress = older_pairs[need_compress_from:]
            new_turns_count = len(pairs_to_compress)

            if new_turns_count > 0:
                logger.info(f"增量压缩: 从第 {need_compress_from} 轮开始, 新增 {new_turns_count} 轮")
                medium_start = max(0, len(older_pairs) - self.medium_turns)

                # 高层压缩
                high_pairs = older_pairs[:medium_start]
                if high_pairs:
                    high_messages = [msg for pair in high_pairs for msg in pair]
                    self._high_summary = await compress_high(
                        self.llm,
                        previous_summary=self._high_summary.summary if self._high_summary else "",
                        new_messages=high_messages,
                    )

                # 中等压缩
                medium_pairs = older_pairs[medium_start:]
                if medium_pairs:
                    medium_messages = [msg for pair in medium_pairs for msg in pair]
                    self._medium_summary = await compress_medium(
                        self.llm,
                        medium_messages,
                    )

            # 拼接摘要文本
            if self._high_summary:
                summary_parts.append(f"[历史摘要] {self._high_summary.summary}")
                if self._high_summary.key_entities:
                    summary_parts.append(f"[关键实体] {', '.join(self._high_summary.key_entities)}")

            if self._medium_summary:
                summary_parts.append(f"[近期摘要] {self._medium_summary.summary}")
                if self._medium_summary.key_entities:
                    summary_parts.append(f"[相关实体] {', '.join(self._medium_summary.key_entities)}")

        # 4. 构建摘要系统消息
        summary_text = "\n".join(summary_parts) if summary_parts else ""

        # 5. Token 预算检查与裁剪
        actual_usage = {
            "system_prompt": self.budget.count_tokens(system_prompt),
            "history_summary": self.budget.count_tokens(summary_text),
            "documents": self.budget.count_tokens(documents_text),
        }

        # 裁剪摘要如果超预算
        if actual_usage["history_summary"] > self.budget.budgets.get("history_summary", 800):
            summary_text = self.budget.trim_text(
                summary_text, self.budget.budgets.get("history_summary", 800)
            )
            actual_usage["history_summary"] = self.budget.count_tokens(summary_text)

        # 6. 组装最终消息列表
        result = []

        # 摘要作为系统消息
        if summary_text:
            result.append(SystemMessage(content=f"以下是之前的对话摘要：\n{summary_text}"))

        # 最近的完整对话
        recent_messages = [msg for pair in recent_pairs for msg in pair]

        # 检查最近对话是否超预算
        recent_tokens = self.budget.count_messages_tokens(recent_messages)
        recent_budget = self.budget.budgets.get("recent_history", 2000)

        if recent_tokens > recent_budget:
            # 从最老的开始裁剪，保留最新的
            trimmed = self._trim_messages_to_budget(recent_messages, recent_budget)
            result.extend(trimmed)
            logger.info(f"最近对话裁剪: {len(recent_messages)} -> {len(trimmed)} 条")
        else:
            result.extend(recent_messages)

        self._last_processed_count = len(messages)

        # 7. 将压缩结果写入 Redis 缓存（供下次增量使用）
        if conversation_id and self.cache and older_pairs:
            self.cache.save_summary(
                conversation_id=conversation_id,
                high_summary=self._high_summary,
                medium_summary=self._medium_summary,
                compressed_turns=len(older_pairs),
                total_turns=total_turns,
            )

        total_tokens = self.budget.count_messages_tokens(result)
        logger.info(
            f"记忆管理完成: {len(result)} 条消息, ~{total_tokens} tokens "
            f"(摘要 {actual_usage['history_summary']} + 最近 {self.budget.count_messages_tokens(recent_messages)})"
        )

        return result

    def _trim_messages_to_budget(self, messages: List[Any], budget: int) -> List[Any]:
        """
        从最新消息开始倒序保留，直到 token 预算用完。

        原因：最近的对话最有价值，优先保留。
        """
        result = []
        used = 0

        for msg in reversed(messages):
            msg_tokens = self.budget.count_messages_tokens([msg])
            if used + msg_tokens > budget:
                break
            result.insert(0, msg)
            used += msg_tokens

        return result

    def reset(self, conversation_id: Optional[str] = None):
        """重置缓存（新会话时调用）"""
        self._high_summary = None
        self._medium_summary = None
        self._last_processed_count = 0
        if conversation_id and self.cache:
            self.cache.delete_summary(conversation_id)
        logger.info("记忆管理器已重置")
