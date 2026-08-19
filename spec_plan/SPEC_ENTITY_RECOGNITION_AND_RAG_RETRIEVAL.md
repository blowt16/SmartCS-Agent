# 实体识别与并行 RAG 检索流程设计规格

> **用途**: 深入设计纯 RAG 链路的两个核心机制——**实体识别**（LLM 实现，与任务拆解合并为一次调用）与**多实体并行 RAG 检索流程**（子 query → HyDE → 向量/混合检索 → 合并去重 → 相关性评分 → 汇总回答），含数据模型、提示词规则、回退策略、验证方案与未来演进路线  
> **技术栈**: LangGraph 0.3.x（Send map-reduce）+ pgvector + Redis + DeepSeek/Ollama + qwen text-embedding-v4（向量统一走 DashScope API；SentenceTransformer 仅 LOCAL 分支预留）  
> **状态**: 设计规格，待审查后实施（结构前提已满足：阶段 0 单例化与阶段 3 子图简化均已落地）  
> **关联文档**: [[SPEC_ENTITY_PARALLEL_RAG.md]]（主线改造规格，本文为其 §4.2/§4.4 的机制展开）[[PLAN_GraphRAG_TO_StandardRAG.md]] [[PROJECT_ANALYSIS.md]]

---

## 目录

1. [范围与定位](#1-范围与定位)
2. [总体流程](#2-总体流程)
3. [实体识别设计](#3-实体识别设计)
4. [子查询拆解设计](#4-子查询拆解设计)
5. [RAG 检索流程设计（分支内部）](#5-rag-检索流程设计分支内部)
6. [并行执行与状态数据流](#6-并行执行与状态数据流)
7. [端到端示例](#7-端到端示例)
8. [回退与异常处理](#8-回退与异常处理)
9. [验证方案](#9-验证方案)
10. [与主线规格的接口约定](#10-与主线规格的接口约定)

---

## 1. 范围与定位

本文档是主线规格 `SPEC_ENTITY_PARALLEL_RAG.md` 的核心机制展开版，两者分工：

| 文档 | 职责 |
|---|---|
| SPEC_ENTITY_PARALLEL_RAG.md | 整体链路改造：入口指代消解、子图简化、缓存前置、分阶段实施步骤 |
| **本文档** | 实体识别与 RAG 检索**机制本身**：schema、提示词、算法流程、边界条件、演进路线 |

依赖关系：本文档的机制运行在主线规格改造后的图结构上（planner 直连 customer_tools，无 guardrails/tool_selection），输入为入口指代消解后的 `resolved_question`。

## 2. 总体流程

```
resolved_question（入口指代消解后，含原始意图）
  → planner 节点（一次 LLM 调用）
      输出 { entity_count, tasks: [{name, sub_query}] }
  → 条件：entity_count ≥ 2 且校验通过
      ├─ 是 → Send("customer_tools") × N 并行扇出（map-reduce）
      │       每个分支独立执行 §5 检索流程
      │       各分支 records 经 cyphers 累加器合并
      └─ 否 → 单分支 Send（整句检索）
  → summarize（LLM，基于 original_question + 全部 records）
  → final_answer → 输出 answer + history 记录
```

**时序特征**：N 个分支并行执行，分支内 HyDE/检索/评分串行；总延迟 ≈ max(各分支延迟) + summarize，而非各分支延迟之和。

## 3. 实体识别设计

### 3.1 当前方案：LLM 识别（与拆解合并一次调用）

识别不单独成步，而是 planner 一次调用的组成部分（主线规格 §4.2）。LLM 同时完成：识别实体清单 → 统计 entity_count → 为每个实体生成子 query。

**为什么暂用 LLM**（评审结论）：
- 实现成本最低，无需维护词典/模型
- 覆盖口语叫法、别名、新上架商品（词典方案的死角）
- 与拆解同一次调用完成，不增加调用次数

**已知代价**（可接受，有校验兜底）：
- 漏识别（2 个识别成 1 个）→ 单分支整句检索，质量下降但不出错
- 误识别（配件当独立产品）→ 提示词约束"只识别明确实体"
- 输出随机性 → temperature=0 + 结构化输出 + 一致性校验

### 3.2 实体类型定义

| 类型 | 定义 | 计入 entity_count |
|---|---|---|
| Product | 具体产品（"扫地机器人X1""智能灯泡"） | ✅ |
| Category | 产品类别（"智能家居""智能照明""扫地机器人"泛指） | ✅ |
| Unknown | 无法确定类型 | ❌ |

规则：**只有 Product/Category 计入 entity_count**；Unknown 与 Customer/Supplier 类词汇（非本业务域）不计入，避免"我家的客服"之类表达触发拆解。

### 3.3 识别规则与边界

1. 只识别**明确**的实体，不过度推断——"X1"单独出现且上下文无法确定是产品时不算
2. 实体名称保持原样（不做同义改写，如"扫地机"不改成"扫地机器人"）
3. 同一实体的别名/简称在 query 中重复出现（"扫地机器人X1，就是那个X1"）只算 1 个
4. 并列枚举（顿号/和/以及/跟/与 连接的两个产品名）算 2 个
5. 实体数量无上限，N 个拆 N 个（不硬编码 2）

### 3.4 一致性校验（节点层，防 LLM 输出异常）

| 异常 | 校验规则 | 处置 |
|---|---|---|
| 数量不匹配 | `len(tasks) != entity_count` | 整体回退单分支 |
| 空子 query | 任一 `sub_query` 空白 | 整体回退单分支 |
| 无实体 | `entity_count == 0` 或 LLM 输出为空 | 单任务=原问题 |
| 字段缺失 | Pydantic 解析失败（structured output 失败） | try/except → 单任务=原问题 |

**原则：宁可少拆，不可错拆**——回退到单分支只损失召回，错拆产生误导性对比回答。

### 3.5 未来演进路线：三级确定性识别（待触发，非本期实施）

评审中确认的确定性方案留档，当 LLM 成本敏感或漏识别率高时启用：

| 级别 | 方法 | 成本 | 命中场景 | 启用条件 |
|---|---|---|---|---|
| L1 | 目录词典 + AC 自动机/FlashText + 型号正则（`[A-Za-z]+\d+`） | 0 | 精确产品名/型号 | 目录 SKU 稳定且可导出 |
| L2 | jieba 分词 + 目录名 Embedding 相似匹配（pgvector 余弦阈值） | 1 次 embedding（毫秒级） | 别名、口语叫法、轻微错字 | L1 漏检率超标 |
| L3 | LLM | 1 次调用 | L1/L2 失手的新表述 | 始终作为兜底 |

词典来源：产品目录表（构建时导出名称+别名+型号）。切换时 planner 的识别部分降级为确定性函数，LLM 只保留拆解职责（输入带已识别实体清单，见主线规格待确认 #1）。

## 4. 子查询拆解设计

### 4.1 拆解触发条件

```
触发拆解：entity_count ≥ 2 且 一致性校验通过
不拆解：entity_count ≤ 1 → 单分支，子 query = 原问题
```

### 4.2 子 query 生成规则（提示词硬约束）

1. **独立完整**：含实体全名，无指代（"它/这个"）、无省略，单独检索即可命中
2. **继承原 query 的意图维度**：拆解不改变用户想做的事，只改变"问谁"
3. **互不重叠**：N 个子 query 覆盖 N 个实体，不生成"两个产品对比"之类的跨实体子 query（对比由 summarize 基于汇总结果完成）
4. **不添加原 query 没有的信息**：不脑补具体型号、价格、参数

### 4.3 意图维度映射表（提示词示例素材）

| 原 query 意图 | 识别信号 | 子 query 模板（示例） |
|---|---|---|
| 对比/哪个好 | "哪个好/对比/区别/比较/优缺点" | "{实体} 的优缺点和适用场景是什么？" |
| 价格 | "多少钱/价格/贵/优惠/降价" | "{实体} 的售价和优惠政策是什么？" |
| 选购 | "推荐/适合/选哪个/买哪个" | "{实体} 的适用人群和口碑如何？" |
| 库存 | "有货/现货/库存/多久发货" | "{实体} 的库存和发货情况如何？" |
| 参数 | "参数/配置/尺寸/功率" | "{实体} 的主要参数和配置是什么？" |
| 售后 | "保修/退换/维修/坏了" | "{实体} 的保修和售后服务政策是什么？" |

未命中信号的意图 → 子 query 用中性模板："{实体} 的相关信息是什么？"

### 4.4 正反示例（写入提示词 few-shot）

```
正确：
原 query: "扫地机器人A和扫地机器人B哪个更好"
子 query: ["扫地机器人A的优缺点和适用场景是什么？", "扫地机器人B的优缺点和适用场景是什么？"]

错误一（裸实体名，意图丢失）:
子 query: ["扫地机器人A", "扫地机器人B"]

错误二（跨实体子 query，与拆解目的冲突）:
子 query: ["扫地机器人A和B的参数对比", "哪个性价比高"]

错误三（脑补信息）:
原 query 未提价格，子 query 却问 "扫地机器人A多少钱"
```

## 5. RAG 检索流程设计（分支内部）

每个并行分支（一个子 query）在 `customer_tools` 节点内执行同一流水线：

```
子 query (task)
  → ① HyDE（可选，开关控制）
  → ② 向量检索（pgvector top_k）
  → ③ 混合检索（BM25 + 向量 RRF，语料来自向量库全量文档缓存）
  → ④ 按文档 id 合并去重（多路结果）
  → ⑤ 相关性评分（LLM 批量一次调用，过滤 irrelevant）
  → ⑥ records 打包 → searches 累加器（现状状态字段名，早期设计稿写作 cyphers）
```

### 5.1 ① HyDE（每个子 query 独立生成）

**启用条件**：`settings.HYDE_ENABLED`（默认 True）且子 query 非空。

**执行方式**（与现状的差异是重点）：

```
现状（预处理阶段）: enhanced_query = 子query + "参考线索: " + 假想答案   ← 拼接，稀释向量信号
本方案（分支内）  : 分别用 [子query] 和 [假想答案] 检索 → 按 id 合并去重 ← 两路信号独立利用
```

**流程**：
1. LLM 为子 query 生成 100-200 字假想答案（含产品专业术语，`generate_hypothetical_answer` 复用现状函数）
2. 假想答案单独执行向量检索（**不做混合检索**——BM25 对长文本假想答案无效）
3. 两路检索结果按文档 id 合并去重，进入混合检索合并池

**失败降级**：HyDE 生成异常/超时 → 跳过，仅用子 query 检索（try/except 包裹，不影响主流程）。

**边际收益说明**：子 query 含具体型号全名时 HyDE 收益低，但保留开关供实测对比；后续可细化"仅对无型号的模糊实体启用"（见 §3.5 演进路线联动）。

### 5.2 ② 向量检索

- 复用 `VectorStoreQuery.search(query, top_k=settings.VECTOR_SEARCH_TOP_K)`
- 返回 `{text, id, score, metadata}` 列表
- 检索输入：子 query + 假想答案（如启用）分别调用

### 5.3 ③ 混合检索（BM25 + 向量 RRF）

- 复用 `HybridRetriever`（jieba 分词 + rank-bm25），语料来自向量库全量文档
- **前提**：语料缓存随单例化（主线规格阶段 0），避免每分支全量拉取+重编码
- 失败降级：try/except → 仅用向量检索结果（现状逻辑保留）

### 5.4 ④ 合并去重

```python
merged: Dict[str, dict] = {}
for doc in vector_results + hybrid_results:
    key = doc.get("id") or doc.get("text", "")[:50]
    merged.setdefault(key, doc)   # 首见保留；后续可通过 score 择优（默认首见）
```

多路结果（子 query 向量 / HyDE 向量 / 混合）统一合并，按 id 去重。

### 5.5 ⑤ 相关性评分（批量）

- **一次 LLM 调用评全部文档**（现状已批量，`relevance_grader.py:111-117`）：每条文档截断 300 字，结构化输出 `{document_index, relevance, reasoning}` 列表
- 过滤：只保留 `relevance == "relevant"` 的文档，附 `relevance_score` 字段
- 失败降级：评分异常 → 返回全部结果（现状兜底保留）
- 模型建议：grader 用轻量档模型（主线规格待确认 #3），temperature 沿用 `settings.LLM_GRADER_TEMPERATURE`

### 5.6 ⑥ records 打包

```python
VectorSearchOutputState(
    task=子query,
    query=子query,
    errors=errors,
    records={"result": 拼接文本, "hybrid_docs": 相关文档列表},
    steps=["execute_vector_search"],
)
```

records 经 `cyphers` 累加器（`Annotated[List, add]`）跨分支合并，供 summarize 消费。

## 6. 并行执行与状态数据流

### 6.1 map-reduce 扇出

```python
# edges.py（主线规格 §4.3）
def map_reduce_planner_to_tool_selection(state: OverallState) -> List[Send]:
    return [
        Send("customer_tools", {"task": t.question, "parent_task": t.parent_task})
        for t in state.get("tasks", [])
    ]
```

N 个子任务 → N 个 `customer_tools` 实例并行执行（LangGraph superstep 语义），`cyphers` 累加器自动合并各分支结果。

### 6.2 状态字段流转

| 字段 | 写入方 | 消费方 | 说明 |
|---|---|---|---|
| `question`（子图 Input） | 上层（resolved_question） | planner、summarize 兜底 | 检索主 query |
| `original_question`（子图 Input） | 上层（= question） | summarize | 保留对比意图的完整问题 |
| `tasks`（累加） | planner | edges 扇出 | 拆解产物 |
| `cyphers`（累加） | 各 customer_tools 分支 | summarize、final_answer | 检索结果合并 |
| `summary` | summarize | final_answer | 最终回答 |
| `history`（最近 5 条） | final_answer | 下次调用输入 | 问答痕迹 |

### 6.3 并行注意事项

1. **单例化前置**：向量库客户端/Embedding 模型必须单例（主线规格阶段 0），否则 N 分支并发初始化导致资源翻倍
2. **LLM 客户端复用**：分支内 HyDE 与 grader 共用一个 LLM 实例（工厂注入），不新建客户端
3. **无共享可变状态**：分支间只通过状态累加器交换数据，不依赖执行顺序——并行安全的前提

## 7. 端到端示例

**输入**：第二轮用户问"那你家的扫地机器人A和B哪个更好？"（入口消解后 resolved_question 不变）

**执行轨迹**（含预期日志）：

```
1. planner LLM 调用
   日志: Total Sub Task: 2
   日志: Sub Task[1]: 扫地机器人A的优缺点和适用场景是什么？
   日志: Sub Task[2]: 扫地机器人B的优缺点和适用场景是什么？

2. 并行分支（两个 customer_tools 实例）
   分支1:
     日志: HyDE 假设性答案生成完成 (xxx字)
     日志: 向量检索完成: 返回 10 条结果
     日志: 混合检索补充了 8 条文档
     日志: 相关性评分完成: 18 条 -> 6 条 relevant
   分支2: （同上，各自独立）

3. 汇总（等待两个分支全部完成）
   summarize 输入: question="那你家的扫地机器人A和B哪个更好？" + 12 条 relevant 记录
   输出: 对比式回答（A: ...；B: ...；结论: ...）

4. final_answer: answer + history 记录（含两个分支的 cyphers 痕迹）
```

**LLM 调用统计**：planner(1) + 分支1(HyDE 1 + grader 1) + 分支2(HyDE 1 + grader 1) + summarize(1) = **6 次**（不含入口路由/改写），其中 4 次在并行分支内。

## 8. 回退与异常处理

| # | 场景 | 处置 | 用户体验 |
|---|---|---|---|
| 1 | planner 结构化输出解析失败 | 单任务=原问题，单分支 | 正常回答（不拆解） |
| 2 | entity_count ≥ 2 但校验失败 | 单分支整句检索 | 正常回答（对比质量降） |
| 3 | entity_count ≤ 1 | 单分支，子 query=原问题 | 正常回答 |
| 4 | HyDE 生成失败 | 跳过 HyDE，仅子 query 检索 | 无感知 |
| 5 | 混合检索失败 | 仅向量检索结果 | 无感知 |
| 6 | 相关性评分失败 | 返回全部检索结果 | 无感知 |
| 7 | 某分支检索全空 | records 空，summarize 按剩余结果汇总；全空走现状兜底 | 如实告知未查到 |
| 8 | 分支超时 | 子图外层 TimeoutGuard 30s 降级回答（现状保留） | 降级提示语 |
| 9 | 分支内任意异常 | try/except 记录 errors，分支返回空 records | 其他分支结果不受影响 |

**原则**：任何拆解/检索环节失败都降级为"整句单分支"或"部分结果"，**绝不让异常中断 SSE 流**。

## 9. 验证方案

### 9.1 功能场景清单

| # | 场景 | 预期 |
|---|---|---|
| 1 | "扫地机器人A和B哪个好" | 拆 2 任务，并行 2 分支，对比式回答 |
| 2 | "A、B、C三款有什么区别" | 拆 3 任务（验证不硬编码 2） |
| 3 | "扫地机器人X1多少钱" | 单分支，正常检索 |
| 4 | "智能门锁和扫地机器人"（类别+类别） | 拆 2 任务，Category 类实体计入 |
| 5 | "我家的客服能帮我查订单吗" | entity_count 0，单分支（"客服"不算实体） |
| 6 | "X1和它的配件" | 配件不计入 → 单分支或仅 X1（按提示词"只识别明确实体"） |
| 7 | HyDE 开关关闭 | 分支内无 HyDE 日志，检索正常 |
| 8 | 检索库为空/相关性全过滤 | 兜底回答，无异常 |
| 9 | 连续两轮追问 | 入口消解后进入本流程，两轮互不串扰 |

### 9.2 机制指标（日志统计）

| 指标 | 统计方式 | 目标 |
|---|---|---|
| 拆解正确率 | 人工抽查 50 条对比类 query，子 query 满足 §4.2 三条规则的比例 | ≥ 90% |
| 漏拆率 | 日志中 entity_count ≥ 2 但实际多实体的样本占比 | 观察基线，为 §3.5 演进提供依据 |
| 回退率 | 校验失败触发单分支的比例 | < 10% |
| 分支并行收益 | 双实体查询耗时 vs 单分支×2 估算耗时 | 并行路径更短 |
| 调用次数 | 按 §7 统计每场景 LLM 调用数 | 符合主线规格 §7.1 目标表 |

### 9.3 质量评估（可选，二期）

- 对比式回答人工评分：信息覆盖（两个产品都有实质信息）、对比维度贴合原意图、无编造
- 检索召回：固定评测集上拆解后并行检索 vs 整句检索的命中率对比

## 10. 与主线规格的接口约定

| 接口 | 约定 |
|---|---|
| 输入 | `resolved_question`（入口消解产物）、`original_question`（= resolved_question）、`state.messages`（只读，不修改） |
| 配置依赖 | `settings.HYDE_ENABLED`、`settings.VECTOR_SEARCH_TOP_K`、`settings.HYBRID_RETRIEVAL_TOP_K/N`、`settings.RELEVANCE_GRADING_ENABLED`、`settings.LLM_GRADER_TEMPERATURE` |
| 前置依赖 | customer_tools 单例化（阶段 0）、planner 直连（阶段 3）——本文档机制依赖主线规格先落地图结构改造 |
| 输出 | `answer`（summarize 产物）、`history`（问答痕迹）、`cyphers`（检索证据，供审计/评测） |

---

## 附：与现状实现的差异对照

| 维度 | 现状 | 本方案 |
|---|---|---|
| 实体识别 | 已删除（lg_builder 中无实体识别步骤，Neo4j 已整体移除） | 并入 planner 一次调用，结果驱动拆解 |
| 拆解触发 | LLM 自由决定（planner 提示词无实体约束） | entity_count ≥ 2 确定性触发 |
| HyDE | 预处理阶段对整句一次，拼接进 enhanced_query（现状仍如此） | 分支内每个子 query 独立生成，独立检索合并 |
| 工具选择 | 单工具直连已实现（阶段 3 落地，无选择调用） | 保持直连 |
| 汇总输入 | 预处理后的 question | original_question（对比意图完整） |
| 资源 | 向量库客户端已单例（阶段 0）；**语料缓存未实施**（每请求仍全量拉取，node.py:158） | 单例 + 语料缓存 |
