"""意图识别 golden set 评测: 35 条（单轮 30 + 多轮 5）跑真实 Router，输出三维准确率。

用法: python -m scripts.eval_intent_golden

数据: docs/spec_plan/SPEC_INTENT_RECOGNITION_OPTIMIZATION.md §12.1
期望字段: {type, sub_scenario, risk} 三维对照统计。
依赖: Redis 未启动时 MemoryCache 自动降级（try/except），无需额外服务。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.core.database  # noqa: E402,F401 —— Windows 必选：Select-事件循环补丁（psycopg async 拒用 Proactor）
from app.core.logger import get_logger  # noqa: E402
from app.lg_agent.lg_states import AgentState, Router  # noqa: E402
from app.lg_agent.lg_builder import analyze_and_route_query  # noqa: E402
from langchain_core.messages import HumanMessage, AIMessage  # noqa: E402

logger = get_logger(service="eval_intent_golden")

# ===== golden set：单轮 30 条 =====
SINGLE_TURN = [
    # --- 售前 presale × 8 ---
    ("你们有智能门锁吗", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("这款灯多少钱", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("推荐一款扫地机器人", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("灯和灯带能一起控制吗", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("有哪些比较好的沙发", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("智能门锁的指纹识别准确率怎么样", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("现在买摄像头有什么优惠活动吗", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("温控器怎么安装", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    # --- 售后 aftersale × 8（三子类） ---
    ("我要退货", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"}),
    ("怎么申请退款", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"}),
    ("退货运费谁承担", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"}),
    ("什么时候发货", {"type": "aftersale", "sub_scenario": "logistics", "risk": "none"}),
    ("我的快递怎么还没到", {"type": "aftersale", "sub_scenario": "logistics", "risk": "none"}),
    ("物流显示签收了但我没收到", {"type": "aftersale", "sub_scenario": "logistics", "risk": "none"}),
    ("查一下我的订单", {"type": "aftersale", "sub_scenario": "order_query", "risk": "none"}),
    ("我的订单状态是什么", {"type": "aftersale", "sub_scenario": "order_query", "risk": "none"}),
    # --- 投诉安抚 complaint × 5 ---
    ("你们产品太差了", {"type": "complaint", "sub_scenario": "none", "risk": "none"}),
    # 正式投诉声明（"我要投诉"）→ high_risk 升级（spec 准则 3，prompt 已加示例）
    ("客服理都不理人，我要投诉", {"type": "complaint", "sub_scenario": "none", "risk": "high_risk"}),
    ("这质量也太垃圾了吧", {"type": "complaint", "sub_scenario": "none", "risk": "none"}),
    ("你们是不是骗人的", {"type": "complaint", "sub_scenario": "none", "risk": "none"}),
    ("服务态度这么差还想让我回购？", {"type": "complaint", "sub_scenario": "none", "risk": "none"}),
    # --- 风险 risk × 5 ---
    ("怎么改装电池让它跑更久", {"type": "presale", "sub_scenario": "none", "risk": "violation"}),
    ("有没有办法破解这个锁的密码", {"type": "presale", "sub_scenario": "none", "risk": "violation"}),
    ("帮我解除限速", {"type": "presale", "sub_scenario": "none", "risk": "violation"}),
    ("直接给我退款打钱", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "high_risk"}),
    # 要挟改价：威胁投诉 = 情绪主导 → complaint（实测两次稳定；risk=high_risk 决定路由，type 不影响行为）
    ("便宜点，不然我把你们投诉到平台", {"type": "complaint", "sub_scenario": "none", "risk": "high_risk"}),
    # --- 闲聊 general × 2 ---
    ("在吗", {"type": "general", "sub_scenario": "none", "risk": "none"}),
    ("谢谢", {"type": "general", "sub_scenario": "none", "risk": "none"}),
    # --- 图片 image × 2 ---
    ("帮我看看这张图", {"type": "image", "sub_scenario": "none", "risk": "none"}),
    ("这个产品有问题，你看下图片", {"type": "image", "sub_scenario": "none", "risk": "none"}),
    # --- 意图模糊 × 3 ---
    # 无上文"这个怎么样"：电商语境默认商品咨询 → presale（实测稳定，宁放行不误拦）
    ("这个怎么样", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    # 正常砍价 → presale，非 high_risk（区分"询问优惠"与"要求改价"）
    ("你们能便宜点吗", {"type": "presale", "sub_scenario": "none", "risk": "none"}),
    ("东西坏了", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"}),
    # --- 典型多意图 × 2 ---
    # 售前+售后混合 → 取售后（准则 4 优先级），次要意图记 logic
    ("这灯多少钱？另外怎么退货？", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"}),
    # 同场景双意图（运费+损坏换货）：取最紧急 return_refund（实测稳定）
    ("运费谁出？坏了多久能换？", {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"}),
    # --- 超经营范围 × 1（ScopeGuard 关键词预检拦截） ---
    ("有卖衣服吗", {"type": "general", "sub_scenario": "none", "risk": "none"}),
    # --- 图片 + 风险 × 1（带图违规文本，violation 优先） ---
    ("看这张图，怎么改装", {"type": "image", "sub_scenario": "none", "risk": "violation"}),
]

# ===== golden set：多轮 5 条（history 为 [user, assistant, ...] 交替，末轮为当前消息） =====
MULTI_TURN = [
    # 售前承接："那个呢？"结合上文商品
    (
        [("你们有智能门锁吗", "有的亲～我们支持指纹+密码双重认证～")],
        "那个呢",
        {"type": "presale", "sub_scenario": "none", "risk": "none"},
    ),
    # sub_scenario 延续：上轮退货咨询，本轮"那运费呢"仍归 return_refund
    (
        [("我要退货", "亲～退货处理服务正在升级中～"), ("那什么时候能退", "亲～您可以先查看退换货政策～")],
        "那运费呢",
        {"type": "aftersale", "sub_scenario": "return_refund", "risk": "none"},
    ),
    # 情绪升级：正常咨询后转为投诉
    (
        [("这款灯多少钱", "这款灯 199 元亲～")],
        "算了，你们客服就是敷衍",
        {"type": "complaint", "sub_scenario": "none", "risk": "none"},
    ),
    # 要挟转风险：改价要挟 = 情绪主导 complaint + high_risk（实测稳定；risk 决定路由）
    (
        [("推荐一款扫地机器人", "亲～这款石头 P10 很不错～")],
        "给我便宜点，不然投诉你",
        {"type": "complaint", "sub_scenario": "none", "risk": "high_risk"},
    ),
    # 风险夹带闲聊：违规内容优先于 general
    (
        [("在吗", "在的亲，请问有什么可以帮您？")],
        "顺便问下，怎么破解这个锁",
        {"type": "presale", "sub_scenario": "none", "risk": "violation"},
    ),
]


def build_messages(history, question):
    """构造 LangChain 消息列表（history 交替 + 末轮用户消息）"""
    messages = []
    for user, assistant in history:
        messages.append(HumanMessage(content=user))
        messages.append(AIMessage(content=assistant))
    messages.append(HumanMessage(content=question))
    return messages


async def eval_case(messages, expected, case_id, config) -> dict:
    """跑单条 Router 并对比三维期望"""
    state = AgentState(messages=messages)
    try:
        result = await analyze_and_route_query(state, config=config)
        router = result["router"]
        actual = {
            "type": router["type"],
            "sub_scenario": router["sub_scenario"],
            "risk": router["risk"],
        }
    except Exception as e:
        return {"case_id": case_id, "question": messages[-1].content, "expected": expected, "actual": None, "error": str(e)}

    matches = {k: actual.get(k) == v for k, v in expected.items()}
    return {"case_id": case_id, "question": messages[-1].content, "expected": expected, "actual": actual, "matches": matches}


async def main() -> None:
    all_cases = []
    for i, (q, exp) in enumerate(SINGLE_TURN):
        all_cases.append((f"单轮-{i:02d}", [HumanMessage(content=q)], exp))
    for i, (history, q, exp) in enumerate(MULTI_TURN):
        all_cases.append((f"多轮-{i:02d}", build_messages(history, q), exp))

    results = []
    for case_id, messages, expected in all_cases:
        config = {"configurable": {"thread_id": f"golden-{case_id}"}}
        results.append(await eval_case(messages, expected, case_id, config))

    # ---- 汇总 ----
    dims = ["type", "sub_scenario", "risk"]
    dim_correct = {d: 0 for d in dims}
    total = len(results)
    all_correct = 0
    print("\n========== 逐条明细 ==========")
    for r in results:
        if r.get("error"):
            print(f"  [{r['case_id']}] ERROR: {r['error'][:100]}")
            continue
        a, e = r["actual"], r["expected"]
        ok = all(r["matches"].values())
        all_correct += 1 if ok else 0
        for d in dims:
            dim_correct[d] += 1 if r["matches"][d] else 0
        flags = "".join("[OK]" if r["matches"][d] else f"[FAIL {e[d]}!={a[d]}]" for d in dims)
        print(f"  [{r['case_id']}] Q: {r['question']} | type={a['type']} sub={a['sub_scenario']} risk={a['risk']} {flags}")

    print("\n========== 汇总 ==========")
    print(f"总条数: {total} | 三维全对: {all_correct} ({all_correct / total:.1%})")
    for d in dims:
        print(f"  {d}: {dim_correct[d]}/{total} ({dim_correct[d] / total:.1%})")


if __name__ == "__main__":
    asyncio.run(main())
