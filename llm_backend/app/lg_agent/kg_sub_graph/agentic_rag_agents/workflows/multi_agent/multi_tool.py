from typing import Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_neo4j import Neo4jGraph
from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from pydantic import BaseModel

# 导入输入输出状态定义
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.state import (
    InputState,
    OutputState,
    OverallState,
)
# 导入guardrails逻辑
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.guardrails.node import create_guardrails_node
# 导入分解节点
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner import create_planner_node
# 导入工具选择节点
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.tool_selection import create_tool_selection_node
# 导入预定义Cypher节点
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.predefined_cypher import create_predefined_cypher_node
# 导入向量检索节点
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools import create_vector_search_query_node

from ...components.errors import create_error_tool_selection_node
from ...components.final_answer import create_final_answer_node
from ...components.summarize import create_summarization_node

from .edges import (
    guardrails_conditional_edge,
    map_reduce_planner_to_tool_selection,
)

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
    graph: Neo4jGraph,
    tool_schemas: List[type[BaseModel]],
    predefined_cypher_dict: Dict[str, str],
    scope_description: Optional[str] = None,
    tool_preference: Optional[str] = None,  # 策略偏好，可选值为 "predefined_cypher", "vector_search_query"
) -> CompiledStateGraph:
    """
    Create a multi tool Agent workflow using LangGraph.
    This workflow allows an agent to select from various tools to complete each identified task.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM to use for processing
    graph : Neo4jGraph
        The Neo4j graph wrapper.
    tool_schemas : List[BaseModel]
        A list of Pydantic class defining the available tools.
    predefined_cypher_dict : Dict[str, str]
        A Python dictionary of Cypher query names as keys and Cypher queries as values.
    scope_description: Optional[str], optional
        A short description of the application scope, by default None
    tool_preference : Optional[str], optional
        策略偏好，可选值为 "predefined_cypher", "vector_search_query"
        当有明确偏好时，直接走指定工具，跳过 LLM 选择

    Returns
    -------
    CompiledStateGraph
        The workflow.
    """
    # 1. 创建guardrails节点
    guardrails = create_guardrails_node(
        llm=llm, graph=graph, scope_description=scope_description
    )

    # 2. 如果通过guardrails，则会针对用户的问题进行任务分解
    planner = create_planner_node(llm=llm)

    predefined_cypher = create_predefined_cypher_node(
        graph=graph, predefined_cypher_dict=predefined_cypher_dict
    )

    customer_tools = create_vector_search_query_node()

    # 工具选择节点，根据用户的问题选择合适的工具
    tool_selection = create_tool_selection_node(
        llm=llm,
        tool_schemas=tool_schemas,
        tool_preference=tool_preference,
    )

    summarize = create_summarization_node(llm=llm)

    final_answer = create_final_answer_node()

    # 创建状态图
    main_graph_builder = StateGraph(OverallState, input=InputState, output=OutputState)

    main_graph_builder.add_node(guardrails)
    main_graph_builder.add_node(planner)
    main_graph_builder.add_node(predefined_cypher)
    main_graph_builder.add_node("customer_tools", customer_tools)
    main_graph_builder.add_node(summarize)
    main_graph_builder.add_node(tool_selection)
    main_graph_builder.add_node(final_answer)

    # 添加边
    main_graph_builder.add_edge(START, "guardrails")
    main_graph_builder.add_conditional_edges(
        "guardrails",
        guardrails_conditional_edge,
    )
    main_graph_builder.add_conditional_edges(
        "planner",
        map_reduce_planner_to_tool_selection,  # type: ignore[arg-type, unused-ignore]
        ["tool_selection"],
    )

    main_graph_builder.add_edge("predefined_cypher", "summarize")
    main_graph_builder.add_edge("customer_tools", "summarize")
    main_graph_builder.add_edge("summarize", "final_answer")

    main_graph_builder.add_edge("final_answer", END)

    return main_graph_builder.compile()
