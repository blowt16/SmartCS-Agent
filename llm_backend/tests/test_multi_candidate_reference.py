"""多候选指代测试（纯本地，不调 LLM）——2026-09-26。

背景（docs/项目问题.md #21）：助手一次列出多款商品后，用户用"这款/多少钱"
指代时，消解器**擅自选一个**，且实测选择不稳定（同对话两次运行分别选中不同型号）。
设计见 `docs/spec_plan/已完成/SPEC_MULTI_CANDIDATE_REFERENCE.md`：
检测到 ≥2 个同等候选时不猜，路由到澄清节点列出候选（并带上助手上一条已给的信息）。

覆盖：
    · 输出解析（JSON / ```json 围栏 / 纯文本向后兼容 / 各类畸形输入）
    · route_query 分支优先级（risk > 多候选 > type；无候选不得触发）
    · clarify_node 候选段（有候选才追加；无候选不得污染既有话术）
    · **反例**：上一轮用户已指明商品主体，下一轮用指代 → 不得误判为多候选
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.lg_agent.lg_builder as lgb
from app.lg_agent.lg_prompts import CLARIFY_FALLBACK_REPLY
from app.lg_agent.lg_states import AgentState
from app.services.pronoun_resolver import (
    ResolveResult,
    _parse_resolve_output,
)


# ==================== 1. 输出解析 ====================

def test_parse_plain_json():
    r = _parse_resolve_output(
        '{"resolved": "小米智能门锁2多少钱", "candidates": []}', "多少钱")
    assert r.query == "小米智能门锁2多少钱"
    assert r.candidates == []


def test_parse_json_with_candidates():
    r = _parse_resolve_output(
        '{"resolved": "多少钱", "candidates": ["小米门锁2", "鹿客S50F", "小米XMZNMS04LM"]}',
        "多少钱")
    assert r.query == "多少钱"
    assert r.candidates == ["小米门锁2", "鹿客S50F", "小米XMZNMS04LM"]


def test_parse_json_in_markdown_fence():
    """模型常把 JSON 包在 ```json 围栏里。"""
    r = _parse_resolve_output(
        '```json\n{"resolved": "扫地机器人X1有货吗", "candidates": []}\n```', "那个有货吗")
    assert r.query == "扫地机器人X1有货吗"
    assert r.candidates == []


# ==================== 2. 纯文本向后兼容（最关键：解析失败不得劣化既有行为）====================

def test_parse_plain_text_backward_compatible():
    """模型未按 JSON 输出时，整体作为 query、候选为空——行为同改造前。"""
    r = _parse_resolve_output("扫地机器人X1有货吗", "那个有货吗")
    assert r.query == "扫地机器人X1有货吗"
    assert r.candidates == []


@pytest.mark.parametrize("bad", [
    "{}",                                   # resolved 缺失
    '{"resolved": "", "candidates": []}',   # resolved 为空 → 回落原消息
    '{"resolved": "x"',                     # JSON 截断
    '{"candidates": ["A", "B"]}',           # 只有候选、无 resolved
    '{"resolved": "多少钱", "candidates": "不是数组"}',
    "",                                     # 空
    "   ",                                  # 空白
])
def test_parse_never_raises_and_keeps_query_usable(bad):
    """任何畸形输入都不得抛异常，且必须留下可用的 query（否则下游拿到空串）。"""
    r = _parse_resolve_output(bad, "原消息")
    assert isinstance(r, ResolveResult)
    assert r.query


def test_parse_trusts_candidates_when_json_valid():
    """JSON 可解析且 candidates 合法即**采信**——模型给出候选就是在说"有歧义"，
    缺 resolved 只影响 query 回落，不构成不采信候选的理由（宁可问，不可猜）。"""
    r = _parse_resolve_output('{"candidates": ["A", "B"]}', "多少钱")
    assert r.query == "多少钱"      # resolved 缺失 → 回落原消息
    assert r.ambiguous              # 候选仍被采信


def test_parse_plain_text_yields_no_candidates():
    """非 JSON 输出不得凭空产生候选——否则纯文本兜底会把普通回答误判成歧义。"""
    r = _parse_resolve_output("小米智能门锁2多少钱", "多少钱")
    assert r.query == "小米智能门锁2多少钱"
    assert r.candidates == []


def test_parse_filters_invalid_candidates():
    """候选里的非字符串/空串必须过滤——它们会被拼进澄清话术与日志。"""
    r = _parse_resolve_output(
        '{"resolved": "多少钱", "candidates": ["A", "", "  ", 123, null, "B"]}', "多少钱")
    assert r.candidates == ["A", "B"]


def test_candidates_capped_without_silence():
    """候选上限在代码层兜底（prompt 是请求，不能只靠模型自觉）。

    实测候选会随轮次滚雪球（3→6→7），而列 7 个选项用户同样没法回答。
    上下文由调用方（resolve_pronouns_ex）截断，此处验证纯函数层不做截断、
    由上层统一处理——避免两处逻辑不一致。
    """
    many = [f"商品{i}" for i in range(9)]
    r = _parse_resolve_output(
        '{"resolved": "多少钱", "candidates": ' + repr(many).replace("'", '"') + '}', "多少钱")
    assert len(r.candidates) == 9          # 解析层不截断，保持输入原貌


def test_parse_ambiguous_keeps_raw_query():
    """多候选时 resolved 应原样返回用户消息（不擅自锁定对象）。"""
    r = _parse_resolve_output('{"resolved": "多少钱", "candidates": ["A", "B"]}', "多少钱")
    assert r.query == "多少钱"


# ==================== 3. route_query 分支优先级 ====================

def _state(query="多少钱", candidates=None, **router_fields):
    st = AgentState(messages=[HumanMessage(content=query)])
    st.router["type"] = router_fields.get("type", "presale")
    st.router["risk"] = router_fields.get("risk", "none")
    st.router["sub_type"] = router_fields.get("sub_type", "none")
    if candidates is not None:
        st.ref_candidates = candidates
    return st


def test_multi_candidate_routes_to_clarify():
    st = _state(candidates=["A", "B", "C"])
    assert lgb.route_query(st) == "clarify_node"


def test_single_candidate_does_not_trigger():
    """候选唯一（1 个）不是歧义——正常走 type 分支。"""
    st = _state(candidates=["A"])
    assert lgb.route_query(st) == "create_research_plan"


def test_no_candidate_field_does_not_trigger():
    """旧 checkpoint / 无候选：字段缺失也必须正常工作（不得 AttributeError）。"""
    st = _state()
    del st.ref_candidates
    assert lgb.route_query(st) == "create_research_plan"


def test_risk_beats_multi_candidate():
    """违规消息不因指代歧义改道——安全优先。"""
    st = _state(candidates=["A", "B"], risk="violation")
    assert lgb.route_query(st) == "risk_intercept"
    st = _state(candidates=["A", "B"], risk="high_risk")
    assert lgb.route_query(st) == "transfer_human"


# ==================== 4. clarify_node 多候选话术（确定性，不走 LLM）====================
#
# ⚠️ 这里断言的是**确定性模板**，不是 LLM 输出。设计变更理由（实测）：
# 两次让 LLM 生成该话术并"把候选逐条列出"均失败——模型看到上一条助手回复已列过商品，
# 便只写"帮您把刚才提到的两款列出来"而**不列**（第二次还换用词绕过 prompt 里的禁用表述）。
# 故候选列表改由 `_render_candidate_lines` 代码生成。详见 spec §9.4。

_DOOR_HIST = (
    "亲～我们有多款智能门锁：\n"
    "- 小米智能门锁2 指静脉版：¥1099.00（有货）\n"
    "- 小米全自动智能门锁Pro：¥1358.00（有货）"
)


class _StubModel:
    """若澄清节点在多候选路径上调了 LLM，本桩会记录到 received —— 用于断言"没调"。"""

    received = None

    def __init__(self, *a, **k):
        pass

    async def ainvoke(self, messages):
        _StubModel.received = messages
        return AIMessage(content="<不该走到这里>")


def _patch(monkeypatch):
    _StubModel.received = None
    monkeypatch.setattr(lgb, "ChatDeepSeek", _StubModel)
    monkeypatch.setattr(lgb, "ChatOllama", _StubModel)


def test_render_candidate_lines_extracts_price():
    out = lgb._render_candidate_lines(
        ["小米智能门锁2 指静脉版", "小米全自动智能门锁Pro"], _DOOR_HIST)
    assert "1️⃣ 小米智能门锁2 指静脉版 —— ¥1099.00" in out
    assert "2️⃣ 小米全自动智能门锁Pro —— ¥1358.00" in out


def test_render_candidate_lines_never_fabricates_price():
    """助手上一条没给价格时只写型号——宁缺毋滥，绝不编造。"""
    out = lgb._render_candidate_lines(["鹿客 S50F"], "亲～我们有多款门锁：鹿客 S50F（有货）")
    assert "鹿客 S50F" in out
    assert "¥" not in out


@pytest.mark.asyncio
async def test_clarify_multi_candidate_lists_candidates(monkeypatch):
    """多候选时必须**真的把候选列出来**（这是本功能的核心价值）。"""
    _patch(monkeypatch)
    st = _state(query="多少钱", candidates=["小米智能门锁2 指静脉版", "小米全自动智能门锁Pro"])
    st.messages = [AIMessage(content=_DOOR_HIST), st.messages[-1]]

    out = await lgb.clarify_node(st, config={"configurable": {"thread_id": "mc-1"}})
    content = out["messages"][0].content

    assert "小米智能门锁2 指静脉版" in content, "候选 1 未列出——用户拿不到可回复的序号"
    assert "小米全自动智能门锁Pro" in content, "候选 2 未列出"
    assert "¥1099.00" in content, "价格未带出（用户不回答也该拿到信息）"


@pytest.mark.asyncio
async def test_clarify_multi_candidate_skips_llm(monkeypatch):
    """确定性路径**不得**调 LLM——模型已被证明会拒绝重列候选。"""
    _patch(monkeypatch)
    st = _state(query="多少钱", candidates=["A款", "B款"])
    await lgb.clarify_node(st, config={"configurable": {"thread_id": "mc-2"}})
    assert _StubModel.received is None, "多候选路径不应调用 LLM"


@pytest.mark.asyncio
async def test_clarify_without_candidates_still_uses_llm(monkeypatch):
    """无候选时保持既有行为：LLM 生成针对性澄清话术。"""
    _patch(monkeypatch)
    st = _state(query="在吗", candidates=[])   # 显式置空
    st.router["type"] = "clarify"
    st.router["logic"] = "无主题词"

    await lgb.clarify_node(st, config={"configurable": {"thread_id": "mc-3"}})

    assert _StubModel.received is not None, "普通澄清仍应走 LLM"
    sp = _StubModel.received[0]["content"]
    assert "候选对象" not in sp, "无候选时不得出现候选段"


# ==================== 5. 反例：上一轮已指明主体（真实 LLM，见 e2e 与 app/test）====================
#
# 「用户上一轮说了具体商品，下一轮用指代」必须**不触发**澄清——否则会频繁打扰。
# 该判断依赖模型对"候选是否唯一/是否已确立"的理解，桩模型测不了，
# 故在 app/test/test_pronoun_resolve.py 的 M 组用真实 LLM 覆盖，并在端到端
# 脚本的"对话5"里做链路级验证（见 SPEC_MULTI_CANDIDATE_REFERENCE §7.3）。
