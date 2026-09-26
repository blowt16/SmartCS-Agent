# SPEC：多候选指代 —— 不擅自选定，改为带信息追问

> 归档状态：⏳ 待实施（2026-09-26 编写，待用户审核）
> 关联：`docs/项目问题.md` #21（本 spec 解决的问题）、#20（消解防泄漏，已修）、`SPEC_ENTRY_LLM_RESOLUTION`（入口统一消解）
> 上游观测：`docs/spec_plan/已完成/SPEC_INTENT_RULE_LAYER.md` §11（端到端多轮首次暴露）

---

## 1. 问题定性

**这不是 bug，是"指代本身真的有歧义"。**

助手一次列出多款商品后（实测场景）：

```
用户: 你们有智能门锁吗
助手: 我们这边有智能门锁的哦～给您整理了几款在售的：
      · 小米智能门锁2 指静脉版：¥1099.00（有货）
      · 鹿客 S50F：¥1299.00（有货）
      · 小米智能门锁 XMZNMS04LM：¥899.00（有货）
用户: 多少钱          ← "这款"指哪一款？三种理解都成立
```

系统现状：**消解器擅自选一个**。实测危害有三：

| 危害 | 证据（2026-09-26 端到端） |
|---|---|
| 答非所问 | 用户问"这款保修多久"，被消解成"8H 智能电动床保修多久"，回了电动床的保修政策 |
| **选择不稳定** | 同一条对话两次运行，分别选中"鹿客 T1 pro"与"小米智能门锁2 指静脉版" |
| 用户无从察觉 | 系统从不告知"我按 X 理解"，猜错了用户也不知道 |

**为什么不能简单禁止助手列多商品**：导购场景下列多款是**正常且必要**的（用户问"推荐几款"就是要多款）。矛盾必须在**指代环节**化解，不能靠限制助手输出。

---

## 2. 方案选型

| 方案 | 结论 | 理由 |
|---|---|---|
| **① 追问 + 候选带信息** | ✅ **采纳（用户确认）** | 不猜错；且助手上一条回复里**本来就有**这些商品的价格/型号，追问时直接搬运，用户即使不回答也已拿到信息——不白等一轮 |
| ② 纯追问（只列候选名） | 否 | 用户还得再问一次价格，多一轮且没给信息 |
| ③ 告知式（继续猜 + "我按 X 理解"） | 否 | 只把不确定性摆上台面，没解决；且实测"猜"本身不稳定 |
| ④ 编号式（助手列商品带序号） | 暂缓 | 治本但只覆盖"第 N 个"的说法（"那个小米的"仍歧义）；与 ① 不冲突，可后续叠加 |

**关键设计依据**：候选的关键信息（型号/价格/库存）**已存在于助手的上一条回复中**，澄清节点拿到的是完整 history（经 `MemoryManager`），因此**不需要为候选重新检索**——只做话术组织。这让 ① 的成本远低于"对每个候选各查一次 RAG"。

---

## 3. 架构设计

```
main.py 入口（复刻现有顺序）
  │
  ├─ _is_filler 闸门 ──────────────┐
  ├─ resolve_pronouns_ex(...)      │  ← 改为调用 _ex 版本，一次 LLM 调用
  │     └─ 返回 ResolveResult       │
  │        { query, candidates[] }  │
  │                                 │
  └─ InputState(messages=query, ref_candidates=candidates)   ← 新增字段
        │
        ▼
   graph.astream(...)
        │
        ▼
   analyze_and_route_query（规则层 → LLM，均不变）
        │
        ▼
   route_query 优先级：
        ① risk=violation     → risk_intercept      （安全最高，不因多候选改道）
        ② risk=high_risk     → transfer_human
        ③ ref_candidates ≥2  → clarify_node        【新增】
        ④ 图片               → create_image_query
        ⑤ type 分支          → （不变）
        │
        ▼
   clarify_node
        └─ 有候选时：追加「候选确认」段，列出候选 + 助手上一条已给的信息
```

**为什么复用 clarify_node 而不新建节点**：两者都是"系统没把握 → 问用户"，澄清节点已有完整话术风格、降级路径与 history 管理；差异仅在 prompt 的一段补充说明。新建节点会重复这三样。

---

## 4. 详细设计

### 4.1 消解器：多候选检测（`app/services/pronoun_resolver.py`）

#### 4.1.1 返回契约

```python
@dataclass
class ResolveResult:
    query: str                                  # 消解后文本；歧义时 = 原消息（不擅自选定）
    candidates: list[str] = field(default_factory=list)   # 指代的多个同等候选；空 = 无歧义
```

#### 4.1.2 新增 `resolve_pronouns_ex`，旧函数变薄包装

```python
async def resolve_pronouns_ex(llm_service, messages, raw_query) -> ResolveResult:
    """消解主入口（结构化版）。失败/超时/空一律降级 ResolveResult(query=raw_query, candidates=[])"""
    ...

async def resolve_pronouns(llm_service, messages, raw_query) -> str:
    """兼容包装：只取 query。既有 3 个生产调用点无需改动。"""
    return (await resolve_pronouns_ex(llm_service, messages, raw_query)).query
```

**为什么新增而非改现有签名**：`resolve_pronouns` 有 3 个生产调用点（`main.py:297`、`redis_semantic_cache.py:148`、`evaluation/runner.py:47`）+ 多处测试。新增 `_ex` 版本让这些调用点**零改动**，只有 `main.py` 切到 `_ex`。

#### 4.1.3 输出格式：JSON + **纯文本向后兼容兜底**

prompt 末段由"只输出消解后的完整问题文本"改为：

```
输出 JSON（不要任何解释、不要 markdown 围栏）：
{"resolved": "消解后的完整问题", "candidates": ["候选1", "候选2"]}
```

解析函数：

```python
def _parse_resolve_output(text: str, raw_query: str) -> ResolveResult:
    """解析消解输出。

    兼容纯文本：模型未按 JSON 输出（或旧格式）时，整体作为 query、candidates 为空
    —— 保证既有消解行为在任何解析失败下都不劣化。
    """
```

解析须处理：① ```json 围栏；② candidates 元素非字符串/空串 → 过滤；③ `resolved` 缺失或为空 → 回落 `raw_query`；④ 任何异常 → 整体纯文本兼容。

> ⚠️ **不要新增必填占位符**：本项目刚因 `CLARIFY_SYSTEM_PROMPT` 的 `{question}` 缺参、`str.format` 抛 KeyError 被吞而静默降级的事故（`项目问题.md` #22）排查过同类问题。本节的 JSON 输出格式**直接写死在 prompt 常量文本里**，不经 `str.format` 注入；§4.4 的候选段同理走**独立常量 + 字符串拼接**。

#### 4.1.4 prompt 新增规则 10

在现有规则 1–9（含 2026-09-26 新增的防助手话术泄漏 6/7/8）之后追加：

```
10. **多候选检测**：如果用户当前问题使用了指代词或省略，而上文中有**多个同等合理**的候选
    对象（典型场景：助手上一条回复里列了多款商品），无法唯一确定指代目标——**不要擅自
    选定一个**，把这些候选放入 candidates；其余情况 candidates 必须为空数组。
    · 候选必须是**助手回复里确实出现过的具体对象**（商品名/型号），不得编造、不得臆测
    · 用户已指明目标时（如"小米那款保修多久"），candidates 为空，正常消解
    · 有多候选歧义时，resolved 直接**原样返回用户消息**（不锁定任何对象），
      下游会据此反问用户，不会用 resolved 去检索
```

**与规则 6 的关系**：规则 6 允许"用户没说时，从助手回复里取商品实体"——那是**候选唯一**时的正常消解。规则 10 是**候选多个**时的例外出口。两者不冲突，规则 10 在检测到多候选时优先。

### 4.2 状态传递（`app/lg_agent/lg_states.py`）

```python
@dataclass(kw_only=True)
class InputState:
    messages: Annotated[list[AnyMessage], add_messages]
    # 多候选指代：入口消解检测到用户指代有 ≥2 个同等候选时填入（SPEC_MULTI_CANDIDATE_REFERENCE）
    # 空 = 无歧义。AgentState 继承本字段，route_query 与 clarify_node 均读取。
    ref_candidates: list[str] = field(default_factory=list)
```

**为什么加在 `InputState` 而非 `AgentState`**：图构建为 `StateGraph(AgentState, input=InputState)`（`lg_builder.py:613`），入口只能传 `InputState` 声明的字段；`AgentState` 继承自它，故加在此处一处生效。

**旧 checkpoint 兼容**：`route_query`/`clarify_node` 读取时用 `getattr(state, "ref_candidates", [])` 或 `.get` 风格兜底，避免续聊改造前的会话时 `AttributeError`（同 `sub_type` 的处理，见 `SPEC_INTENT_RULE_LAYER` §11.1 D-c）。

### 4.3 路由分支（`lg_builder.py` `route_query`）

在 `risk` 两级之后、图片分支之前插入：

```python
    # 多候选指代：消解阶段检测到用户指代有 ≥2 个同等候选 → 不猜，问用户
    # 位置在 risk 之后：违规/高风险消息不因指代歧义改道（安全优先）
    candidates = getattr(state, "ref_candidates", None) or []
    if len(candidates) >= 2:
        logger.info("意图路由: 多候选指代({} 个: {}) → 节点=clarify_node | query: '{}'",
                    len(candidates), "、".join(candidates[:3]), query)
        return "clarify_node"
```

**优先级理由**：安全（risk）> 不猜（多候选）> 图片 > 场景 type。

### 4.4 澄清话术（`lg_prompts.py` + `lg_builder.py` `clarify_node`）

**不修改 `CLARIFY_SYSTEM_PROMPT` 的占位符**（避免重蹈 #22），新增独立常量按需拼接：

```python
# 多候选指代的追加说明（仅在 clarify_node 拿到 ref_candidates 时追加；
# 独立常量 + 字符串拼接，不动 CLARIFY_SYSTEM_PROMPT 的 {logic}/{question} 占位符）
CLARIFY_MULTI_CANDIDATE_SECTION = """
【本轮特殊情况：用户用了指代，但上文有多个同等候选，无法确定指哪一款】
候选对象：
{candidates}

请按以下要求生成澄清询问：
1. 先表明需要确认是哪一款，再把候选**逐条列出并带序号**（1️⃣ 2️⃣ 3️⃣…），方便用户直接回复序号
2. 每一条候选**带上你上一条回复里已经给出的关键信息**（型号、价格等）——
   直接引用上文，**不要重新检索、不要编造**；上一条没给的信息就不要写
3. 一次只问这一个问题，语气友好（亲～/😊），不要复述本段说明
"""
```

`clarify_node` 中的拼接（**在 `.format()` 之后追加**，不参与格式化）：

```python
system_prompt = CLARIFY_SYSTEM_PROMPT.format(
    logic=state.router["logic"], question=question
)
candidates = getattr(state, "ref_candidates", None) or []
if len(candidates) >= 2:
    system_prompt += CLARIFY_MULTI_CANDIDATE_SECTION.format(
        candidates="\n".join(f"- {c}" for c in candidates)
    )
```

**预期效果**：

```
亲～您问的是哪一款呢？😊 帮您列一下刚才提到的三款：

1️⃣ 小米智能门锁2 指静脉版 —— ¥1099.00（有货）
2️⃣ 鹿客 S50F —— ¥1299.00（有货）
3️⃣ 小米智能门锁 XMZNMS04LM —— ¥899.00（有货）

回复序号或型号都可以哦～
```

---

## 5. 边界情况处理表

| # | 场景 | 期望行为 |
|---|---|---|
| 1 | 助手列 3 款，用户"多少钱" | `candidates=[A,B,C]` → clarify → 列出 3 款及其价格 |
| 2 | 助手列 1 款，用户"多少钱" | 候选唯一 → `candidates=[]` → 正常消解 → presale（**行为不变**） |
| 3 | 助手列 3 款，用户"小米那款保修多久" | 用户已指明 → `candidates=[]` → 正常消解 |
| 4 | 用户消息本身完整、无指代 | `candidates=[]`（不触发） |
| 5 | 多候选 + `risk=violation/high_risk` | **risk 优先**，不因多候选改道（安全优先） |
| 6 | 消解超时/异常/返回空 | 降级 `ResolveResult(query=raw_query, candidates=[])`，**不触发**（保持现有降级语义） |
| 7 | 模型未按 JSON 输出 | 整体作为 `query`，`candidates=[]`（向后兼容，行为同改造前） |
| 8 | 候选 ≥2，但助手上一条没给价格 | 只列候选名，不编造信息 |
| 9 | 首条消息（无历史） | 不消解 → `candidates=[]` |
| 10 | 纯语气词（`_is_filler`） | 跳过消解 → `candidates=[]` |
| 11 | 续聊改造前落盘的 checkpoint | `getattr` 兜底取空列表，不抛 `AttributeError` |
| 12 | 候选 ≥2 但用户消息是售后/投诉等 | 仍走 clarify（指代不明与场景无关）；`ref_candidates` 与 `type` 判定互不影响，但**路由以候选优先**——因为消解未锁定对象，任何场景判定都建立在错误的 query 上 |
| 13 | **澄清一轮后用户仍不指明对象**（如回"能便宜点吗"） | **仍会再次触发澄清**（该句是省略主语，规则 10 同样覆盖）。这是**已知局限**，与"不做澄清上限"的既有决策一致（`项目问题.md` #22）；第二轮的澄清话术因带上候选列表而**不与第一轮雷同**，用户至少知道系统卡在哪 |

---

## 6. 改动文件清单

| 文件 | 改动 |
|---|---|
| `app/services/pronoun_resolver.py` | 新增 `ResolveResult`、`resolve_pronouns_ex`、`_parse_resolve_output`；`resolve_pronouns` 改薄包装；prompt 加规则 10 与新输出格式 |
| `app/lg_agent/lg_states.py` | `InputState` 加 `ref_candidates` 字段 |
| `app/lg_agent/lg_prompts.py` | 新增 `CLARIFY_MULTI_CANDIDATE_SECTION` |
| `app/lg_agent/lg_builder.py` | `route_query` 加多候选分支；`clarify_node` 追加候选段 |
| `main.py` | 改用 `resolve_pronouns_ex`，把 `candidates` 传入 `InputState` |
| `tests/test_multi_candidate_reference.py` | **新增**：解析器边界 + 路由分支优先级 + clarify 候选段 |
| `scripts/e2e_multi_turn.py` | **新增（固化）**：端到端多轮脚本。此前仅存在于临时目录，但它已接连挖出 `项目问题.md` #20/#21/#22 三个缺陷——**节点级评测全绿而它一跑就现形**。固化为可复跑资产，纳入本 spec 的验收手段（§7.3） |
| `app/test/test_pronoun_resolve.py` | 扩充：真实 LLM 的多候选检测用例（含"用户已指明"的反例） |
| `docs/项目问题.md` | #21 状态更新为已修复 + 验证记录 |

**不改**：`redis_semantic_cache.py`、`evaluation/runner.py`（经薄包装零改动）；`ROUTER_SYSTEM_PROMPT`、规则层词表（与指代无关）。

---

## 7. 测试与验收

### 7.1 单元测试（桩模型，不调 LLM）

| 组 | 断言 |
|---|---|
| 解析器 | JSON 正常 / ```json 围栏 / 纯文本兼容 / `resolved` 缺失 / `candidates` 含非字符串 / 畸形 JSON 全不抛异常 |
| 解析器 | 多候选时 `query == raw_query`（不擅自选定） |
| `route_query` | `candidates≥2` → `clarify_node`；`candidates` 为空/缺失 → **不触发**（走原 type 分支） |
| `route_query` | `risk=violation` + `candidates≥2` → **仍走 risk_intercept**（安全优先） |
| `clarify_node` | 有候选时 system prompt 含候选与序号要求；无候选时**不含**候选段（不污染既有话术） |
| 回归 | 构造 `AgentState` 时**不传** `ref_candidates` → 所有既有路径正常（旧 checkpoint 兼容） |

### 7.2 真实 LLM 测试（扩充 `app/test/test_pronoun_resolve.py`）

| 用例 | 历史 | 当前消息 | 期望 |
|---|---|---|---|
| M-1 多候选 | 助手列 3 款门锁（含价格） | "多少钱" | `candidates` 长度 3，且均出现在助手回复中 |
| M-2 候选唯一 | 助手只列 1 款 | "多少钱" | `candidates == []`，`query` 含该型号 |
| M-3 用户已指明 | 助手列 3 款 | "小米那款保修多久" | `candidates == []`，`query` 含"小米" |
| M-4 无指代 | 助手列 3 款 | "你们支持七天无理由吗" | `candidates == []` |
| **R 回归** | — | #20 的 A-1~A-4 防泄漏 + R-1/R-2 消解能力 | **必须全过**（prompt 改动不得回退） |

### 7.3 端到端验收

复跑 `e2e_multi.py` 的对话1（助手列多款门锁 → "多少钱"）与对话2（推荐扫地机器人 → "这款保修多久"）：

| 验收点 | 判据 |
|---|---|
| 对话1 T2 | 从"消解成某个具体型号"变为**列出候选追问**；回答含 ≥2 款型号及其价格 |
| 对话2 T2 | 同上（问题 B 的原始复现场景） |
| 对话1 T3「能便宜点吗」 | **仍会再触发一次澄清**（该句是省略主语，规则 10 覆盖）——这是预期的已知局限（§5 #13），不是缺陷。验收点在于**第二轮的澄清话术不与第一轮雷同、且仍带候选与信息** |
| 对话3（情绪升级） | 逐轮行为与改造前一致（本轮不含指代，不应受影响） |
| 日志 | 出现 `意图路由: 多候选指代(N 个: ...)` |

> ⚠️ **本项验收不要写成"T3 应正常消解、不再追问"**——那是我初稿里的想当然。只要用户始终不指明对象，澄清就会持续触发，这正是 §8 R1（触发频率）风险的具体形态。真实流量下若观测到该循环过长，再评估"澄清 N 轮后降级"策略（与 `项目问题.md` #22 同源）。

### 7.4 硬指标

| 指标 | 阈值 |
|---|---|
| `pytest llm_backend/tests/ -q` | 无**新增**失败（既有 #8 BM25 + #19 flaky 除外） |
| `app/test/test_pronoun_resolve.py` | 通过数 ≥ 现有 48，失败数**不超过**现有 11（均为既有缓存层失败） |
| #20 防泄漏用例 | **100% 通过**（本次改动直接动了同一 prompt） |
| 规则层/意图识别 | **完全不受影响**（未改 `intent_rules.py`、`ROUTER_SYSTEM_PROMPT`）；golden set 46/46 应保持 |

---

## 8. 风险与已知局限

| # | 风险 | 说明与缓解 |
|---|---|---|
| R1 | **触发频率可能过高** | 助手列多商品很常见，"列多款 → 用户用指代"就意味着反问。缓解：仅在 `candidates ≥ 2` **且**用户确实用了指代时触发（规则 10 已限定）；用户指明型号时不打扰。**但真实频率当前无法判断**——只有端到端一组样本，需上线后由日志观测（`意图路由: 多候选指代` 的出现比例） |
| R2 | **prompt 改 JSON 输出可能劣化既有消解质量** | 缓解：纯文本向后兼容兜底（解析失败=整体作为 query，行为同改造前）+ #20 的 6 项防泄漏/回归用例作为硬门槛 |
| R3 | 候选可能不完整或编造 | 缓解：prompt 明确限定"必须是助手回复里确实出现过的对象"；澄清话术要求"不重新检索、不编造"，只引用上文已有信息 |
| R4 | 澄清回答被语义缓存命中 | 澄清话术依赖上下文（用户原话 + 候选），被缓存复用会答非所问。当前 `.env` `SEMANTIC_CACHE_ENABLED=false`，缓存链路关闭；**缓存开关恢复前须评估**（另见 `项目问题.md` #11/#12） |
| R5 | **澄清可能连续触发**（主要局限） | `ref_candidates` 是**单轮状态、不跨轮保留**，故不会"记着旧候选追问"；但只要用户新一轮**仍不指明对象**（"能便宜点吗""那个长的"），规则 10 会重新检测到多候选并再次澄清。用户越迟疑、轮次越多。缓解：澄清话术带上候选与信息，用户看到具体选项后指明对象的概率高于面对泛问；且每轮话术不雷同。**根治需"澄清轮次上限"，与 `项目问题.md` #22 同源，用户已决策暂不做**。上线后由日志中该分支的出现比例判断是否需要 |
