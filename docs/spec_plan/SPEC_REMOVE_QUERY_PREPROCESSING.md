# 移除查询预处理管道实施规格（BudgetGuard + 纠错/扩展/Multi-Query+HyDE）

> **用途**: 完全移除 graphrag-query 知识库查询模块（`create_research_plan` 节点）内的查询预处理管道——BudgetGuard 预算控制门控下的 ① 查询纠错（correct_query）② 查询扩展（expand_query）③ Multi-Query + HyDE（rewrite_query）三步 LLM 调用，子图输入直接使用入口指代消解后的问题（resolved_question，未经管道增强），其余功能（ScopeGuard / TimeoutGuard / 多工具子图）不受影响
> **技术栈**: LangGraph 0.3.x + FastAPI + DeepSeek/Ollama
> **状态**: **待实施**（2026-08-21 决策，含 Multi-Query+HyDE 一并删除、物理删除文件、文档同步更新）
> **关联文档**: [[PROJECT_ANALYSIS.md]] [[SPEC_ENTITY_PARALLEL_RAG.md]] [[STUDY_NOTES.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题](#2-现状链路与问题)
3. [删除内容清单](#3-删除内容清单)
4. [目标架构（删除后）](#4-目标架构删除后)
5. [影响面分析（保留项确认）](#5-影响面分析保留项确认)
6. [文档与 spec 同步](#6-文档与-spec-同步)
7. [验证方案](#7-验证方案)
8. [决策记录](#8-决策记录)
9. [风险与避坑清单](#9-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. `lg_builder.py` 的 `create_research_plan` 节点（graphrag-query 知识库查询入口）在进入 Multi-Tool 子图前，先跑一段查询预处理管道：`BudgetGuard` 预算控制门控下的三步串行 LLM 调用——① 查询纠错（错别字修正）② 查询扩展（同义词补充）③ Multi-Query + HyDE（多查询生成 + 假设文档嵌入）
2. 三步均为**非必要**步骤（`essential=False`，大多数场景正确率已高、成本收益比低）
3. `BudgetGuard` 的唯一用途就是门控这三步（`BudgetConfig` 全为代码内默认值 `max_llm_calls=12 / max_total_tokens=50000 / essential_calls_reserved=5`，无外部配置注入）
4. 入口指代消解（main.py `/api/langgraph/query`，RESOLVE_* 配置）已前置——进入节点的 query 已是消解后的完整问题，纠错/扩展在此重复处理同一文本，收益进一步降低

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| LLM 调用次数下降 | graphrag-query 每轮减少 3~4 次 LLM 调用（纠错 1 + 扩展 1 + Multi-Query+HyDE 2 并行） |
| 链路简化 | 删除 1 个安全组件（BudgetGuard）+ 1 个组件目录（query_rewriting/，4 个 git 跟踪文件） |
| 功能保全 | ScopeGuard / TimeoutGuard / 多工具子图 / 语义缓存 / 指代消解全部不变，行为可验证 |

### 1.3 设计原则

1. **只删不重构**：仅移除管道相关代码，不顺手改动其他节点逻辑
2. **子图输入契约不变**：`InputState = {question, data, history}` 三字段完整提供，子图零改动
3. **全项目同步清理**：代码、文档、spec、测试引用一处不漏（CLAUDE.md 全局检索规则）

---

## 2. 现状链路与问题

### 2.1 现状调用链（create_research_plan 节点，lg_builder.py L393-479）

```
create_research_plan
  ├─ 构建 model（DeepSeek/Ollama）
  ├─ create_multi_tool_workflow(llm)          ← 编译子图
  ├─ budget = BudgetGuard()                    ← ① 删除
  ├─ 查询预处理管道（L421-456，全部删除）：
  │    ① correct_query(model, resolved_question)   ← 纠错（can_call 门控, record 300 tokens）
  │    ② expand_query(model, corrected_question)    ← 扩展（can_call 门控, record 300 tokens）
  │    ③ rewrite_query(model, expanded_question)    ← Multi-Query+HyDE（can_call 门控, record 500 tokens）
  │       → rewritten.enhanced_query
  │    logger.info("预处理预算消耗: ...")
  ├─ input_state = {"question": enhanced_question, "data": [], "history": []}
  └─ TimeoutGuard(30s).wrap(multi_tool_workflow.ainvoke(input_state,
        config={"configurable": {"__pregel_checkpointer": None}}))   ← 保留
```

### 2.2 问题

1. 三步预处理均非必要（`essential=False`），每轮固定增加 3~4 次 LLM 调用与响应延迟
2. 入口已做指代消解，纠错/扩展处理的是同一文本，收益重复
3. `BudgetGuard` 的预算上限（12 次调用 / 50000 tokens）在典型查询中远达不到，门控形同虚设却保留代码与概念成本
4. `correct_and_expand` 组合入口全项目无调用者，属死代码

---

## 3. 删除内容清单

### 3.1 代码文件删除（git 跟踪，共 4 个）

```
llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/agent_safety/budget_guard.py
llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/query_rewriting/__init__.py   （空文件）
llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/query_rewriting/node.py        （rewrite_query / generate_multi_queries / generate_hypothetical_answer）
llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/query_rewriting/query_correction.py （correct_query / expand_query / correct_and_expand）
```

### 3.2 代码编辑

| 文件 | 修改 |
|---|---|
| `llm_backend/app/lg_agent/lg_builder.py` | L39-41 import 去掉 `BudgetGuard`（保留 ScopeGuard/TimeoutGuard）；删 L30 `import asyncio`（死 import）；删 L421-456 管道整块；`resolved_question` 保留上移，直接进 `input_state` |
| `.../components/agent_safety/__init__.py` | 删 BudgetGuard import + `__all__` 条目；docstring"4 大防线"改为"2 大防线（ScopeGuard / TimeoutGuard）" |

### 3.3 删除后目标代码形态（create_research_plan 关键段）

```python
    # 创建多工具工作流（planner → 向量检索 → summarize → final_answer）
    multi_tool_workflow = create_multi_tool_workflow(llm=model)

    # 指代消解已前置到系统入口（main.py /api/langgraph/query），
    # 进入本节点的 query 已是消解后的完整问题，直接取当前问题
    resolved_question = state.messages[-1].content if state.messages else ""

    # 准备输入状态 — 直接使用消解后的问题
    input_state = {"question": resolved_question, "data": [], "history": []}

    # 超时保护：包装工作流调用，30 秒超时返回降级回答
    # （__pregel_checkpointer=None 注释与 TimeoutGuard.wrap 参数原样保留）
    timeout = TimeoutGuard(timeout_seconds=settings.RAG_TIMEOUT_SECONDS)
    response = await timeout.wrap(
        multi_tool_workflow.ainvoke(
            input_state,
            config={"configurable": {"__pregel_checkpointer": None}},
        ),
        fallback={"answer": "抱歉，系统处理超时，请稍后再试。"},
        conversation_id=conversation_id or "",
    )
    return {"messages": [AIMessage(content=response["answer"])]}
```

---

## 4. 目标架构（删除后）

```
create_research_plan
  ├─ 构建 model（DeepSeek/Ollama）
  ├─ create_multi_tool_workflow(llm)
  ├─ resolved_question = state.messages[-1].content        ← 入口已消解
  ├─ input_state = {"question": resolved_question, "data": [], "history": []}
  └─ TimeoutGuard(30s).wrap(multi_tool_workflow.ainvoke(input_state,
        config={"configurable": {"__pregel_checkpointer": None}}))
        → AIMessage(answer)
```

子图 `create_multi_tool_workflow` 不变：`planner[读 state.question] → Send(map_reduce) → customer_tools[RAGRetrieverService 混合检索] → summarize → final_answer`。

---

## 5. 影响面分析（保留项确认）

| 组件 | 状态 | 依据 |
|---|---|---|
| `ScopeGuard`（scope_guard.py） | **保留** | lg_builder.py L73 analyze_and_route_query 经营范围预检，与管道无关 |
| `TimeoutGuard`（safety_guards.py） | **保留** | lg_builder.py L470-478 包裹子图调用，依赖 `settings.RAG_TIMEOUT_SECONDS`（config.py:121，.env L81） |
| `create_multi_tool_workflow` 子图 | **保留** | 只读 `state["question"]`；data/history 无消费者，直接构造合法 |
| 入口指代消解 + 语义缓存链路 | **保留** | main.py L267-408，图执行前完成，不经过预处理 |
| memory 层 `TokenBudgetManager` | **保留** | `memory/token_budget.py`，与 BudgetGuard 无关（勿混淆） |
| `__pregel_checkpointer=None` | **保留** | 阶段 3 踩坑后加的（Send 不可序列化），删错会复现已知 bug |
| 前端 / 测试 | 无引用 | frontend/、llm_backend/tests/ 零命中；test_smoke.py 仅 import main |

---

## 6. 文档与 spec 同步

### 6.1 `docs/PROJECT_ANALYSIS.md`（现状文档，需完整自洽，共 9+ 处）

| 位置 | 修改 |
|---|---|
| L203 | 目录树 `agent_safety/ # 护栏（Scope/Budget/Timeout）` → `（Scope/Timeout）`；删 `query_rewriting/` 行 |
| L320-329 | mermaid stateDiagram：删 `QueryPreprocess` 子状态（QueryCorrect→QueryExpand→MultiQueryHyDE），`create_research_plan` 直接 `[*] --> MultiToolWorkflow` |
| L421 | §4.8 护栏表删 BudgetGuard 行 |
| L426-434 | §4.9"查询预处理管道"整节删除（分隔线保留） |
| L464 | mermaid `KG["5.5 graphrag-query<br/>预处理 + 向量检索子图"]` → 去掉"预处理 + " |
| L543 | 节点描述改为"直接进入 Multi-Tool 子图（TimeoutGuard 30 秒超时保护）；查询纠错/扩展/Multi-Query+HyDE 与 BudgetGuard 已移除（2026-08-21）" |
| L545-562 | mermaid：删 P/P1/P2/P3 四个节点，`A --> R` 直连，连线闭环 |
| L711-721 | §8.5"🌟 查询预处理管道 + 预算控制"整节删除；§8.6 顺延编号为 §8.5 |
| L758/L760 | "多层护栏"删"预算控制"字样；"成本控制意识"改为"语义缓存降本 + 入口指代消解门控" |
| L971/L981 | 删"查询预处理"字样；L981 替换为"入口指代消解 + 语义缓存前置（图执行前完成上下文处理与缓存短路），子图保持精简" |

**保留不动**：L407-413、L689、L980 的"预算"是 memory 层 TokenBudgetManager 描述，勿误删。

### 6.2 对比/笔记文档

- `docs/SHOP_SAGE_ANALYSIS.md` L61-64：调用链块 `查询预处理 5 步` → `multi_tool 子图`，块前加"⚠️ 该调用链描述滞后"注
- `STUDY_NOTES.md` L1154：责任链模式条目（"改写→纠错→扩展→HyDE 流水线"）替换为"**装饰器模式**：TimeoutGuard.wrap 包装子图调用，超时自动降级返回兜底回答"

### 6.3 spec_plan 系列（只标注不重写，沿用 L58 过期标注先例）

| 文件 | 位置 | 标注 |
|---|---|---|
| `SPEC_ENTITY_PARALLEL_RAG.md` | L5 / L58 / L124 / L243 / L381 / L384 / L484 / L514 | 追加"已于 2026-08-21 删除"标注（预处理 5 步 → 直接删除；L484 待确认 #2 → ✅ 已决议删除） |
| `SPEC_ENTITY_RECOGNITION_AND_RAG_RETRIEVAL.md` | L182 | `generate_hypothetical_answer` 复用现状 → 加注"该函数已随 query_rewriting 目录删除" |
| `SPEC_CONTEXT_ENGINEERING.md` | L630 | 表格"查询预处理管道（5步）"单元格加注"（已删除 2026-08-21）" |

---

## 7. 验证方案（按序执行）

1. **引用清零**：`grep -rn "BudgetGuard\|budget_guard\|query_rewriting\|correct_query\|expand_query\|rewrite_query\|generate_multi_queries\|generate_hypothetical_answer" --include="*.py" llm_backend` → 预期零命中（docs/spec 中的标注性提及除外）
2. **import 冒烟**：`cd llm_backend && uv run python -c "import main"`、`uv run python -c "import app.lg_agent.lg_builder"`
3. **graph compile**：`uv run python -c "from app.lg_agent.lg_builder import builder; g = builder.compile(); print(g.get_graph().nodes.keys())"` → 5 节点仍在（analyze_and_route_query / respond_to_general_query / get_additional_info / create_research_plan / create_image_query），编译无错
4. **测试**：`uv run pytest llm_backend/tests/test_smoke.py -v`（test_app_importable）；全量 `uv run pytest -q`
5. **可选端到端**：启动服务发 graphrag 查询，日志确认无 `预处理预算消耗`、无 `query_correction` 等行，回答正常流式返回

---

## 8. 决策记录

| 决策点 | 决议（2026-08-21） |
|---|---|
| Multi-Query + HyDE（rewrite_query，③）是否保留 | **一并删除**——用户确认，彻底降本。注意：指代消解在入口（main.py `/api/langgraph/query`）已执行，`state.messages[-1].content` 拿到的就是消解后的完整问题（resolved_question）；删除管道后子图输入直接用它，**不再经过纠错/扩展/HyDE 改写**（"原始问题"指未经管道增强的问题，非未消解的原始输入） |
| 文件处理方式 | **物理删除**——budget_guard.py + query_rewriting/ 整目录（correct_and_expand 无调用者，零残留） |
| 文档是否同步 | **同步更新**——PROJECT_ANALYSIS.md 完整自洽、spec 系列标注、STUDY_NOTES 替换条目 |

---

## 9. 风险与避坑清单

1. **stale import**：lg_builder.py L30 `import asyncio` 管道删除后成死 import，必须一并删（全文已确认无其他 asyncio 使用）
2. **子图输入兼容性**：`__pregel_checkpointer=None` 与 TimeoutGuard.wrap 参数必须原样保留——阶段 3 踩坑后加的（Send 不可序列化、超时降级），删错会复现已知 bug
3. **勿伤无关组件**：`scope_guard.py`、`safety_guards.py`、`memory/token_budget.py`（TokenBudgetManager）绝不碰；`agent_safety/__init__.py` 只动 BudgetGuard 相关 3 处
4. **文档误删风险**：PROJECT_ANALYSIS.md L407-413 / L689 / L980 的"预算"是 memory 层描述，保留；改 L758/L760 时只删"预算控制"字样
5. **mermaid 语法完整性**：PROJECT_ANALYSIS.md 两处 mermaid 删节点后必须连线闭环（无悬挂箭头指向已删节点），否则 GitHub 渲染报错
6. **spec_plan 只标注不重写**：正文是历史实施记录，直接改写破坏文档时间线；沿用 L58 已存在的"⚠️ 过期描述"标注先例
7. **并发修改冲突**：lg_builder.py 是高频文件，实施前 `git status` 确认无未提交改动
8. **__pycache__ 残留**：删除模块的 .pyc 未被 git 跟踪，不影响仓库；Python 会跳过孤儿 pyc
