# 意图识别模块优化实施规格（MVP）

> **用途**: 将现有 4 类技术路由（general/additional/graphrag/image）重构为**场景驱动路由**——单次合并输出「场景意图 + 风险意图 + 技术路由」，为后续业务子 agent（售前/售后/投诉安抚）预留路由接口；参考福客AI（FreeCall AI）项目分析报告 §9 意图识别方案（场景/来源/风险三维度）与 §16.3 对我们的项目启示
> **技术栈**: LangGraph 0.3.25（主图 StateGraph）+ DeepSeek/Ollama（ROUTER_TEMPERATURE=0 低温结构化输出）+ PostgreSQL（PostgresSaver 会话检查点）
> **状态**: 待实施（2026-08-27 设计定稿，经用户逐项确认：范围 / 合并输出 / 场景驱动路由 / additional-query 取消 / 占位节点行为）
> **关联文档**: [[SPEC_RAGAS_EVAL.md]]（现有评测口径，不受本改动影响）[[PROJECT_ANALYSIS.md]]（项目全景）

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题分析](#2-现状链路与问题分析)
3. [方案选型决策记录](#3-方案选型决策记录)
4. [目标架构](#4-目标架构)
5. [风险意图定义](#5-风险意图定义)
6. [Prompt 设计草案](#6-prompt-设计草案)
7. [节点改动清单](#7-节点改动清单)
8. [业务 agent 接口预留](#8-业务-agent-接口预留)
9. [边界情况处理表](#9-边界情况处理表)
10. [影响面分析](#10-影响面分析)
11. [实施步骤](#11-实施步骤)
12. [验证方案](#12-验证方案)
13. [决策记录](#13-决策记录)
14. [风险与避坑清单](#14-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 参考福客AI调研报告（2026-08-24 官方来源严格版）§9/§16.3：意图识别应包含**场景意图**（售前咨询/商品参数/价格活动/物流异常/退款退货/投诉安抚）、**来源意图**（渠道→口径）、**风险意图**（违规拦截/高风险转人工）三维度，且"意图识别不是孤立分类，而是结合上下文的情景判断"
2. 本项目现状是纯**技术路由**（general-query / additional-query / graphrag-query / image-query 四类，`lg_states.py:7`）：所有业务问题一锅端进 RAG 子图，无场景维度、无投诉安抚路径、无电商风险意图拦截（仅有经营范围关键词预检 `ScopeGuard`）
3. 用户已规划业务子 agent 架构（售前/售后/投诉安抚各自独立 agent，RAG 检索封装为 tool 供售后 agent 使用），本次优化为其**前置依赖**：先让路由具备场景识别能力，业务 agent 就位后仅改路由目的地

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 场景意图识别 | 售前 / 售后（退货退款、物流异常、订单查询）/ 投诉安抚，单次调用合并输出 |
| 风险意图识别 | violation（违规咨询拦截）/ high_risk（高风险操作转人工），与场景独立判断 |
| 场景驱动路由 | 路由分支按场景划分；售前复用现有 RAG 子图；售后/投诉安抚走占位节点（返回提示） |
| 业务 agent 接口预留 | 售后/投诉安抚占位节点接口与 multi_tool 子图同构（question+history→answer），后续仅改路由目的地 |
| 路由准确率可评测 | golden set（30 条）三维准确率（type / sub_scenario / risk）可复现输出 |

### 1.3 明确不做（MVP 边界）

- **来源意图**（直播间/短视频/搜索/店铺）：项目无多渠道接入，天然豁免，后续有渠道接入再做
- **业务子 agent 实现**（售前/售后/投诉安抚 agent）：后续独立 spec；本阶段只留路由目的地占位 + 接口形状
- **订单/售后业务系统**：无订单库，售后消息只能给政策话术/提示，无法真正查单
- **前端展示意图标签**：前端不展示 intent 信息，本次不动

---

## 2. 现状链路与问题分析

### 2.1 现状链路

```
用户消息 → 指代消解（main.py /api/langgraph/query）→ LangGraph 主图
   │
   ▼ analyze_and_route_query（lg_builder.py:55）
   ├─ ① ScopeGuard 关键词预检（经营范围，零延迟拦截：服装/理财/违规品类）
   ├─ ② MemoryManager 管理对话历史（摘要压缩 + Redis 增量缓存）
   └─ ③ LLM 低温结构化输出 Router{type, logic}（ROUTER_TEMPERATURE=0）
   ▼ route_query（lg_builder.py:108）
   ├─ general-query    → respond_to_general_query（纯 LLM 闲聊）
   ├─ additional-query → get_additional_info（guardrails 检查 + 追问）
   ├─ graphrag-query   → create_research_plan（RAG 子图：planner→检索→summarize→final）
   └─ image-query      → create_image_query（视觉模型）
```

### 2.2 问题分析

| # | 问题 | 现状证据 | 影响 |
|---|------|---------|------|
| P1 | **无业务场景维度** | Router 只有 4 类技术路由（`lg_states.py:7-10`）；商品咨询/价格/物流/退换/投诉全部进 graphrag-query 一个分支 | 投诉无安抚路径、售后无引导、场景无法驱动业务处理 |
| P2 | **无风险意图拦截** | 仅有经营范围关键词预检（`scope_guard.py`）；"改价/直接退款要求"、"违规改装"等无拦截 | 高风险消息与正常消息同路径处理，无防护 |
| P3 | **additional-query 是死胡同** | `get_additional_info` 追问订单号等（`lg_prompts.py:90-108`），但项目无订单系统，追问后无法闭环 | 追问白费一轮交互；且"信息不足 vs 知识库可答"边界模糊（prompt 中专门写了 `lg_prompts.py:22-24` 的补救条款，本身就是误判信号） |
| P4 | **识别结果不参与业务处理** | router.logic 只注入 general/additional 的回答 prompt，graphrag 路径完全不用 | 场景信息丢失，回答无场景话术差异化 |
| P5 | **无路由评测手段** | RAGAS 评测（SPEC_RAGAS_EVAL）只覆盖 graphrag-query 子图，直调子图绕过路由 | 路由改动无法量化验证 |

### 2.3 与福客AI报告差距对照

| 维度 | 福客AI（§9） | 本项目现状 | 本次改动 |
|------|-------------|-----------|---------|
| 场景意图 | 售前/参数/价格活动/物流异常/退款退货/投诉安抚六大场景 | ❌ 无 | ✅ 售前 / 售后（退换/物流/订单查询）/ 投诉安抚 |
| 风险意图 | 违规咨询明确拒绝（D5）、高风险转人工复核（D3/D4） | ⚠️ 仅经营范围关键词 | ✅ violation 拦截 / high_risk 转人工 |
| 来源意图 | 直播间/短视频/搜索/店铺→口径（F2） | ❌ 无 | ➖ 不做（无多渠道） |
| 上下文判断 | "结合订单、商品、用户、售后、历史对话判断真实意图" | 仅对话历史（MemoryManager 管理） | 保持现状（历史对话），订单/商品上下文随业务系统接入 |

---

## 3. 方案选型决策记录

### 3.1 场景落地方式：滤镜 vs 开关

| 候选 | 结论 | 理由 |
|---|---|---|
| **场景驱动路由（开关）** | ✅ 采纳（用户确认） | 用户已规划业务子 agent（售前/售后/投诉安抚），场景必须能独立路由；对应福客"五选一处理分支"（D8）与"按业务场景分配专业 Agent"（A5） |
| 场景仅影响话术（滤镜） | 否决 | 路由走向不变则业务 agent 无法按场景接管，与远期架构冲突 |

### 3.2 识别结构：单次合并 vs 两层独立

| 候选 | 结论 | 理由 |
|---|---|---|
| **单次调用合并输出**（一次 LLM 调用输出 type+sub_scenario+risk+logic） | ✅ 采纳（用户确认） | 路由决策是一次性动作，三个维度基于同一消息+同一段历史分类，拆两次调用不增加信息只增加延迟（约 +0.5~1s）与成本；router 已用低温结构化输出，扩展字段改动面小；后续业务 agent 上线时字段不变，仅改路由目的地 |
| 两层独立识别（场景层 + 技术层） | 否决 | 多一次 LLM 调用；两层各自调优的收益在 MVP 阶段不值当 |

### 3.3 additional-query 去留

| 候选 | 结论 | 理由 |
|---|---|---|
| **取消 additional-query，追问下沉到业务 agent** | ✅ 采纳（用户确认） | 追问是业务处理的一部分而非独立意图：售前 agent 问预算/偏好、售后 agent 问订单号，内容因场景而异，独立路由无法场景化追问；业务 agent prompt 内置"信息不足先追问"指令即可覆盖（福客 SPIIN 流程即"信息收齐再推荐"，F3）；少一个分支=少一类误判面；无订单系统现状下追问本就无法闭环 |
| 保留 | 否决 | 追问后无法真正处理（无订单系统）；与场景路由结构重叠 |

### 3.4 售后 agent 架构预研（后续 spec 的设计依据）

用户询问"售后 agent 用哪个架构方式好"，结合福客报告分析（结论写入本 spec 供后续 agent 方案引用）：

| 候选 | 结论 | 理由 |
|---|---|---|
| **工作流骨架 + LLM 决策点 + 工具（方式 C）** | ✅ 推荐 | 对应福客官方售后链路（F4/F5：识别问题→按 SOP 补材料→远程排查或建工单→人工处理→结果回传）+ 五选一处理分支（D8）+ FDE 分级自主性（H2：规则清晰→自动化、固定步骤→工作流、动态判断→Agent）。骨架确定性保证可控可追踪（A6），决策点 LLM 结构化输出保证判断弹性（A5 分析→决策→调用→结果校验） |
| 纯 ReAct 循环 | 否决 | 自由多轮循环无 SOP 约束，延迟/成本高（福客 3 秒响应 G1 难达标），审计难 |
| 纯固定工作流（全规则） | 否决 | 售后变体多（投诉夹杂售后、多商品混合、模糊描述），规则写不全面；福客是"AI 结合商品资料、故障知识、售后规则和顾客素材进行推理"（F5），判断是动态的 |

本阶段按此结论预留接口（见 §8），后续售后 agent spec 直接引用本决策。

### 3.5 MVP 占位节点行为

| 候选 | 结论 | 理由 |
|---|---|---|
| **占位节点返回提示** | ✅ 采纳（用户确认） | 路由表真实指向占位节点（售后/投诉安抚返回"功能建设中"提示），接口真实可验证；业务 agent 就位后仅改路由目的地 |
| 降级复用 RAG 子图 | 否决 | 用户确认占位行为，路由表要真实反映"售后尚未开通"的状态 |

---

## 4. 目标架构

### 4.1 目标架构总览（树形）

```
                        用户消息（文本/图片）
                              │
                              ▼
                     指代消解（main.py:391，前置，不变）
                              │
                              ▼
        ┌─────────────────────────────────────────────┐
        │ 意图识别节点 analyze_and_route_query          │
        │  ① ScopeGuard 经营范围预检（关键词级，保留）    │
        │     └─ 超范围 → 走 general 闲聊路径（现状不变） │
        │  ② LLM 低温合并输出 Router                    │
        │     {type + sub_scenario + risk + logic}     │
        └─────────────────────────────────────────────┘
                              │ risk 优先级最高
        ┌──────────┬──────────┼───────────┬────────────┬───────────────┐
        ▼          ▼          ▼           ▼            ▼               ▼
   risk=       risk=      type=image  type=general  type=presale  场景分支
  violation   high_risk    图片节点    闲聊节点       售前路由
        │          │       【现有      【现有      │【复用现有 RAG 子图】
        │          │        ·不动】     ·不动】     ▼
        ▼          ▼                               现有 RAG 子图
   风险拦截节点   转人工节点                          (create_research_plan)
  【新增】       【新增】                            planner→检索→summarize→final
   拒绝话术      无法在线处理话术                     （售前导购，零改动）
                                                          │
                                              ┌───────────┴───────────┐
                                              ▼                       ▼
                                        type=aftersale          type=complaint
                                        售后路由                投诉安抚路由
                                        │【新增·占位】           │【新增·占位】
                                        ▼                       ▼
                                    售后占位节点               投诉安抚占位节点
                                    （返回"服务升级中"提示，    （返回安抚占位话术，
                                      接口预留）                接口预留）
                                        │                       │
                                        └────── 后续演进 ────────┘
                                                │
                          ┌─────────────────────┼──────────────────────┐
                          ▼                     ▼                      ▼
                    售后 agent 子图        投诉安抚 agent 子图      售前 agent（可选增强）
                    （方式 C 骨架：        （安抚话术 + 情绪升级     （复用 RAG 子图 +
                     信息确认 → RAG tool   判断 → high_risk 升级）     导购话术，后续 spec）
                     → 政策检索 → 五选一
                     分支决策 → 回答）
```

**读图要点**：深色实线框为本次改动/新增；虚线框为后续业务 agent 演进方向（本阶段只留接口，见 §8）。路由优先级从上到下递减：risk 拦截 > image/general 技术路由 > 场景分支（presale/aftersale/complaint）。

### 4.2 意图识别输出结构（Router 扩展）

`lg_states.py:7` 改造：

```python
class Router(TypedDict):
    """Classify user query: scenario + risk + routing type."""
    logic: str                      # 分类理由（保留，供回答生成参考）
    type: Literal[
        "presale",                  # 售前：商品咨询/参数/价格活动/推荐导购
        "aftersale",                # 售后：退货退款/物流异常/订单查询
        "complaint",                # 投诉安抚：情绪不满/投诉（情绪主导）
        "general",                  # 闲聊（原 general-query）
        "image",                    # 图片（原 image-query）
    ]
    sub_scenario: Literal[
        "return_refund", "logistics", "order_query", "none",
    ]                               # 仅 aftersale 时有效；为售后 agent 预留分流决策输入
    risk: Literal[
        "none", "violation", "high_risk",
    ]                               # violation=违规咨询拦截；high_risk=高风险操作转人工
```

**要点**：原 4 类技术路由中 `graphrag-query` 被 `presale` 取代（语义合并："知识库可答的业务问题"=售前导购）、`additional-query` 删除；`general`/`image` 语义不变。`sub_scenario` 是本次为售后 agent 预留的关键接口字段。

### 4.3 路由表（MVP 占位版）

```
意图识别输出（risk 优先级最高）
   │
   ├─ risk=violation   → 风险拦截节点【新增】拒绝话术 + 合规引导
   ├─ risk=high_risk   → 转人工节点【新增】说明无法在线直接处理
   ├─ type=image       → 现有图片节点（create_image_query，不动）
   ├─ type=general     → 现有闲聊节点（respond_to_general_query，不动）
   ├─ type=presale     → 现有 RAG 子图（create_research_plan，售前导购复用）
   ├─ type=aftersale   → 售后占位节点【新增】返回"售后功能建设中"提示
   └─ type=complaint   → 投诉安抚占位节点【新增】返回提示
```

**路由优先级原则**：risk 拦截最优先——违规/高风险消息不进入任何业务处理路径（福客 D5"违规咨询明确拒绝"）。

### 4.4 状态设计

- `AgentState` 结构不变（`router` 字段已存在，`lg_states.py:59`），仅 `Router` 类型扩展
- 占位节点不写状态字段（纯静态话术返回），避免为一次性占位设计持久化

---

## 5. 风险意图定义

| risk 值 | 触发示例（智能家居语境） | 响应 |
|---------|------------------------|------|
| `violation` 违规咨询 | "怎么解除限速/改装电池让它跑更久"、"有没有破解/越狱办法"、"帮我写虚假功效宣传话术" | **拒绝话术**：明确拒绝 + 合规引导（"抱歉亲，这类改装存在安全隐患，我们不支持也不建议…"，对应福客 D5） |
| `high_risk` 高风险操作 | "给我改价/便宜点直接改"、"我要退款直接退钱"、"态度太差我要投诉到平台" | **转人工话术**：说明无法在线直接操作 + 安抚 + 已记录反馈会有专人跟进（对应福客 D3/D4"敏感问题只述事实不推测原因，转人工复核"） |
| `none` | 正常咨询 | 正常路由 |

**识别准则**（写入 prompt）：
1. 风险意图与场景**独立判断**："问退款政策" → aftersale + none；"要求直接退款打款" → aftersale + high_risk；"怎么改装电池" → violation（无论挂在哪个场景）
2. 区分"**询问政策**"与"**要求执行操作**"：前者正常回答，后者按风险处理
3. 情绪激烈/投诉升级（威胁投诉平台、辱骂）→ high_risk（转人工话术），即便场景是 complaint
4. 拿不准时 risk 取 none，宁放行不误拦（拦截误伤代价高于漏拦——漏拦最多进入正常问答，误拦直接拒绝用户）

---

## 6. Prompt 设计草案

### 6.1 ROUTER_SYSTEM_PROMPT 重写（`lg_prompts.py:7`）

```text
你是一个电商智能客服的意图识别引擎。你的任务是对用户询问同时判断三个维度：
场景类型（type）、售后子场景（sub_scenario）、风险意图（risk）。

## type 场景分类
- presale 售前：商品参数/价格/活动/推荐/使用咨询等知识库可答的业务问题（如"你们有智能门锁吗""这款灯多少钱""推荐一款扫地机器人"）
- aftersale 售后：退货退款、物流异常、订单查询（如"怎么退货""什么时候发货""查一下我的订单"）
- complaint 投诉安抚：情绪不满、服务抱怨、投诉（情绪主导，区别于 aftersale 的理性业务咨询）
- general 闲聊：与业务无关的闲聊（如"在吗""今天天气"）
- image 图片：用户提供了图片需要分析

## sub_scenario（仅当 type=aftersale 时给出，否则为 none）
- return_refund 退货退款
- logistics 物流异常
- order_query 订单查询

## risk 风险判断（独立于场景，优先级最高）
- violation 违规咨询：改装/破解/违禁品/虚假功效承诺等违规内容 → 明确拒绝
- high_risk 高风险操作：要求改价、直接退款打款等操作执行请求，或情绪激烈/投诉升级 → 需转人工
- none 无风险

## 分类准则
1. 结合对话历史判断真实意图（如"那个呢？"需结合上文商品上下文）
2. 区分"询问政策"与"要求执行操作"：问退款政策 → 正常；要求直接退钱 → high_risk
3. 拿不准时 risk 取 none，宁放行不误拦
```

### 6.2 删除的 prompt

- `GET_ADDITIONAL_SYSTEM_PROMPT`（`lg_prompts.py:90`）——追问下沉业务 agent
- `GUARDRAILS_SYSTEM_PROMPT`（`lg_prompts.py:162`）——范围检查并入风险判断，经营范围预检由 `ScopeGuard`（关键词级，保留）承担

### 6.3 占位节点话术

```text
售后占位：亲～售后处理服务正在升级中，暂时无法在线为您处理，您可以先查看店铺的退换货政策（知识库可答部分），或稍后再来咨询～
投诉安抚占位：亲～非常抱歉给您带来不好的体验，我们非常重视您的反馈，客服专员会尽快跟进处理～
```

（话术内容可在实施时按店铺口径微调，结构不变。）

---

## 7. 节点改动清单

| 文件 | 动作 | 内容 |
|------|------|------|
| `llm_backend/app/lg_agent/lg_states.py` | 修改 | `Router` 类型扩展为 type 五值 + sub_scenario + risk |
| `llm_backend/app/lg_agent/lg_prompts.py` | 修改/删除 | ROUTER_SYSTEM_PROMPT 重写（§6.1）；删 GET_ADDITIONAL_SYSTEM_PROMPT、GUARDRAILS_SYSTEM_PROMPT |
| `llm_backend/app/lg_agent/lg_builder.py` | 修改/新增/删除 | ① analyze_and_route_query：ScopeGuard 预检保留但拦截返回值改为新枚举（`lg_builder.py:76` 的 `type="general-query"` → `type="general"`），合并输出三维 Router（结构化输出模型同步扩展）；② route_query：按 §4.3 路由表接条件边；③ 新增风险拦截节点 / 转人工节点 / 售后占位节点 / 投诉安抚占位节点（静态话术，见 §6.3）；④ 删除 get_additional_info 节点与 AdditionalGuardrailsOutput 类 |
| `llm_backend/main.py` | 检查 | 无 additional 相关引用（预计无改动，需全局检索确认） |

**全局检索要求**（项目规范）：`get_additional_info`、`AdditionalGuardrailsOutput`、`GET_ADDITIONAL_SYSTEM_PROMPT`、`GUARDRAILS_SYSTEM_PROMPT`、`Router` 全部引用点逐一核对同步修改。

---

## 8. 业务 agent 接口预留

### 8.1 占位节点接口形状

与现有 `create_multi_tool_workflow` 子图（`multi_tool.py:32`）同构：

```python
InputState{question: str, history: list} → OutputState{answer: str}
```

后续售后/投诉安抚 agent 就位后，仅改 `route_query` 条件边的目的地，识别模块与接口形状不动。

### 8.2 后续 agent 规格引用（决策记录）

- **售后 agent**：工作流骨架 + LLM 决策点 + 工具（§3.4 方式 C）——骨架：信息确认 → 政策检索（RAG tool）→ 五选一分支决策（政策解释/补材料/远程排查/转人工/风险拦截）；RAG 子图封装为 tool（`ToolRegistry` 模式已存在，`function_tools.py:13`）
- **投诉安抚 agent**：安抚话术 + 情绪升级判断（high_risk 升级路径），是否挂 RAG tool 由后续 spec 定
- **售前 agent**：复用现有 RAG 子图，可增强为导购话术（后续 spec）

---

## 9. 边界情况处理表

| # | 场景 | 预期行为 |
|---|------|---------|
| 1 | "我要退货"（无订单号、无情绪） | aftersale + return_refund + none → 售后占位提示 |
| 2 | "你们客服太差了！我要投诉！" | complaint + high_risk（情绪激烈）→ 转人工话术 |
| 3 | "怎么改装电池让它跑更久" | violation → 风险拦截拒绝话术 |
| 4 | "这款灯能便宜点吗" | presale + none → RAG 子图正常导购 |
| 5 | "在吗" | general → 闲聊节点 |
| 6 | "帮我看看这个（附图）" | image（config 有 image_path）→ 图片节点 |
| 7 | "什么时候发货？" | aftersale + logistics → 售后占位提示 |
| 8 | 对话历史中"那个呢？"（上文提过商品） | 结合历史识别 presale → RAG 子图（指代消解已前置 main.py:391） |
| 9 | "退款政策是什么？" | aftersale + none（询问政策非要求执行）→ 售后占位提示 |
| 10 | 超经营范围（"有卖衣服吗"） | ScopeGuard 关键词预检拦截 → general 路径的"无关问题"话术（保留现状） |
| 11 | 拿不准的消息 | risk 取 none，type 按最可能场景；不误拦 |

---

## 10. 影响面分析

| 面 | 影响 | 处理 |
|----|------|------|
| graphrag-query 语义变更 | RAG 子图本身**零改动**（`multi_tool.py`/检索链路/RAGAS 评测不受影响）；仅主图路由目的地不变（presale 仍走 create_research_plan） | 无需回归重测 RAGAS |
| 语义缓存（redis_semantic_cache） | 缓存键基于 query 相似度，与路由无关 | 无影响 |
| 指代消解（main.py:391） | 前置节点，路由变化不影响 | 无影响 |
| 前端 | 不展示 intent | 无影响 |
| 评测（SPEC_RAGAS_EVAL） | 直调子图绕过路由 | 无影响 |
| 会话恢复（PostgresSaver） | Router 结构变化仅影响**新会话**；旧 checkpoint 中的 Router 字段为旧 4 值结构，恢复后 route_query 遇到旧值可能 ValueError | 需处理：旧 checkpoint 消息流进入 analyze_and_route_query 会重新走一遍路由（新结构），历史 router 字段只在条件边读取——确认 route_query 读取的是本轮新输出而非旧 checkpoint 残留；实现时验证 |

---

## 11. 实施步骤

1. **扩展 Router 结构**（`lg_states.py`）→ 验证：类型定义通过，五值枚举正确
2. **重写 ROUTER_SYSTEM_PROMPT**（`lg_prompts.py` §6.1）+ 删除 additional/guardrails prompt → 验证：无残留引用
3. **改造 analyze_and_route_query**（`lg_builder.py`）：ScopeGuard 保留，结构化输出模型扩展 → 验证：单条消息输出三维字段
4. **改造 route_query + 新增 4 节点**（风险拦截/转人工/售后占位/投诉安抚），删除 get_additional_info → 验证：路由表全分支可达
5. **全局检索核对**：`get_additional_info`、`AdditionalGuardrailsOutput`、`GET_ADDITIONAL_SYSTEM_PROMPT`、`GUARDRAILS_SYSTEM_PROMPT`、`Router` 全项目引用点
6. **构建路由 golden set**（30 条，§12）→ 验证：三维准确率输出
7. **端到端验证**（§12 典型对话流）→ 验证：全场景走通

---

## 12. 验证方案

### 12.1 路由 golden set 评测（新增）

构建 30 条覆盖全维度的消息集，逐条跑 `analyze_and_route_query`，对照期望 `{type, sub_scenario, risk}` 统计准确率：

| 类别 | 条数 | 示例 |
|------|------|------|
| 售前 presale | 8 | "你们有智能门锁吗""这款灯多少钱""推荐一款扫地机器人""灯和灯带能一起控制吗" |
| 售后 aftersale | 8（含三子类） | "怎么退货""什么时候发货""查一下订单""退货运费谁承担"（return_refund/logistics/order_query 覆盖） |
| 投诉安抚 complaint | 5 | "你们产品太差了""客服不理人我要投诉" |
| 风险 violation/high_risk | 5 | "怎么改装电池""直接给我退款""便宜点改价" |
| 闲聊 general | 2 | "在吗""谢谢" |
| 图片 image | 2 | 带图消息（config 注入 image_path） |

- 评测脚本：`llm_backend/scripts/` 或 `app/test/` 下临时脚本（沿用现有"先实测再定方案"惯例），输出逐条明细 + 三维准确率
- 不纳入正式 evaluation 包（evaluation 是 RAGAS 子图评测，职责分离）

### 12.2 端到端典型对话流

售前导购 / 售后提示 / 投诉提示 / 违规拦截 / 改价转人工 / 图片 / 闲聊 各跑一遍，人工核对回答符合预期。

### 12.3 回归

- RAGAS 评测不受影响（子图零改动，可抽查一轮确认）
- 旧会话恢复路径验证（§10 影响面最后一行）

---

## 13. 决策记录

| # | 决策 | 结论 | 时间 |
|---|------|------|------|
| 1 | MVP 范围 | 场景意图 + 风险意图；来源识别不做 | 2026-08-27 |
| 2 | 场景分类体系 | 售前 / 售后（退货退款、物流异常、订单查询）/ 投诉安抚（用户自定义，合并福客六场景为三类） | 2026-08-27 |
| 3 | 场景落地方式 | 场景驱动路由（开关），非仅话术（滤镜） | 2026-08-27 |
| 4 | 识别结构 | 单次调用合并输出（type + sub_scenario + risk + logic） | 2026-08-27 |
| 5 | additional-query | 取消，追问下沉到业务 agent prompt | 2026-08-27 |
| 6 | graphrag-query | 语义合并为 presale（售前路由），RAG 子图零改动复用 | 2026-08-27 |
| 7 | 占位节点行为 | 售后/投诉安抚返回"功能建设中"提示（用户确认），接口与 multi_tool 同构 | 2026-08-27 |
| 8 | 售后 agent 架构（后续 spec 依据） | 工作流骨架 + LLM 决策点 + 工具（福客 F4/F5/D8/H2 映射，方式 C） | 2026-08-27 |
| 9 | 图片/闲聊 | 路由与节点不动 | 2026-08-27 |

---

## 14. 风险与避坑清单

| # | 风险 | 对策 |
|---|------|------|
| 1 | presale 取代 graphrag-query 后，历史 prompt 中"商品品类推荐即使没给具体型号也应归类检索"的条款（`lg_prompts.py:22-24`）需在新 prompt 保留，否则售前误判为 general | 新 prompt 保留该准则 |
| 2 | 旧会话 checkpoint 中的旧 Router 4 值结构与新 route_query 不兼容 | 实现时验证恢复路径：analyze_and_route_query 每轮重跑，route_query 读本轮新输出；golden set 评测前先跑一个旧会话恢复用例 |
| 3 | risk 误拦代价高（直接拒绝用户） | prompt 明确"宁放行不误拦"；golden set 中专门放边界用例 |
| 4 | 占位节点话术暴露"功能未开通"可能影响体验 | 话术措辞用"服务升级中"，口径可按店铺微调；占位期短（业务 agent 就位即替换） |
| 5 | 结构化输出字段增加后模型可能输出非法值（如 sub_scenario 非枚举值） | with_structured_output 强制 schema 校验（pydantic Literal），校验失败走降级：type 默认 general、risk 默认 none；实施时验证 |
| 6 | 全局删除遗漏 additional 引用 | 按项目"全局检索"规范逐符号核对（§7） |
