from typing import Any, Callable, Coroutine, Dict, List
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.base import Runnable
from app.core.config import settings
from app.core.logger import get_logger

# 获取日志记录器
logger = get_logger(service="planner_node")

from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.models import Task
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.models import PlannerOutput
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.prompts import create_planner_prompt_template
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.state import InputState


# 定义planner prompt
planner_prompt = create_planner_prompt_template()

# 拆解上限：派生自 settings（.env 可调），与提示词中的上限同源。
# 模块级派生一次，先例见 rrf_fusion.py:22 / memory_cache.py:36；.env 变更需重启生效。
MAX_TASKS = settings.PLANNER_MAX_TASKS


def _fallback(question: str) -> List[Task]:
    """所有回退的唯一出口：单分支整句检索（question 恒为原 query 原文，不经 LLM）。"""
    return [Task(question=question, parent_task=question)]


def create_planner_node(
    llm: BaseChatModel,
) -> Callable[[InputState], Coroutine[Any, Any, Dict[str, Any]]]:
    """
    Create a planner node to be used in a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM used to process data.

    Returns
    -------
    Callable[[InputState], OverallState]
        The LangGraph node.
    """

    # 创建planner chain
    planner_chain: Runnable[Dict[str, Any], Any] = (
        planner_prompt | llm.with_structured_output(PlannerOutput)
    )

    async def planner(state: InputState) -> Dict[str, Any]:
        """
        拆解用户问题为可独立检索的子任务；任何异常/校验失败均回退单分支整句。
        """
        question = state.get("question", "")
        try:
            planner_output = await planner_chain.ainvoke({"question": question})
            entity_count = planner_output.entity_count
        except Exception:
            # L0：调用/结构化输出异常 → 整句。loguru 记录堆栈用 logger.exception
            logger.exception("planner 调用失败，回退单分支整句")
            entity_count, planner_output = -1, None

        # 信任拆解的条件：2<=任务数<=MAX_TASKS、sub_query 均非空、互不重复。
        # 不做 len==entity_count 硬校验——列表型拆解（entity_count=1 拆 2）合法。
        if (
            planner_output is not None
            and 2 <= len(planner_output.tasks) <= MAX_TASKS
            and all(t.sub_query.strip() for t in planner_output.tasks)
            and len({t.sub_query.strip() for t in planner_output.tasks})
            == len(planner_output.tasks)
        ):
            task_list = [
                Task(question=t.sub_query.strip(), parent_task=question)
                for t in planner_output.tasks
            ]
            logger.info(
                "planner_decision: split={} (entity_count={}, names={})",
                len(task_list), entity_count, [t.name.strip() for t in planner_output.tasks],
            )
        else:
            # 单任务场景：LLM 的单任务文本一律丢弃（防改写，用原 query 原文）。
            # name 仅作观测（§7.1 空名率），不流入检索侧——实体约束已于 2026-09-19 取消（§8 D10）。
            single_name = ""
            if planner_output is not None and len(planner_output.tasks) == 1:
                single_name = planner_output.tasks[0].name.strip()
            task_list = _fallback(question)
            reason = "llm_error" if planner_output is None else f"tasks={len(planner_output.tasks)}"
            logger.info("planner_decision: fallback (reason={}, name='{}')", reason, single_name)

        logger.info("Total Sub Task: {}", len(task_list))
        for i, task in enumerate(task_list):
            logger.info("Sub Task[{}]: {}", i + 1, task.question)
        return {"tasks": task_list}

    return planner
