"""
查询改写模块：多查询生成 + HyDE（假设文档嵌入）

多查询生成（Multi-Query Generation）：
    从不同角度生成多个查询版本，提高检索召回率。
    原理：用户的表述方式可能和文档的表述方式不同，
    生成多个版本能覆盖更多语义空间。

HyDE（Hypothetical Document Embedding）：
    让 LLM 先生成一个"假想答案"，用假想答案去做向量检索。
    原理：假想答案中会包含专业术语和相关概念，
    语义上比用户的口语化问题更接近真实文档，检索效果更好。
"""

import asyncio
from typing import List

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(service="query_rewriting")


# ==================== 数据模型 ====================


class MultiQueryOutput(BaseModel):
    """多查询生成的结构化输出"""
    queries: List[str] = Field(
        description="从不同角度改写的查询列表，包含3个版本"
    )


class RewrittenQuery(BaseModel):
    """查询改写的最终结果"""
    original_query: str          # 原始问题
    multi_queries: List[str]     # 多查询生成结果（含原始问题，共4个）
    hypothetical_answer: str     # HyDE 假设性答案
    enhanced_query: str          # 增强查询（原始问题 + HyDE 线索）


# ==================== 提示词 ====================

MULTI_QUERY_SYSTEM_PROMPT = """你是一个电商客服系统的查询改写助手。
你的任务是：从不同角度重新表述用户的问题，帮助系统检索到更多相关信息。

规则：
1. 生成3个不同角度的版本
2. 保持原始问题的核心意图不变
3. 第一个版本：用更专业/规范的术语重新表述
4. 第二个版本：从问题的对立面或相关场景角度提问
5. 第三个版本：把问题拆解为更具体的子问题

示例：
原始: "灯泡不亮了"
  版本1: "智能灯泡无法点亮，可能的原因和排查方法"
  版本2: "智能灯泡故障诊断，指示灯状态说明"
  版本3: "智能灯泡不亮的售后维修流程和退换货政策"
"""

HYDE_SYSTEM_PROMPT = """你是一个电商领域的专家客服。
请为用户的咨询问题写一段专业的回答。

注意：
- 你不需要知道确切答案，只需要写一段合理的、专业的回答
- 回答中要包含相关产品的专业术语和关键词
- 这段文字将用于在知识库中检索相关文档
- 回答控制在100-200字之间
"""

# ==================== 核心函数 ====================


async def generate_multi_queries(
    llm: BaseChatModel,
    question: str,
) -> List[str]:
    """
    多查询生成：从不同角度生成多个查询版本

    Args:
        llm: 语言模型实例
        question: 用户的原始问题

    Returns:
        包含原始问题和3个改写版本的列表（共4个）
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", MULTI_QUERY_SYSTEM_PROMPT),
        ("human", "原始问题: {question}\n\n请生成3个不同角度的改写版本。"),
    ])

    chain = prompt | llm.with_structured_output(MultiQueryOutput)
    result = await chain.ainvoke({"question": question})

    # 原始问题放在头部，保证不会被丢掉
    all_queries = [question] + result.queries

    logger.info("多查询生成完成，共 {} 个版本:", len(all_queries))
    for i, q in enumerate(all_queries):
        logger.info("  Query[{}]: {}", i, q)

    return all_queries


async def generate_hypothetical_answer(
    llm: BaseChatModel,
    question: str,
) -> str:
    """
    HyDE：生成假设性答案

    原理说明：
        用户问题（口语化）     知识库文档（专业化）
             "灯不亮了"    vs   "LED智能灯泡故障排查指南：指示灯闪烁..."

        两者的向量在语义空间中距离较远，直接用"灯不亮了"检索可能匹配不到。

        解决方案：让 LLM 先生成一段"假想答案"。
        假想答案中会自然包含专业术语（如"LED驱动""恒流源"等），
        用假想答案做检索，语义上更接近真实文档，召回率更高。

    Args:
        llm: 语言模型实例
        question: 用户的原始问题

    Returns:
        LLM 生成的假设性答案文本
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", HYDE_SYSTEM_PROMPT),
        ("human", "问题: {question}"),
    ])

    chain = prompt | llm
    result = await chain.ainvoke({"question": question})
    hypothetical_answer = result.content

    logger.info("HyDE 假设性答案生成完成 ({}字):", len(hypothetical_answer))
    logger.info("  {}...", hypothetical_answer[:100])

    return hypothetical_answer


async def rewrite_query(
    llm: BaseChatModel,
    question: str,
) -> RewrittenQuery:
    """
    查询改写主入口：并行执行多查询生成 + HyDE

    流程：
        1. 多查询生成（Multi-Query）和 HyDE 并行执行（asyncio.gather）
        2. 合并结果，生成增强查询

    为什么并行？两个任务都只需调用 LLM，互不依赖。
    并行执行只耗时 max(多查询时间, HyDE时间)，而非两者之和。

    Args:
        llm: 语言模型实例
        question: 用户的原始问题

    Returns:
        RewrittenQuery 对象，包含所有改写结果
    """
    logger.info("开始查询改写，原始问题: {}", question)

    # 并行执行，节省一轮 LLM 调用的时间
    multi_queries, hypothetical_answer = await asyncio.gather(
        generate_multi_queries(llm, question),
        generate_hypothetical_answer(llm, question),
    )

    # 增强查询 = 原始问题 + HyDE 线索
    # 这样下游的向量检索能利用到 HyDE 的专业术语
    enhanced_query = f"{question}\n参考线索: {hypothetical_answer}"

    result = RewrittenQuery(
        original_query=question,
        multi_queries=multi_queries,
        hypothetical_answer=hypothetical_answer,
        enhanced_query=enhanced_query,
    )

    logger.info("查询改写完成")
    return result
