"""
基础安全护栏：迭代限制、Cypher 安全校验、超时保护

① MaxIterationGuard
    防止工具调用死循环。每次工具调用 +1，超过阈值强制终止。
    放在 multi_tool_workflow 的循环边中，每次循环检查。

② CypherSafetyValidator
    在 Text2Cypher 生成 Cypher 后、执行前，检查是否包含危险操作。
    危险关键词：DELETE, DETACH DELETE, DROP, REMOVE, CREATE (节点/关系),
                SET (修改属性), MERGE (可能创建)

③ TimeoutGuard
    用 asyncio.wait_for 包装整个 agent 调用，超时返回降级回答。
    默认超时 30 秒。
"""

import re
import asyncio
from typing import Optional, Any, Awaitable

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
        logger.info(f"迭代计数 {conversation_id}: {count}/{self.max_iterations}")
        return count

    def is_exceeded(self, conversation_id: str) -> bool:
        """是否已超过最大迭代次数"""
        return self._counters.get(conversation_id, 0) >= self.max_iterations

    def reset(self, conversation_id: str):
        """重置计数器"""
        self._counters.pop(conversation_id, None)

    def get_count(self, conversation_id: str) -> int:
        return self._counters.get(conversation_id, 0)


# ===== ② Cypher 安全校验 =====

class CypherSafetyValidator:
    """
    Cypher 查询安全校验器。

    在 LLM 生成的 Cypher 执行前做安全检查，拦截所有写操作。

    拦截规则：
        - DDL: CREATE, DROP, ALTER
        - DML 写操作: DELETE, DETACH DELETE, SET, REMOVE, MERGE
        - CALL（可能调用存储过程）
        - FOREACH（批量写操作）
    """

    DANGEROUS_PATTERNS = [
        (r'\bDELETE\b', "包含 DELETE 操作"),
        (r'\bDETACH\s+DELETE\b', "包含 DETACH DELETE 操作"),
        (r'\bDROP\b', "包含 DROP 操作"),
        (r'\bCREATE\b', "包含 CREATE 操作"),
        (r'\bMERGE\b', "包含 MERGE 操作"),
        (r'\bSET\b', "包含 SET 操作（修改属性）"),
        (r'\bREMOVE\b', "包含 REMOVE 操作"),
        (r'\bCALL\b', "包含 CALL 操作（存储过程）"),
        (r'\bFOREACH\b', "包含 FOREACH 操作（批量写）"),
    ]

    def validate(self, cypher: str) -> tuple[bool, str]:
        """
        校验 Cypher 是否安全。

        Returns:
            (is_safe, reason)
        """
        if not cypher or not cypher.strip():
            return False, "Cypher 为空"

        cypher_upper = cypher.upper()

        for pattern, reason in self.DANGEROUS_PATTERNS:
            if re.search(pattern, cypher_upper):
                logger.warning(f"Cypher 安全拦截: {reason}, 查询: {cypher[:100]}")
                return False, reason

        return True, "安全"

    def sanitize(self, cypher: str) -> str:
        """校验通过返回原查询，不通过返回空字符串"""
        is_safe, reason = self.validate(cypher)
        if is_safe:
            return cypher
        logger.warning(f"Cypher 不安全（{reason}），拒绝执行")
        return ""


# ===== ③ 响应超时 =====

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
                f"请求超时: {conversation_id}, 超过 {self.timeout}s, 返回降级回答"
            )
            return fallback
