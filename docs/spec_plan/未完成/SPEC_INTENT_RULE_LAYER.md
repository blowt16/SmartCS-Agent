# SPEC：意图识别模块迭代 —— 规则判定层前置 + 售后二级路由

> 归档状态：⏳ 待实施（2026-09-26 编写，待用户审核）
> 关联：`SPEC_INTENT_RECOGNITION_OPTIMIZATION.md`（已完成）、`SPEC_INTENT_CLARIFY.md`（已完成）
> 本文档不含未决占位符；所有词表、prompt、代码形态均已实测验证（见 §3、§8）。

---

## 1. 背景与目标

### 1.1 现状

意图识别目前是**单次 LLM 调用**（`lg_builder.py:48` `analyze_and_route_query`），输出三个字段：

| 字段 | 取值 | 说明 |
|---|---|---|
| `type` | presale / aftersale / complaint / general / image / clarify | 场景 |
| `risk` | none / violation / high_risk | 风险 |
| `logic` | str | 分类理由，供下游 prompt 注入 |

规则代码只承担两类前置门控，**不参与意图分类**：

- `ScopeGuard`（`scope_guard.py:40`）：经营范围关键词/正则拦截，零延迟
- `pronoun_detector`（`main.py:294`）：入口指代消解触发判定，已由 `SPEC_ENTRY_LLM_RESOLUTION` 改造中

### 1.2 本次目标

在前置加一层**规则判定层**：明确的意图（售前咨询 / 售后 / 闲聊）零延迟直接判定，规则未命中才降级走 LLM；同时为售后场景补上**二级路由细分**。

### 1.3 收益与代价（实测，见 §3）

| 项 | 实测值 |
|---|---|
| 规则层命中率（真实问句留出集，35 条） | **57%** → 这部分 query **省一次 LLM 调用** |
| 规则层命中率（现有 golden set，46 条难例集） | 37% |
| 命中后场景判错（两个集合合计 81 条） | **0 条** |
| 风险用例漏判（8 条 violation/high_risk） | **0 条**（全部正确交回 LLM） |
| 延迟收益 | 命中即返回，**省一次 LLM 往返**（绝对毫秒数取决于 provider，本 spec 不承诺具体数值） |

**注意**：golden set 是刻意堆难例的回归集（clarify / complaint / 多意图 / 风险密集），命中率天然偏低；留出集更接近真实流量。**规则层的词表不得针对 golden set 调优**，判据始终是"留出集零误判"。

---

## 2. 关键决策记录

### 2.1 用户决策（2026-09-26 确认）

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 顶层 `complaint` 与二级场景 `complaint` 冲突 | **顶层保留，二级去掉 complaint**。凡投诉语义一律走顶层 complaint 节点，二级含 `order_query` 共 6 类 |
| D2 | 规则层短路后 risk 谁判 | **风险信号词命中则不短路，仍走 LLM**。规则层不判 risk，不产生新的漏拦面 |
| D3 | 规则层覆盖哪些顶层场景 | **presale / aftersale（含二级）/ general**。complaint / clarify / image 全部交回 LLM |
| D4 | 二级场景本次用途 | **只落 state + 日志 + 评测，路由不变**。`route_query` 仍送 aftersale 到 `aftersale_placeholder`，为用户可见行为零变化，纯为售后 Agent 预留接口 |

### 2.2 历史决策反转（重要）

本次**推翻了 2026-08-27 的决策 #13**。原决策见提交 `944b92d`：

> "移除 sub_scenario 维度（简化设计）——售后子场景判断需订单/历史等上下文，识别层只有对话文本，下沉到售后 Agent 工作流骨架第一步"

`lg_states.py:18-19` 的现有注释、`ROUTER_SYSTEM_PROMPT` 首段、`SPEC_INTENT_RECOGNITION_OPTIMIZATION.md:211` 均记录该决策，**本次需同步改写**。

**为什么可以推翻**（待用户确认此理由）：原理由"子场景判断需订单上下文"混淆了两个环节——
- **识别**环节只回答"用户嘴上说的是哪类诉求"，这是纯语义分类，对话文本足够；
- **执行**环节（真去查订单、算差价、发起退货）才需要订单数据。

本次改的是识别环节，把二级场景**作为信号**产出（D4：不改路由、不触发任何执行），因此不触碰原决策的顾虑。真到售后 Agent 落地时，仍由 Agent 自己决定要不要用这个信号、要不要查订单。

### 2.3 命名变更

| 旧（`944b92d` 之前） | 新 | 变更 |
|---|---|---|
| `return_refund` | `return_refund` | 不变 |
| `logistics` | `logistics_query` | 改名（对齐用户口径）；**词表收窄**——"查订单/订单状态"移出，归 `order_query` |
| `order_query` | `order_query` | **保留**（用户 2026-09-26 决策：查订单是售后独立子场景，不并入物流） |
| — | `exchange` | 新增（换货） |
| — | `reship` | 新增（补发） |
| — | `other` | 新增（售后兜底） |
| `none` | `none` | 不变（非 aftersale 时） |

最终取值共 7 个：`logistics_query` / `return_refund` / `exchange` / `reship` / `order_query` / `other` / `none`。

---

## 3. 实测验证结论（2026-09-26，写 spec 前完成）

### 3.1 回归基线

```
python -m scripts.eval_intent_golden
总条数: 46 | 二维全对: 46 (100.0%)
  type: 46/46 (100.0%)   risk: 46/46 (100.0%)
```

**这是本次改造的回归红线：改造后必须仍为 46/46。**

### 3.2 规则层原型命中分布

原型为纯本地函数（无 LLM / 无 DB），对现有 46 条 golden set 逐条跑规则判定：

| 集合 | 条数 | 规则命中 | 命中后 type 判错 |
|---|---|---|---|
| golden set（现有） | 46 | 17（37%） | **0** |
| 留出集（手写 35 条真实问句，不在 golden 内） | 35 | 20（57%） | **0** |

留出集未命中的 15 条，全部是**有意不命中**：

| 类别 | 条数 | 说明 |
|---|---|---|
| clarify（在吗/嗯/那个） | 3 | 不在 D3 覆盖范围 |
| complaint（客服态度差/什么破质量） | 2 | 不在 D3 覆盖范围 |
| 风险信号闸门 | 4 | "我要投诉"、"怎么破解密码"、"帮我解除限速"、"直接退钱给我" —— G1 有意让行 |
| 闲聊整句不匹配（"好的知道了"） | 1 | 整句匹配不做子串，有意保守 |
| 多意图 / 二级撞车 | 2 | "这个多少钱，另外怎么退货"、"我的订单到哪了"（订单词与物流词同现） |
| 词表未覆盖 | 3 | "米家窗帘支持小爱同学吗"、"这个锁防水吗"、"还在保修期吗"（最后一条是"只留质保"决策的预期结果） |

### 3.3 词表宽严的实测教训（决定 §5 词表设计）

| 变体 | 命中（golden） | 误判 | 结论 |
|---|---|---|---|
| v2 基线（只收高辨识度业务词） | 15/46 (33%) | 0 | 安全基线 |
| **+ 售后兜底词 → `other`** | **17/46 (37%)** | **0** | **净收益，采纳** |
| + 售前补泛词（"怎么样"等） | 20/46 (43%) | **1** | 毒药词："这个怎么样"本该 clarify 被吃成 presale，**不采纳** |
| + 二级场景补词（"运费谁""能换"…） | 14/46 (30%) | 0 | **反而降覆盖**——补词触发"二级多命中"闸门，交回 LLM 变多，不采纳 |
| `order_query` 收裸词"订单" | — | 0 | 留出集 19/35；"我要退掉这个订单"被"订单"撞成二级多命中 → 交回 LLM，**不采纳** |
| `order_query` 只收搭配词（定稿） | — | 0 | 留出集 **20/35 (57%)**，撞车减少 1 条，**采纳** |

**核心原则（写入代码注释）**：

> 词表只收高辨识度业务词，泛词（怎么 / 什么 / 怎么样 / 支持 / 功能）一律不收。
> **宁可漏——漏了交回 LLM，行为等同改造前；不可错——错了短路走错分支，是真实回归。**

风险信号词是这条原则的**唯一例外，方向相反**：风险词表宁可多收。因为它的作用是"拒绝短路"而非"判定风险"——多收一个词只是少省一次 LLM 调用，**永远不会导致判错**（判 risk 的仍是 LLM）。

---

## 4. 架构设计

### 4.1 三层判定结构

图拓扑**不变**（仍是 `analyze_and_route_query` 单个节点），规则层作为该节点内部的第一个分支：

```
analyze_and_route_query(state, config)          lg_builder.py:48
  │
  ├─ ⓪ ScopeGuard 经营范围预检                  既有，不动（lg_builder.py:63-69）
  │     └─ 拦截 → Router(type=general, source=rule)      [既有行为]
  │
  ├─ ① 规则判定层【新增，零延迟、无 IO、无 LLM】
  │     └─ 命中 → Router(...) 直接返回，跳过 ②③          [新行为]
  │
  └─ ② LLM 识别层                              既有，未命中时走这里
        ├─ MemoryManager 组装历史               lg_builder.py:82-90
        ├─ with_structured_output(Router)       lg_builder.py:96-102
        └─ 异常降级 general/none                lg_builder.py:100-102
```

**为什么不拆成独立节点**：拆节点要动 `builder.add_node` / `add_edge` / `add_conditional_edges` 三处（`lg_builder.py:549-561`），并把中间结果在 state 里多走一轮；而规则层与 LLM 层是**同一个决策的两条路径**，共享返回契约。放同一节点内是内聚的。代价：`stream_filter.py:20` 的 `INTERNAL_NODES` 白名单无需改动（节点名没变）。

### 4.2 规则层短路的四道闸门

**四条全部满足才短路**；任何一条不满足 → 交回 LLM（等价于改造前行为）：

| # | 闸门 | 不满足时 |
|---|---|---|
| G1 | 无风险信号词命中 | 交回 LLM（D2：保 risk 判定） |
| G2 | 命中场景词（`presale` 或 `aftersale` 词表） | 交回 LLM |
| G3 | **单意图**：presale 与 aftersale 词不同时命中 | 交回 LLM（多意图的"句首发/最强烈者"是语义任务） |
| G4 | 判为 aftersale 时，二级场景唯一确定（或用兜底词定 `other`） | 交回 LLM |

判定顺序（**先售后后售前**，因为售后诉求更具体、误判代价更高）：

```
① ScopeGuard 拦截？                 → general / source=rule
② 风险信号词命中？                   → 交回 LLM（保 risk 判定，G1）
③ 一次算清三类命中：二级场景词 / 售后兜底词 / 售前词
   ├─ （二级 或 兜底）且 售前 同时命中 → 交回 LLM（多意图，G3）
   ├─ 二级场景命中多个                → 交回 LLM（G4）
   ├─ 命中唯一二级场景                → aftersale / <该二级> / source=rule
   ├─ 未命中二级、但有兜底词          → aftersale / other / source=rule
   ├─ 仅售前命中                      → presale / none / source=rule
   └─ 都未命中                        → 转 ④
④ 整句等于闲聊词？                   → general / none / source=rule
⑤ 都不满足                           → 交回 LLM（G2）
```

> **顺序要点**：售后兜底词必须**参与** ③ 的多意图判定，不能排在售前之后裁决——否则"这个多少钱，坏了怎么办"会被短路成 `presale`。此点在原型中已实测修正（详见 §6.1 实现注意 2）。

---

## 5. 规则表设计（实测版，可直接落地）

### 5.1 风险信号词 `RISK_SIGNALS`

命中即**拒绝短路**（不是判风险，是交给 LLM 判）。

```python
RISK_SIGNALS = [
    # 投诉升级（投诉语义一律走顶层 complaint，D1）
    "投诉", "举报", "曝光", "告你们", "315",
    # 违规咨询
    "改装", "破解", "解除限速", "越狱",
    # 高风险操作（要求执行资金/权限动作）
    "打钱", "直接退钱", "直接退款", "退我钱", "强制", "私自",
]
```

> 维护口径：**宁可多收**。多收一个词的代价只是少省一次 LLM 调用；漏一个词就漏一次拦截。但注意——漏了也只是退回改造前行为（全走 LLM），**不会比现在更差**。

### 5.2 售后二级场景词表 `AFTERSALE_RULES`（有序，先精确后宽泛）

```python
AFTERSALE_RULES = [
    ("return_refund", ["退货", "退款", "退钱", "退了", "退掉", "不要了",
                       "无理由退", "申请退", "退单", "取消订单"]),
    ("exchange",      ["换货", "换一个", "换个", "换新", "换一款", "换成",
                       "想换", "以旧换新"]),
    ("reship",        ["补发", "少发", "漏发", "缺件", "少了一件", "少东西",
                       "没发", "少给"]),
    ("logistics_query", ["物流", "快递", "发货", "什么时候到", "到哪了", "到哪",
                         "签收", "派送", "运单", "配送", "几天到", "送到了吗",
                         "我的包裹"]),
    ("order_query",   ["订单状态", "查订单", "查下订单", "我的订单", "订单号",
                       "订单记录", "订单进度"]),
]
```

> **`order_query` 只收搭配词，不收裸词"订单"**——实测：裸词"订单"会与退货/物流词频繁撞车（"我要退掉这个订单"→ 被"订单"撞成二级多命中，白白交回 LLM）。收窄后留出集命中 +1 条（§3.3）。代价是"订单怎么了"这类裸订单问句交回 LLM，可接受。
>
> **已知撞车（有意保留）**："我的订单到哪了"同时命中 `order_query`（"我的订单"）与 `logistics_query`（"到哪了"）→ 二级多命中 → 交回 LLM。这类句子确实是订单+物流双重诉求，交回 LLM 判定更稳，不算损失。

### 5.3 售后兜底词 `AFTERSALE_GATE`（命中 → `aftersale` / `other`）

```python
AFTERSALE_GATE = [
    "坏了", "损坏", "故障", "质量问题", "售后", "退换", "维修", "返修",
    "三包", "过保", "价保",
]
```

命中兜底词说明**确定是售后**但归不出细类，此时给 `other` 而不是交回 LLM——这是实测（§3.3）的净收益项，+4pp 覆盖且零误判。

> **裁决顺序**：四个具体子场景词表（含 `order_query`）先于兜底词表裁决。`"这个灯坏了要退货"` 应归 `return_refund`（具体诉求）而非 `other`（兜底）。

### 5.4 售前词表 `PRESALE_RULES`

```python
PRESALE_RULES = [
    "多少钱", "价格", "价钱", "优惠", "活动", "折扣", "促销",
    "推荐", "哪款", "哪个好", "参数", "规格", "尺寸", "型号",
    "怎么安装", "有货", "库存", "现货", "有卖",
    "质保", "续航", "耗电", "材质",
]
```

> **`保修` 有意不收，仅收 `质保`**（用户 2026-09-26 决策）。这两个词有语义歧义——"这个锁质保几年"（问政策，知识库可答，presale）vs "我要保修"（售后诉求）；"保修"的动词用法更重，故只保留名词性更强的"质保"。
> 代价（实测）：`"还在保修期吗"` 命中不了 → 交回 LLM。这是**预期结果**，不是缺陷。

### 5.5 闲聊词表 `GENERAL_WHOLE`（**必须整句匹配**）

```python
GENERAL_WHOLE = {"谢谢", "好的", "好", "收到", "知道了", "明白", "你好", "您好",
                 "再见", "拜拜", "ok", "嗯嗯", "哈哈", "谢谢啦", "多谢", "感谢"}

_PUNCT = re.compile(r"[\s，。！？!?,.~～…、；;：:\"'“”‘’()（）]")
# 命中条件：_PUNCT.sub("", query) in GENERAL_WHOLE
```

**必须整句匹配，不能用子串包含**。反例："谢谢，那我退货怎么办" 含"谢谢"，若按子串会短路成 general，实际是 aftersale。

**有意不收**：`在吗`、`嗯`、`那个` —— golden set 期望这三条是 **clarify** 而非 general（`eval_intent_golden.py:68,80,84`），规则层不判 clarify（D3），收了反而制造误判。

### 5.6 词表配置形态

**代码内常量**，与既有 `ScopeGuard`（`scope_guard.py:23-37`）同风格。理由：词表是判定逻辑的一部分，改动需过评测（§8）；外置 JSON 会引入加载/校验/热更新三样新东西，当前规模（约 70 词）不值。

---

## 6. 代码改动清单

### 6.1 新增 `llm_backend/app/lg_agent/intent_rules.py`

```python
"""意图识别规则判定层：明确意图零延迟直接判定，未命中降级 LLM。

设计原则（实测校准，见 SPEC_INTENT_RULE_LAYER.md §3.3）：
    词表只收高辨识度业务词，泛词一律不收。
    宁可漏——漏了交回 LLM，行为等同改造前；不可错——错了短路走错分支，是真实回归。
    例外：风险信号词宁可多收（它只拒绝短路，不判风险，多收永不致错）。
"""
import re
from typing import Literal, Optional

from app.core.logger import get_logger

logger = get_logger(service="intent_rules")

AftersaleSubType = Literal[
    "logistics_query", "return_refund", "exchange", "reship", "other", "none"
]

RISK_SIGNALS = [...]            # §5.1
AFTERSALE_RULES = [...]         # §5.2
AFTERSALE_GATE = [...]          # §5.3
PRESALE_RULES = [...]           # §5.4
GENERAL_WHOLE = {...}           # §5.5
_PUNCT = re.compile(...)


def classify_by_rules(query: str) -> Optional[tuple[str, AftersaleSubType, str]]:
    """规则判定。命中返回 (type, sub_type, reason)，未命中返回 None（降级 LLM）。

    调用前须已过 ScopeGuard（经营范围预检在 lg_builder.py 里独立前置）。
    """
    if not query:
        return None

    # G1 风险信号闸门：命中不短路，交回 LLM 判 risk
    for w in RISK_SIGNALS:
        if w in query:
            logger.info("规则层让行: 风险信号[{}] → 交 LLM", w)
            return None

    # 三类词一次算清（下面按优先级裁决）
    subs = []
    for sub, words in AFTERSALE_RULES:
        hit = next((w for w in words if w in query), None)
        if hit:
            subs.append((sub, hit))                    # 按 AFTERSALE_RULES 顺序，先精确后宽泛
    gate = next((w for w in AFTERSALE_GATE if w in query), None)
    pre = next((w for w in PRESALE_RULES if w in query), None)
    aftersale_hit = bool(subs) or bool(gate)

    # G3 多意图闸门：售后（含兜底词）与售前同时命中 → 交 LLM
    #     ⚠️ 兜底词必须参与此判定，否则"这个多少钱，坏了怎么办"会被误判成 presale
    if aftersale_hit and pre:
        logger.info("规则层让行: 多意图[{}+{}] → 交 LLM",
                    subs[0][0] if subs else gate, pre)
        return None

    # G4 二级场景必须唯一
    if len({s for s, _ in subs}) > 1:
        logger.info("规则层让行: 二级场景多命中{} → 交 LLM", [s for s, _ in subs])
        return None

    if subs:
        return "aftersale", subs[0][0], f"售后词[{subs[0][1]}]→{subs[0][0]}"
    if gate:
        return "aftersale", "other", f"售后兜底词[{gate}]→other"
    if pre:
        return "presale", "none", f"售前词[{pre}]"

    if _PUNCT.sub("", query) in GENERAL_WHOLE:
        return "general", "none", "闲聊整句匹配"

    return None   # 未命中 → 降级 LLM
```

> **实现注意（三处易错点）**：
> 1. `subs` 须按 `AFTERSALE_RULES` 的列表顺序取**每个子场景第一个**命中的词，保证"退货运费谁承担"稳定归 `return_refund` 而不被后续类目抢走。
> 2. `AFTERSALE_GATE` 必须在售前之前参与**多意图判定**——若把兜底词放在售前之后裁决，"这个多少钱，坏了怎么办"会短路成 presale（实测：修正前会错，修正后正确让行）。
> 3. `aftersale` 的四个子场景词表须先于兜底词表裁决：`"这个灯坏了要退货"` 应归 `return_refund` 而非 `other`。

### 6.2 改 `llm_backend/app/lg_agent/lg_states.py`

```python
class Router(TypedDict):
    """Classify user query: scenario + aftersale sub-scenario + risk."""
    logic: str
    type: Literal[
        "presale", "aftersale", "complaint", "general", "image", "clarify",
    ]
    # 售后二级场景（2026-09-26 恢复该维度，推翻 2026-08-27 决策 #13）——
    # 识别层只回答"用户说的是哪类诉求"（纯语义分类），
    # "查订单/算差价/发起退货"等执行动作仍由售后 Agent 负责。
    sub_type: Literal[
        "logistics_query",  # 物流查询
        "return_refund",    # 退货退款
        "exchange",         # 换货
        "reship",           # 补发
        "order_query",      # 订单查询
        "other",            # 其他/兜底
        "none",             # 非 aftersale 时必须为 none
    ]
    risk: Literal["none", "violation", "high_risk"]
    source: Literal["rule", "llm"]   # 判定来源：规则层短路 / LLM 识别


# AgentState.router 默认值（:71）同步补两字段
router: Router = field(default_factory=lambda: Router(
    type="general", sub_type="none", risk="none", logic="", source="llm"))
```

**必改原因**：`Router` 是 `TypedDict`，`AgentState.router` 的 `default_factory`（`lg_states.py:71`）构造时不补字段，下游 `state.router["sub_type"]` 会 KeyError。

**`source` 字段用途**：日志打点 + 评测统计规则命中率 + 线上排查"这轮是规则判的还是 LLM 判的"。不加则无法评估规则层效果，也无法定位误判来源。

### 6.3 改 `llm_backend/app/lg_agent/lg_prompts.py`

`ROUTER_SYSTEM_PROMPT` 完整修订文本（第 1 段与新增 `sub_type` 章节；其余章节不动）：

```
你是一个电商智能客服的意图识别引擎。你的任务是对用户询问同时判断三个维度：
场景类型（type）、售后二级场景（sub_type）、风险意图（risk）。

## type 场景分类
（原文不动）

## sub_type 售后二级场景（仅当 type=aftersale 时给出，否则必须为 none）
- logistics_query 物流查询：问包裹/快递/发货/配送/签收
  （如"我的快递到哪了""什么时候发货""快递一直不动"）
- return_refund 退货退款：要退货、要退款、问退货流程与运费
  （如"我要退货""怎么申请退款""退货运费谁承担"）
- exchange 换货：要换商品/型号/颜色（如"想换个颜色""能换个型号吗"）
- reship 补发：少发/漏发/缺件，要求补寄（如"少发了一件""能补发吗"）
- order_query 订单查询：查订单本身的状态/进度/订单号
  （如"查一下我的订单""我的订单状态是什么""订单号能帮我查一下吗"）
- other 其他/兜底：明确是售后但归不进上面任何一类（如"东西坏了"要求维修）

口径（必须遵守）：
1. type=aftersale 时禁止输出 none；type≠aftersale 时必须输出 none
2. 一句话同时涉及订单与物流（如"我的订单到哪了"）时，优先判 logistics_query
   （用户实际关心的是包裹位置）；只问订单本身、未提物流的判 order_query
3. 含投诉语义的一律 type=complaint、sub_type=none（纯情绪发泄与售后事由引发的投诉
   都归 complaint，二级场景仅为 aftersale 服务）

## risk 风险判断（独立于场景，优先级最高）
（原文不动）

## 分类准则
（原文不动）

仅输出符合 schema 的 JSON（type / sub_type / risk / logic 四个字段）。
```

**首段那句"售后子场景（退货退款/物流/订单查询）由售后处理环节判断"必须删除**（`lg_prompts.py:8`）——它是 §2.2 被推翻的决策的原文。

### 6.4 改 `llm_backend/app/lg_agent/lg_builder.py`

`analyze_and_route_query` 内插入规则层调用（在 ScopeGuard 之后、选模型之前）：

```python
    # ③ 经营范围预检（关键词级，零延迟）—— 既有，不动
    ...
    if not in_scope:
        return {"router": Router(type="general", sub_type="none", risk="none",
                                 logic=f"超出经营范围: {scope_reason}", source="rule")}

    # ④ 意图规则层（零延迟）：明确意图直接短路，未命中降级 LLM（见 SPEC_INTENT_RULE_LAYER）
    rule_hit = classify_by_rules(user_question)
    if rule_hit:
        r_type, r_sub, r_reason = rule_hit
        logger.info("意图规则层命中: type={} sub_type={} | {} | query: '{}'",
                    r_type, r_sub, r_reason, user_question)
        return {"router": Router(type=r_type, sub_type=r_sub, risk="none",
                                 logic=f"规则层判定：{r_reason}", source="rule")}

    # ⑤ LLM 识别层（既有路径）
    ...
```

LLM 分支两处 `Router(...)` 构造需补字段：

| 位置 | 现状 | 改后 |
|---|---|---|
| `lg_builder.py:69` | `Router(type="general", risk="none", logic=...)` | 补 `sub_type="none", source="rule"` |
| `lg_builder.py:102` | `Router(type="general", risk="none", logic="结构化输出失败降级")` | 补 `sub_type="none", source="llm"` |
| LLM 正常返回 | `response = cast(Router, await ...)` | 补 `source="llm"`；`sub_type` 按下方兜底表校正 |

**LLM 返回的 `sub_type` 需按跨字段一致性校正**（schema 的 `Literal` 只能约束取值集合，约束不了 `type` 与 `sub_type` 的搭配）：

| `type` | 模型给的 `sub_type` | 校正为 |
|---|---|---|
| aftersale | 五个合法值之一 | 原样保留 |
| aftersale | `none` 或非法值 | `other` |
| 非 aftersale | 任意 | `none` |

`route_query`（`lg_builder.py:106`）**不改分支逻辑**（D4：路由不变），仅在日志行追加 `sub_type`，便于线上观察。

### 6.5 改 `llm_backend/scripts/eval_intent_golden.py`

1. **期望结构扩为三维**：`{type, risk}` → `{type, risk, sub_type?}`（`sub_type` 可选，用例未标则不参与统计）
2. **新增留出集 `HELDOUT`**：把 §3.2 验证用的 35 条真实问句固化进脚本（防止词表在迭代中不知不觉过拟合 golden set）
3. **汇总加两个维度**：`sub_type` 准确率、**规则命中率**（`source=="rule"` 的占比）
4. **新增 `--rule-off` 开关**：跳过规则层跑纯 LLM，用于 A/B 对比（证明规则层未降低准确率）

### 6.6 新增 `llm_backend/tests/test_intent_rules.py`

规则层是纯函数（无 LLM / 无 DB / 无网络），必须补单测。当前 `route_query` 与 `analyze_and_route_query` **无任何 pytest 覆盖**（仅 `test_stream_filter.py` 间接引用），本单测是第一份。

---

## 7. 边界情况处理表

| # | 输入 | 规则层 | 最终判定（走谁） | 说明 |
|---|---|---|---|---|
| 1 | "我要退货" | 命中 | `aftersale/return_refund`（**规则**） | 零 LLM 调用 |
| 2 | "这个锁质保几年" | 命中 `质保` | `presale/none`（**规则**） | 零 LLM 调用 |
| 2b | "还在保修期吗" | 未命中 | `presale/none`（LLM） | "保修"有意不收（§5.4），交回 LLM |
| 2c | "查一下我的订单" | 命中 | `aftersale/order_query`（**规则**） | 零 LLM 调用 |
| 2d | "我的订单到哪了" | G4 让行（订单+物流撞车） | `aftersale/logistics_query`（LLM） | 二级多命中交回 LLM（§5.2） |
| 3 | "我要投诉" | G1 让行 | `complaint/high_risk`（LLM） | 投诉走顶层（D1），risk 由 LLM 判 |
| 4 | "直接给我退款打钱" | G1 让行（"打钱"） | `aftersale/high_risk`（LLM） | **改造前是这条，改造后必须仍是** |
| 5 | "这灯多少钱？另外怎么退货？" | G3 让行 | `presale/none`（LLM） | 多意图，句首发主导是语义任务 |
| 6 | "退货运费谁承担" | 命中 | `aftersale/return_refund`（**规则**） | 运费属退货政策，非物流 |
| 7 | "运费谁出？坏了多久能换？" | G4 让行（二级多命中） | `aftersale/other`（LLM） | 交回 LLM 反而能得到更准的二级 |
| 8 | "东西坏了" | 命中兜底词 | `aftersale/other`（**规则**） | §5.3 净收益项 |
| 9 | "谢谢" | 命中整句 | `general/none`（**规则**） | 零 LLM 调用 |
| 10 | "谢谢，那我退货怎么办" | 命中 `退货` | `aftersale/return_refund`（**规则**） | 整句匹配保证不被"谢谢"吃掉 |
| 11 | "在吗" / "嗯…" / "那个呢" | 未命中 | `clarify/none`（LLM） | clarify 不在规则层范围（D3） |
| 12 | "有卖衣服吗" | ScopeGuard 拦截 | `general/none`（规则） | 既有行为，优先级最高 |
| 13 | "看这张图，怎么改装" | G1 让行（"改装"） | `image/violation`（LLM） | 图片分支在 `route_query` 里优先于 type |
| 14 | 空消息 | 未命中 | `general/none`（LLM） | 既有降级 |
| 15 | LLM 结构化输出失败 | — | `general/none`，`source=llm` | 既有降级，补 `sub_type="none"` |
| 16 | 带图消息，文本命中规则词（如图 + "我要退货"） | 命中 | `aftersale/return_refund`（**规则**） | 无行为变化：`route_query:134` 的 `hasattr(state,"config")` 图片分支是死代码（`AgentState` 无 `config` 字段，已记录于 `docs/superpowers/specs/2026-09-26-系统运行流程图-design.md:457`），带图时 type 本来就由 LLM 按同样的文本判定，结论一致 |

---

## 8. 验证方案

### 8.1 实施步骤与验证点

| # | 步骤 | 验证 |
|---|---|---|
| 1 | 写 `tests/test_intent_rules.py`（先写测试） | `pytest tests/test_intent_rules.py` 全绿；覆盖 §7 表全部 15 行 |
| 2 | 实现 `intent_rules.py` | 同上 |
| 3 | 改 `lg_states.py`（Router 加字段 + 默认值） | `python -c "from app.lg_agent.lg_states import AgentState; print(AgentState(messages=[]).router)"` 无 KeyError |
| 4 | 改 `lg_prompts.py`（prompt 加 sub_type） | 人工核对首段旧决策句已删 |
| 5 | 改 `lg_builder.py`（接入 + 补字段） | `python -m scripts.eval_intent_golden` **type/risk 仍 46/46** |
| 6 | 扩评测脚本（三维期望 + 留出集 + 命中率） | 见 8.2 |
| 7 | 全量回归 | `pytest llm_backend/tests/ -q` 无**新增**失败（既有失败项见 `docs/项目问题.md` #8） |
| 8 | 文档同步 | 见 §9 |

### 8.2 验收标准（硬指标）

| 指标 | 阈值 | 来源 |
|---|---|---|
| golden set `type` 准确率 | **= 100%（46/46）** | 回归红线，不得低于改造前 |
| golden set `risk` 准确率 | **= 100%（46/46）** | 同上 |
| 留出集规则层误判数 | **= 0** | §3.2 已达；实施后不得回退 |
| 留出集 `type` 准确率 | ≥ 改造前 | 需 `--rule-off` 跑一次对比 |
| 规则层命中率（留出集） | ≥ 55% | §3.2 实测 57%，实施后不得显著低于 |
| `pytest tests/test_intent_rules.py` | 全绿 | 纯本地，无外部依赖 |

### 8.3 端到端实测（可选，起服务）

选取 3 条做真实链路验证：

| 查询 | 预期 |
|---|---|
| "我要退货" | 日志出现 `意图规则层命中: type=aftersale sub_type=return_refund`，**无 LLM 调用日志** |
| "我要投诉" | 日志出现 `规则层让行: 风险信号[投诉]`，随后有 LLM 调用 |
| "谢谢" | 规则命中 general，零 LLM 调用 |

> ⚠️ 起服务须用 `uvicorn ... --loop asyncio`（Selector loop，见既有运维约束），**验证完必须 kill**，否则占用 127.0.0.1:8000 抢走 `run.py` 的请求日志。

---

## 9. 文档同步清单

| 文件 | 改动 |
|---|---|
| `llm_backend/app/lg_agent/lg_states.py:18-19` | 改写"售后子场景不再由识别层判断"注释 → 记录 2026-09-26 恢复及理由 |
| `llm_backend/app/lg_agent/lg_prompts.py:8` | 删除"售后子场景由售后处理环节判断" |
| `docs/项目问题.md` | 新增条目记录本次改造；核对 #5/#6（纯售后意图、参数级售后归类）当前状态 |
| `docs/PROJECT_ANALYSIS.md` | §4.3 双维识别 → 三维；§4.2 Router 字段表（`:481`）；§5.2 路由决策（`:855-880`） |
| `README.md` | `:21/:35/:44/:172` 意图识别描述同步 |
| 本 spec | 实施完成后按 CLAUDE.md §6 更新"归档状态"并 `git mv` 至 `docs/spec_plan/已完成/` |

### 9.1 图表资产：**本次不改，推迟整改**（用户 2026-09-26 决策）

`docs/diagrams/` 下有 `00a/00b/10/11/20` 五张流程图，其中 `10-presales-orchestration` 与 `00b-dialogue-dispatch` 的 spec JSON 含意图路由节点；另有 `30-intent-routing.json`（意图识别与路由决策图）用户正在别的会话中编写。

**决策：等用户的流程图优化完成后，再连同本次 `Router` 字段与判定层次的变化一并整改。** 本 spec 不包含图表改动，实施时**不要动 `docs/diagrams/` 下任何文件**。

> 遗留提示：`30-intent-routing.json` 与本次 spec 描述的是同一件事的两面（图 vs 文字）。用户完成图表后，两处口径需对齐一次——届时以本 spec §2.3 的 7 个 `sub_type` 取值与 §5 词表为准。

---

## 10. 风险与已知局限

| # | 项 | 说明 |
|---|---|---|
| R1 | **规则误判是新增故障面** | 改造前所有 query 都过 LLM，判错概率由 LLM 决定；改造后 37~57% 的 query 由词表决定。缓解：判据"宁可漏不可错"+ 留出集零误判 + 单测覆盖边界 |
| R2 | 词表随业务上新需维护 | 新品类/新售后话术出现时词表可能漏收 → 只是回退到 LLM，非故障。**但无自动化发现机制**，需靠日志中 `source=rule` 占比变化人工观察 |
| R3 | 规则层不判 risk（D2） | 含风险信号词的消息仍走 LLM，这部分**没有延迟收益**。这是有意的安全取舍 |
| R4 | 二级场景准确率未知 | 规则层二级场景已实测；**LLM 层的二级场景准确率未实测**（本次未构造二级 golden 期望）。实施时需在 golden set 补二级期望后测出 |
| R5 | 规则层短路跳过 `MemoryManager` | 规则层不看历史（多轮指代必须靠 LLM，未命中即降级）。短路省掉一次 `MemoryManager.manage()` + Redis 读写——**这是额外收益**，但需确认下游节点（`respond_to_general_query` 等）自会调 manage（已确认：`lg_builder.py:186-192`） |
| R6 | golden set 中 `sub_type` 期望尚不存在 | 现有 46 条只标了 `{type, risk}`，所以**规则层给出的二级场景在评测里暂无对照**，只能靠单测（§6.6）与留出集人工核对。补齐二级期望是实施时的必做项（§6.5 第 1 条） |
