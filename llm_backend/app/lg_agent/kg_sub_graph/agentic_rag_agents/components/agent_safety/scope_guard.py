"""
③ 全局经营范围护栏

为什么需要？
    原有的 guardrails 只在 multi_tool_workflow（graphrag-query 路径）中生效，
    respond_to_general_query 和 get_additional_info 等路径没有统一检查。

做法：
    用关键词 + 正则做快速预检（不需要 LLM，零延迟），
    匹配到明显超范围的词直接返回拒绝。
    不确定的交由后续 LLM guardrails 节点判断。
"""

import re
from typing import Optional

from app.core.logger import get_logger

logger = get_logger(service="scope_guard")

# 明确超范围关键词（命中直接拒绝）
OUT_OF_SCOPE_KEYWORDS = [
    "服装", "衣服", "裤子", "裙子", "鞋", "运动鞋",
    "化妆品", "口红", "护肤品", "香水",
    "食品", "零食", "饮料", "外卖",
    "医药", "药品", "处方",
    "汽车", "买房", "租房", "旅游", "机票",
    "股票", "基金", "理财", "贷款",
    "赌博", "彩票",
]

# 超范围正则模式
OUT_OF_SCOPE_PATTERNS = [
    r"(买|推荐).*(衣服|裤子|鞋子|口红|化妆品)",
    r"(有|卖).*(车|房|股票|基金)",
]


class ScopeGuard:
    """
    经营范围预检护栏。

    三级判断：
        - 明确超范围 → 拒绝
        - 不确定 → 放行（交给下游 LLM guardrails 精确判断）
    """

    def __init__(
        self,
        out_of_scope: Optional[list[str]] = None,
    ):
        self.out_of_scope = out_of_scope or OUT_OF_SCOPE_KEYWORDS
        self.out_patterns = [re.compile(p) for p in OUT_OF_SCOPE_PATTERNS]

    def check(self, query: str) -> tuple[bool, str]:
        """
        检查查询是否在经营范围内。

        Returns:
            (is_in_scope, reason)
        """
        if not query:
            return True, "空查询"

        query_lower = query.lower()

        # 1. 检查明确超范围关键词
        for keyword in self.out_of_scope:
            if keyword in query_lower:
                logger.info("经营范围拦截: 命中 '{}'", keyword)
                return False, f"超出经营范围（{keyword}）"

        # 2. 检查超范围正则
        for pattern in self.out_patterns:
            if pattern.search(query_lower):
                logger.info("经营范围拦截: 命中超范围模式")
                return False, "超出经营范围"

        # 3. 不确定 → 放行
        return True, "通过预检"
