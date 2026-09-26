"""Router 置信度字段测试（纯本地，规则命中路径不调 LLM）——2026-09-26。

背景：用户提出按置信度阈值（0.75）路由到澄清节点。实测（81 条样本）显示
模型自评置信度中位数 0.92、仅 3 条低于 0.75，且 4 条判错全在高置信区
（"模型自信地判错"），阈值 0.75 需误伤 2 条判对的才拦下 1 条判错的。
**故决策：confidence 只记录、不参与路由**，先攒真实流量分布再定阈值。
设计见 `docs/spec_plan/已完成/SPEC_INTENT_RULE_LAYER.md`。

覆盖：Router 默认值含 confidence / 置信度校正边界 / 判定来源与置信度的配套关系。
"""
import pytest

from app.lg_agent.lg_builder import _normalize_confidence, _normalize_sub_type
from app.lg_agent.lg_states import AgentState


# ==================== Router 默认值 ====================

def test_router_default_has_confidence():
    """AgentState 默认 router 必须含 confidence，否则下游 .get 之外的下标访问会 KeyError。"""
    r = AgentState(messages=[]).router
    assert "confidence" in r
    assert r["confidence"] == 0.0          # 未判定 = 无置信度，不用 1.0 冒充确定
    assert r["source"] == "llm"


# ==================== _normalize_confidence 边界 ====================

@pytest.mark.parametrize("raw,expected", [
    (0.92, 0.92),      # 合法值原样
    (0.0, 0.0),
    (1.0, 1.0),
    (1.5, 0.0),        # 越界 → 0.0
    (-0.1, 0.0),       # 越界 → 0.0
    (None, 0.0),       # 缺失 → 0.0
    ("0.8", 0.0),      # 字符串不静默转换（避免"看起来有值"的假数据）
    (True, 0.0),       # bool 是 int 子类，必须显式排除
])
def test_normalize_confidence(raw, expected):
    assert _normalize_confidence(raw) == expected


def test_normalize_confidence_handles_nan():
    """NaN 不能放过——NaN 会让任何阈值比较恒为 False，静默绕过判定。"""
    assert _normalize_confidence(float("nan")) == 0.0


# ==================== _normalize_sub_type 边界（既有实现的补测） ====================

@pytest.mark.parametrize("router_type,raw,expected", [
    ("aftersale", "return_refund", "return_refund"),
    ("aftersale", "order_query", "order_query"),
    ("aftersale", "none", "other"),         # aftersale 不可无二级 → 兜底 other
    ("aftersale", None, "other"),
    ("aftersale", "bogus", "other"),
    ("presale", "return_refund", "none"),   # 非售后必须 none
    ("presale", None, "none"),
    ("general", "exchange", "none"),
])
def test_normalize_sub_type(router_type, raw, expected):
    assert _normalize_sub_type(router_type, raw) == expected


# ==================== 规则层短路路径：置信度为 1.0（确定性命中）====================

@pytest.mark.asyncio
async def test_rule_hit_yields_full_confidence(monkeypatch):
    """规则层短路是确定性的，confidence 记 1.0，且 source=rule。

    分析置信度分布时须按 source 过滤——规则命中不经过模型，混入会拉高分布。
    """
    from langchain_core.messages import HumanMessage

    import app.lg_agent.lg_builder as lgb
    from app.lg_agent.lg_states import AgentState as AS

    state = AS(messages=[HumanMessage(content="我要退货")])
    out = await lgb.analyze_and_route_query(state, config={"configurable": {"thread_id": "t"}})
    router = out["router"]
    assert router["source"] == "rule"
    assert router["confidence"] == 1.0
    assert router["sub_type"] == "return_refund"
