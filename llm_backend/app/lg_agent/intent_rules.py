"""意图识别规则判定层：明确意图零延迟直接判定，未命中降级 LLM。

为什么需要？
    意图识别原为纯 LLM 单次调用（`lg_builder.analyze_and_route_query`）。大量
    真实问句（"我要退货""多少钱""谢谢"）语义明确，每次过一遍 LLM 是浪费延迟与成本。
    本层用关键词表做零延迟预判，命中即短路；未命中交回 LLM，行为等同改造前。

设计原则（实测校准，见 SPEC_INTENT_RULE_LAYER.md §3.3）：
    词表只收高辨识度业务词，泛词（怎么/什么/怎么样/支持/功能）一律不收。
    **宁可漏——漏了交回 LLM，行为等同改造前；不可错——错了短路走错分支，是真实回归。**
    例外：风险信号词宁可多收——它只拒绝短路、不判风险，多收永不致错。

四道让行闸门（任一不满足即交回 LLM）：
    G1 风险信号词命中 → 规则层不判 risk，漏拦会出事
    G2 未命中任何场景词
    G3 多意图：售后（含兜底词）与售前同时命中
    G4 售后二级场景多命中（分不出细类）

调用约定：调用前须已过 ScopeGuard（经营范围预检在 lg_builder 里独立前置）。
"""

import re
from typing import Literal, Optional, Tuple

from app.core.logger import get_logger

logger = get_logger(service="intent_rules")

AftersaleSubType = Literal[
    "logistics_query", "return_refund", "exchange", "reship", "order_query",
    "other", "none",
]

# 风险信号词：命中即让行（**不是**判风险，是交回 LLM 判）。
# 维护口径与其余词表相反——宁可多收：多收只是少省一次 LLM 调用，永不致错。
RISK_SIGNALS = [
    # 投诉升级（投诉语义一律走顶层 complaint，二级场景不含 complaint）
    "投诉", "举报", "曝光", "告你们", "315",
    # 违规咨询
    "改装", "破解", "解除限速", "越狱",
    # 高风险操作（要求执行资金/权限动作）
    "打钱", "直接退钱", "直接退款", "退我钱", "强制", "私自",
]

# 售后二级场景词表（有序：先精确后宽泛；同一子场景内取首个命中词）
# 注意 order_query 只收搭配词、不收裸词"订单"——裸词会与退货/物流词频繁撞车
# （"我要退掉这个订单"被"订单"撞成二级多命中），详见 spec §5.2。
AFTERSALE_RULES = [
    ("return_refund", ["退货", "退款", "退钱", "退了", "退掉", "不要了",
                       "无理由退", "申请退", "退单", "取消订单"]),
    ("exchange", ["换货", "换一个", "换个", "换新", "换一款", "换成",
                  "想换", "以旧换新"]),
    ("reship", ["补发", "少发", "漏发", "缺件", "少了一件", "少东西",
                "没发", "少给"]),
    ("logistics_query", ["物流", "快递", "发货", "什么时候到", "到哪了", "到哪",
                         "签收", "派送", "运单", "配送", "几天到", "送到了吗",
                         "我的包裹"]),
    ("order_query", ["订单状态", "查订单", "查下订单", "我的订单", "订单号",
                     "订单记录", "订单进度"]),
]

# 售后兜底词：命中说明确定是售后但归不出细类 → sub_type=other
# （比交回 LLM 省一次调用；实测 +4pp 覆盖且零误判，spec §3.3）
AFTERSALE_GATE = [
    "坏了", "损坏", "故障", "质量问题", "售后", "退换", "维修", "返修",
    "三包", "过保", "价保",
]

# 售前：知识库可答的业务问题
# "保修"有意不收（用户决策）：它与"质保"歧义，"我要保修"是售后诉求而非政策咨询。
PRESALE_RULES = [
    "多少钱", "价格", "价钱", "优惠", "活动", "折扣", "促销",
    "推荐", "哪款", "哪个好", "参数", "规格", "尺寸", "型号",
    "怎么安装", "有货", "库存", "现货", "有卖",
    "质保", "续航", "耗电", "材质",
]

# 闲聊：**必须整句匹配**（去标点后完全等于），不能用子串包含——
# 否则"谢谢，那我退货怎么办"会被"谢谢"吃掉判成 general。
# 有意不收"在吗/嗯/那个"：这些应归 clarify（不在本层范围），收了反而制造误判。
GENERAL_WHOLE = {"谢谢", "好的", "好", "收到", "知道了", "明白", "你好", "您好",
                 "再见", "拜拜", "ok", "嗯嗯", "哈哈", "谢谢啦", "多谢", "感谢"}

_PUNCT = re.compile(r"[\s，。！？!?,.~～…、；;：:\"'“”‘’()（）]")


def _first_hit(query: str, words: list[str]) -> Optional[str]:
    """返回词表中首个命中的词（词表有序，先精确后宽泛）。"""
    return next((w for w in words if w in query), None)


def classify_by_rules(query: str) -> Optional[Tuple[str, AftersaleSubType, str]]:
    """规则判定。命中返回 (type, sub_type, reason)，未命中返回 None（降级 LLM）。

    Args:
        query: 用户当前消息（规则层不看历史——多轮指代必须靠 LLM，未命中即降级）。

    Returns:
        (type, sub_type, reason) 或 None。
    """
    if not query:
        return None

    # G1 风险信号闸门：命中即让行，交回 LLM 判 risk
    risk = _first_hit(query, RISK_SIGNALS)
    if risk:
        logger.info("规则层让行: 风险信号[{}] → 交 LLM | query: '{}'", risk, query)
        return None

    # 三类词一次算清，下面按优先级裁决
    subs: list[tuple[str, str]] = []
    for sub, words in AFTERSALE_RULES:
        hit = _first_hit(query, words)
        if hit:
            subs.append((sub, hit))
    gate = _first_hit(query, AFTERSALE_GATE)
    presale = _first_hit(query, PRESALE_RULES)
    aftersale_hit = bool(subs) or bool(gate)

    # G3 多意图闸门：售后（含兜底词）与售前同时命中 → 交回 LLM
    # ⚠️ 兜底词必须参与此判定，否则"这个多少钱，坏了怎么办"会被误判成 presale
    if aftersale_hit and presale:
        logger.info("规则层让行: 多意图[{}+{}] → 交 LLM | query: '{}'",
                    subs[0][0] if subs else gate, presale, query)
        return None

    # G4 二级场景必须唯一（具体子场景先于兜底词裁决）
    if len({s for s, _ in subs}) > 1:
        logger.info("规则层让行: 二级场景多命中{} → 交 LLM | query: '{}'",
                    [s for s, _ in subs], query)
        return None

    if subs:
        return "aftersale", subs[0][0], f"售后词[{subs[0][1]}]→{subs[0][0]}"
    if gate:
        return "aftersale", "other", f"售后兜底词[{gate}]→other"
    if presale:
        return "presale", "none", f"售前词[{presale}]"

    if _PUNCT.sub("", query) in GENERAL_WHOLE:
        return "general", "none", "闲聊整句匹配"

    return None  # G2 未命中 → 降级 LLM
