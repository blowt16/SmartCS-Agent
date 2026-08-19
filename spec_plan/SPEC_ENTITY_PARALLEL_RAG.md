# 多实体并行 RAG 检索链路重构实施规格

> **用途**: 去除 Cypher 查询、纯 RAG 检索后，重构查询预处理与子图链路——入口指代消解（正则门控 + LLM）+ 实体识别与任务拆解合并为一次 LLM 调用 + 多实体并行 RAG 检索（HyDE 内置于检索工具），并配套 LLM 调用成本与响应时间优化  
> **技术栈**: LangGraph 0.3.x + FastAPI + ChromaDB/pgvector + Redis + DeepSeek/Ollama  
> **状态**: 设计规格（方案已敲定），待人工审查后实施  
> **关联文档**: [[PROJECT_ANALYSIS.md]] [[PLAN_GraphRAG_TO_StandardRAG.md]] [[SPEC_CONTEXT_ENGINEERING.md]] [[docs/SHOP_SAGE_ANALYSIS.md]]

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

1. 已去除 Cypher 查询（Text2Cypher/预定义 Cypher 模板），知识库检索收敛为**纯 RAG**（向量检索 + 混合检索 + 相关性评分），Neo4j 不再参与查询链路
2. 现状一次知识库查询需 **10~13 次 LLM 调用**（路由、改写、纠错、实体识别、扩展、Multi-Query+HyDE、guardrails、planner、tool_selection、相关性评分、summarize），其中多处已失去存在意义（实体识别结果仅打日志、单工具场景的 LLM 工具选择、与顶层路由重复的 guardrails）
3. 已确认的运行期问题（详见 docs/SHOP_SAGE_ANALYSIS.md P0 清单）：
   - `tool_selection` 无工具可选时 `Send("error_tool_selection")` 到未注册节点（`tool_selection/node.py:128` vs `multi_tool.py:107-113`）
   - `tool_preference="predefined_cypher"` 快路径传空参数恒失败（`lg_builder.py:444-446` + `predefined_cypher/node.py:53-57`）
   - `customer_tools` 每请求重建 ChromaDB client + SentenceTransformer + 全量拉取文档（`customer_tools/node.py:126-151`）

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| LLM 调用次数下降 | 单实体查询 ≤6 次；双实体对比查询 ≤9 次（其中 4 次在并行分支）；缓存命中 0 次 |
| 响应时间下降 | 消除每请求重建模型/客户端/图编译的开销（秒级 → 毫秒级） |
| 多实体对比查询质量提升 | "A 和 B 哪个好"拆为 N 个子 query 并行检索，按实体覆盖产品信息 |
| 消灭已知运行期 bug | 上述 3 个问题随结构简化一并删除 |
| 语义保护 | 指代消解只补全、不改义；summarize 基于用户原 query 回答 |

### 1.3 设计原则

1. **state.messages 只读**（沿用 SPEC_CONTEXT_ENGINEERING.md 原则二）：指代消解结果存独立字段，不替换、不修改原始消息流
2. **宁多勿漏**：正则门控宁可误触发（代价=1 次 LLM 调用），不可漏检（代价=整条检索链路失效）
3. **确定性优先**：实体数量≥2 才拆解；拆解结果必须通过一致性校验，失败一律回退单分支
4. **temperature=0 + 提示词约束双保险**：低温保证输出确定性，语义保真靠提示词规则（只补全、不扩展、不添加历史没有的信息）

---

## 2. 现状链路与问题

### 2.1 现状调用链（一次 graphrag-query）

```
analyze_and_route_query（路由 LLM，输出 Router 含 complexity/entity_count）
  → create_research_plan
      ├─ 预处理 5 步：上下文改写(1) → 纠错(1) → 实体识别链接 Neo4j(1，结果仅打日志)
      │              → 扩展(1) → Multi-Query+HyDE(2 并行)
      └─ 编译子图 ainvoke
           guardrails(1) → planner(1) → tool_selection(1/任务)
             → predefined_cypher | customer_tools(相关性评分 1/任务)
             → summarize(1) → final_answer
```

### 2.2 问题清单（本方案要解决的）

| # | 问题 | 处置 |
|---|---|---|
| 1 | 指代消解在**路由之后**（`create_research_plan` 内），路由器看到未消解 query | 前置到入口（路由 LLM 之前） |
| 2 | 指代消解无门控，每轮必调 LLM（首条消息除外） | 正则门控 + 短 query 启发式 |
| 3 | 实体识别结果（实体 ID/类型）仅打日志，不进入下游 | 改造为 planner 拆解的依据（实体名 → 子 query） |
| 4 | 实体识别、planner、tool_selection 三次 LLM 调用职责重叠 | 合并为 planner 一次调用（识别+拆解）；tool_selection 删除（单工具直连） |
| 5 | guardrails 与顶层路由（ScopeGuard + Router）重复判定经营范围 | 删除，保留顶层守卫 |
| 6 | HyDE 在预处理阶段针对整句做一次，拆解后各子 query 无法利用 | 移入 customer_tools 内部，每个子 query 独立 HyDE |
| 7 | summarize 使用预处理后的 question，对比意图可能被稀释 | 透传 original_question（指代消解后的完整原问题） |
| 8 | customer_tools 每请求重建重型资源，并行分支下放大 | 模块级单例（阶段 0 前置） |
| 9 | 语义缓存只服务 `/api/chat`，主链路 `/api/langgraph/query` 未用 | 前置到入口（指代消解后、路由前） |

---

## 3. 目标架构总览

```
入口（/api/langgraph/query）
  → analyze_and_route_query 节点内部：
      ScopeGuard 关键词预检
      → 指代门控：正则命中 或 (query≤8字 且 有历史) ?
          ├─ 是 → LLM 指代消解（最近3轮，temperature=0，结构化输出）→ resolved_question 存入 state.question
          └─ 否 → resolved_question = 原文
      → 语义缓存 lookup（key=resolved_question，user 维度）
          ├─ 命中 → 直接返回缓存答案（跳过路由与后续全部节点）
          └─ 未命中 → 路由 LLM（输出 Router）
              ├─ general-query / additional-query / image-query → 原路径不变
              └─ graphrag-query → create_research_plan
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

**关键结构变化**：删除 guardrails、tool_selection、predefined_cypher 三个节点；planner 直连 customer_tools；保留 map-reduce `Send` 并行结构与 `cyphers` 累加器（`state.py:67`）。

---

## 4. 模块详细设计

### 4.1 入口指代消解（正则门控 + LLM）

**位置**：`analyze_and_route_query` 节点内部，ScopeGuard 预检之后、路由 LLM 调用之前（`lg_builder.py:76-81` 之后）。

**门控函数**（新增于 `components/query_rewriting/node.py`）：

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

**结构化输出 schema**（替换 `components/planner/models.py` 的 `PlannerOutput`）：

```python
class EntitySubQuery(BaseModel):
    name: str        # 实体名，如 "扫地机器人X1"
    sub_query: str   # 继承原意图的子 query，如 "扫地机器人X1的优缺点和适用场景是什么？"

class PlannerOutput(BaseModel):
    entity_count: int = Field(description="识别出的产品/类别实体数量")
    tasks: List[EntitySubQuery] = Field(
        default_factory=list,
        description="entity_count>=2 时每个实体一项；entity_count<=1 时为空或仅一项",
    )
```

**提示词规则**（重写 `kg_sub_graph/prompts/kg_prompts.py` 的 `PLANNER_SYSTEM_PROMPT`）：

1. 识别 query 中的产品/类别实体（Product/Category 类型），输出 `entity_count`；无实体则为 0
2. `entity_count >= 2` 时，为**每个实体**生成一个独立子 query：
   - 必须包含实体全名，无指代、无省略
   - **必须继承原 query 的意图维度**：对比/哪个好 → 优缺点、参数、适用场景；价格 → 售价、优惠；选购 → 适合人群、口碑
   - 子 query 之间独立、不重叠、不依赖其他任务结果
   - 不硬编码 2 个：实体数 N 拆 N 个
3. `entity_count <= 1` 时：`tasks` 为空或仅一项（`sub_query` 可空），表示单分支整句检索
4. 只识别明确实体，不过度推断；Unknown 类型不计入 entity_count

**节点校验与回退**（改 `components/planner/node.py:42-71`）：

```python
planner_output = await planner_chain.ainvoke({"question": state.get("question", "")})

if (
    planner_output.entity_count >= 2
    and len(planner_output.tasks) == planner_output.entity_count
    and all(t.sub_query.strip() for t in planner_output.tasks)
):
    task_list = [
        Task(question=t.sub_query, parent_task=state.get("question", ""))
        for t in planner_output.tasks
    ]
else:
    # 回退：单分支整句检索
    task_list = [Task(question=state.get("question", ""), parent_task=state.get("question", ""))]
```

**校验规则**：实体数 ≥2 但任务数与实体数不一致、或任一 `sub_query` 为空 → 整体回退单分支（宁可少拆，不可错拆）。

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

**`lg_builder.py` create_research_plan 清理**：
- 删除：`tool_preference` 三分支（:444-452）、`tool_schemas` 导入与定义（:464-466）、`cypher_dict` 导入（:468）、`scope_description`（:471-481）、`get_neo4j_graph()`（:456-461）、实体识别调用（:522-532）
- 预处理管道缩减：主链路只保留已前置的指代消解；纠错/扩展/Multi-Query 的保留方案见 §8 待确认 #2（默认跳过，输入子图的 `question` = resolved_question）
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

**顺序**（analyze_and_route_query 节点内）：

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

| 场景 | 回退行为 | 位置 |
|---|---|---|
| 指代门控误触发（query 完整无需改写） | LLM 按"完整则原样返回"规则输出原 query，无副作用 | 4.1 |
| 指代消解 LLM 失败/超时 | 用原 query 继续（try/except 包裹） | 4.1 |
| 实体识别 count ≤ 1 | 单分支整句检索 | 4.2 |
| 拆解校验失败（数量不匹配 / sub_query 为空） | 整体回退单分支整句检索 | 4.2 |
| planner 输出为空 | 单任务=原问题（现状兜底逻辑保留） | 4.2 |
| HyDE 生成失败 | 跳过 HyDE，仅用子 query 检索 | 4.4 |
| 混合检索失败 | 仅用向量检索结果（现状 try/except 保留） | 4.4 |
| 相关性评分失败 | 返回全部检索结果（现状兜底保留） | 4.4 |
| 检索全空 | summarize 现状 "No data to summarize." 分支 | 4.5 |
| 子图整体超时 | TimeoutGuard 30s 降级回答（现状保留） | lg_builder.py:558 |
| 缓存服务不可用 | 跳过缓存直查（try/except） | 4.6 |

---

## 6. 分阶段实施步骤

> 每阶段独立可验证、可提交。提交信息遵循项目规范 `[类型] 简述`，推送 `origin main`（远程无 dev 分支）。

### 阶段 0：前置修复——customer_tools 单例化（并行前提）

**Files**: `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/customer_tools/node.py`

- [ ] **Step 1**: 新增 `get_vector_store()` 模块级懒加载单例（代码见 §4.4），节点内两处 `VectorStoreQuery()` 替换
- [ ] **Step 2**: 验证：连续两次请求日志中 `VectorStoreQuery.__init__` 只出现一次；`uv run python -c "from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import get_vector_store; a=get_vector_store(); b=get_vector_store(); assert a is b"`
- [ ] **Step 3**: 提交 `[fix] customer_tools 向量库客户端单例化，消除每请求重建 ChromaDB/Embedding 开销`

### 阶段 1：入口指代消解

**Files**: `llm_backend/app/core/config.py`、`.../components/query_rewriting/node.py`、`llm_backend/app/lg_agent/lg_builder.py`

- [ ] **Step 1**: config 新增 `REWRITE_TEMPERATURE: float = 0.0`、`REWRITE_MAX_TURNS: int = 3`
- [ ] **Step 2**: `query_rewriting/node.py` 新增 `REFERENCE_PATTERN` 与 `need_reference_resolution()`（代码见 §4.1）；`_format_chat_history` 增加 `max_turns` 参数（默认 3，调用处传 `settings.REWRITE_MAX_TURNS`）
- [ ] **Step 3**: `analyze_and_route_query` 内 ScopeGuard 之后插入门控+消解逻辑（代码见 §4.1）；`rewrite_llm` 用 `temperature=settings.REWRITE_TEMPERATURE` 实例化；结果存 `state.question`，`state.messages` 不动；路由 LLM 输入用 resolved_question
- [ ] **Step 4**: 验证：
  - 第二轮追问"那个有货吗"→ 日志出现 `上下文感知改写: '那个有货吗' → '扫地机器人X1有货吗'`
  - 完整问题"扫地机器人X1有货吗"（含"这"？否）→ 无改写日志，直走路由
  - 短句"多少钱"且有历史 → 触发改写；首轮"多少钱"（无历史）→ 不触发
- [ ] **Step 5**: 提交 `[feat] 入口指代消解：正则门控 + 3 轮历史 + temperature=0，路由前完成改写`

### 阶段 2：planner 改造（实体识别 + 拆解一次调用）

**Files**: `.../components/planner/models.py`、`.../components/planner/node.py`、`kg_sub_graph/prompts/kg_prompts.py`

- [ ] **Step 1**: `models.py` 新增 `EntitySubQuery`，重写 `PlannerOutput`（schema 见 §4.2）
- [ ] **Step 2**: `kg_prompts.py` 的 `PLANNER_SYSTEM_PROMPT` 按 §4.2 四条规则重写（含双实体对比示例："扫地机器人A和B哪个更好" → 2 个子 query 各含"优缺点和适用场景"）
- [ ] **Step 3**: `planner/node.py` 校验与回退逻辑（代码见 §4.2）
- [ ] **Step 4**: 验证：
  - "扫地机器人A和B哪个更好" → 日志 `Total Sub Task: 2`，两任务并行
  - "扫地机器人X1多少钱" → `Total Sub Task: 1`，单分支
  - 构造拆解失败（临时改低 temperature 或用模糊问题观察）→ 回退单分支
- [ ] **Step 5**: 提交 `[feat] planner 合并实体识别与任务拆解为一次调用，含一致性校验与单分支回退`

### 阶段 3：子图简化 + lg_builder 清理 + 删 Cypher 遗留

**Files**: `.../workflows/multi_agent/multi_tool.py`、`.../workflows/multi_agent/edges.py`、`llm_backend/app/lg_agent/lg_builder.py`；删除清单见 §4.3

- [ ] **Step 1**: `multi_tool.py` 删 guardrails / predefined_cypher / tool_selection 节点与对应边，START 直连 planner；签名简化为 `create_multi_tool_workflow(llm)`
- [ ] **Step 2**: `edges.py` 的 `map_reduce_planner_to_tool_selection` 改 Send 到 `"customer_tools"`（代码见 §4.3）
- [ ] **Step 3**: `lg_builder.py` create_research_plan 清理（清单见 §4.3）：删 tool_preference 三分支、tool_schemas、cypher_dict、scope_description、neo4j 连接、实体识别调用；`input_state` 增加 `original_question`（见 §4.5）；子图改为模块级单次编译
- [ ] **Step 4**: 删除 §4.3 文件清单（独立提交）
- [ ] **Step 5**: 验证：`uv run python -c "import llm_backend.app.lg_agent.lg_builder"` 无导入错误；端到端查询走通；日志无 guardrails/tool_selection/predefined_cypher 记录；`error_tool_selection` 未注册 bug 随代码删除
- [ ] **Step 6**: 提交 `[feat] 子图简化：去 guardrails/tool_selection/predefined_cypher，planner 直连 RAG 检索节点` + `[fix] 删除 Cypher 遗留组件与死代码`

### 阶段 4：HyDE 移入 customer_tools + 原 query 透传

**Files**: `.../components/customer_tools/node.py`、`.../components/state.py`、`.../components/summarize/node.py`、`llm_backend/app/core/config.py`

- [ ] **Step 1**: config 新增 `HYDE_ENABLED: bool = True`
- [ ] **Step 2**: `create_vector_search_query_node(llm)` 改造（代码见 §4.4）：接收 llm、HyDE 开关、假想答案独立检索合并去重
- [ ] **Step 3**: `state.py` InputState 增加 `original_question: str`；`summarize/node.py` 改用 `state.get("original_question", state.get("question"))`
- [ ] **Step 4**: 验证：双实体对比查询 → 每个分支日志出现独立 HyDE 生成记录；summarize 回答以对比形式呈现两个产品；关闭 `HYDE_ENABLED` 后分支内无 HyDE 日志
- [ ] **Step 5**: 提交 `[feat] HyDE 移入 RAG 工具分支内（开关控制、独立检索合并），summarize 基于用户原 query`

### 阶段 5：语义缓存前置

**Files**: `llm_backend/app/services/redis_semantic_cache.py`、`llm_backend/app/lg_agent/lg_builder.py`

- [ ] **Step 1**: 修复三项已知问题（§4.6）：`redis.asyncio` 客户端、user 维度索引替代全量 `keys()` 扫描、`_auto_cleanup` 移出构造器改 lifespan 初始化
- [ ] **Step 2**: `analyze_and_route_query` 内指代消解后插入缓存 lookup，命中直返（代码见 §4.6）；回答生成后回写缓存（挂 on_complete 或节点返回值处）
- [ ] **Step 3**: 验证：同一问题连续两次提问 → 第二次日志出现缓存命中且**无路由/检索/summarize 日志**；指代追问"那个有货吗"（前轮问过同款）→ 消解后命中同一缓存 key
- [ ] **Step 4**: 提交 `[feat] 语义缓存前置到主链路（指代消解后查缓存）+ 修复同步客户端/全量扫描/任务泄漏`

### 阶段 6：收尾验证与文档

- [ ] **Step 1**: 按 §7 验证方案跑完整场景清单，记录调用次数对比表
- [ ] **Step 2**: 更新 README 与 docs/PROJECT_ANALYSIS.md 中已过期的 Cypher/GraphRAG 描述
- [ ] **Step 3**: 提交 `[docs] 同步纯 RAG 链路改造后的文档描述`

---

## 7. 验证方案

### 7.1 LLM 调用次数对比（日志统计法）

现状每个节点均有 `logger.info` 打点，按日志行统计各类场景调用次数：

| 场景 | 现状（约） | 目标 | 验收标准 |
|---|---|---|---|
| 单实体简单查询（"扫地机器人X1多少钱"） | 10 | ≤6 | 路由+改写(按需)+planner+grader+summarize |
| 双实体对比（"A和B哪个好"） | 13 | ≤9（4 次在并行分支） | planner 1 次输出 entity_count≥2 且 2 个并行分支 |
| 指代追问（第二轮"那个有货吗"） | 10+ | ≤7 | 门控触发改写后正常走通 |
| 完整问题首轮 | 10 | ≤5（无改写） | 正则未命中，跳过改写 |
| 缓存命中（重复提问） | 10+ | 0 | 仅缓存日志，无路由/检索/summarize 日志 |

### 7.2 功能场景清单（人工回归）

1. 单实体事实查询 → 回答正确、引用检索内容
2. 双实体对比查询 → 回答含两个产品各自信息并以对比形式呈现
3. 三实体查询 → 拆 3 个并行分支（验证不硬编码 2）
4. 指代追问（那个/它/多少钱）→ 消解后检索命中
5. 省略句短追问（"有货吗"）且有历史 → 门控启发式触发消解
6. 超范围问题（"有衣服吗"）→ ScopeGuard/路由降级 general-query，不进 RAG 链路
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
| 1 | 顶层 Router 已输出 `entity_count`（`lg_states.py:17`），planner 再次识别实体是否冗余 | 方案 A（本规格）：planner 独立一次调用（已敲定）；方案 B：Router schema 扩展输出实体名+子 query，planner 变纯函数，再省 1 次调用 | B 可作后续优化，需改 Router schema 与路由提示词 |
| 2 | 纠错/扩展/Multi-Query 是否保留 | 默认跳过；复杂查询（`complexity > 0.7`）时纠错+扩展合并为 1 次调用（复用 `correct_and_expand`） | 不保留则复杂查询召回略降；保留则 +1~2 次调用 |
| 3 | 轻量模型分层（LIGHT 档：改写/grader/HyDE 用便宜快模型） | Settings 加第四路 `ServiceType`，改写与 grader 先切 | 成本/延迟再降，需 provider 支持 |
| 4 | summarize 流式化（子图 ainvoke → astream） | 放最后做，体验项 | 首 token 延迟从"全链路完成"降为"检索完成" |
| 5 | 主链路缓存的 user 维度标识来源 | 从请求头/thread_id 解析 user_id；无则全局匹配降级 | 需确认 `/api/langgraph/query` 前端是否传用户标识 |
| 6 | 向量库语料增量刷新（单例化后文档新增不生效） | lifespan 定时/手动刷新接口；或索引更新后失效单例 | 单例化引入的新问题，需定义刷新策略 |

---

## 9. 风险与避坑清单

1. **state.messages 只读**：消解结果绝不可写回消息流（破坏 checkpointer 恢复与审计），必须走独立状态字段（`state.question`）
2. **temperature=0 不等于语义保真**：语义保护靠提示词三条规则 + "完整则原样返回"，实施时保留现有 `CONTEXT_REWRITE_SYSTEM_PROMPT` 规则不弱化
3. **正则宁多勿漏**：误触发代价 1 次 LLM 调用，漏检代价整链路失效；词表上线后按日志统计漏检样本迭代
4. **单例化线程安全**：`get_vector_store()` 懒加载在并发首请求下可能双初始化，用锁或 lifespan 预热；语料刷新需失效机制（待确认 #6）
5. **删 guardrails 的前提**：顶层 ScopeGuard + Router 降级路径必须可靠（超范围 → general-query），回归场景清单 #6
6. **planner 回退宁可少拆**：数量不一致/空子 query 一律单分支，不允许半拆半整
7. **并行分支的重资源**：单例化必须先于并行改造（阶段 0 前置），否则 N 分支放大模型加载
8. **缓存语义正确性**：key 必须用消解后 query + user 维度；缓存答案需含"命中时间/来源"标记便于排查陈旧缓存
9. **删除文件前全局检索引用**：删除 §4.3 清单前 grep 全项目确认无残留 import（避免复现 `error_tool_selection` 式未注册引用）
10. **每阶段独立提交**：严格按阶段提交，禁止跨阶段合并提交；每阶段验证通过再进入下一阶段

---

## 附：文件变更总览

| 文件 | 变更 | 阶段 |
|---|---|---|
| `app/core/config.py` | +`REWRITE_TEMPERATURE`/`REWRITE_MAX_TURNS`/`HYDE_ENABLED` | 1/4 |
| `app/lg_agent/lg_builder.py` | 入口门控+消解、缓存前置、create_research_plan 清理、input_state 加 original_question | 1/3/5 |
| `app/lg_agent/lg_states.py` | 不变（question 字段复用） | — |
| `components/query_rewriting/node.py` | +门控函数、`max_turns` 参数化 | 1 |
| `components/planner/models.py` | `EntitySubQuery`/`PlannerOutput` 重写 | 2 |
| `components/planner/node.py` | 校验与回退逻辑 | 2 |
| `kg_sub_graph/prompts/kg_prompts.py` | `PLANNER_SYSTEM_PROMPT` 重写 | 2 |
| `workflows/multi_agent/multi_tool.py` | 删 3 节点、直连、签名简化 | 3 |
| `workflows/multi_agent/edges.py` | Send 目标改 customer_tools | 3 |
| `components/customer_tools/node.py` | 单例化 + HyDE 入内 | 0/4 |
| `components/state.py` | InputState +`original_question` | 4 |
| `components/summarize/node.py` | 用 original_question | 4 |
| `app/services/redis_semantic_cache.py` | 三项修复 | 5 |
| §4.3 删除清单 | 删除 Cypher/guardrails/tool_selection 遗留 | 3 |
