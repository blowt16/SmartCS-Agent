"""
查询纠错与查询扩展模块

查询纠错（Query Correction）：
    修正用户输入中的错别字、口语化表述。
    例如："扫第机器人" → "扫地机器人"
    使用 LLM 语义纠错，而非传统拼写检查库。
    原因：LLM 能结合上下文判断正确用词（如"智通"在语境中应为"智能"）。

查询扩展（Query Expansion）：
    补充同义词、上下位词，扩大检索的语义覆盖范围。
    例如："灯泡" → "灯泡 LED灯 照明灯具 智能照明"
    原因：知识库文档可能用专业术语（"LED灯"）而非口语（"灯泡"），
    扩展后能匹配到更多相关文档，提高召回率。

位置：
    在预处理管道中，位于"实体识别"之前。
    因为：
    - 纠错必须在实体识别之前（错别字会导致实体匹配失败）
    - 扩展必须在实体识别之前（扩展后的文本提供更多匹配线索）
    （指代消解已前置到系统入口，本模块不再处理）
"""

from typing import List

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(service="query_correction")


# ==================== 数据模型 ====================


class CorrectionResult(BaseModel):
    """查询纠错的结构化输出"""
    corrected_query: str = Field(description="纠错后的查询文本")
    has_correction: bool = Field(description="是否进行了纠错")
    corrections: List[str] = Field(
        default_factory=list,
        description="具体的纠错项，如 ['扫第->扫地', '智通->智能']"
    )


class ExpansionResult(BaseModel):
    """查询扩展的结构化输出"""
    original_query: str = Field(description="原始查询")
    expanded_terms: List[str] = Field(
        default_factory=list,
        description="扩展的同义词/相关词列表"
    )
    expanded_query: str = Field(
        description="扩展后的完整查询（原始查询 + 扩展词）"
    )


# ==================== 提示词 ====================


CORRECTION_SYSTEM_PROMPT = """你是一个电商领域的文本纠错专家。
修正用户输入中的错别字和口语化表述。

规则：
1. 只修正明确的错误（错别字、乱码、明显笔误）
2. 不要改变原始意图，不要添加新信息
3. 如果没有错误，原样返回
4. 电商领域常见纠错：智通→智能、扫第→扫地、摄象头→摄像头
"""


EXPANSION_SYSTEM_PROMPT = """你是一个电商领域的查询扩展专家。
为用户的查询补充同义词和相关术语，帮助检索到更多相关文档。

规则：
1. 补充2-4个同义词或相关术语
2. 覆盖：口语 ↔ 书面语、简称 ↔ 全称、俗名 ↔ 专业名
3. 只补充相关词汇，不要添加无关的热门词
4. 如果查询已经很精确（如具体产品型号），不需要过度扩展

常见扩展：
- 灯泡 → LED灯、照明灯具
- 扫地机器人 → 扫地机、清洁机器人
- 摄像头 → 监控摄像头、安防摄像头
"""


# ==================== 核心函数 ====================


async def correct_query(
    llm: BaseChatModel,
    question: str,
) -> str:
    """
    查询纠错：修正用户输入中的错别字。

    为什么用 LLM 而非传统拼写检查？
        传统拼写检查（如 pyspellchecker）基于字典匹配，
        无法结合语义判断。例如"智通灯泡"中，"智通"不是一个词，
        拼写检查可能认为它是对的（因为它是个合法的字符串）。
        而 LLM 能结合上下文推断"智通"在"灯泡"前面应该是"智能"。

    Args:
        llm: 语言模型实例
        question: 用户输入的问题

    Returns:
        纠错后的文本（如果没有错误则原样返回）
    """
    if not question.strip():
        return question

    prompt = ChatPromptTemplate.from_messages([
        ("system", CORRECTION_SYSTEM_PROMPT),
        ("human", "请检查并修正以下查询中的错误:\n{question}"),
    ])

    chain = prompt | llm.with_structured_output(CorrectionResult)
    result = await chain.ainvoke({"question": question})

    if result.has_correction and result.corrections:
        logger.info("查询纠错: '{}' -> '{}'", question, result.corrected_query)
        for c in result.corrections:
            logger.info("  修正: {}", c)
    else:
        logger.info("查询无需纠错: '{}'", question)

    return result.corrected_query


async def expand_query(
    llm: BaseChatModel,
    question: str,
) -> str:
    """
    查询扩展：补充同义词和相关术语。

    为什么需要查询扩展？
        知识库文档中可能使用不同的表述方式：
        - 用户说"灯泡"，文档写"LED智能照明灯"
        - 用户说"扫地机器人"，文档写"智能清洁机器人"
        扩展同义词后，下游检索能匹配到更多文档。

    Args:
        llm: 语言模型实例
        question: 用户的问题

    Returns:
        扩展后的查询文本（原始查询 + 同义词）
    """
    if not question.strip():
        return question

    prompt = ChatPromptTemplate.from_messages([
        ("system", EXPANSION_SYSTEM_PROMPT),
        ("human", "请为以下查询扩展同义词和相关术语:\n{question}"),
    ])

    chain = prompt | llm.with_structured_output(ExpansionResult)
    result = await chain.ainvoke({"question": question})

    if result.expanded_terms:
        logger.info("查询扩展: '{}'", question)
        logger.info("  扩展词: {}", ", ".join(result.expanded_terms))
        logger.info("  扩展后: '{}'", result.expanded_query)
    else:
        logger.info("查询无需扩展: '{}'", question)
        return question

    return result.expanded_query


async def correct_and_expand(
    llm: BaseChatModel,
    question: str,
) -> str:
    """
    查询纠错 + 扩展的主入口：先纠错，再扩展。

    顺序原因：
        纠错必须先于扩展——如果"扫第机器人"不先纠错为"扫地机器人"，
        扩展时会基于错误文本生成同义词，导致检索偏差。

    Args:
        llm: 语言模型实例
        question: 用户问题

    Returns:
        纠错 + 扩展后的查询文本
    """
    # 第一步：纠错
    corrected = await correct_query(llm, question)

    # 第二步：扩展
    expanded = await expand_query(llm, corrected)

    return expanded
