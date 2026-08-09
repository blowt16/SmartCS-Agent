"""
④ Token/调用预算护栏

为什么需要？
    一次用户请求可能触发 10+ 次 LLM 调用（预处理管道 5 次 + multi_tool 5 次）。
    恶意用户或异常场景可能导致更多调用，成本直接爆炸。

做法：
    BudgetGuard 作为全局计数器，每次 LLM 调用前 +1 并累加 token。
    超过上限时，跳过非必要的 LLM 调用（如查询扩展、HyDE），
    保留必要的调用（路由、工具执行）。
"""

from typing import Optional
from dataclasses import dataclass

from app.core.logger import get_logger

logger = get_logger(service="budget_guard")


@dataclass
class BudgetConfig:
    """预算配置"""
    max_llm_calls: int = 12            # 单次请求最大 LLM 调用次数
    max_total_tokens: int = 50000      # 单次请求最大总 token 消耗
    essential_calls_reserved: int = 5  # 为必要调用预留的次数


@dataclass
class CallRecord:
    """单次调用记录"""
    node: str
    tokens: int
    is_essential: bool


class BudgetGuard:
    """
    LLM 调用预算管理器。

    用法：
        guard = BudgetGuard()
        if guard.can_call("query_expansion", essential=False):
            guard.record("query_expansion", tokens=500, essential=False)
            # ... 执行 LLM 调用
        else:
            # 跳过非必要调用
    """

    def __init__(self, config: Optional[BudgetConfig] = None):
        self.config = config or BudgetConfig()
        self._calls: list[CallRecord] = []

    @property
    def total_calls(self) -> int:
        return len(self._calls)

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self._calls)

    @property
    def essential_calls(self) -> int:
        return sum(1 for c in self._calls if c.is_essential)

    def can_call(self, node: str, essential: bool = False) -> bool:
        """检查是否还能发起 LLM 调用"""
        # 必要调用不阻止，只警告
        if essential:
            if self.essential_calls >= self.config.max_llm_calls:
                logger.warning(f"必要调用额度接近上限: {node}")
            return True

        # 非必要调用检查预算
        non_essential_used = self.total_calls - self.essential_calls
        non_essential_budget = (
            self.config.max_llm_calls - self.config.essential_calls_reserved
        )

        if non_essential_used >= non_essential_budget:
            logger.warning(f"非必要调用预算耗尽: {node}, 已用 {non_essential_used}/{non_essential_budget}")
            return False

        if self.total_tokens >= self.config.max_total_tokens:
            logger.warning(f"Token 预算耗尽: {self.total_tokens}/{self.config.max_total_tokens}")
            return False

        return True

    def record(self, node: str, tokens: int, essential: bool = False):
        """记录一次 LLM 调用"""
        self._calls.append(CallRecord(node=node, tokens=tokens, is_essential=essential))
        logger.info(
            f"LLM 调用: {node}, {tokens} tokens, "
            f"总 {self.total_calls}/{self.config.max_llm_calls}"
        )

    def reset(self):
        """重置预算"""
        self._calls.clear()
