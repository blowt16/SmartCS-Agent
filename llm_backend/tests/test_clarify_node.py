"""澄清节点测试（桩模型，不调真实 LLM）——2026-09-26。

背景：`CLARIFY_SYSTEM_PROMPT` 模板含 `{logic}` 与 `{question}` 两个占位符，
但调用处只传了 `logic` → `str.format` 抛 `KeyError: 'question'` → 被外层
`except` 吞掉 → **每次澄清都降级为静态模板**（同一句话，与 history 无关）。

该缺陷自澄清功能上线（提交 `7f30db8`）起存在且从未生效过：golden set 只测路由
节点（`type=clarify` 判得对），**从不执行本节点**；spec 的验收项也只写"路由分支
可达"。端到端多轮首次真正跑到该节点即暴露（用户连续收到一字不差的同一句澄清话术）。

本测试用桩模型替换 LLM，断言两件事：
    ① 不走静态兜底 —— 即 `format` 未抛异常（KeyError 的直接防线）
    ② 发给模型的 system prompt 里**确实带上了用户原话与 router logic**
       —— 只断言①不够：把 `{question}` 从模板里删掉同样能让①通过，
          但那样澄清就失去"结合用户原话针对性询问"的能力
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.lg_agent.lg_builder as lgb
from app.lg_agent.lg_prompts import CLARIFY_FALLBACK_REPLY
from app.lg_agent.lg_states import AgentState


class _StubModel:
    """桩模型：记录收到的消息，返回固定澄清话术（不联网）。"""

    received = None

    def __init__(self, *args, **kwargs):
        pass

    async def ainvoke(self, messages):
        _StubModel.received = messages
        return AIMessage(content="亲～您是想了解这款门锁的哪些信息呢？价格、参数还是使用？😊")


def _patch_models(monkeypatch):
    """两个 provider 分支都换成桩，测试不依赖 AGENT_SERVICE 配置。"""
    monkeypatch.setattr(lgb, "ChatDeepSeek", _StubModel)
    monkeypatch.setattr(lgb, "ChatOllama", _StubModel)


@pytest.mark.asyncio
async def test_clarify_node_does_not_fall_back(monkeypatch):
    """澄清节点不得降级静态模板（format 占位符必须齐全）。"""
    _patch_models(monkeypatch)
    state = AgentState(messages=[HumanMessage(content="那个呢")])
    state.router["logic"] = "用户仅发送指代词，无上文可指代，属意图不明"

    out = await lgb.clarify_node(state, config={"configurable": {"thread_id": "t-1"}})

    assert out["messages"][0].content != CLARIFY_FALLBACK_REPLY, (
        "澄清节点走了静态兜底 —— 说明 CLARIFY_SYSTEM_PROMPT.format() 抛异常被吞"
    )


@pytest.mark.asyncio
async def test_clarify_prompt_carries_question_and_logic(monkeypatch):
    """system prompt 必须带上用户原话与 router logic（针对性询问的前提）。"""
    _patch_models(monkeypatch)
    state = AgentState(messages=[HumanMessage(content="那个呢")])
    state.router["logic"] = "用户仅发送指代词，无上文可指代，属意图不明"

    await lgb.clarify_node(state, config={"configurable": {"thread_id": "t-2"}})

    assert _StubModel.received, "桩模型未被调用"
    system_prompt = _StubModel.received[0]["content"]
    assert "那个呢" in system_prompt, "system prompt 缺少用户原话（{question} 未替换）"
    assert "用户仅发送指代词" in system_prompt, "system prompt 缺少 router logic"


@pytest.mark.asyncio
async def test_clarify_falls_back_only_on_llm_failure(monkeypatch):
    """静态兜底仍须可用：LLM 真失败时才降级（不能把兜底一并删掉）。"""

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def ainvoke(self, messages):
            raise RuntimeError("llm down")

    monkeypatch.setattr(lgb, "ChatDeepSeek", _Boom)
    monkeypatch.setattr(lgb, "ChatOllama", _Boom)
    state = AgentState(messages=[HumanMessage(content="那个呢")])
    state.router["logic"] = "意图不明"

    out = await lgb.clarify_node(state, config={"configurable": {"thread_id": "t-3"}})

    assert out["messages"][0].content == CLARIFY_FALLBACK_REPLY
