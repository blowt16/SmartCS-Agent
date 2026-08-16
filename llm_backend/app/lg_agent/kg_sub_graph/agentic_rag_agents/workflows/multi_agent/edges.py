"""LangGraph edges that are used in multiple workflows."""

from typing import List, Literal

from langgraph.types import Send

from ...components.state import OverallState


def guardrails_conditional_edge(
    state: OverallState,
) -> Literal["planner", "final_answer"]:
    match state.get("next_action"):
        case "final_answer":
            return "final_answer"
        case "end":
            return "final_answer"
        case "planner":
            return "planner"
        case _:
            return "final_answer"


def tool_select_conditional_edge(
    state: OverallState,
) -> Literal["summarize", "final_answer"]:
    match state.get("next_action"):
        case "summarize":
            return "summarize"
        case "final_answer":
            return "final_answer"
        case _:
            return "final_answer"


def map_reduce_planner_to_tool_selection(state: OverallState) -> List[Send]:
    """Map each identified task in the planner stage to a tool_selection node."""
    return [
        Send(
            "tool_selection",
            {
                "question": task.question,
                "parent_task": task.parent_task,
            },
        )
        for task in state.get("tasks", list())
    ]
