"""
基础安全护栏：迭代限制、超时保护

① MaxIterationGuard
    防止工具调用死循环。每次工具调用 +1，超过阈值强制终止。
    放在 multi_tool_workflow 的循环边中，每次循环检查。

② TimeoutGuard
    用 asyncio.wait_for 包装整个 agent 调用，超时返回降级回答。
    默认超时 30 秒。
"""

import asyncio
from typing import Any, Awaitable

from app.core.logger import get_logger

logger = get_logger(service="safety_guards")


# ===== ① 最大迭代次数 =====

class MaxIterationGuard:
    """
    工具调用迭代计数器。

    用法：
        guard = MaxIterationGuard(max_iterations=5)
        if guard.is_exceeded("conv_123"):
            return {"error": "已达到最大调用次数"}
        guard.increment("conv_123")
    """

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations
        self._counters: dict[str, int] = {}

    def increment(self, conversation_id: str) -> int:
        """递增计数器，返回当前值"""
        self._counters[conversation_id] = self._counters.get(conversation_id, 0) + 1
        count = self._counters[conversation_id]
        logger.info("迭代计数 {}: {}/{}", conversation_id, count, self.max_iterations)
        return count

    def is_exceeded(self, conversation_id: str) -> bool:
        """是否已超过最大迭代次数"""
        return self._counters.get(conversation_id, 0) >= self.max_iterations

    def reset(self, conversation_id: str):
        """重置计数器"""
        self._counters.pop(conversation_id, None)

    def get_count(self, conversation_id: str) -> int:
        return self._counters.get(conversation_id, 0)


# ===== ② 响应超时 =====

class TimeoutGuard:
    """
    异步调用超时保护。

    用法：
        timeout = TimeoutGuard(timeout_seconds=30)
        result = await timeout.wrap(
            agent.ainvoke(input),
            fallback="请求超时，请稍后再试"
        )
    """

    def __init__(self, timeout_seconds: int = 30):
        self.timeout = timeout_seconds

    async def wrap(
        self,
        coro: Awaitable[Any],
        fallback: Any = None,
        conversation_id: str = "",
    ) -> Any:
        """包装异步调用，超时返回降级结果"""
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.error(
                "请求超时: {}, 超过 {}s, 返回降级回答",
                conversation_id, self.timeout,
            )
            return fallback
