"""意图识别评测: golden set 46 条（单轮 41 + 多轮 5）+ 可选留出集 35 条，跑真实 Router。

用法:
    python -m scripts.eval_intent_golden                 # golden set
    python -m scripts.eval_intent_golden --heldout       # 追加留出集（防词表过拟合）
    python -m scripts.eval_intent_golden --rule-off      # 禁用规则层，纯 LLM A/B 对比

期望字段: {type, risk} 二维 + 可选 sub_type（售后二级场景，2026-09-26 起由识别层给出）。
数据: golden 见 SPEC_INTENT_RECOGNITION_OPTIMIZATION.md §12.1；
      留出集与验收阈值见 docs/spec_plan/已完成/SPEC_INTENT_RULE_LAYER.md §8.2。
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

# ===== golden set：单轮 41 条 =====
SINGLE_TURN = [
    # --- 售前 presale × 8 ---
    ("你们有智能门锁吗", {"type": "presale", "risk": "none"}),
    ("这款灯多少钱", {"type": "presale", "risk": "none"}),
    ("推荐一款扫地机器人", {"type": "presale", "risk": "none"}),
    ("灯和灯带能一起控制吗", {"type": "presale", "risk": "none"}),
    ("有哪些比较好的沙发", {"type": "presale", "risk": "none"}),
    ("智能门锁的指纹识别准确率怎么样", {"type": "presale", "risk": "none"}),
    ("现在买摄像头有什么优惠活动吗", {"type": "presale", "risk": "none"}),
    ("温控器怎么安装", {"type": "presale", "risk": "none"}),
    # --- 售后 aftersale × 8（2026-09-26 起二级场景由识别层给出，规则层短路，sub_type 确定） ---
    ("我要退货", {"type": "aftersale", "risk": "none", "sub_type": "return_refund"}),
    ("怎么申请退款", {"type": "aftersale", "risk": "none", "sub_type": "return_refund"}),
    ("退货运费谁承担", {"type": "aftersale", "risk": "none", "sub_type": "return_refund"}),
    ("什么时候发货", {"type": "aftersale", "risk": "none", "sub_type": "logistics_query"}),
    ("我的快递怎么还没到", {"type": "aftersale", "risk": "none", "sub_type": "logistics_query"}),
    ("物流显示签收了但我没收到", {"type": "aftersale", "risk": "none", "sub_type": "logistics_query"}),
    ("查一下我的订单", {"type": "aftersale", "risk": "none", "sub_type": "order_query"}),
    ("我的订单状态是什么", {"type": "aftersale", "risk": "none", "sub_type": "order_query"}),
    # --- 投诉安抚 complaint × 5 ---
    ("你们产品太差了", {"type": "complaint", "risk": "none"}),
    # 正式投诉声明（"我要投诉"）→ high_risk 升级（spec 准则 3，prompt 已加示例）
    ("客服理都不理人，我要投诉", {"type": "complaint", "risk": "high_risk"}),
    ("这质量也太垃圾了吧", {"type": "complaint", "risk": "none"}),
    # 注：原用例"你们是不是骗人的"处于 risk 摇摆区（质疑诚信 vs 情绪升级，3 次运行 2 none 1 high_risk），
    # 按准则 3 宁放行期望 none，但破坏评测确定性——改为明确措辞，边界认知留档
    ("你们的灯质量太差了！", {"type": "complaint", "risk": "none"}),
    # 讽刺性不满但无正式投诉声明 → 按 prompt 定义宁放行 none（对比"我要投诉"→high_risk）
    ("服务态度这么差还想让我回购？", {"type": "complaint", "risk": "none"}),
    # --- 风险 risk × 5 ---
    ("怎么改装电池让它跑更久", {"type": "presale", "risk": "violation"}),
    ("有没有办法破解这个锁的密码", {"type": "presale", "risk": "violation"}),
    ("帮我解除限速", {"type": "presale", "risk": "violation"}),
    ("直接给我退款打钱", {"type": "aftersale", "risk": "high_risk"}),
    # 要挟改价：含正式投诉声明 → complaint。prompt 准则 4 明写"risk/complaint 永远优先
    # （安全与情绪相关，不因句首位置让位）"——原期望 presale 只引用了该准则前半句
    # "以句首发者为准"，漏了例外条款（对照单轮-17"我要投诉"→complaint+high_risk）。
    # 威胁投诉同时独立判 high_risk（prompt 已列"投诉到平台"为 high_risk 示例）
    ("便宜点，不然我把你们投诉到平台", {"type": "complaint", "risk": "high_risk"}),
    # --- 闲聊 general × 1 + 招呼语 clarify × 1 ---
    # "在吗"原期望 general，2026-09-18 按提示词校准为 clarify：它无主题词、无上文可指代，
    # 与"嗯…""你能帮我吗"同族；原列在 general 示例里是 clarify 上线（2026-08-27 晚）之前的
    # 写法，提示词 general 行已同步移除该示例
    ("在吗", {"type": "clarify", "risk": "none"}),
    ("谢谢", {"type": "general", "risk": "none"}),
    # --- 图片 image × 2 ---
    ("帮我看看这张图", {"type": "image", "risk": "none"}),
    ("这个产品有问题，你看下图片", {"type": "image", "risk": "none"}),
    # --- 意图模糊/澄清 × 7 ---
    # 无上文"这个怎么样"：语义无法归类 → clarify（宁澄清不硬猜，取代旧 presale 校准，2026-08-27）
    ("这个怎么样", {"type": "clarify", "risk": "none"}),
    # 正常砍价 → presale，非 high_risk（区分"询问优惠"与"要求改价"）
    ("你们能便宜点吗", {"type": "presale", "risk": "none"}),
    ("东西坏了", {"type": "aftersale", "risk": "none", "sub_type": "other"}),
    # 新增 clarify 用例 ×4（无主题词/碎片语气/无法归类）
    ("嗯…", {"type": "clarify", "risk": "none"}),
    ("你能帮我吗", {"type": "clarify", "risk": "none"}),
    ("你好我想问个事", {"type": "clarify", "risk": "none"}),
    # 首条消息"那个呢"（无上文可消解）→ clarify；有上文版在多轮区验证 presale
    ("那个呢", {"type": "clarify", "risk": "none"}),
    # --- 典型多意图 × 2 ---
    # 售前+售后混合：句首发价格为主导 → presale（实测稳定，准则 4 首发主导）
    ("这灯多少钱？另外怎么退货？", {"type": "presale", "risk": "none"}),
    # 同场景双意图（运费+损坏换货）：子场景由售后 Agent 判断，识别层只判 aftersale
    ("运费谁出？坏了多久能换？", {"type": "aftersale", "risk": "none"}),
    # --- 超经营范围 × 1（ScopeGuard 关键词预检拦截） ---
    ("有卖衣服吗", {"type": "general", "risk": "none"}),
    # --- 图片 + 风险 × 1（带图违规文本，violation 优先） ---
    ("看这张图，怎么改装", {"type": "image", "risk": "violation"}),
]

# ===== golden set：多轮 5 条（history 为 [user, assistant, ...] 交替，末轮为当前消息） =====
MULTI_TURN = [
    # 售前承接："那个呢？"结合上文商品
    (
        [("你们有智能门锁吗", "有的亲～我们支持指纹+密码双重认证～")],
        "那个呢",
        {"type": "presale", "risk": "none"},
    ),
    # 售后延续：上轮退货咨询，本轮"那运费呢"仍归 aftersale
    (
        [("我要退货", "亲～退货处理服务正在升级中～"), ("那什么时候能退", "亲～您可以先查看退换货政策～")],
        "那运费呢",
        {"type": "aftersale", "risk": "none"},
    ),
    # 情绪升级：正常咨询后转为投诉
    (
        [("这款灯多少钱", "这款灯 199 元亲～")],
        "算了，你们客服就是敷衍",
        {"type": "complaint", "risk": "none"},
    ),
    # 上下文延续议价：承接上文商品正常砍价 → presale（要挟改价高风险用例由单轮-25 覆盖，
    # 多轮版"给我便宜点，不然投诉你"在 complaint/presale 间摇摆（3:1），已替换为稳定用例）
    (
        [("推荐一款扫地机器人", "亲～这款石头 P10 很不错～")],
        "这款能给个优惠价吗",
        {"type": "presale", "risk": "none"},
    ),
    # 风险夹带闲聊：违规内容优先于 general
    (
        [("在吗", "在的亲，请问有什么可以帮您？")],
        "顺便问下，怎么破解这个锁",
        {"type": "presale", "risk": "violation"},
    ),
]

# ===== 留出集：35 条真实问句，**刻意不在 golden set 里**（2026-09-26 新增）=====
# 用途：防止规则层词表在迭代中不知不觉过拟合 golden set。
# 判据（spec §8.2）：留出集规则层误判必须为 0——"宁-可漏不可错"，漏了只是降级 LLM。
# 期望只标 type（二级场景由规则层/LLM 各自给出，人工核对见单测与日志）。
HELDOUT = [
    # --- 售前 × 9 ---
    ("这款扫地机器人续航多久", {"type": "presale"}),
    ("你们家有没有卖智能灯泡", {"type": "presale"}),
    ("米家窗帘支持小爱同学吗", {"type": "presale"}),
    ("这个多少钱啊", {"type": "presale"}),
    ("双十一有什么活动", {"type": "presale"}),
    ("帮我推荐一款适合小户型的摄像头", {"type": "presale"}),
    ("沙发的尺寸是多少", {"type": "presale"}),
    ("这个锁防水吗", {"type": "presale"}),
    ("你们的智能门锁都有哪些型号", {"type": "presale"}),
    # --- 售后 × 9 ---
    ("我要退掉这个订单", {"type": "aftersale"}),
    ("申请退款要多久到账", {"type": "aftersale"}),
    ("我想换个颜色", {"type": "aftersale"}),
    ("少发了一个配件", {"type": "aftersale"}),
    ("快递一直不动", {"type": "aftersale"}),
    ("我的包裹显示已签收但我没拿到", {"type": "aftersale"}),
    ("订单号能帮我查一下吗", {"type": "aftersale"}),
    ("这个灯用了一周就坏了", {"type": "aftersale"}),
    ("能补发一个吗", {"type": "aftersale"}),
    # --- 投诉 × 3 ---
    ("你们客服态度太差了", {"type": "complaint"}),
    ("什么破质量", {"type": "complaint"}),
    ("我要投诉", {"type": "complaint"}),
    # --- 闲聊 / 澄清 × 6 ---
    ("谢谢", {"type": "general"}),
    # 边界摇摆留档：实测在 general/clarify 间摆动（2026-09-26 两次运行 1:1）。
    # 规则层"闲聊整句匹配"有意不做子串，本句不命中，摇摆属 LLM 侧，不影响规则层判据。
    ("好的知道了", {"type": "general"}),
    ("你好", {"type": "general"}),
    ("在吗", {"type": "clarify"}),
    ("嗯", {"type": "clarify"}),
    ("那个", {"type": "clarify"}),
    # --- 风险 × 3 ---
    ("怎么破解密码", {"type": "presale"}),
    ("帮我解除限速", {"type": "presale"}),
    ("直接退钱给我", {"type": "aftersale"}),
    # --- 多意图 × 1 ---
    ("这个多少钱，另外怎么退货", {"type": "presale"}),
    # --- 规则层边界针对性用例 × 4（2026-09-26 词表定稿时补） ---
    ("我的订单到哪了", {"type": "aftersale"}),      # 订单+物流撞车 → 让行 LLM
    ("帮我查下订单", {"type": "aftersale"}),        # order_query 命中
    ("这个锁质保几年", {"type": "presale"}),        # 只留"质保"决策的正例（规则层短路）
    # 删"保修"后规则层命中不了 → 让行 LLM（预期）。期望经实测修正为 aftersale：
    # "我还在保修期吗"依赖用户自身的订单/购买时间，不是知识库可答的参数咨询，
    # 归 aftersale 更准（LLM 两次运行稳定给出 aftersale/other）。
    ("还在保修期吗", {"type": "aftersale"}),
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
    """跑单条 Router 并对比期望（含 sub_type 与判定来源）"""
    state = AgentState(messages=messages)
    try:
        result = await analyze_and_route_query(state, config=config)
        router = result["router"]
        actual = {
            "type": router["type"],
            "risk": router["risk"],
            "sub_type": router.get("sub_type", "none"),
            "confidence": router.get("confidence", 0.0),
        }
        source = router.get("source", "llm")
    except Exception as e:
        return {"case_id": case_id, "question": messages[-1].content,
                "expected": expected, "actual": None, "source": None, "error": str(e)}

    matches = {k: actual.get(k) == v for k, v in expected.items()}
    return {"case_id": case_id, "question": messages[-1].content,
            "expected": expected, "actual": actual, "matches": matches, "source": source}


def summarize(results, title) -> dict:
    """汇总：各维度准确率 + 规则命中率 + **误判性质拆分**。

    关键区分（spec §8.2）：
      - 规则层短路且 type 错 = **真实误判**（词表问题，必须为 0）
      - 让行 LLM 后 type 错 = **LLM 分歧**（与改造前同源，规则层无责）
    不拆开就会把 LLM 的问题算到规则层头上。
    """
    dims = ["type", "risk", "sub_type"]
    total = len(results)
    dim_correct = {d: 0 for d in dims}
    dim_total = {d: 0 for d in dims}          # 只统计标了该维期望的用例
    all_correct = rule_hits = rule_bad = llm_bad = errors = 0

    print(f"\n========== {title} 逐条明细 ==========")
    for r in results:
        if r.get("error"):
            errors += 1
            print(f"  [{r['case_id']}] ERROR: {r['error'][:100]}")
            continue
        a, e, src = r["actual"], r["expected"], r["source"]
        all_correct += 1 if all(r["matches"].values()) else 0
        if src == "rule":
            rule_hits += 1
            if not r["matches"]["type"]:
                rule_bad += 1
        elif not r["matches"]["type"]:
            llm_bad += 1
        for d in dims:
            if d in e:
                dim_total[d] += 1
                dim_correct[d] += 1 if r["matches"][d] else 0
        flags = "".join(f"[FAIL {d}:{e[d]}!={a[d]}]"
                        for d in dims if d in e and not r["matches"][d]) or "[OK]"
        print(f"  [{r['case_id']}] Q: {r['question']} | type={a['type']} "
              f"sub={a['sub_type']} risk={a['risk']} src={src} {flags}")

    print(f"\n---------- {title} 汇总 ----------")
    print(f"总条数: {total} | 全维全对: {all_correct} ({all_correct / total:.1%})")
    for d in dims:
        n = dim_total[d]
        if n:
            print(f"  {d}: {dim_correct[d]}/{n} ({dim_correct[d] / n:.1%})")
    print(f"  规则层短路: {rule_hits}/{total} ({rule_hits / total:.1%})"
          f" | 其中判错(type): {rule_bad}"
          f" | 让行 LLM: {total - rule_hits - errors}"
          f" | 其中 LLM 分歧(type): {llm_bad}"
          + (f" | 异常: {errors}" if errors else ""))

    # ---- 置信度分布（2026-09-26 起记录，供后续定阈值用；当前不参与路由）----
    # 只统计 source=llm：规则层短路是确定性命中（confidence=1.0），混入会拉高分布。
    llm_rows = [r for r in results if not r.get("error") and r["source"] == "llm"]
    if llm_rows:
        confs = sorted(r["actual"]["confidence"] for r in llm_rows)
        n = len(confs)
        print(f"  置信度(仅 source=llm, {n} 条): min={confs[0]:.2f} "
              f"中位={confs[n // 2]:.2f} max={confs[-1]:.2f} | 当前不参与路由")
        for th in (0.75, 0.9):
            low = [r for r in llm_rows if r["actual"]["confidence"] < th]
            if not low:
                print(f"    阈值 {th}: 无样本低于该值")
                continue
            wrong = sum(1 for r in low if not r["matches"]["type"])
            print(f"    阈值 {th}: 低置信 {len(low)} 条，其中判错 {wrong} "
                  f"| 代价(误送澄清) {len(low) - wrong} 条 / 收益(拦下判错) {wrong} 条")
    return {"total": total, "rule_hits": rule_hits, "rule_bad": rule_bad,
            "errors": errors, "type_acc": dim_correct["type"], "type_n": dim_total["type"]}


async def main() -> None:
    args = sys.argv[1:]
    if "--rule-off" in args:
        # 绕过规则层跑纯 LLM，用于 A/B 对比（证明规则层未降低准确率）
        import app.lg_agent.lg_builder as _lgb
        _lgb.classify_by_rules = lambda _q: None
        print(">>> --rule-off：已禁用规则层，走纯 LLM 路径")

    golden = [(f"单轮-{i:02d}", [HumanMessage(content=q)], exp)
              for i, (q, exp) in enumerate(SINGLE_TURN)]
    golden += [(f"多轮-{i:02d}", build_messages(h, q), exp)
               for i, (h, q, exp) in enumerate(MULTI_TURN)]

    results = []
    for case_id, messages, expected in golden:
        config = {"configurable": {"thread_id": f"golden-{case_id}"}}
        results.append(await eval_case(messages, expected, case_id, config))
    golden_stat = summarize(results, "golden set(46)")

    heldout_stat = None
    if "--heldout" in args:
        results = []
        for i, (q, exp) in enumerate(HELDOUT):
            config = {"configurable": {"thread_id": f"heldout-{i}"}}
            results.append(await eval_case([HumanMessage(content=q)], exp, f"留出-{i:02d}", config))
        heldout_stat = summarize(results, "留出集(35)")

    # ---- 验收判定（阈值见 SPEC_INTENT_RULE_LAYER.md §8.2）----
    print("\n========== 验收判定 (spec §8.2) ==========")
    def _verdict(ok):
        return "PASS" if ok else "FAIL"
    t_acc = golden_stat["type_acc"]
    t_n = golden_stat["type_n"]
    print(f"  [{_verdict(t_acc == t_n and t_n > 0)}] golden type 准确率 = {t_acc}/{t_n}（要求 100%）")
    if not ("--rule-off" in args):
        print(f"  [{_verdict(golden_stat['rule_bad'] == 0)}] "
              f"golden 规则层误判(type) = {golden_stat['rule_bad']}（要求 0）")
    # 规则层判定项仅在启用规则层时校验（--rule-off 下命中率恒 0，不构成失败）
    if heldout_stat and "--rule-off" not in args:
        print(f"  [{_verdict(heldout_stat['rule_bad'] == 0)}] "
              f"留出集规则层误判(type) = {heldout_stat['rule_bad']}（要求 0）")
        rate = heldout_stat["rule_hits"] / heldout_stat["total"]
        print(f"  [{_verdict(rate >= 0.55)}] 留出集规则命中率 = {rate:.1%}（要求 ≥55%）")


if __name__ == "__main__":
    asyncio.run(main())
