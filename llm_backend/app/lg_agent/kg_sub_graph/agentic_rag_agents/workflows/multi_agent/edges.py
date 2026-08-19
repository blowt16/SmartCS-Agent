"""LangGraph edges that are used in multiple workflows."""

from typing import List

from langgraph.types import Send

from ...components.state import OverallState


def map_reduce_planner_to_customer_tools(state: OverallState) -> List[Send]:
    """Map each identified task in the planner stage to a customer_tools (向量检索) node."""
    return [
        Send(
            "customer_tools",
            {
                "task": task.question,
                "question": task.question,
                "parent_task": task.parent_task,
            },
        )
        for task in state.get("tasks", list())
    ]
