"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.state import OverallState
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.summarize.prompts import create_summarization_prompt_template
from app.tools.doc_block_renderer import render_doc_blocks, render_dynamic_rows

generate_summary_prompt = create_summarization_prompt_template()


def _assemble_evidence(searches: list) -> str:
    """跨分支证据装配：chunk_id 去重 + 动态行按 sku 合并 + 统一渲染。

    重复来源：跨商品混合块（docs/项目问题.md #3 实测 66%）会被 A/B 两条子 query
    各召回一次，现状原样拼接导致同一块进 prompt 两次。
    同时消除现状把 records 整体（含 hybrid_docs/dynamic_rows 原始 dict）塞进 prompt
    的冗余——渲染文本已含全部事实，原始 dict 属重复通道。

    注意：只做去重与合并，不做相关性过滤——相关性由精排排序 + LLM 取值判断承担
    （SPEC_PLANNER_ENTITY_SPLIT_AND_RETRIEVAL §4.6 / §8 D10：实测证明系统侧再做一层
    相关性收窄会误杀正确块）。
    """
    seen: set = set()
    docs: list = []
    dynamic_rows: dict = {}
    for s in searches:
        records = s.records if hasattr(s, "records") else (s or {}).get("records")
        if not records:
            continue
        for d in records.get("hybrid_docs") or []:
            key = d.get("chunk_id") or d.get("id")
            # 无键块不参与去重：直接保留。若照原写法 key=None，首个无键块占用 None
            # 后，其余无键块会被整体丢弃（静默丢证据）。现状 chunk_id 恒在
            # （rag_retriever_service.py:60），此护栏防上游结构变动。
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            docs.append(d)
        dynamic_rows.update(records.get("dynamic_rows") or {})

    if not docs and not dynamic_rows:
        return ""
    dynamic_text = render_dynamic_rows(dynamic_rows)
    static_text = render_doc_blocks(docs)
    return f"{dynamic_text}\n\n{static_text}" if dynamic_text else static_text


def create_summarization_node(
    llm: BaseChatModel,
) -> Callable[[OverallState], Coroutine[Any, Any, dict[str, Any]]]:
    """
    Create a Summarization node for a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM do perform processing.

    Returns
    -------
    Callable[[OverallState], OutputState]
        The LangGraph node.
    """

    generate_summary = generate_summary_prompt | llm | StrOutputParser()

    async def summarize(state: OverallState) -> Dict[str, Any]:
        """
        装配跨分支证据（去重 + 合并）后生成总结。
        """
        evidence = _assemble_evidence(state.get("searches", list()))
        if evidence:
            summary = await generate_summary.ainvoke(
                {"question": state.get("question"), "results": evidence}
            )
        else:
            summary = "No data to summarize."

        return {"summary": summary, "steps": ["summarize"]}

    return summarize
