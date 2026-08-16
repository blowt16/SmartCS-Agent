"""
A tool_selection node must
* take a single task at a time
* retrieve a list of available tools
    * custom pre-written cypher executors
        * these can be numerous and may be retrieved in the same fashion as CypherQuery node contents
    * unstructured text search (sim search)
* decide the appropriate tool for the task
* generate and validate parameters for the selected tool
* send the validated parameters to the appropriate tool node
"""

from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional, Set
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import PydanticToolsParser
from langchain_core.runnables.base import Runnable
from langgraph.types import Command, Send
from pydantic import BaseModel

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.state import ToolSelectionInputState
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.tool_selection.prompts import create_tool_selection_prompt_template

# 定义工具选择提示词
tool_selection_prompt = create_tool_selection_prompt_template()


def create_tool_selection_node(
    llm: BaseChatModel,
    tool_schemas: List[type[BaseModel]],
    tool_preference: Optional[str] = None,  # 策略偏好
) -> Callable[[ToolSelectionInputState], Coroutine[Any, Any, Command[Any]]]:
    """
    Create a tool_selection node to be used in a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM used to process data.
    tool_schemas : Sequence[Union[Dict[str, Any], type, Callable, BaseTool]
        tools schemas that inform the LLM which tools are available.
    tool_preference : Optional[str], optional
        策略偏好，可选值为 "predefined_cypher", "vector_search_query"
        当有明确偏好时，直接走指定工具，跳过 LLM 选择，节省时间

    Returns
    -------
    Callable[[ToolSelectionInputState], ToolSelectionOutputState]
        The LangGraph node.
    """

    # 构建工具选择链，由大模型根据传递过来的 Task，在预定义的工具列表中选择一个工具。
    tool_selection_chain: Runnable[Dict[str, Any], Any] = (
        tool_selection_prompt
        | llm.bind_tools(tools=tool_schemas)
        | PydanticToolsParser(tools=tool_schemas, first_tool_only=True)
    )

    async def tool_selection(
        state: ToolSelectionInputState,
    ) -> Command[Literal["predefined_cypher", "customer_tools"]]:
        """
        Choose the appropriate tool for the given task.
        """
        # 如果有明确的策略偏好，直接走指定工具，跳过 LLM 选择
        if tool_preference == "predefined_cypher":
            return Command(
                goto=Send(
                    "predefined_cypher",
                    {
                        "task": state.get("question", ""),
                        "query_name": "predefined_cypher",
                        "query_parameters": {},
                        "steps": ["tool_selection"],
                    },
                )
            )
        elif tool_preference == "vector_search_query":
            return Command(
                goto=Send(
                    "customer_tools",
                    {
                        "task": state.get("question", ""),
                        "query_name": "vector_search_query",
                        "query_parameters": {},
                        "steps": ["tool_selection"],
                    },
                )
            )

        # 无偏好时，使用 LLM 选择工具
        tool_selection_output: BaseModel = await tool_selection_chain.ainvoke(
            {"question": state.get("question", "")}
        )

        # 根据路由到对应的工具节点
        if tool_selection_output is not None:
            tool_name: str = tool_selection_output.model_json_schema().get("title", "")
            tool_args: Dict[str, Any] = tool_selection_output.model_dump()
            if tool_name == "predefined_cypher":
                return Command(
                    goto=Send(
                        "predefined_cypher",
                        {
                            "task": state.get("question", ""),
                            "query_name": tool_name,
                            "query_parameters": tool_args,
                            "steps": ["tool_selection"],
                        },
                    )
                )
            else:
                return Command(
                    goto=Send(
                        "customer_tools",
                        {
                            "task": state.get("question", ""),
                            "query_name": tool_name,
                            "query_parameters": tool_args,
                            "steps": ["tool_selection"],
                        },
                    )
                )

        # handle instance where no tool is chosen
        else:
            return Command(
                goto=Send(
                    "error_tool_selection",
                    {
                        "task": state.get("question", ""),
                        "errors": [
                            f"Unable to assign tool to question: `{state.get('question', '')}`"
                        ],
                        "steps": ["tool_selection"],
                    },
                )
            )

    return tool_selection
