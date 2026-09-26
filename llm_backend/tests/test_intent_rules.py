"""意图规则判定层测试(纯函数,不调 LLM / 不连库)——2026-09-26 意图识别迭代。

背景:意图识别此前为纯 LLM 单次调用,本层在其前面加一道零延迟规则判定:
明确意图直接短路,未命中降级 LLM。设计见
`docs/spec_plan/已完成/SPEC_INTENT_RULE_LAYER.md`。

判据(实测校准,spec §3.3):
    词表只收高辨识度业务词,**宁可漏——漏了交回 LLM(等价改造前行为),
    不可错——错了短路走错分支,是真实回归**。
    例外:风险信号词宁可多收(它只拒绝短路、不判风险,多收永不致错)。

覆盖:四个售后二级场景 + 订单查询 + 兜底 other / 售前 / 闲聊整句 /
四道让行闸门(风险信号、多意图、二级多命中、未命中) / 词表边界(保修不收、质保收)。
"""
import pytest

from app.lg_agent.intent_rules import classify_by_rules


def _sub(query):
    """命中时返回 sub_type,未命中返回 None(便于断言让行)。"""
    hit = classify_by_rules(query)
    return hit[1] if hit else None


def _type(query):
    hit = classify_by_rules(query)
    return hit[0] if hit else None


# ==================== 规则命中:售后二级场景 ====================

@pytest.mark.parametrize("query,expected", [
    # return_refund 退货退款
    ("我要退货", "return_refund"),
    ("怎么申请退款", "return_refund"),
    ("退货运费谁承担", "return_refund"),      # 运费属退货政策,不是物流
    ("我要退掉这个订单", "return_refund"),    # 具体诉求优先于 order_query 撞车
    # exchange 换货
    ("我想换个颜色", "exchange"),
    ("能换货吗", "exchange"),
    # reship 补发
    ("少发了一个配件", "reship"),
    ("能补发一个吗", "reship"),
    # logistics_query 物流查询
    ("什么时候发货", "logistics_query"),
    ("我的快递怎么还没到", "logistics_query"),
    ("物流显示签收了但我没收到", "logistics_query"),
    ("快递一直不动", "logistics_query"),
    # order_query 订单查询
    ("查一下我的订单", "order_query"),
    ("我的订单状态是什么", "order_query"),
    ("订单号能帮我查一下吗", "order_query"),
    ("帮我查下订单", "order_query"),
    # other 售后兜底(确定是售后,归不出细类)
    ("东西坏了", "other"),
    ("这个灯用了一周就坏了", "other"),
    ("我要申请售后", "other"),
])
def test_aftersale_subtype_hit(query, expected):
    """命中售后时给出对应二级场景,且 type 必须是 aftersale。"""
    assert _type(query) == "aftersale"
    assert _sub(query) == expected


def test_return_reason_is_nonempty():
    """命中必须带理由串(供下游 logic 字段与日志排查)。"""
    hit = classify_by_rules("我要退货")
    assert hit is not None
    assert hit[2]


# ==================== 规则命中:售前 ====================

@pytest.mark.parametrize("query", [
    "这款灯多少钱",
    "现在买摄像头有什么优惠活动吗",
    "推荐一款扫地机器人",
    "温控器怎么安装",
    "沙发的尺寸是多少",
    "你们的智能门锁都有哪些型号",
])
def test_presale_hit(query):
    """明确售前咨询短路,sub_type 固定 none。"""
    assert _type(query) == "presale"
    assert _sub(query) == "none"


def test_presale_keeps_zhibao_drops_baoxiu():
    """只留"质保",不收"保修"(用户决策:保修动词用法歧义更重)。"""
    assert _type("这个锁质保几年") == "presale"
    assert classify_by_rules("还在保修期吗") is None      # 交回 LLM


# ==================== 规则命中:闲聊(必须整句匹配) ====================

@pytest.mark.parametrize("query", ["谢谢", "好的", "你好", "嗯嗯", "谢谢啦"])
def test_general_whole_match(query):
    assert _type(query) == "general"
    assert _sub(query) == "none"


def test_general_requires_whole_match():
    """"谢谢,那我退货怎么办"不能被"谢谢"吃掉——整句匹配是硬要求。"""
    assert _type("谢谢，那我退货怎么办") == "aftersale"
    assert _sub("谢谢，那我退货怎么办") == "return_refund"


# ==================== 让行闸门 G1:风险信号 ====================

@pytest.mark.parametrize("query", [
    "我要投诉",                    # 投诉走顶层 complaint,二级不含 complaint
    "客服理都不理人，我要投诉",
    "便宜点，不然我把你们投诉到平台",
    "直接给我退款打钱",            # 含"退款"售后词,但风险信号优先让行
    "怎么改装电池让它跑更久",
    "有没有办法破解这个锁的密码",
    "帮我解除限速",
    "看这张图，怎么改装",
])
def test_risk_signal_defers_to_llm(query):
    """风险信号命中即让行——规则层不判 risk,漏判会出事(spec D2)。"""
    assert classify_by_rules(query) is None


# ==================== 让行闸门 G3:多意图 ====================

@pytest.mark.parametrize("query", [
    "这灯多少钱？另外怎么退货？",       # 售前 + 售后
    "这个多少钱，坏了怎么办",           # 售前 + 售后兜底词(易漏,兜底词须参与多意图判定)
])
def test_multi_intent_defers_to_llm(query):
    assert classify_by_rules(query) is None


# ==================== 让行闸门 G4:二级场景多命中 ====================

def test_multi_subtype_defers_to_llm():
    """"我的订单到哪了"同时命中 order_query 与 logistics_query → 交回 LLM。"""
    assert classify_by_rules("我的订单到哪了") is None


def test_same_subtype_twice_still_hits():
    """同一子场景命中多个词不算多命中——"我要退货退款"仍是 return_refund。"""
    assert _sub("我要退货退款") == "return_refund"


# ==================== 让行闸门 G2:未命中 ====================

@pytest.mark.parametrize("query", [
    "在吗",                        # clarify 不在规则层范围(D3)
    "嗯…",
    "那个呢",
    "你能帮我吗",
    "你们产品太差了",               # complaint 不在规则层范围
    "什么破质量",
    "这个锁防水吗",                 # 词表未覆盖
    "好的知道了",                   # 闲聊整句不匹配,不做子串
    "",
])
def test_unmatched_defers_to_llm(query):
    assert classify_by_rules(query) is None


# ==================== 裁决优先级 ====================

def test_specific_subtype_beats_gate():
    """具体子场景词先于兜底词裁决:"这个灯坏了要退货"归 return_refund 而非 other。"""
    assert _sub("这个灯坏了要退货") == "return_refund"


def test_aftersale_beats_presale_when_only_aftersale_matched():
    """"退货运费谁承担"含"运费"不含售前词——不该被误判为售前。"""
    assert _type("退货运费谁承担") == "aftersale"
