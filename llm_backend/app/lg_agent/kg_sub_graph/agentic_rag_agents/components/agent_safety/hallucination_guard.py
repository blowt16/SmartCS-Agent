"""
⑤ 幻觉检测闭环

为什么需要？
    LLM 可能编造不存在的产品信息。更危险的是幻觉传播：
    第一轮编造了"X1 Pro售价1999元"，第二轮基于幻觉继续编造。

做法：
    1. 将回答拆分为独立声明（claims）
    2. 逐条检查是否有检索文档支撑
    3. 无支撑的标记为幻觉
    4. 生成修正后的回答
    5. 幻觉率超过阈值时返回降级回答
"""

from typing import List
from pydantic import BaseModel, Field
from langchain_core.language_models import BaseChatModel

from app.core.logger import get_logger

logger = get_logger(service="hallucination_guard")


class ClaimCheck(BaseModel):
    """单个声明的幻觉检查结果"""
    claim: str = Field(description="回答中的一个独立声明")
    is_supported: bool = Field(description="是否有检索文档支撑")
    evidence: str = Field(default="", description="支撑依据")


class HallucinationCheckResult(BaseModel):
    """幻觉检查结果"""
    claims: List[ClaimCheck] = Field(description="各声明的检查结果")
    hallucination_rate: float = Field(description="幻觉率（0-1）")
    corrected_answer: str = Field(description="移除幻觉后的修正回答")


HALLUCINATION_CHECK_PROMPT = """你是一个事实核查专家。
检查 AI 回答中的每个声明是否有检索文档支撑。

步骤：
1. 将回答拆分为独立声明
2. 逐条检查是否有支撑
3. 生成修正后的回答（保留有支撑的，移除无支撑的）

检索文档：
{context}

AI 的回答：
{answer}

请逐条检查并给出修正后的回答。"""


class HallucinationGuard:
    """
    幻觉检测与修正。

    用法：
        guard = HallucinationGuard(llm=judge_llm, threshold=0.5)
        result = await guard.check(answer, context)
        safe_answer = guard.get_safe_answer(result)
    """

    def __init__(self, llm: BaseChatModel, threshold: float = 0.5):
        self.llm = llm
        self.threshold = threshold

    async def check(self, answer: str, context: str) -> HallucinationCheckResult:
        """检查回答是否存在幻觉"""
        if not answer or not context:
            return HallucinationCheckResult(
                claims=[], hallucination_rate=0.0, corrected_answer=answer,
            )

        prompt = HALLUCINATION_CHECK_PROMPT.format(context=context[:3000], answer=answer)
        chain = self.llm.with_structured_output(HallucinationCheckResult)
        result = await chain.ainvoke(prompt)

        supported = sum(1 for c in result.claims if c.is_supported)
        total = len(result.claims) if result.claims else 1
        logger.info("幻觉检测: {} 声明, {} 有支撑, 幻觉率 {:.1%}", total, supported, result.hallucination_rate)

        return result

    def get_safe_answer(
        self,
        result: HallucinationCheckResult,
        fallback: str = "抱歉，根据目前的信息无法确定这个问题的准确答案。",
    ) -> str:
        """获取安全回答：幻觉率低于阈值返回修正回答，超过则降级"""
        if not result.claims:
            return fallback

        if result.hallucination_rate >= self.threshold:
            logger.warning("幻觉率 {:.1%} 超过阈值，使用降级回答", result.hallucination_rate)
            return fallback

        return result.corrected_answer or fallback
