"""
Token 裁剪阈值管理器

给上下文的两部分各设一个 token 上限，超出即裁剪：
    对话摘要      800 tokens  （压缩后的历史，按句裁剪、保留开头）
    最近完整对话  2000 tokens （保留原文，从最老的开始丢弃）

注意：这里只管"各部分裁剪阈值"，不做全局总量校验——总量取决于模型上下文窗口，
由调用方按实际模型决定（当前 .env 为 deepseek-v4-flash，窗口远大于早期设计所设想的
8K），本模块不承担总窗口兜底。
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
    Token 裁剪阈值管理器：给上下文各部分设 token 上限，并提供计数/裁剪工具。
    """

    DEFAULT_BUDGETS = {
        "history_summary": 800,
        "recent_history": 2000,
    }

    def __init__(self, budgets: Optional[Dict[str, int]] = None):
        self.budgets = budgets or self.DEFAULT_BUDGETS.copy()

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
