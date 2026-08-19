"""
Token 预算管理器

为什么需要 Token 预算管理？
    LLM 有固定的上下文窗口大小（如 DeepSeek 8K tokens）。
    如果对话历史 + 检索文档 + 系统提示词超过了窗口大小，会报错或截断。

    预算管理的作用：在调用 LLM 之前，检查各部分的 token 数量，
    如果超出预算，按优先级裁剪或压缩，确保总 token 数在窗口内。

预算分配策略（默认）：
    系统提示词    500 tokens  （固定，不裁剪）
    对话摘要      800 tokens  （压缩后的历史）
    最近完整对话  2000 tokens （保留原文）
    检索文档      4000 tokens （RAG 检索结果）
    预留回答空间  700 tokens  （给 LLM 回答）
    总计          8000 tokens
"""

from typing import List, Dict, Any, Optional

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
    ENCODING = tiktoken.get_encoding("cl100k_base")
except ImportError:
    TIKTOKEN_AVAILABLE = False
    ENCODING = None

from app.core.logger import get_logger

logger = get_logger(service="token_budget")


class TokenBudgetManager:
    """
    Token 预算管理器：分配、检查、裁剪上下文各部分的 token 数量。
    """

    DEFAULT_BUDGETS = {
        "system_prompt": 500,
        "history_summary": 800,
        "recent_history": 2000,
        "documents": 4000,
        "response": 700,
    }

    def __init__(self, total_budget: int = 8000, budgets: Optional[Dict[str, int]] = None):
        self.total_budget = total_budget
        self.budgets = budgets or self.DEFAULT_BUDGETS.copy()

        allocated = sum(self.budgets.values())
        if allocated > total_budget:
            logger.warning("预算总和 {} 超过总预算 {}, 自动缩放", allocated, total_budget)
            scale = total_budget / allocated
            for key in self.budgets:
                self.budgets[key] = int(self.budgets[key] * scale)

        logger.info("Token 预算分配: {}, 总预算: {}", self.budgets, total_budget)

    def count_tokens(self, text: str) -> int:
        """
        计算 text 的 token 数量。
        使用 tiktoken（精确），未安装则用简单估算。
        """
        if TIKTOKEN_AVAILABLE and ENCODING:
            return len(ENCODING.encode(text))
        else:
            # 简单估算：中文约 1.5 字符/token，英文约 4 字符/token
            chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
            other_chars = len(text) - chinese_chars
            return int(chinese_chars / 1.5 + other_chars / 4)

    def count_messages_tokens(self, messages: List[Any]) -> int:
        """计算 messages 列表的总 token 数量。"""
        total = 0
        for msg in messages:
            if hasattr(msg, "content"):
                content = msg.content
            elif isinstance(msg, dict):
                content = msg.get("content", "")
            else:
                content = str(msg)
            total += self.count_tokens(content) + 8  # +8 是格式开销
        return total

    def is_over_budget(self, actual: Dict[str, int]) -> bool:
        """检查各部分是否超预算。"""
        for key, tokens in actual.items():
            budget = self.budgets.get(key, 0)
            if tokens > budget:
                logger.warning("{} 超预算: {} > {}", key, tokens, budget)
                return True
        return False

    def trim_text(self, text: str, target_tokens: int) -> str:
        """
        将 text 裁剪到目标 token 数量。
        优先保留开头的句子，因为开头通常包含重要信息。
        """
        if self.count_tokens(text) <= target_tokens:
            return text

        # 按句子分割
        sentences = []
        current = ""
        for char in text:
            current += char
            if char in ['。', '！', '？', '.', '!', '?', '\n']:
                sentences.append(current)
                current = ""
        if current:
            sentences.append(current)

        result = ""
        for sent in sentences:
            if self.count_tokens(result + sent) > target_tokens:
                break
            result += sent

        logger.info("文本裁剪: {} -> {} tokens", self.count_tokens(text), self.count_tokens(result))
        return result

    def get_remaining_budget(self, used: Dict[str, int]) -> int:
        """计算剩余可用预算。"""
        total_used = sum(used.values())
        return max(0, self.total_budget - total_used)
