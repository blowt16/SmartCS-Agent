"""planner 节点校验与回退测试（SPEC_PLANNER_ENTITY_SPLIT_AND_RETRIEVAL §7.1）。

用 Stub LLM 直接控制 with_structured_output 的返回，覆盖：
  拆解通过（多实体 / 列表+详情）/ 单任务回退（丢弃 LLM 文本）/ 异常回退
  / 超限回退 / 空串回退 / 重复回退
"""
import pytest
from langchain_core.runnables import RunnableLambda

from app.core.config import settings
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.models import (
    EntitySubQuery,
    PlannerOutput,
)
from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.node import (
    MAX_TASKS,
    create_planner_node,
)


class _StubLLM:
    """最小 LLM 桩：with_structured_output 返回 Runnable，ainvoke 回放预设结果。"""

    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error

    def with_structured_output(self, _schema):
        async def _ainvoke(_messages):
            if self._error is not None:
                raise self._error
            return self._result
        return RunnableLambda(_ainvoke)


def _out(**kw) -> PlannerOutput:
    return PlannerOutput(**kw)


async def _run(result=None, error=None, question="测试问题"):
    node = create_planner_node(llm=_StubLLM(result, error))
    return await node({"question": question})


# ---------- 拆解通过 ----------

async def test_split_multi_entity():
    """多实体：2 条，各含实体全名与价格意图。"""
    out = _out(entity_count=2, tasks=[
        EntitySubQuery(name="米家智能晾衣机2",
                       sub_query="米家智能晾衣机2的功能实用性、优缺点和售价是多少？"),
        EntitySubQuery(name="米家智能晾衣机Pro",
                       sub_query="米家智能晾衣机Pro的功能实用性、优缺点和售价是多少？"),
    ])
    q = "米家智能晾衣机2和米家智能晾衣机Pro哪个更实用？它们分别多少钱？"
    tasks = (await _run(out, question=q))["tasks"]
    assert len(tasks) == 2
    assert tasks[0].question == "米家智能晾衣机2的功能实用性、优缺点和售价是多少？"
    assert all("它们" not in t.question for t in tasks)       # 无指代
    assert all(t.parent_task == q for t in tasks)             # parent_task 由节点注入


async def test_split_list_plus_detail():
    """列表+详情并列：任务数可大于 entity_count（不做 len==entity_count 校验）。"""
    out = _out(entity_count=1, tasks=[
        EntitySubQuery(name="米家智能晾衣机", sub_query="米家智能晾衣机有哪些型号？"),
        EntitySubQuery(name="米家智能晾衣机", sub_query="米家智能晾衣机各型号的售价分别是多少？"),
    ])
    tasks = (await _run(out))["tasks"]
    assert len(tasks) == 2


# ---------- 回退：单任务 ----------

@pytest.mark.parametrize("question", [
    "小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？",
    "有没有适合小户型的智能电动沙发？",
])
async def test_fallback_single_task_uses_original_text(question):
    """LLM 返回单任务：文本丢弃，任务 = 原 query 原文（防改写）。"""
    llm_text = "小米智能门锁M30能否人脸识别、续航与远程告警？"   # 与原文不同
    out = _out(entity_count=1, tasks=[
        EntitySubQuery(name="小米智能门锁M30", sub_query=llm_text)])
    tasks = (await _run(out, question=question))["tasks"]
    assert len(tasks) == 1
    assert tasks[0].question == question          # 原 query 原文，非 LLM 改写
    assert tasks[0].parent_task == question


async def test_fallback_on_llm_error():
    """LLM 抛异常：不向上抛，回退单分支整句。"""
    q = "米家智能晾衣机2的承重是多少？"
    tasks = (await _run(error=RuntimeError("boom"), question=q))["tasks"]
    assert len(tasks) == 1
    assert tasks[0].question == q


# ---------- 回退：校验失败 ----------

async def test_fallback_when_exceeds_max_tasks():
    """任务数 > MAX_TASKS → 整条回退为单分支（不截断到 MAX_TASKS）。"""
    out = _out(entity_count=4, tasks=[
        EntitySubQuery(name=f"商品{i}", sub_query=f"商品{i}的价格是多少？")
        for i in range(MAX_TASKS + 1)
    ])
    tasks = (await _run(out, question="原问题") )["tasks"]
    assert len(tasks) == 1
    assert tasks[0].question == "原问题"


async def test_fallback_when_exceeds_configured_max_tasks(monkeypatch):
    """MAX_TASKS 派生自 settings.PLANNER_MAX_TASKS —— 改配置即改行为（同源验证）。"""
    import app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.node as node_mod
    monkeypatch.setattr(node_mod, "MAX_TASKS", 2)
    out = _out(entity_count=3, tasks=[
        EntitySubQuery(name=f"商品{i}", sub_query=f"商品{i}的价格是多少？")
        for i in range(3)
    ])
    tasks = (await _run(out, question="原问题"))["tasks"]
    assert len(tasks) == 1, "MAX_TASKS=2 时 3 条任务必须回退"


async def test_fallback_when_blank_sub_query():
    out = _out(entity_count=2, tasks=[
        EntitySubQuery(name="A", sub_query="A 的价格是多少？"),
        EntitySubQuery(name="B", sub_query="   "),
    ])
    tasks = (await _run(out, question="原问题"))["tasks"]
    assert len(tasks) == 1


async def test_fallback_when_duplicate_sub_query():
    out = _out(entity_count=1, tasks=[
        EntitySubQuery(name="A", sub_query="A 有哪些型号？"),
        EntitySubQuery(name="A", sub_query="  A 有哪些型号？  "),   # strip 后重复
    ])
    tasks = (await _run(out, question="原问题"))["tasks"]
    assert len(tasks) == 1


async def test_fallback_when_empty_task_list():
    tasks = (await _run(_out(entity_count=0, tasks=[]), question="原问题"))["tasks"]
    assert len(tasks) == 1
    assert tasks[0].question == "原问题"


# ---------- 配置同源 ----------

def test_max_tasks_derives_from_settings():
    """拆解上限必须来自 settings（.env 可调），不得硬编码。"""
    assert MAX_TASKS == settings.PLANNER_MAX_TASKS


def test_prompt_carries_configured_max_tasks():
    """提示词中的上限与 MAX_TASKS 同源（改 .env 后提示词跟随）。"""
    from app.lg_agent.kg_sub_graph.prompts.kg_prompts import PLANNER_SYSTEM_PROMPT
    assert f"总数不超过 {settings.PLANNER_MAX_TASKS}" in PLANNER_SYSTEM_PROMPT
    assert "<<MAX_TASKS>>" not in PLANNER_SYSTEM_PROMPT   # 占位符必须已被替换
