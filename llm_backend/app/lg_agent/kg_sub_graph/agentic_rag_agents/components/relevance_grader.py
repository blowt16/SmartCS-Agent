"""
相关性评分模块（Relevance Grading）

解决什么问题：
    检索返回的结果不一定都和用户问题相关。如果直接把不相关的结果
    喂给 LLM 生成回答，会产生幻觉或偏题的回答。

    相关性评分在检索之后、回答生成之前插入，用 LLM 对每条检索结果
    做二值评分（relevant / irrelevant），只有通过评分的结果才会被
    传递给下游。

    如果相关结果数量 < 阈值，会触发重新检索（切换 GraphRAG 搜索策略）。

工作流程：
    检索结果 → LLM 逐条评分 → 过滤掉 irrelevant
      → 相关结果数 >= 阈值 → 返回
      → 相关结果数 < 阈值  → 切换策略重检索（最多 1 次）

参考：
    SuperMew 项目使用了类似的相关性门控机制，
    LangGraph 官方文档的 "Corrective RAG" 模式也是这个思路。
"""

from typing import List, Dict, Any

from pydantic import BaseModel, Field

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="relevance_grader")


# ==================== 数据模型 ====================


class RelevanceGrade(BaseModel):
    """单条检索结果的相关性评分"""
    document_index: int = Field(description="文档在列表中的索引")
    relevance: str = Field(description="relevant 或 irrelevant")
    reasoning: str = Field(description="判断理由（一句话）")


class RelevanceGradeOutput(BaseModel):
    """LLM 对所有文档的评分结果"""
    grades: List[RelevanceGrade] = Field(description="每条文档的相关性评分")


# ==================== 提示词 ====================


RELEVANCE_GRADING_PROMPT = """你是一个检索结果相关性评分器。

给定用户问题和一组检索到的文档片段，判断每个片段是否与问题相关。

评分标准：
- relevant：片段包含能直接或间接回答用户问题的信息
- irrelevant：片段与用户问题无关，或仅包含边缘信息

规则：
1. 严格评分，宁缺毋滥
2. 只要有实质性的相关信息就算 relevant
3. 如果片段只是提到了相同的实体但内容无关，算 irrelevant
"""


# ==================== 核心函数 ====================


async def grade_relevance(
    llm: BaseChatModel,
    query: str,
    documents: List[Dict[str, Any]],
    content_key: str = "text",
) -> List[Dict[str, Any]]:
    """
    对检索结果进行相关性评分，过滤掉不相关的文档。

    流程：
        1. 把 query 和每个文档片段拼接，让 LLM 逐条评分
        2. 只保留 relevant 的文档
        3. 如果 relevant 数量 < 阈值，记录日志（由调用方决定是否重检索）

    Args:
        llm: 语言模型实例
        query: 用户查询
        documents: 检索结果列表
        content_key: 文档中用于评分的文本字段名

    Returns:
        过滤后的相关文档列表（带 relevance_score 字段）
    """
    if not documents:
        logger.info("无检索结果需要评分")
        return []

    if not settings.RELEVANCE_GRADING_ENABLED:
        logger.info("相关性评分已禁用，跳过评分")
        return documents

    logger.info(f"开始相关性评分: query='{query[:50]}...', 共 {len(documents)} 条结果")

    # 构造文档摘要（截断太长的内容，避免 prompt 超长）
    doc_summaries = []
    for i, doc in enumerate(documents):
        text = doc.get(content_key, str(doc))[:300] if isinstance(doc, dict) else str(doc)[:300]
        doc_summaries.append(f"[{i}] {text}")

    docs_text = "\n\n".join(doc_summaries)

    prompt = ChatPromptTemplate.from_messages([
        ("system", RELEVANCE_GRADING_PROMPT),
        ("human", "用户问题: {query}\n\n检索结果:\n{documents}\n\n请逐条评分。"),
    ])

    chain = prompt | llm.with_structured_output(RelevanceGradeOutput)

    try:
        result = await chain.ainvoke({
            "query": query,
            "documents": docs_text,
        })
    except Exception as e:
        logger.warning(f"相关性评分失败，返回全部结果: {e}")
        return documents

    # 构建评分索引
    grade_map = {}
    for grade in result.grades:
        grade_map[grade.document_index] = grade.relevance

    # 过滤：只保留 relevant 的文档
    relevant_docs = []
    for i, doc in enumerate(documents):
        relevance = grade_map.get(i, "irrelevant")
        if relevance == "relevant":
            scored_doc = dict(doc) if isinstance(doc, dict) else {"text": doc}
            scored_doc["relevance_score"] = "relevant"
            relevant_docs.append(scored_doc)
        else:
            logger.debug(f"  文档[{i}] 评为 irrelevant，已过滤")

    logger.info(
        f"相关性评分完成: {len(documents)} 条 -> {len(relevant_docs)} 条 relevant"
    )

    return relevant_docs


async def grade_and_ensure_min_results(
    llm: BaseChatModel,
    query: str,
    documents: List[Dict[str, Any]],
    graphrag_api=None,
    content_key: str = "text",
) -> List[Dict[str, Any]]:
    """
    相关性评分 + 自动重检索（如果相关结果不足）。

    这是本模块的主入口函数。

    流程：
        1. 对检索结果做相关性评分
        2. 如果 relevant 结果 >= 阈值 -> 直接返回
        3. 如果 relevant 结果 < 阈值 -> 切换 GraphRAG 策略重检索
        4. 对重检索结果再次评分
        5. 合并两次的 relevant 结果返回

    Args:
        llm: 语言模型实例
        query: 用户查询
        documents: 初始检索结果
        graphrag_api: GraphRAGAPI 实例（用于重检索）
        content_key: 文档文本字段名

    Returns:
        过滤后的相关文档列表
    """
    # 第一步：评分
    relevant_docs = await grade_relevance(llm, query, documents, content_key)

    threshold = settings.RELEVANCE_THRESHOLD

    # 第二步：检查是否需要重检索
    if len(relevant_docs) >= threshold:
        logger.info(f"相关结果充足 ({len(relevant_docs)} >= {threshold})，无需重检索")
        return relevant_docs

    logger.info(
        f"相关结果不足 ({len(relevant_docs)} < {threshold})，尝试切换策略重检索"
    )

    # 第三步：重检索
    if graphrag_api is None:
        logger.warning("未提供 GraphRAGAPI 实例，无法重检索")
        return relevant_docs

    retry_docs = await _retry_with_different_strategy(graphrag_api, query)

    if not retry_docs:
        logger.info("重检索未返回新结果")
        return relevant_docs

    # 第四步：对重检索结果评分
    retry_relevant = await grade_relevance(llm, query, retry_docs, content_key)

    # 第五步：合并（去重）
    combined = _merge_and_deduplicate(relevant_docs, retry_relevant, content_key)

    logger.info(
        f"重检索后合并结果: 原始 {len(relevant_docs)} + 重检索 {len(retry_relevant)} "
        f"-> 合并后 {len(combined)} 条"
    )

    return combined


async def _retry_with_different_strategy(
    graphrag_api,
    query: str,
) -> List[Dict[str, Any]]:
    """
    切换 GraphRAG 搜索策略进行重检索。

    策略切换规则：
        local  -> drift（local 只查局部，drift 能沿图谱关系扩展）
        global -> local（global 太宏观，local 更精确）
        drift  -> local（drift 没找到，退回 local）
        basic  -> local（basic 最简单，升级到 local）
    """
    current_type = graphrag_api.query_type
    strategy_map = {
        "local": "drift",
        "global": "local",
        "drift": "local",
        "basic": "local",
    }
    new_type = strategy_map.get(current_type, "local")

    logger.info(f"重检索策略切换: {current_type} -> {new_type}")

    # 临时修改查询类型
    original_type = graphrag_api.query_type
    graphrag_api.query_type = new_type

    try:
        result = await graphrag_api.query_graphrag(query)
        response_text = result.get("response", "")

        if response_text:
            return [{"text": response_text, "source": f"graphrag_{new_type}_retry"}]
        return []
    except Exception as e:
        logger.warning(f"重检索失败: {e}")
        return []
    finally:
        # 恢复原始查询类型
        graphrag_api.query_type = original_type


def _merge_and_deduplicate(
    docs_a: List[Dict[str, Any]],
    docs_b: List[Dict[str, Any]],
    text_key: str = "text",
) -> List[Dict[str, Any]]:
    """合并两组文档并按文本内容去重"""
    seen = set()
    merged = []

    for doc in docs_a + docs_b:
        text = doc.get(text_key, "")[:100] if isinstance(doc, dict) else str(doc)[:100]
        if text not in seen:
            seen.add(text)
            merged.append(doc)

    return merged
