"""
基础安全护栏：超时保护

TimeoutGuard
    用 asyncio.wait_for 包装整个 agent 调用，超时返回降级回答。
    默认超时 30 秒。
"""

import asyncio
from typing import Any, Awaitable

from app.core.logger import get_logger

logger = get_logger(service="safety_guards")


# ===== 响应超时 =====

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
