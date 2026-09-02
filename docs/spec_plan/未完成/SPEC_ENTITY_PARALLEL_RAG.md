# 多实体并行 RAG 检索链路重构实施规格
> **归档状态**: 🔶 部分实施（2026-09-02 二次审计，依据 main 代码与 git 历史；一次审计同日稍早，表述有误处以此行为准）
> **已落地清单**：
> - ✅ 阶段 0/3：customer_tools 收敛为进程级单例 `RAGRetrieverService.search`（cf9e37b，纯混合检索零 LLM 调用）；子图简化（planner 直连、删 guardrails/tool_selection/Cypher 遗留）全落地
> - ✅ 阶段 3 Step 3 剩余项"模块级单次编译"：2026-09-02 由 29e6f73 落地（`get_research_graph` 懒加载双检锁单例，lg_builder.py:411-436，消除每请求 llm 实例化 + compile ~40ms 并复用 httpx 连接池）——至此**阶段 3 全步骤完成**
> - ✅ 阶段 1（入口指代消解）/阶段 5（语义缓存前置）：**未按本 spec 4.1/4.6 的原代码形态实施**，但目标已由替代方案在 main 入口侧落地——多轮无条件 LLM 消解 + 入口缓存 lookup/update 无条件调用（22b0c90，见 [[SPEC_ENTRY_LLM_RESOLUTION.md]]）；语义缓存侧 redis.asyncio/分桶索引/实例池三项修复已完成（4882018，见 [[SPEC_SEMANTIC_CACHE_RESOLVE.md]]，已归档 已完成/）
> **未实施清单**：
> - ⏳ 阶段 2（planner 实体识别+子 query 拆解）与阶段 4（HyDE 入分支 + original_question 透传）零代码；子文档 [[SPEC_ENTITY_RECOGNITION_AND_RAG_RETRIEVAL.md]] 同为设计态，其 §5.1/§10 前提已失效（generate_hypothetical_answer 已删、planner 直连结构已收敛）
> - ⏳ 阶段 6 收尾（§7.1 数字修订见下；本次文档同步为阶段 6 Step 2 的一部分）
> **2026-09-02 二次审计重点（planner 节点）**：按现状代码实测（planner/models.py、node.py、kg_prompts.py、multi_tool.py、lg_builder.py:411-436、lg_states.py、customer_tools、RAGRetrieverService）——planner 提示词仍为 Cypher/北风商贸时代模板、输出模型无 entity_count 与一致性校验（空任务可穿透为空答）、拆解复用 T=0.7 研究 LLM 无确定性约束、单任务场景固定空转 1 次 LLM、检索端无子 query 增强。详见 **§2.3**（新发现落档，阶段 2/4 实施前必读）。

> **用途**: 去除 Cypher 查询、纯 RAG 检索后，重构查询预处理与子图链路——入口指代消解（正则门控 + LLM）+ 实体识别与任务拆解合并为一次 LLM 调用 + 多实体并行 RAG 检索（HyDE 内置于检索工具），并配套 LLM 调用成本与响应时间优化  
> **技术栈**: LangGraph 0.3.x + FastAPI + pgvector + Redis + DeepSeek/Ollama + qwen text-embedding-v4（向量统一走 DashScope API，无本地 SentenceTransformer 主链路）  
> **状态**: **部分实施** —— 阶段 0（customer_tools 单例化）与阶段 3（子图简化/删除 Cypher 遗留）已落地；阶段 1（入口指代消解）/2（planner 实体识别+拆解）/4（HyDE 入内 + original_question）/5（语义缓存前置）及阶段 3 的"模块级单次编译"未实施；阶段 3 的"预处理管道缩减"已落地为**直接删除**（2026-08-21，BudgetGuard + query_rewriting 整体移除，主链路以 resolved_question 直进子图，见 [[SPEC_REMOVE_QUERY_PREPROCESSING.md]]）  
> **关联文档**: [[PROJECT_ANALYSIS.md]] [[PLAN_GraphRAG_TO_StandardRAG.md]] [[SPEC_CONTEXT_ENGINEERING.md]] [[docs/SHOP_SAGE_ANALYSIS.md]] [[SPEC_ENTRY_LLM_RESOLUTION.md]] [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] [[SPEC_ENTITY_RECOGNITION_AND_RAG_RETRIEVAL.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题](#2-现状链路与问题)
3. [目标架构总览](#3-目标架构总览)
4. [模块详细设计](#4-模块详细设计)
5. [回退与异常处理总表](#5-回退与异常处理总表)
6. [分阶段实施步骤](#6-分阶段实施步骤)
7. [验证方案](#7-验证方案)
8. [待确认事项](#8-待确认事项)
9. [风险与避坑清单](#9-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 已去除 Cypher 查询（Text2Cypher/预定义 Cypher 模板），知识库检索收敛为**纯 RAG**（向量 ∥ BM25 混合检索 + RRF 融合 + Reranker 精排，零 LLM 调用），Neo4j 不再参与查询链路
2. 现状一次知识库查询需 **10~13 次 LLM 调用**（路由、改写、纠错、实体识别、扩展、Multi-Query+HyDE、guardrails、planner、tool_selection、相关性评分、summarize），其中多处已失去存在意义（实体识别结果仅打日志、单工具场景的 LLM 工具选择、与顶层路由重复的 guardrails）—— ⚠️ **该句按 2026-09-02 代码已过期**：预处理 5 步（2026-08-21）、guardrails/tool_selection/相关性评分（阶段 3）相继删除后，无缓存命中时整链路 LLM 调用实际为 **3~4 次**（入口消解多轮时 1 + Router 1 + planner 1 + summarize 1，见 §2.1/§7.1）
3. 已确认的运行期问题（详见 docs/SHOP_SAGE_ANALYSIS.md P0 清单，**均已随阶段 0/3 落地修复**）：
   - `tool_selection` 无工具可选时 `Send("error_tool_selection")` 到未注册节点 —— 已随 tool_selection 组件删除
   - `tool_preference="predefined_cypher"` 快路径传空参数恒失败 —— 已随 predefined_cypher 组件删除
   - `customer_tools` 每请求重建重型资源 → 已收敛为进程级单例 `RAGRetrieverService`（get_rag_retriever_service，cf9e37b）；原文"客户端已单例化（get_vector_store()，node.py:104-119）、每请求全量拉取文档仍存在（node.py:158）"**均已过期**——语料与索引现由 service 持有，node.py 仅剩查询调用（customer_tools/node.py:54-59）

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| LLM 调用次数下降 | ⚠️ **该目标已提前达成**（2026-09-02 实测现状 3~4 次/查询，见 §7.1），剩余阶段不再以"减调用"为主诉求，改为"**不因拆解/HyDE 增调用**"：单实体查询 ≤4 次；双实体对比查询 ≤7 次（planner 仍 1 次、HyDE 按分支开关计入）；缓存命中 0 次 |
| 响应时间下降 | ✅ 已达成（29e6f73）：消除每请求重建模型/客户端/图编译的开销（原 ~40ms/次固定开销归零，llm httpx 连接池跨请求复用） |
| 多实体对比查询质量提升 | "A 和 B 哪个好"拆为 N 个子 query 并行检索，按实体覆盖产品信息 |
| 消灭已知运行期 bug | 上述 3 个问题随结构简化一并删除 |
| 语义保护 | 指代消解只补全、不改义；summarize 基于用户原 query 回答 |

### 1.3 设计原则

> 原则 1/2 属阶段 1（入口消解）原设计——该阶段已由 main 入口侧替代方案落地（22b0c90，无条件多轮消解），仅原则 3/4 对未实施的阶段 2/4 继续生效：

1. **state.messages 只读**（沿用 SPEC_CONTEXT_ENGINEERING.md 原则二）：指代消解结果存独立字段，不替换、不修改原始消息流
2. **宁多勿漏**：正则门控宁可误触发（代价=1 次 LLM 调用），不可漏检（代价=整条检索链路失效）
3. **确定性优先**：实体数量≥2 才拆解；拆解结果必须通过一致性校验，失败一律回退单分支
4. **temperature=0 + 提示词约束双保险**：低温保证输出确定性，语义保真靠提示词规则（只补全、不扩展、不添加历史没有的信息）

---

## 2. 现状链路与问题

### 2.1 现状调用链（一次 graphrag-query）

> ⚠️ 本节原描述写于阶段 3 落地前（guardrails/tool_selection/predefined_cypher/Neo4j 实体识别/预处理 5 步均已删除），下述"现状链路（2026-09-02 实测）"为准。

```
main 入口 /api/langgraph/query（多轮无条件 LLM 消解 → 语义缓存 lookup，22b0c90）
  → 主图 analyze_and_route_query（Router LLM，T=0，输出 logic/type/risk，无 entity_count 字段）
      └─ type=presale → create_research_plan
            └─ 售前 MultiTool 子图（进程级单例，get_research_graph，29e6f73）
                 planner（LLM 拆任务，T=0.7）→ Send ×N 并行
                   → customer_tools（零 LLM）：RAGRetrieverService.search
                      HNSW ∥ BM25 → RRF → Reranker 精排，top_k=5（RERANKER_TOP_K）
                 → summarize（LLM，基于 state.question=完整原问题）→ final_answer
真实 LLM 调用（无缓存命中）：入口消解(多轮时 1) + Router 1 + planner 1 + summarize 1 = 3~4 次/查询
```

### 2.2 问题清单（本方案要解决的）

| # | 问题 | 处置（✅=已落地；⏳=未实施，2026-09-02 核） |
|---|---|---|
| 1 | 指代消解在**路由之后**（`create_research_plan` 内），路由器看到未消解 query | ✅ 已前置到系统入口 main 侧（22b0c90）；实现形态见 [[SPEC_ENTRY_LLM_RESOLUTION.md]] |
| 2 | 指代消解无门控，每轮必调 LLM（首条消息除外） | ✅ 原"正则门控"思路已被替代：多轮非语气词消息**无条件 LLM 消解**、纯语气词/无历史直通（22b0c90） |
| 3 | 实体识别结果（实体 ID/类型）仅打日志，不进入下游 | ⏳ planner 拆解仍无实体依据（§2.3 P1），待阶段 2 |
| 4 | 实体识别、planner、tool_selection 三次 LLM 调用职责重叠 | ✅ tool_selection 已删、planner 直连（阶段 3）；⏳ 实体识别入 planner（阶段 2） |
| 5 | guardrails 与顶层路由（ScopeGuard + Router）重复判定经营范围 | ✅ 已删除，保留顶层守卫 |
| 6 | HyDE 在预处理阶段针对整句做一次，拆解后各子 query 无法利用 | ⏳ 阶段 4 未实施；检索端无 HyDE（§2.3 P5） |
| 7 | summarize 使用预处理后的 question，对比意图可能被稀释 | ✅ 预处理已整体删除，summarize 输入 state.question=完整原 query（lg_builder.py:462-469 → summarize/node.py:50-56），"稀释"源已不存在；⏳ original_question 字段透传（阶段 4）仍有审计价值但不急 |
| 8 | customer_tools 每请求重建重型资源，并行分支下放大 | ✅ 已收敛为 `RAGRetrieverService` 进程级单例（cf9e37b，纯检索零 LLM 调用） |
| 9 | 语义缓存服务 `/api/langgraph/query` 主链路 | ✅ 入口 lookup/update 无条件调用（22b0c90）；缓存侧三项修复已完成（4882018，[[SPEC_SEMANTIC_CACHE_RESOLVE.md]] 已归档） |
| 10 | planner 拆解提示词为 Cypher/北风商贸时代模板，无实体识别/意图继承规则（§2.3 P1） | ⏳ 阶段 2 核心；2026-09-02 实测发现 |
| 11 | PlannerOutput 无 entity_count、节点无一致性校验，空/重复任务可穿透成空答（§2.3 P2） | ⏳ 阶段 2 核心；2026-09-02 实测发现 |
| 12 | planner 复用 T=0.7 研究 LLM，拆解决策无确定性约束（§2.3 P3） | ⏳ 阶段 2 配套；2026-09-02 实测发现 |
| 13 | 单任务场景 planner 固定空转 1 次 LLM；Router schema 已无 entity_count 可复用（§2.3 P4） | ⏳ 待评估：方案 B 需先扩展 Router（lg_states.py:7-22），见 §8 #1 |
| 14 | 检索端无子 query HyDE/实体约束，实体 top-5 覆盖无保证（§2.3 P5） | ⏳ 阶段 4；2026-09-02 实测发现 |

### 2.3 planner 节点实测问题（2026-09-02 审计落档）

> 本节按 2026-09-02 main 代码逐一核实（planner/models.py、node.py、planner/prompts.py、kg_prompts.py、multi_tool.py、edges.py、customer_tools/node.py、lg_builder.py:411-436、config.py），是"阶段 2/4 为什么还没实施、实施前必须先处理什么"的答案。P1-P5 与 §2.2 #10-14 一一对应。

**P1 · 拆解提示词与纯 RAG 商品检索场景错配（Cypher 时代残留）**
- `PLANNER_SYSTEM_PROMPT`（kg_prompts.py:8-38）规则仅"拆为独立子任务"，示例全部为 Neo4j 时代的库表式拆分（"北风商贸有哪些饮料类产品？价格是多少？"→ 拆产品/价格两问；"订单10248…"、"供应商 Exotic Liquids…"），**无任何商品实体识别、意图继承、实体名回填约束**；human 模板另叠一份重复规则（planner/prompts.py:14-21）。
- 当前下游是**商品知识 docx 的全库向量混合检索**（`RAGRetrieverService.search`，HNSW∥BM25→RRF→Reranker，top_k=5），不是表查询——旧示例对"X 和 Y 哪个更好"类 query 完全无指导：LLM 可任意选择"整句单任务（另一实体证据可能被 top-5 截断）"或"按属性拆（任务粒度失控、summarize 结果膨胀）"。
- 目标：阶段 2 按 §4.2 重写——替换用**完整 prompt 全文（2026-09-03 v2，5 示例）**已在 §4.2 落档：子问单元拆解模型（多实体对比拆、列表+详情并列拆两类动机；同实体属性细节/零实体/单意图不拆）、单任务强制=原 query、禁止空列表、MAX_TASKS=3，schema 与节点校验同步更新（数量锚废除）。

**P2 · 输出模型无拆解约束、节点校验薄弱（空任务可穿透成空答）**
- `PlannerOutput` = 直接复用 `tasks: List[Task]`（planner/models.py:8-12），无 entity_count、无实体级子 query 模型；节点回退仅覆盖**空列表**一种情形（node.py:49-57），**不校验** task.question 空串 / 任务数异常 / 重复任务。
- 穿透链：空串 task → edges.py:13-22 `Send("customer_tools", task.question="")` → customer_tools/node.py:55-56 记 errors"未提供查询文本"返回空记录 → summarize/node.py:58-59 "No data to summarize." → **整链路空回答**（是失败而非降级；§5 总表也未覆盖此场景）。with_structured_output 不保证非空（schema 无 min_length/pattern 约束）。
- `Task.parent_task` 为必填且由 LLM 逐任务生成（每任务重复一遍父问题原文，可被改写），下游仅 edges.py:18 传入即弃，无业务消费（customer_tools 只用 task；final_answer 只记 task/records）——schema 应去掉或由节点注入。
- 目标：阶段 2 落 §4.2 校验回退（数量一致 + 非空 + 去重 → 不满足整体回退单分支），§5 补"空任务"一行。

**P3 · 拆解决策无确定性约束（T=0.7 共享实例）**
- planner 复用子图单例注入的 research LLM（multi_tool.py:50 ← lg_builder.py:432-434：`ChatDeepSeek(..., temperature=settings.LLM_TEMPERATURE)`），`LLM_TEMPERATURE=0.7`（config.py:154）；而路由/消解均为 0（`RESOLVE_LLM_TEMPERATURE` config.py:78、`ROUTER_TEMPERATURE` config.py:155）。同 query 不同轮次的任务数/任务边界随采样漂移 → 检索覆盖、日志可比性、缓存（若将来拆解入 key）都不稳定。违反本文档设计原则 4（temperature=0 双保险）。
- 目标：阶段 2 为 planner 设专用低温实例或沿用 `create_planner_node` 构造参数补 temperature 传入（实施时定）。

**P4 · 单任务场景固定空转 1 次 LLM（Router 无字段可借）**
- 每次 presale 查询必经 planner 一次 LLM（multi_tool.py:68 START→planner），而单实体/事实查询（主流量）恒拆 1 任务=原 query → 该次调用信息增益趋零（还需 ~1 次模型往返延迟）。旧文"Router 已输出 entity_count（lg_states.py:17）"已失效：2026-08-27 路由重构后 Router = logic/type/risk（lg_states.py:7-22），无任何可复用的实体/拆解字段。
- 可选出路（实施阶段 2 时一并评估，勿过度设计）：方案 B 扩展 Router schema 带实体字段（拆解并入路由，见 §8 #1）；或简单 query 规则直通（字符数+连词判断，牺牲召回换延迟）。若选规则直通需先实测误拆率（对比句拆单的代价=质量回归）。

**P5 · 检索端无子 query 增强，实体 top-5 覆盖无保证**
- customer_tools/node.py:54-59 直接 `retriever.search(task)`（内部 top_k=5，RAGRetrieverService），无 HyDE、无实体约束、无按子 query 独立检索合并。即使阶段 2 拆出双实体任务，检索端也不能保证两实体的商品文档都进各自 top-5（文档库含政策/品类块，混合 query 可能整体偏向一方）。阶段 2 与 4（HyDE 入分支 + 合并去重）需**组合**实施才闭环——单做其一对比质量提升有限。

---

## 3. 目标架构总览

> 📌 架构图对照现实（2026-09-02）：入口指代/缓存段已按 22b0c90 **落地在 main.py 入口**（无条件多轮消解 + lookup 命中短路，无正则门控），不在 analyze_and_route_query 内；路由类型名已重构为 `presale/aftersale/complaint/general/image/clarify`（lg_states.py:10-17），presale → create_research_plan；子图（planner→Send→customer_tools→summarize→final）结构已全部落地。图内仍标"门控/次数要求"的行均为原始目标态，以 §2.1 实测链路与 §7.1 为口径。

```
入口（/api/langgraph/query，main.py）
  → 多轮无条件 LLM 消解（22b0c90）→ 语义缓存 lookup
      命中 → 直接返回缓存答案（跳过主图全部节点）
  → 未命中 → 主图 analyze_and_route_query（ScopeGuard 预检 → 路由 LLM，输出 Router）
      ├─ general / aftersale / complaint / image / clarify → 各自分支
      └─ presale → create_research_plan
            └─ 子图 ainvoke（进程级单例编译，29e6f73）
                 START → planner（实体识别+拆解，一次 LLM 调用）
                           ├─ entity_count ≥ 2 且校验通过
                           │    → Send("customer_tools") × N 并行
                           │        每分支内部：HyDE(可选,开关) → 向量/混合检索
                           │                  → 合并去重 → 相关性评分(批量)
                           │        各分支 records 经 searches 累加器合并
                           └─ 否则 → 单分支 Send（整句检索）
                 → summarize（基于 original_question + 全部 records）
                 → final_answer → END
                    └─ 编译子图 ainvoke（一次编译，模块级复用）
                         START → planner（实体识别+拆解，一次 LLM 调用）
                                   ├─ entity_count ≥ 2 且校验通过
                                   │    → Send("customer_tools") × N 并行
                                   │        每分支内部：HyDE(可选,开关) → 向量/混合检索
                                   │                  → 合并去重 → 相关性评分(批量)
                                   │        各分支 records 经 cyphers 累加器合并
                                   └─ 否则 → 单分支 Send（整句检索）
                         → summarize（基于 original_question + 全部 records）
                         → final_answer → END
  → SSE 流式输出
```

**关键结构变化**：删除 guardrails、tool_selection、predefined_cypher 三个节点；planner 直连 customer_tools；保留 map-reduce `Send` 并行结构与 `searches` 累加器（现状状态字段名为 `searches`，`state.py:62`；早期设计稿写作 `cyphers`）。**本节子图结构已全部落地**（multi_tool.py:62-78，2026-09-02 二修：阶段 3 备注中遗留的"模块级单次编译"也已由 29e6f73 完成）；目标架构中剩余未落地部分为 4.1 节内的入口门控代码形态（已被替代方案覆盖）、planner 实体拆解（阶段 2）与 HyDE/original_question（阶段 4）——图中"相关性评分(批量)/cyphers/HyDE"字样为阶段 4 目标态，现状 customer_tools 分支为纯 `RAGRetrieverService.search`（见 §2.1 实测链路）。

---

## 4. 模块详细设计

### 4.1 入口指代消解（正则门控 + LLM）

> ⚠️ 2026-09-02：本节代码形态（analyze_and_route_query 内正则门控）**未实施且不再计划实施**——目标已由 main 入口侧替代方案落地（22b0c90：多轮非语气词消息无条件 LLM 消解 + 纯语气词/无历史直通 + 入口缓存 lookup/update，见 [[SPEC_ENTRY_LLM_RESOLUTION.md]]）。本节保留作设计追溯与回退参照，实施请引用该 spec。

**位置**（原设计）：`analyze_and_route_query` 节点内部，ScopeGuard 预检之后、路由 LLM 调用之前（`lg_builder.py:76-81` 之后，现 `lg_builder.py:64-70`）。

**门控函数**（原规划新增于 `components/query_rewriting/node.py` ⚠️ 该文件已于 2026-08-21 随查询预处理管道删除；消解现状由 `app/services/pronoun_resolver.py` + `redis_semantic_cache._resolve_message` 实现，见 [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] 与 [[SPEC_ENTRY_LLM_RESOLUTION.md]]）：

```python
# 指代/指示词 + 高频省略追问模式（宁多勿漏，词表按日志调优）
REFERENCE_PATTERN = re.compile(
    r"它|他|她|他们|它们|这|那|这些|那些|该|前者|后者|"
    r"上面|前面|刚才|之前|还有吗|还有呢|呢$|多少钱|有货吗|能退吗|贵吗|怎么用"
)

def need_reference_resolution(query: str, history_len: int) -> bool:
    """指代/指示词命中，或短追问且有历史（覆盖正则难以命中的省略句）。"""
    return bool(REFERENCE_PATTERN.search(query)) or (
        len(query.strip()) <= 8 and history_len > 0
    )
```

**门控调用**（`lg_builder.py` analyze_and_route_query 内）：

```python
resolved_question = state.messages[-1].content
if need_reference_resolution(resolved_question, len(state.messages) - 1):
    resolved_question = await context_aware_rewrite(rewrite_llm, state.messages)
# state.messages 保持原样；resolved_question 存入返回的 state.question
# 路由 LLM 的输入使用 resolved_question 替换最后一条消息内容
```

**LLM 参数**：
- 专用实例 `rewrite_llm`：`temperature=0`（新增 `settings.REWRITE_TEMPERATURE: float = 0.0`），模型可用轻量档（见 §8 待确认 #3）
- `_format_chat_history(messages[:-1], max_turns=3)`（现状 5 轮降为 3，`node.py:105` 增加 `max_turns` 参数）
- 复用现有 `ContextRewrittenOutput` 结构化输出与 `CONTEXT_REWRITE_SYSTEM_PROMPT` 三条规则（只补全、不扩展、不添加历史没有的信息、完整则原样返回），不改提示词

**语义保护双保险**：temperature=0 保证确定性；"不改变语义"由提示词规则 + 回退（LLM 返回原样）保证——temperature 本身不承诺语义保真。

### 4.2 实体识别 + 任务拆解合并（planner 改造，一次 LLM 调用）

> **2026-09-03 v2 修订（用户评审后）**：拆分触发从"实体数"推广为**子问单元模型**——同时覆盖两类拆分动机：**多实体**（证据文档互斥，混合 query 的 top-5 会偏向一方）与**列表型+详情型子问并列**（如"有哪些…？价格/参数…？"命中的文档块不是同一批）；单实体的多个**属性细节子问（功能+噪音+续航）一律合并不拆**（命中的是同几篇商品文档，拆开只增分支与 summarize 输入膨胀）。`entity_count` 降级为决策辅助与日志字段，**不再作数量一致性校验锚**（列表型拆解下 tasks 数可 > entity_count）。

**结构化输出 schema**（替换 `components/planner/models.py` 的 `PlannerOutput`）：

```python
class EntitySubQuery(BaseModel):
    name: str = ""     # 主题词：实体名/清单范围（日志与观测用，可空）
    sub_query: str     # 可直接独立检索的子问文本（自含主题词，禁止指代）

class PlannerOutput(BaseModel):
    entity_count: int = Field(default=0, ge=0, description="识别到的商品/品类实体数（决策辅助与日志，不作数量校验锚）")
    tasks: List[EntitySubQuery] = Field(
        default_factory=list,
        description="1..3 条；无法拆分时恰 1 条且=原 query 逐字复制；拆分时每条为可独立检索的子问",
    )
```

**提示词规则**（重写 `kg_sub_graph/prompts/kg_prompts.py` 的 `PLANNER_SYSTEM_PROMPT`；2026-09-03 v2）：

1. 拆分动机只有两种，其余一律不拆：
   - **多实体（≥2）**：为每个实体生成 1 个独立子任务，任务数 = 实体数，每任务继承原 query 对该实体的全部意图
   - **列表型 + 详情型并列**（"有哪些/几款/推荐"与"价格/参数"同句）：拆 2——列表 1 条，详情按实体归并 1 条
2. 不拆场景（单任务 = 原 query 逐字）：单实体的多个属性细节问、零实体条件/推荐句、单一意图问；**同实体属性细节禁止拆**
3. 通用约束：任务文本自含检索主题词（实体全名/品类/清单范围），禁止指代（它/它们/这款/哪个）；任务间不重复、不相互依赖；**任务数上限 3**；拿不准宁可合并为 1 个整句
4. 输出 `entity_count`（0/1/N，只识别明确提到的商品/品类名，泛指词不算实体名）
5. **任何情况下禁止返回空任务列表**（`[]`/null 均为违规输出）——宁可返回 1 条整句任务，不可遗漏实体

**替换用完整 prompt 全文（阶段 2 Step 2 产物，2026-09-03 v2，示例精简为 5 条覆盖常见拆分/不拆场景）**：

```text
# —— 整体替换 PLANNER_SYSTEM_PROMPT（kg_prompts.py）——
你是一个电商平台智能客服系统中的任务规划组件（商品知识问答场景）。
你的职责：判断用户问题是否需要拆成多个【可独立检索】的子任务，并输出任务列表。
entity_count = 问题中明确提到的商品/品类实体数量（0/1/N）；"哪款/有没有/推荐几个"等泛指不构成实体。

拆分判断（按顺序执行）：
1. 若问题中提到多个商品/品类实体：
   为每个实体生成 1 个独立子任务，任务数量 = 实体数量；
   每个子任务必须包含实体全名，禁止指代（它/它们/这款/那个）；
   每个子任务继承原问题对该实体的全部意图维度：
     对比/哪个更好 → 优缺点、规格参数、适用场景；
     价格/优惠     → 售价、促销活动；
     选购/推荐     → 适合人群、口碑评价。
2. 否则，若问题把【列表型子问】（有哪些/几款/推荐）与【详情型子问】（价格/参数/功能）明确并列：
   拆为 2 个子任务——列表问 1 条，详情问按实体归并 1 条。
3. 其余情况一律不拆：
   任务列表只含 1 个任务，文本 = 用户原问题【逐字复制】，不得改写、删减或补充。
   包括：同一商品的多个属性细节问（功能+噪音+续航，必须合并）、零实体条件/推荐句、单一意图问。

所有任务通用要求：
- 任务文本必须自含检索主题词（实体全名/品类/清单范围），每条都能脱离原问题独立检索
- 任务之间内容不得重复、不得相互依赖
- 任务数量上限 3；拿不准是否该拆时，宁可合并为 1 个整句任务
- 任何情况下不得返回空任务列表（[] 或 null 均为违规输出）

示例：
- 问题：扫地机器人X1支持自动集尘吗？
  entity_count：1
  任务：["扫地机器人X1支持自动集尘吗？"]
  # 单一意图问 → 不拆，原句

- 问题：扫地机器人X1和扫地机器人X2哪个性价比高？它们分别多少钱？
  entity_count：2
  任务：["扫地机器人X1的优缺点、性价比和售价是多少？",
        "扫地机器人X2的优缺点、性价比和售价是多少？"]
  # 多实体 → 每实体 1 条，继承对比+价格意图；"它们分别多少钱"的指代被实体名替换

- 问题：有哪些适合小户型的扫地机器人？分别多少钱？
  entity_count：1（"扫地机器人"为品类实体）
  任务：["有哪些适合小户型的扫地机器人？",
        "适合小户型的扫地机器人各自的售价是多少？"]
  # 列表型 + 详情型并列 → 拆 2；此时任务数 > entity_count 是合法的

- 问题：扫地机器人X1能扫拖一体吗？噪音大吗？续航多久？
  entity_count：1
  任务：["扫地机器人X1能扫拖一体吗？噪音大吗？续航多久？"]
  # 同一商品的属性细节问 → 合并为 1 条原句，不拆

- 问题：有没有千元内能自动回充的扫地机器人？
  entity_count：0
  任务：["有没有千元内能自动回充的扫地机器人？"]
  # 零实体条件句 → 不拆，原句

# —— human 模板（planner/prompts.py）缩减为仅占位，删除其中重复规则——
问题: {question}
```

**节点校验与回退**（改 `components/planner/node.py`；**2026-09-03 v2**——校验依据从"数量锚"改为"任务内容自洽 + 上限"：单任务/空/异常一律强制回填原 query，planner 调用异常并入同一回退出口）：

```python
# planner/node.py 节点核心（v2 形态）
MAX_TASKS = 3                                   # 拆解上限：超过一律回退（宁可少拆）

def _fallback(question: str) -> List[Task]:
    """所有回退的唯一出口：单分支整句检索（question 恒为原 query 原文，不经 LLM）"""
    return [Task(question=question, parent_task=question)]

async def planner(state: InputState) -> Dict[str, Any]:
    question = state.get("question", "")
    try:
        planner_output = await planner_chain.ainvoke({"question": question})
        entity_count = planner_output.entity_count
    except Exception as e:                      # L0：调用/结构化输出异常 → 整句
        logger.error("planner 调用失败，回退单分支整句: {}", e)
        entity_count, planner_output = -1, None

    # 信任拆解的条件：2<=任务数<=MAX_TASKS、sub_query 均非空、互不重复。
    # 不做 len==entity_count 硬校验——列表型拆解（entity_count=1 拆 2）合法；
    # 单任务/空列表/空串/重复/超上限/LLM 异常 → 一律回填原 query（不采用 LLM 单任务文本）
    if (
        planner_output is not None
        and 2 <= len(planner_output.tasks) <= MAX_TASKS
        and all(t.sub_query.strip() for t in planner_output.tasks)
        and len({t.sub_query.strip() for t in planner_output.tasks}) == len(planner_output.tasks)
    ):
        task_list = [
            Task(question=t.sub_query, parent_task=question)
            for t in planner_output.tasks
        ]
        logger.info("planner_decision: split={} (entity_count={})", len(task_list), entity_count)
    else:
        task_list = _fallback(question)
        reason = "llm_error" if planner_output is None else f"tasks={len(planner_output.tasks)}"
        logger.info("planner_decision: fallback (reason={})", reason)

    logger.info("Total Sub Task: {}", len(task_list))
    for i, task in enumerate(task_list):
        logger.info("Sub Task[{}]: {}", i + 1, task.question)
    return {"tasks": task_list}
```

**校验规则**：
- 单任务语义 = "子问单元 ≤1"（原"entity_count<=1"判定废除：entity_count=1 的列表+详情并列句可合法拆 2）
- LLM 返回单任务（无论内容是否=原 query）→ 一律经 `_fallback` 回填原文，防改写
- 空列表 / 空串 sub_query / 任务重复 / 任务数 > MAX_TASKS / LLM 调用异常 → 同一回退出口（宁可少拆，不可错拆）
- 拆解质量（该拆没拆、拆得离谱）不再有数量锚可校验，保障 = prompt 示例 + 上限约束 + 三态日志观测（`split(N)/fallback(llm_error)/fallback(tasks=k)`，回退率统计依据，§2.3 P3/P4）

### 4.3 子图结构简化（删 3 节点 + 直连）

**`workflows/multi_agent/multi_tool.py`**：
- 删除节点与边：`guardrails`（含 `guardrails_conditional_edge`）、`predefined_cypher`、`tool_selection`（含 `map_reduce_planner_to_tool_selection` 的 target 改为 customer_tools）
- 新结构：

```python
main_graph_builder.add_node(planner)
main_graph_builder.add_node("customer_tools", customer_tools)
main_graph_builder.add_node(summarize)
main_graph_builder.add_node(final_answer)

main_graph_builder.add_edge(START, "planner")
main_graph_builder.add_conditional_edges(
    "planner",
    map_reduce_planner_to_tool_selection,  # 改为 Send("customer_tools", {"task": t.question, ...})
    ["customer_tools"],
)
main_graph_builder.add_edge("customer_tools", "summarize")
main_graph_builder.add_edge("summarize", "final_answer")
main_graph_builder.add_edge("final_answer", END)
```

- `create_multi_tool_workflow` 签名简化：删除 `graph`、`tool_schemas`、`predefined_cypher_dict`、`scope_description`、`tool_preference` 参数 → `create_multi_tool_workflow(llm: BaseChatModel)`

**`workflows/multi_agent/edges.py`**：

```python
def map_reduce_planner_to_tool_selection(state: OverallState) -> List[Send]:
    return [
        Send("customer_tools", {"task": task.question, "parent_task": task.parent_task})
        for task in state.get("tasks", list())
    ]
```

> 落地实录：实际函数名改为 `map_reduce_planner_to_customer_tools`（edges.py:10-22），Send payload 含 `task/question/parent_task` 三键（customer_tools 只消费 task，见 §2.3 P2）。

**`lg_builder.py` create_research_plan 清理**：
- 删除：`tool_preference` 三分支（:444-452）、`tool_schemas` 导入与定义（:464-466）、`cypher_dict` 导入（:468）、`scope_description`（:471-481）、`get_neo4j_graph()`（:456-461）、实体识别调用（:522-532）
- 预处理管道缩减：主链路只保留已前置的指代消解；纠错/扩展/Multi-Query 的保留方案见 §8 待确认 #2（✅ 已落地为**直接删除**，2026-08-21：输入子图的 `question` = resolved_question，见 [[SPEC_REMOVE_QUERY_PREPROCESSING.md]]）
- 子图编译改为**模块级单次编译**（llm 通过工厂在编译期注入），消除每请求 `create_multi_tool_workflow + compile` 开销

**删除文件清单**（阶段 3 收尾，独立提交）：
- `components/predefined_cypher/`（node.py、utils.py、cypher_dict.py、descriptions.py、prompts.py）
- `components/guardrails/`（node.py、models.py、prompts.py）
- `components/errors/tool_selection/node.py`（未注册节点的宿主）
- `components/tool_selection/`（node.py、prompts.py）
- `kg_tools_list.py` 中 `predefined_cypher` schema（保留 `vector_search_query`）

### 4.4 customer_tools 改造：HyDE 入内 + 单例化

**单例化**（`components/customer_tools/node.py`，阶段 0 前置执行）：

```python
_vector_store: Optional[VectorStoreQuery] = None

def get_vector_store() -> VectorStoreQuery:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStoreQuery()
    return _vector_store
```

节点内 `VectorStoreQuery()` 实例化处全部替换为 `get_vector_store()`。ChromaDB PersistentClient / SentenceTransformer / 全量文档拉取只在首次请求发生；后续考虑 lifespan 预热或增量刷新（待确认 #6）。

**HyDE 移入分支内**（`create_vector_search_query_node(llm)` 增加 llm 参数）：

```python
query = state.get("task", "")

# HyDE：每个子 query 独立生成假想答案（settings.HYDE_ENABLED 开关，默认 True）
retrieval_queries = [query]
if settings.HYDE_ENABLED:
    try:
        hypo = await generate_hypothetical_answer(llm, query)
        if hypo.strip():
            retrieval_queries.append(hypo)
    except Exception as e:
        logger.warning("HyDE 生成失败，跳过: {}", e)

# 对每个检索 query 分别检索（向量 + 混合），按文档 id 合并去重
merged_results = {}
for q in retrieval_queries:
    vector_results = vector_store.search(q, top_k=settings.VECTOR_SEARCH_TOP_K)
    hybrid_results = ...  # 混合检索（语料缓存，见单例化）
    for doc in (vector_results + hybrid_results):
        merged_results[doc_id(doc)] = doc
hybrid_results = list(merged_results.values())

# 相关性评分（批量一次调用，现状逻辑保留，llm 用传入实例）
if hybrid_results and settings.RELEVANCE_GRADING_ENABLED:
    hybrid_results = await grade_relevance(llm=grader_llm, query=query, documents=hybrid_results, content_key="text")
```

**变化要点**：
- HyDE 由"预处理整句一次"改为"每个子 query 独立一次"，假想答案**单独检索后按 id 合并去重**（替代现状 `enhanced_query = question + 参考线索` 的拼接方式，避免向量信号稀释）
- 并行分支中 HyDE 延迟取 max 不叠加
- `grader_llm` 改为工厂注入（沿用现状 DeepSeek/Ollama 分支逻辑），温度沿用 `settings.LLM_GRADER_TEMPERATURE`

### 4.5 summarize 基于用户原 query

**状态**（`components/state.py`）：

```python
class InputState(TypedDict):
    question: str                    # 预处理后的检索用 query
    original_question: str           # 用户原 query（指代消解后的完整问题）
    data: List[Dict[str, Any]]
    history: Annotated[List[HistoryRecord], update_history]
```

**summarize 节点**（`components/summarize/node.py:50-56`）：`generate_summary.ainvoke` 的 `question` 改为 `state.get("original_question", state.get("question"))`，保证对比意图（"哪个更好"）完整传入。

**lg_builder 组装**（create_research_plan）：

```python
input_state = {
    "question": resolved_question,          # 指代消解后的完整问题
    "original_question": state.question,    # 同上（当前主链路不再二次增强）
    "data": [],
    "history": [],
}
```

### 4.6 语义缓存前置（指代消解后、路由前）

> ✅ 2026-09-02：本节约束全部达成——lookup/update 在消解后无条件调用且先于主图路由（22b0c90，main.py:409-452），user 维度按实例分桶（`get_instance(user_id)`）；三项前提修复见 [[SPEC_SEMANTIC_CACHE_RESOLVE.md]]（已完成，4882018）。下方代码顺序保留为原设计对照。

**顺序**（原设计：analyze_and_route_query 节点内；实际落地：main.py 入口）：

```
ScopeGuard → 指代门控/消解 → 缓存 lookup(key=resolved_question, user 维度)
  ├─ 命中 → 直接返回缓存答案（跳过路由 LLM 与后续全部节点）
  └─ 未命中 → 路由 LLM → ...（回答生成完成后回写缓存）
```

**为什么在消解之后**：`"那个有货吗"` 与 `"扫地机器人X1有货吗"` 语义相同但字面不同，若不先消解，两者成为不同缓存 key，命中率大降。

**前提修复**（`app/services/redis_semantic_cache.py`，复用 docs/SHOP_SAGE_ANALYSIS.md 已确认问题）：
1. 同步 `redis` 客户端 → `redis.asyncio`（消除事件循环阻塞）
2. `lookup` 的 `keys(prefix:vec:*)` 全量扫描 → 按 user 维度维护有序索引（ZSET/hash 分桶）
3. `__init__` 内 `asyncio.create_task(self._auto_cleanup())` → 移出构造器，改 lifespan 统一初始化与清理任务

**缓存键维度**：现状按 user_id 隔离；`/api/langgraph/query` 入口需提供 user 维度标识（从请求头/thread 获取），无法获取时降级为全局匹配（待确认 #5）。

---

## 5. 回退与异常处理总表

> 解读提示（2026-09-02）：位置列 4.1/4.6 的两行指代/缓存场景**现状已由 main 入口侧替代实现**（22b0c90 各自的失败回退在 resolve_pronouns/lookup 的 try/except 内），按 4.1/4.6 节顶部注解读；4.2/4.4 行为阶段 2/4 落地目标，其中"planner 任务含空 question"行为现状实测缺口（§2.3 P2）。

| 场景 | 回退行为 | 位置 |
|---|---|---|
| 指代门控误触发（query 完整无需改写） | LLM 按"完整则原样返回"规则输出原 query，无副作用 | 4.1 |
| 指代消解 LLM 失败/超时 | 用原 query 继续（try/except 包裹） | 4.1 |
| 实体识别 count ≤ 1 | 单分支整句检索 | 4.2 |
| 拆解校验失败（数量不匹配 / sub_query 为空） | 整体回退单分支整句检索 | 4.2 |
| planner 输出为空 | 单任务=原问题（现状兜底逻辑保留） | 4.2 |
| planner 任务含空 question / 重复任务 | ⚠️ 现状**无此校验**：空任务穿透 → Send 空 query → 该分支空记录 → summarize 空答（§2.3 P2，2026-09-02 实测缺口）；阶段 2 落地后整体回退单分支 | 4.2 |
| HyDE 生成失败 | 跳过 HyDE，仅用子 query 检索 | 4.4 |
| 混合检索失败 | 仅用向量检索结果（现状 try/except 保留） | 4.4 |
| 相关性评分失败 | 返回全部检索结果（现状兜底保留） | 4.4 |
| 检索全空 | summarize 现状 "No data to summarize." 分支 | 4.5 |
| 子图整体超时 | TimeoutGuard 30s 降级回答（现状保留） | lg_builder.py:558 |
| 缓存服务不可用 | 跳过缓存直查（try/except） | 4.6 |

---

## 6. 分阶段实施步骤

> 每阶段独立可验证、可提交。提交信息遵循项目规范 `[类型] 简述`，推送 `origin main`（远程无 dev 分支）。

### 阶段 0：前置修复——customer_tools 单例化（并行前提）✅ 已落地

**Files**: `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/customer_tools/node.py`

- [x] **Step 1**: 新增 `get_vector_store()` 模块级懒加载单例（代码见 §4.4，实现含 `threading.Lock` 双重检查，node.py:104-119），节点内 `VectorStoreQuery()` 替换
- [x] **Step 2**: 验证：连续两次请求日志中 `VectorStoreQuery.__init__` 只出现一次；`uv run python -c "from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import get_vector_store; a=get_vector_store(); b=get_vector_store(); assert a is b"`
- [x] **Step 3**: 提交 `[fix] customer_tools 向量库客户端单例化，消除每请求重建 ChromaDB/Embedding 开销`

### 阶段 1：入口指代消解 ✅ 已落地（main 入口侧替代方案，2026-09-02 二修）

> ⚠️ 本阶段目标已由 [[SPEC_ENTRY_LLM_RESOLUTION.md]] 落地（22b0c90）：main.py `/api/langgraph/query` 多轮非语气词消息**无条件 LLM 消解**（`resolve_pronouns` + `RESOLVE_SYSTEM_PROMPT` 6 规则），纯语气词/无历史直通；缓存 lookup/update 无条件调用。**下列原步骤（analyze_and_route_query 内正则门控代码形态）全部不适用**，保留为历史设计供追溯，不再按此执行。

**Files**（原设计）: `llm_backend/app/core/config.py`、`.../components/query_rewriting/node.py`（⚠️ 已删除 2026-08-21）、`llm_backend/app/lg_agent/lg_builder.py`

- [ ] **Step 1**: config 新增 `REWRITE_TEMPERATURE: float = 0.0`、`REWRITE_MAX_TURNS: int = 3`
- [ ] **Step 2**: `query_rewriting/node.py`（⚠️ 已删除 2026-08-21）新增 `REFERENCE_PATTERN` 与 `need_reference_resolution()`（代码见 §4.1）；`_format_chat_history` 增加 `max_turns` 参数（默认 3，调用处传 `settings.REWRITE_MAX_TURNS`）
- [ ] **Step 3**: `analyze_and_route_query` 内 ScopeGuard 之后插入门控+消解逻辑（代码见 §4.1）；`rewrite_llm` 用 `temperature=settings.REWRITE_TEMPERATURE` 实例化；结果存 `state.question`，`state.messages` 不动；路由 LLM 输入用 resolved_question
- [ ] **Step 4**: 验证：
  - 第二轮追问"那个有货吗"→ 日志出现 `上下文感知改写: '那个有货吗' → '扫地机器人X1有货吗'`
  - 完整问题"扫地机器人X1有货吗"（含"这"？否）→ 无改写日志，直走路由
  - 短句"多少钱"且有历史 → 触发改写；首轮"多少钱"（无历史）→ 不触发
- [ ] **Step 5**: 提交 `[feat] 入口指代消解：正则门控 + 3 轮历史 + temperature=0，路由前完成改写`

### 阶段 2：planner 改造（实体识别 + 拆解一次调用）⏳ 未实施（本 spec 剩余核心）

> 前置必读：**§2.3 P1-P5（2026-09-02 实测）**。落地方案要点相对 §4.2 的增补：
> - P1：`PLANNER_SYSTEM_PROMPT` 整体替换为 §4.2 v2 全文（2026-09-03，5 条示例覆盖：多实体对比拆、列表+详情并列拆、同实体多属性合并、单意图不拆、零实体不拆）
> - P2：`parent_task` 移出 LLM schema（由节点注入原 query，见 §4.2 校验代码已体现）；校验规则补"task 去重 + 上限 MAX_TASKS=3"
> - P3：planner 拆解温度与 router 一致收敛为 0（§4.2 提示词规则 + 校验回退已够，温度落地方式实施时定）
> - P4：评估"拆解并入 Router（方案 B，§8 #1）"或"规则直通"，决定前先实测简单句误拆率
> - 与阶段 4 组合实施才闭环（P5，多路证据需合并去重），拆解先行落地后须在 §7.2#2/#3 回归对比质量

**Files**: `.../components/planner/models.py`、`.../components/planner/node.py`、`.../components/planner/prompts.py`、`kg_sub_graph/prompts/kg_prompts.py`

- [ ] **Step 1**: `models.py` 新增 `EntitySubQuery`（name 可空主题词）、重写 `PlannerOutput`（schema 见 §4.2 v2：entity_count `ge=0` 默认 0 作辅助日志、tasks 描述 1..3）
- [ ] **Step 2**: 按 §4.2「替换用完整 prompt 全文（2026-09-03 v2，5 示例）」重写：`PLANNER_SYSTEM_PROMPT`（kg_prompts.py）整体替换 + human 模板（planner/prompts.py）缩减为仅 `问题: {question}`（删除其中与 system 重复的 4 条规则）
- [ ] **Step 3**: `planner/node.py` 校验与回退逻辑（v2 代码见 §4.2：try/except 统一回退出口 + 单任务强制回填原 query + 非空/去重/MAX_TASKS=3 + 三态日志；**无数量锚校验**）
- [ ] **Step 4**: 验证（断言均为 v2 日志口径 `reason=tasks=k`）：
  - "扫地机器人X1和扫地机器人X2哪个性价比高？它们分别多少钱？"（多实体）→ `split=2`，两条 Sub Task 各含实体全名与价格意图，无"它们"指代
  - "有哪些适合小户型的扫地机器人？分别多少钱？"（列表+详情并列，entity_count=1）→ `split=2`（合法：任务数 > entity_count）
  - "扫地机器人X1能扫拖一体吗？噪音大吗？续航多久？"（同实体多属性）→ `fallback (reason=tasks=1)`，任务文本 = 原 query 原文
  - "有没有千元内能自动回充的扫地机器人？"（零实体）→ `fallback (reason=tasks=1)`，任务 = 原 query 原文（非 LLM 改写）
  - mock 抛异常 → `fallback (reason=llm_error)`，无异常上抛；mock 返回空列表 / 含空串 / 任务重复 / 4 条以上 → 均 fallback 单分支=原 query
- [ ] **Step 5**: 提交 `[feat] planner 合并实体识别与任务拆解为一次调用，含一致性校验与单分支回退`

### 阶段 3：子图简化 + lg_builder 清理 + 删 Cypher 遗留 ✅ 已全部落地（2026-09-02 二修）

**Files**: `.../workflows/multi_agent/multi_tool.py`、`.../workflows/multi_agent/edges.py`、`llm_backend/app/lg_agent/lg_builder.py`；删除清单见 §4.3

- [x] **Step 1**: `multi_tool.py` 删 guardrails / predefined_cypher / tool_selection 节点与对应边，START 直连 planner；签名简化为 `create_multi_tool_workflow(llm)`（multi_tool.py:32-80）
- [x] **Step 2**: `edges.py` 的 `map_reduce_planner_to_tool_selection` 改 Send 到 `"customer_tools"`（edges.py:10-22）
- [x] **Step 3**: `lg_builder.py` create_research_plan 清理全部完成：tool_preference 三分支、tool_schemas、cypher_dict、scope_description、neo4j 连接、实体识别调用已删；**子图模块级单次编译已于 2026-09-02 由 29e6f73 落地**——`get_research_graph()` 懒加载单例（lg_builder.py:411-436，双检锁），子图 ainvoke 每请求零重建（实测消除 ~40ms/次并复用 llm httpx 连接池）；`input_state` 增加 `original_question`（见 §4.5）**未实施，归入阶段 4**
- [x] **Step 4**: 删除 §4.3 文件清单（组件目录已删除）
- [x] **Step 5**: 验证：`uv run python -c "import llm_backend.app.lg_agent.lg_builder"` 无导入错误；端到端查询走通；日志无 guardrails/tool_selection/predefined_cypher 记录；`error_tool_selection` 未注册 bug 随代码删除
- [x] **Step 6**: 提交 `[feat] 子图简化：去 guardrails/tool_selection/predefined_cypher，planner 直连 RAG 检索节点` + `[fix] 删除 Cypher 遗留组件与死代码` + `[feat] 售前 MultiTool 子图进程级单例化…（29e6f73）`

> **后续实施注意**：Step 3 唯一剩余项 `original_question` 透传属阶段 4，与 HyDE 一并实施；§2.1 现状链路图已更新为实测形态（见上）。

### 阶段 4：HyDE 移入 customer_tools + 原 query 透传 ⏳ 未实施

> 与阶段 2 组合实施（§2.3 P5：单做实体拆解不保证 top-5 逐实体覆盖，需子 query 增强/合并去重闭环）。实施前核对：§4.4 中"get_vector_store 单例化"代码为阶段 0 历史设计——检索现已收敛于 `RAGRetrieverService.search`（零 LLM），HyDE 实现形态建议为 customer_tools 分支内对每个任务 query 先 HyDE 增强再调 `search`，或下沉 service（实施时定）。

**Files**: `.../components/customer_tools/node.py`、`.../components/state.py`、`.../components/summarize/node.py`、`llm_backend/app/core/config.py`

- [ ] **Step 1**: config 新增 `HYDE_ENABLED: bool = True`
- [ ] **Step 2**: `create_vector_search_query_node(llm)` 改造（代码见 §4.4）：接收 llm、HyDE 开关、假想答案独立检索合并去重
- [ ] **Step 3**: `state.py` InputState 增加 `original_question: str`；`summarize/node.py` 改用 `state.get("original_question", state.get("question"))`（现状 question=完整原 query，见 §2.2 #7 处置）
- [ ] **Step 4**: 验证：双实体对比查询 → 每个分支日志出现独立 HyDE 生成记录；summarize 回答以对比形式呈现两个产品；关闭 `HYDE_ENABLED` 后分支内无 HyDE 日志
- [ ] **Step 5**: 提交 `[feat] HyDE 移入 RAG 工具分支内（开关控制、独立检索合并），summarize 基于用户原 query`

### 阶段 5：语义缓存前置 ✅ 已落地（另两 spec 替代实施，2026-09-02 二修）

> 本阶段目标已全部由替代实施完成，**无需再按本节步骤执行**：Step 1 三项修复于 4882018 落地（redis.asyncio / 用户分桶 ZSET 索引替代全量 keys 扫描 / `_auto_cleanup` 移出构造器改 `start_cleanup()` + 实例池，见 [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] ✅ 已完成）；Step 2 的"消解后查缓存"落在 **main.py 入口**（22b0c90：lookup/update 无条件调用，先于主图路由），而非本节的 analyze_and_route_query 内部——位置更早，语义等价且更优（命中直接短路主图）。以下步骤保留为历史设计。

**Files**（原设计）: `llm_backend/app/services/redis_semantic_cache.py`、`llm_backend/app/lg_agent/lg_builder.py`

- [x] **Step 1**: 修复三项已知问题（§4.6）：`redis.asyncio` 客户端、user 维度索引替代全量 `keys()` 扫描、`_auto_cleanup` 移出构造器改 lifespan 初始化（4882018）
- [x] **Step 2**: 缓存 lookup/update 无条件调用，位置 = main.py 入口（22b0c90；本 spec 原设计 analyze_and_route_query 内插入未采用）
- [◐] **Step 3**: 验证部分完成：test_entry_cache.py 20/20 覆盖入口缓存语义（无历史→lookup 执行未命中）；端到端 SSE 命中场景待 §7.2#8 回归
- [x] **Step 4**: 提交由 4882018（缓存侧）+ 22b0c90（入口侧）在其各自 spec 名下完成

### 阶段 6：收尾验证与文档

- [ ] **Step 1**: 按 §7 验证方案跑完整场景清单，记录调用次数对比表
- [ ] **Step 2**: 更新 README、docs/PROJECT_ANALYSIS.md 与本规格文档状态行中已过期的 Cypher/GraphRAG 描述（本项目当前已执行至该步的 spec 状态同步，后续步骤按 §7 验证后继续）
- [ ] **Step 3**: 提交 `[docs] 同步纯 RAG 链路改造后的文档描述`

---

## 7. 验证方案

### 7.1 LLM 调用次数对比（日志统计法）

> ⚠️ 2026-09-02 实测更新：原"现状（约）10~13 次"基于已删除的预处理/guardrails/grader 链路，**全部过期**。现状（无缓存命中）：入口消解（多轮时才 1 次，首轮无历史不计）+ Router 1 + planner 1 + summarize 1 = **3~4 次**；检索（RAGRetrieverService）、合并、final_answer 均零 LLM。下表为改造前后的实际基线，阶段 2 落地后 planner 仍 1 次（不增调用）、阶段 4 按 `HYDE_ENABLED` 开关在并行分支计入。

| 场景 | 现状（2026-09-02 实测） | 阶段 2/4 后目标 | 验收标准 |
|---|---|---|---|
| 单实体简单查询首轮（"扫地机器人X1多少钱"） | 3（Router+planner+summarize） | ≤3（P4 规则直通若落地可 ≤2） | 日志计数 3；单分支检索 |
| 双实体对比（"A和B哪个好"） | 3（拆 N 分支不增 LLM，检索零调用） | ≤5（阶段 4 HyDE 每分支 +1，2 分支共 +2） | planner 输出 entity_count≥2 且 N 个并行分支日志 `检索节点返回…` ×N |
| 指代追问（第二轮"那个有货吗"） | 4（入口消解 1 + Router+planner+summarize） | ≤4 | 入口消解日志一次后正常走通 |
| 完整问题首轮（多轮对话内） | 4（无条件消解 1 + 主链路 3） | ≤4 | 消解规则判"完整原样返回"不增分支 |
| 缓存命中（重复提问） | 0（main.py 入口短路） | 0 | 仅入口缓存日志，无路由/检索/summarize 日志 |

### 7.2 功能场景清单（人工回归）

1. 单实体事实查询 → 回答正确、引用检索内容
2. 双实体对比查询 → 回答含两个产品各自信息并以对比形式呈现
3. 三实体查询 → 拆 3 个并行分支（验证不硬编码 2）
4. 指代追问（那个/它/多少钱）→ 消解后检索命中
5. 省略句短追问（"有货吗"）且有历史 → 入口无条件消解触发（22b0c90 语义，非门控启发式）
6. 超范围问题（"有衣服吗"）→ ScopeGuard/路由降级 general 分支（respond_to_general_query），不进 RAG 链路
7. 拆解异常（构造场景）→ 回退单分支，无报错
8. 重复提问 → 缓存命中，响应显著变快
9. 检索全空 → 兜底回答，无异常中断
10. 流式输出正常、中断/resume 功能不回归

### 7.3 性能验证

- 首次请求 vs 后续请求耗时对比（验证单例化与图编译复用生效）
- 双实体并行 vs 顺序检索耗时对比（验证 map-reduce 并行收益）

---

## 8. 待确认事项

| # | 事项 | 建议 | 影响 |
|---|---|---|---|
| 1 | ⚠️ 2026-09-02 前提失效更新：Router 已于 2026-08-27 重构为 logic/type/risk（lg_states.py:7-22），**entity_count/complexity 字段不存在**——planner 独立识别无冗余之争，而是"planner 的拆解信息从哪来" | 方案 A（本规格）：planner 独立一次调用（维持现状次数，见 §2.3 P4）；方案 B：扩展 Router schema 输出实体名+子 query，planner 变纯函数再省 1 次——**但 Router 决策（presale）与拆解共用一次 LLM 会加重路由 prompt，需先实测** | 阶段 2 前置评估项（§2.3 P4），B 需改 Router schema 与路由提示词 |
| 2 | 纠错/扩展/Multi-Query 是否保留 | 默认跳过；复杂查询（`complexity > 0.7`）时纠错+扩展合并为 1 次调用（复用 `correct_and_expand`） | ✅ 已决议：2026-08-21 随 BudgetGuard 一并删除（correct_and_expand 无调用者，见 [[SPEC_REMOVE_QUERY_PREPROCESSING.md]]） |
| 3 | 轻量模型分层（LIGHT 档：改写/grader/HyDE 用便宜快模型） | Settings 加第四路 `ServiceType`，改写与 grader 先切 | 成本/延迟再降，需 provider 支持 |
| 4 | summarize 流式化（子图 ainvoke → astream） | 放最后做，体验项 | 首 token 延迟从"全链路完成"降为"检索完成" |
| 5 | 主链路缓存的 user 维度标识来源 | 从请求头/thread_id 解析 user_id；无则全局匹配降级 | 需确认 `/api/langgraph/query` 前端是否传用户标识 |
| 6 | 向量库语料增量刷新（单例化后文档新增不生效） | lifespan 定时/手动刷新接口；或索引更新后失效单例 | 单例化引入的新问题，需定义刷新策略 |

---

## 9. 风险与避坑清单

1. **state.messages 只读**：消解结果绝不可写回消息流（破坏 checkpointer 恢复与审计），必须走独立状态字段（`state.question`）——main 入口侧消解后以 resolved 文本构造新消息（22b0c90 形态），raw 原文不保留于图内消息，该取舍在 [[SPEC_ENTRY_LLM_RESOLUTION.md]] 内定案
2. **temperature=0 不等于语义保真**：语义保护靠提示词规则 + "完整则原样返回"（现状消解走 `RESOLVE_SYSTEM_PROMPT` 6 规则 + `RESOLVE_LLM_TEMPERATURE=0`，config.py:78），阶段 2 planner 提示词按 §4.2 落地时同样不弱化"只识别明确实体"
3. **正则宁多勿漏** ⚠️ 已过时：正则门控已废，替换为多轮无条件消解（22b0c90）；本条仅在阶段 2 评估"规则直通跳过 planner"（§2.3 P4）时重新适用（宁多勿漏→宁不直通）
4. **单例化线程安全** ✅：双检锁模式已在 `get_rag_retriever_service`（cf9e37b）/`get_research_graph`（29e6f73）落地，首建竞态已收口；**语料增量刷新失效机制仍未定义**（待确认 #6，service 持有语料单例）
5. **删 guardrails 的前提**：顶层 ScopeGuard + Router 降级路径必须可靠（超范围 → general-query），回归场景清单 #6
6. **planner 回退宁可少拆**：数量不一致/空子 query 一律单分支，不允许半拆半整——**现状尚无该校验**（§2.3 P2/§5 新行），阶段 2 落地项
7. **并行分支的重资源** ✅：检索已收敛为 service 单例（零 LLM 零模型加载），N 分支并行仅放大 pgvector/BM25 查询与 reranker 线程占用（reranker 走 `asyncio.to_thread`），无模型加载放大
8. **缓存语义正确性**：key 必须用消解后 query + user 维度（现状：入口消解后 lookup + 每用户实例分桶，main.py:409-417）；缓存答案需含"命中时间/来源"标记便于排查陈旧缓存
9. **删除文件前全局检索引用**：删除 §4.3 清单前 grep 全项目确认无残留 import（避免复现 `error_tool_selection` 式未注册引用）
10. **每阶段独立提交**：严格按阶段提交，禁止跨阶段合并提交；每阶段验证通过再进入下一阶段
11. **空任务穿透**（2026-09-02 新增，§2.3 P2）：阶段 2 节点校验（非空/数量一致/去重）落地前，不得依赖"结构化输出必然非空"

---

## 附：文件变更总览

| 文件 | 变更 | 阶段 |
|---|---|---|
| `app/core/config.py` | 阶段 1 实际落地为 +`RESOLVE_LLM_TEMPERATURE`/`RESOLVE_MODEL`（22b0c90，原 `REWRITE_*` 设计未用）；阶段 4 +`HYDE_ENABLED` 待实施 | 1✅/4⏳ |
| `main.py`（入口，替代原 lg_builder 内实现） | 多轮无条件 LLM 消解 + 缓存 lookup/update（22b0c90） | 1✅/5✅ |
| `app/lg_agent/lg_builder.py` | create_research_plan 清理（阶段3✅ 29e6f73 子图单例）；input_state 加 original_question 待实施 | 3✅/4⏳ |
| `app/lg_agent/lg_states.py` | 2026-08-27 路由重构：Router=logic/type/risk（无 entity_count） | — |
| `app/services/pronoun_resolver.py` / `pronoun_detector.py` | 入口消解实现载体（22b0c90）；缓存内 `_resolve_message` 旧正则待专项处理（docs/项目问题.md #11） | 1✅ |
| `components/query_rewriting/node.py` | +门控函数、`max_turns` 参数化（⚠️ 文件已随预处理管道删除 2026-08-21；本 spec 阶段 1 不再按此形态实施） | 1 |
| `components/planner/models.py` | `EntitySubQuery`（name 可空主题词）/`PlannerOutput` 重写：entity_count `ge=0` 默认 0（辅助日志、非校验锚）、tasks 描述 1..3（现状仍为 `tasks: List[Task]`，§2.3 P2） | 2⏳ |
| `components/planner/node.py` | v2 校验：try/except 统一回退出口 + 单任务强制回填原 query + 非空/去重/MAX_TASKS=3 + 三态日志，无数量锚（现状仅空列表回退） | 2⏳ |
| `components/planner/prompts.py` | human 模板缩减为仅 `问题: {question}`（删除与 system 重复的 4 条规则） | 2⏳ |
| `kg_sub_graph/prompts/kg_prompts.py` | `PLANNER_SYSTEM_PROMPT` 整体替换为 §4.2 v2 全文（5 示例；现状仍为北风商贸 Cypher 时代模板，§2.3 P1） | 2⏳ |
| `workflows/multi_agent/multi_tool.py` | 删 3 节点、直连、签名简化 ✅（multi_tool.py:32-80） | 3✅ |
| `workflows/multi_agent/edges.py` | Send 目标改 customer_tools ✅ | 3✅ |
| `components/customer_tools/node.py` | 单例化以 `RAGRetrieverService` 收敛落地 ✅（cf9e37b）；HyDE 入内 ⏳ | 0✅/4⏳ |
| `components/state.py` | InputState +`original_question` | 4⏳ |
| `components/summarize/node.py` | 用 original_question（现状用 question=完整原 query） | 4⏳ |
| `app/services/redis_semantic_cache.py` | 三项修复 ✅（4882018，见 [[SPEC_SEMANTIC_CACHE_RESOLVE.md]] 已完成） | 5✅ |
| §4.3 删除清单 | 删除 Cypher/guardrails/tool_selection 遗留 ✅ | 3✅ |
