from langchain_core.language_models import BaseChatModel
from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph

# 导入输入输出状态定义
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.state import (
    InputState,
    OutputState,
    OverallState,
)
# 导入分解节点
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner import create_planner_node
# 导入向量检索节点
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools import create_vector_search_query_node

from ...components.final_answer import create_final_answer_node
from ...components.summarize import create_summarization_node

from .edges import map_reduce_planner_to_customer_tools

from dataclasses import dataclass, field
# 强制要求数据类中的所有字段必须以关键字参数的形式提供。即不能以位置参数的方式传递。
@dataclass(kw_only=True)
class AgentState(InputState):
    """The router's classification of the user's query."""
    steps: list[str] = field(default_factory=list)
    """Populated by the retriever. This is a list of documents that the agent can reference."""
    question: str = field(default_factory=str) # 这个参数用来与子图进行交互
    answer: str = field(default_factory=str)  # 这个参数用来与子图进行交互


def create_multi_tool_workflow(
    llm: BaseChatModel,
) -> CompiledStateGraph:
    """
    Create a multi tool Agent workflow using LangGraph.
    This workflow allows an agent to select from various tools to complete each identified task.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM to use for processing

    Returns
    -------
    CompiledStateGraph
        The workflow.
    """
    # 1. 针对用户的问题进行任务分解
    planner = create_planner_node(llm=llm)

    # 2. 向量检索节点（每个子任务并发执行一次 pgvector 检索）
    customer_tools = create_vector_search_query_node()

    summarize = create_summarization_node(llm=llm)

    final_answer = create_final_answer_node()

    # 创建状态图
    main_graph_builder = StateGraph(OverallState, input=InputState, output=OutputState)

    main_graph_builder.add_node(planner)
    main_graph_builder.add_node("customer_tools", customer_tools)
    main_graph_builder.add_node(summarize)
    main_graph_builder.add_node(final_answer)

    # 添加边
    main_graph_builder.add_edge(START, "planner")
    main_graph_builder.add_conditional_edges(
        "planner",
        map_reduce_planner_to_customer_tools,  # type: ignore[arg-type, unused-ignore]
        ["customer_tools"],
    )

    main_graph_builder.add_edge("customer_tools", "summarize")
    main_graph_builder.add_edge("summarize", "final_answer")

    main_graph_builder.add_edge("final_answer", END)

    return main_graph_builder.compile()
