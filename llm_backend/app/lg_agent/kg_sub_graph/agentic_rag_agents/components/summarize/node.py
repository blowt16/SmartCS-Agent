"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.state import OverallState
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.summarize.prompts import create_summarization_prompt_template

generate_summary_prompt = create_summarization_prompt_template()


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
        Summarize results of the performed vector searches.
        """
        results = []

        # 使用直接属性访问而不是get方法
        for search in state.get("searches", list()):
            # 检查是否是字典类型，使用get方法
            if isinstance(search, dict) and search.get("records") is not None:
                results.append(search.get("records"))
            # 检查是否是Pydantic模型，使用直接属性访问
            elif hasattr(search, "records") and search.records is not None:
                results.append(search.records)
                
        if results:
            summary = await generate_summary.ainvoke(
                {
                    "question": state.get("question"),
                    "results": results,
                }
            )

        else:
            summary = "No data to summarize."

        return {"summary": summary, "steps": ["summarize"]}

    return summarize
