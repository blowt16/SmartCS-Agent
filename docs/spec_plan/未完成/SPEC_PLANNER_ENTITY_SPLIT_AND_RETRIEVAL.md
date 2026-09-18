# planner 实体拆解与零 LLM 检索闭环 实施规格

> **归档状态**: ⏳ 待实施（2026-09-18 定稿，依据 main 分支代码实测与两份源 spec 整合）

> **整合来源**（未实施部分全部移交本稿；两份源稿已于 2026-09-18 `git rm` 删除，全文见 git 历史）：
> - `SPEC_ENTITY_PARALLEL_RAG.md` 阶段 2（planner 改造）/ 阶段 4（检索侧）/ 阶段 6（收尾）——该稿阶段 0/1/3/5 已落地，不属本稿范围
> - `SPEC_ENTITY_RECOGNITION_AND_RAG_RETRIEVAL.md` §3/§4（实体识别与子查询拆解机制）
>
> **已落地前置**（源稿阶段，实施前无需重做，此处保留提交索引以免随源稿删除而失传）：
> 1. 阶段 0 —— customer_tools 单例化（`cf9e37b`）：收敛为 `RAGRetrieverService` 进程级单例，纯混合检索零 LLM
> 2. 阶段 3 —— 子图简化 + 删 Cypher/guardrails/tool_selection 遗留：planner 直连 customer_tools（`multi_tool.py:59-80`）
> 3. 阶段 3 Step 3 —— 子图模块级单次编译（`29e6f73`）：`get_research_graph` 懒加载双检锁单例（`lg_builder.py:410-438`）
> 4. 阶段 1 入口消解 / 阶段 5 语义缓存前置 —— 由替代方案落地在 main 入口侧（`22b0c90` / `4882018`）：多轮无条件 LLM 消解 + 缓存 lookup/update
>
> **与源稿的 5 处差异（2026-09-18 实测后决策，详见 §8）**：
> 1. **检索侧改为零 LLM 闭环**——源稿 §4.4 的 HyDE 与 LLM 相关性评分**不实施**：评分器已被 CrossEncoder 精排有意替代（`reranker_service.py:4` docstring 原文"替代原 LLM 相关性评分（grade_relevance）"），重新引入是架构回退；HyDE 当前代码零痕迹，属新建而非"移入"
> 2. **跨分支去重落在 summarize 节点内**（源稿无明确落点），不新增图节点
> 3. **`original_question` 字段取消**——原动机"预处理后的 question 被稀释"随预处理管道 2026-08-21 整体删除而消失，`state.question` 现即消解后完整句
> 4. **新增实体约束检索**（name→sku→sku_codes），源稿无此项；以此替代 HyDE 承担"逐实体覆盖"职责
> 5. **P4（单任务空转 1 次 LLM）决策为保持独立调用**，不改调用结构（§8 D5）

> **用途**: 售前（presale）链路 planner 节点与检索侧的合并改造——planner 从 Cypher 时代通用拆分模板改为商品场景的子问单元拆解（含实体名回填与一致性校验），检索侧在不新增 LLM 调用的前提下完成跨分支证据去重与实体约束
> **技术栈**: LangGraph 0.3.x（Send map-reduce）+ pgvector HNSW + pg_jieba BM25 + RRF + bge-reranker-v2-m3 CrossEncoder + DeepSeek/Ollama
> **状态**: 设计规格，待实施（前置结构条件已满足：customer_tools 单例化 cf9e37b、子图简化与 planner 直连、子图进程级单例 29e6f73 均已落地）
> **关联文档**: [[PROJECT_ANALYSIS.md]] §4.4 [[docs/项目问题.md]] #2/#3 [[SPEC_RAG_SKU_METADATA]]（sku_codes/chapter 透出）[[SPEC_ENTRY_LLM_RESOLUTION.md]]（入口消解产物入力）

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状实测（2026-09-18）](#2-现状实测2026-09-18)
3. [目标链路](#3-目标链路)
4. [模块详细设计](#4-模块详细设计)
5. [回退与异常处理总表](#5-回退与异常处理总表)
6. [分阶段实施步骤](#6-分阶段实施步骤)
7. [验证方案](#7-验证方案)
8. [决策记录](#8-决策记录)
9. [风险与避坑清单](#9-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

售前链路的 planner 节点自 GraphRAG 时代沿用至今，从未按纯 RAG 商品检索场景重写。当前下游已是**商品知识 docx 的全库混合检索**（`RAGRetrieverService.search`：HNSW ∥ BM25 → RRF → CrossEncoder 精排，`top_k=5`），而 planner 提示词仍在教 LLM 做 Neo4j 库表式拆分（"北风商贸有哪些饮料类产品"、"订单10248"、"供应商 Exotic Liquids"）。

由此产生三类问题：

| 类别 | 问题 | 实测依据 |
|---|---|---|
| 拆解质量 | 无商品实体识别、无意图继承、无实体名回填约束；"X 和 Y 哪个更好"类 query 无指导，LLM 可自由选择整句单任务或按属性乱拆 | §2 P1 |
| 输出健壮性 | 输出模型无约束、节点只对"空列表"回退——空串任务/重复任务/超 3 条均不校验；空串可穿透为 `Send(task="")` → 分支空记录 → summarize 输出 "No data to summarize."（**是失败而非降级**） | §2 P2 |
| 检索覆盖 | 即使拆出双实体任务，检索端无实体归属约束，且跨分支证据不去重 | §2 P5 / §2.3 |

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 拆解可控 | 拆分动机收敛为两类（多实体 / 列表+详情并列）；总数 ≤3；校验失败一律回退单分支，回退率 <10% |
| 消灭空任务穿透 | 空串 / 重复 / 超限 / LLM 异常 → 全部走同一回退出口，`Send(task="")` 结构性不可达 |
| 确定性 | planner 拆解 `temperature=0`（对齐 Router 与入口消解） |
| 逐实体覆盖 | 拆解分支的检索结果限定在实体自身商品块 + 政策/通用块（`sku_codes` 约束，fail-open） |
| 不新增 LLM 调用 | 单实体首轮 ≤3 次（Router + planner + summarize）；缓存命中 0 次。**本稿不引入任何新 LLM 调用点** |
| 证据不重复 | summarize 输入按 `chunk_id` 跨分支去重，同一块只进 prompt 一次 |

### 1.3 设计原则

1. **宁可少拆，不可错拆**：拆解校验失败一律回退单分支（只损失召回，不产生误导性对比回答）
2. **零 LLM 优先**：检索侧增强全部用确定性手段（字段过滤、集合去重），不引入新 LLM 调用点
3. **fail-open**：实体约束解析不到 sku、过滤后候选为空 → 放弃约束继续检索，绝不让增强措施把链路做空
4. **surgical**：`state["searches"]` 结构不动（`final_answer` history 与 `evaluation/runner.py:124` 依赖它做逐分支追溯），去重结果只影响 summarize 的输入装配

---

## 2. 现状实测（2026-09-18）

> 本节按 main 分支代码逐一核实，是实施前的基线。行号精确到当前代码。

### 2.1 planner 节点（P1/P2/P3）

| # | 问题 | 代码证据 |
|---|---|---|
| P1 | `PLANNER_SYSTEM_PROMPT` 仍是 Cypher/北风商贸时代模板（6 个库表示例），无商品实体识别/意图继承/实体名回填规则 | `kg_prompts.py:8-38` |
| P1b | human 模板另叠一份与 system 重复的 4 条规则 | `planner/prompts.py:14-21` |
| P2 | `PlannerOutput` 仅 `tasks: List[Task]`，无 `entity_count`；`Task.question` / `parent_task` 无长度约束，空串可穿透 | `planner/models.py:8-12`、`components/models.py:6-13` |
| P2b | 节点回退**仅覆盖空列表**一种情形（`or` 短路）；无 try/except，LLM 异常直接上抛 | `planner/node.py:49-57` |
| P2c | `parent_task` 为必填且由 LLM 逐任务生成，下游 `edges.py:18` 传入即弃，无业务消费 | `components/models.py:7-9` |
| P3 | planner 复用子图单例注入的 research LLM，`temperature=settings.LLM_TEMPERATURE=0.7`（config.py:154），与 summarize 共用同一实例；而 Router=0（config.py:155）、入口消解=0（config.py:78） | `lg_builder.py:433-436` |

### 2.2 检索侧（P5）

| # | 现状 | 代码证据 |
|---|---|---|
| P5a | `customer_tools` 直接 `retriever.search(query)`，无实体约束、无 HyDE、无相关性评分 | `customer_tools/node.py:54-59` |
| P5b | `RAGRetrieverService.search` 签名仅 `(query, top_k)`，无实体过滤入口 | `rag_retriever_service.py:107-111` |
| P5c | **跨分支无去重**：`summarize` 把 N 个分支的 `records` 原样拼进 prompt；而 `records` 含 `hybrid_docs`/`dynamic_rows` 原始 dict，与已渲染的 `result` 文本**重复**——同一块会在 prompt 中出现两次，且原始 dict 整份额外进 prompt | `summarize/node.py:42-49`、`customer_tools/node.py:83-109` |
| P5d | 跨商品混合块实测占 66%（500 字符贪心切分下 33 块中 22 块跨商品）→ 含 A、B 的混合块会被两条子 query 各召回一次 | `docs/项目问题.md` #3 |

### 2.3 可用的确定性抓手（源稿未记载）

| 抓手 | 位置 | 用途 |
|---|---|---|
| 每条 doc 带 `sku_codes`（`[]` = 政策/通用块）、`chapter`、`rerank_score` | `rag_retriever_service.py:55-73`、`:109` | 实体约束与相关性观测的现成字段 |
| `product_price_stock` 同时有 `sku`（唯一键）与 `product_name`（唯一键），1:1 映射 | `models/product_price_stock.py:13-19` | 商品名 → sku 解析通道 |
| `render_doc_blocks` / `render_dynamic_rows` 为两通道共用渲染器 | `app/tools/doc_block_renderer.py:16-56` | summarize 侧重渲染可直接复用，零格式漂移 |
| `searches` 有 3 个消费方 | `summarize/node.py:42`、`final_answer/node.py:33-38`、`evaluation/runner.py:124` | 决定去重不得改变 `searches` 结构 |

### 2.4 死代码与隐式契约（本稿顺带处理）

| 项 | 位置 | 说明 |
|---|---|---|
| 死 import `create_planner_node` | `lg_builder.py:25` | 全文件仅此一处，无调用点（子图经 `create_multi_tool_workflow` 内部调用） |
| 死模型 `VectorSearchInputState` | `customer_tools/node.py:15-19` | 全项目零引用，与 edges 实际 Send 的 dict 结构已脱节 |
| 死类局部 `AgentState` | `multi_tool.py:21-29` | 与 `lg_states.AgentState` 同名异构，图编译用的是 TypedDict 三件套，此类未被使用 |
| **隐式契约**：planner 节点名 | `multi_tool.py:62` `add_node(planner)` 取函数 `__name__` = `"planner"`，与 `stream_filter.py:20` `INTERNAL_NODES` 字符串耦合 | 重命名函数会让流式黑名单**静默失效**；现有 `test_stream_filter.py:39` 用的是硬编码字符串，守护不到 |

---

## 3. 目标链路

```
main 入口 /api/langgraph/query
  → 多轮无条件 LLM 消解（已落地）→ 语义缓存 lookup（命中短路）
  → 主图 analyze_and_route_query（Router LLM，T=0）
      └─ type=presale → create_research_plan
            └─ 售前子图（进程级单例 get_research_graph）
                 planner（LLM，T=0）
                   ├─ 拆解通过 → tasks=[EntitySubQuery...]，每 Task 带 entity_name
                   └─ 否则     → _fallback：单 Task = 原 query 原文（仍保留 LLM 给的 entity_name）
                 → Send ×N 并行（payload 含 task / entity_name）
                      → customer_tools（零 LLM）
                           entity_name → resolve_skus_by_product_name → entity_skus
                           RAGRetrieverService.search(query, entity_skus=...)
                             HNSW ∥ BM25 → RRF → [实体约束过滤] → 精排 → top_k=5
                 → summarize（LLM，T=0.7）
                      跨分支装配：chunk_id 去重 + 动态行按 sku 合并 + 统一渲染
                 → final_answer
  → SSE 流式输出（闸门按节点名判定，planner 在 INTERNAL_NODES 内）
```

**链路结构无变化**：不新增节点、不新增边，`Send` 扇出与 `searches` 累加器保持现状。改动全部落在节点内部逻辑与检索服务签名。

**LLM 调用次数**（无缓存命中）：单实体首轮 3 次（Router + planner + summarize），多轮 4 次（+入口消解），双实体 3 次（拆 N 分支不增 LLM）。**与本稿实施前完全一致**。

---

## 4. 模块详细设计

### 4.1 planner 结构化输出 schema

**文件**：`.../components/planner/models.py`（整体替换）、`.../components/models.py`（`Task` 增字段）

```python
# —— planner/models.py ——
from typing import List

from pydantic import BaseModel, Field


class EntitySubQuery(BaseModel):
    """LLM 输出的单条任务（与内部 Task 分离：LLM 不再感知 parent_task）"""

    name: str = Field(
        default="",
        description=(
            "本条任务的主题词：商品全名或品类词（如'米家智能晾衣机2'/'智能电动沙发'）。"
            "拆成多条任务（tasks≥2）时必须给出；无法确定实体时留空。"
        ),
    )
    sub_query: str = Field(
        default="",
        description="可直接独立检索的子问文本（自含主题词，禁止指代）",
    )


class PlannerOutput(BaseModel):
    entity_count: int = Field(
        default=0, ge=0,
        description="识别到的商品/品类实体数（决策辅助与日志，不作数量校验锚）",
    )
    tasks: List[EntitySubQuery] = Field(
        default_factory=list,
        description="1..3 条；无法拆分时恰 1 条且=原 query 逐字复制；拆分时每条为可独立检索的子问",
    )
```

```python
# —— components/models.py 的 Task 增一个字段（其余不变）——
class Task(BaseModel):
    question: str = Field(..., description="The question to be addressed.")
    parent_task: str = Field(..., description="The parent task this task is derived from.")
    entity_name: str = Field(
        default="",
        description="本条任务的主题词（planner 注入；检索侧实体约束入力，空=不加约束）",
    )
    data: Optional[Any] = Field(default=None, description="The search result details.")
```

> **`name` 的必填性说明（需评审确认）**：字段用 `default=""` 而非 `Field(...)` 强制必填。理由：`entity_count=0` 的合法 query（如"有没有推荐的吗"）不存在实体，schema 级必填会迫使 LLM 编造主题词；而 `with_structured_output` 校验失败会让**整条 query 回退单分支**，代价大于收益。因此"拆解时必填"约束落在**提示词 + 节点校验**两层：
> - 提示词：拆成多条时每条必须给出主题词
> - 节点：拆解分支（≥2 任务）若存在空 `name` → 该任务不带实体约束（fail-open）+ 日志记录空名率
>
> 若评审认为应改为 schema 级必填，改 `default=""` → `Field(..., min_length=1)` 即可，代价如上。

### 4.2 planner 提示词（全文替换）

**文件**：`app/lg_agent/kg_sub_graph/prompts/kg_prompts.py`（`PLANNER_SYSTEM_PROMPT` 整体替换）、`.../planner/prompts.py`（human 模板缩减）

**system 提示词全文**（基于源稿 2026-09-03 v3 精简稿，**唯一增补**：主题词 `name` 的产出要求，因该字段在本稿中由日志字段升级为检索约束入力）：

```text
你是售前商品问答的任务规划组件。判断用户问题是否需要拆成多个可独立检索的子任务并输出任务列表。
entity_count = 句中明确提到的商品/品类实体数（"哪款/有没有/推荐几个"等泛指不算，无实体则 0）。

拆分判断（按顺序）：
1. 提到多个商品/品类实体 → 每实体 1 个任务，任务继承原问题对该实体的全部意图：
   对比/哪个更好 → 优缺点、规格参数、适用场景；价格/优惠 → 售价、促销；
   选购/推荐 → 适合人群、口碑。
2. 否则，若"有哪些/几款（列表型）"与"价格/参数/功能（详情型）"明确并列 → 拆 2 个任务：
   列表问 1 条、详情问归并 1 条。
3. 其余一律不拆：只返回 1 个任务 = 用户原问题原文（禁止改写、删减）。
   包括：同一商品的多条属性细节问（功能+噪音+续航）、仅提到品类的选购句、单一意图问。
   拿不准是否该拆时同样合并为 1 条。

所有任务：文本必须自含实体全名或品类词、可脱离原问题独立检索、禁止指代（它/它们/这款）；
任务间互不重复、互不依赖；总数不超过 3。
每条任务同时给出主题词 name：商品全名或品类词（拆成多条时必填，将用于检索范围限定）。

示例（商品名取自知识库真实在售）：
- 问题：米家智能晾衣机2的承重是多少公斤？
  entity_count：1 → 任务：[{name: "米家智能晾衣机2", sub_query: "米家智能晾衣机2的承重是多少公斤？"}]     # 单意图不拆

- 问题：米家智能晾衣机2和米家智能晾衣机Pro哪个更实用？它们分别多少钱？
  entity_count：2 → 任务：[{name: "米家智能晾衣机2", sub_query: "米家智能晾衣机2的功能实用性、优缺点和售价是多少？"},
                          {name: "米家智能晾衣机Pro", sub_query: "米家智能晾衣机Pro的功能实用性、优缺点和售价是多少？"}]
  # 多实体每实体 1 条；指代"它们"被实体全名替换

- 问题：米家智能晾衣机有哪些型号？分别多少钱？
  entity_count：1 → 任务：[{name: "米家智能晾衣机", sub_query: "米家智能晾衣机有哪些型号？"},
                          {name: "米家智能晾衣机", sub_query: "米家智能晾衣机各型号的售价分别是多少？"}]
  # 列表+详情并列拆 2（任务数可大于 entity_count）

- 问题：小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？
  entity_count：1 → 任务：[{name: "小米智能门锁M30", sub_query: "小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？"}]  # 属性细节问合并不拆

- 问题：有没有适合小户型的智能电动沙发？
  entity_count：1 → 任务：[{name: "智能电动沙发", sub_query: "有没有适合小户型的智能电动沙发？"}]  # 仅品类的选购句不拆
```

**human 模板**（`planner/prompts.py`，缩减为仅占位——删除与 system 重复的 4 条规则）：

```python
def create_planner_prompt_template() -> ChatPromptTemplate:
    message = "问题: {question}"
    return ChatPromptTemplate.from_messages(
        [("system", PLANNER_SYSTEM_PROMPT), ("human", message)]
    )
```

### 4.3 planner 节点校验与回退

**文件**：`.../components/planner/node.py`（节点函数体替换）

```python
MAX_TASKS = 3  # 拆解上限：超过一律回退（宁可少拆）


def _fallback(question: str, entity_name: str = "") -> List[Task]:
    """所有回退的唯一出口：单分支整句检索（question 恒为原 query 原文，不经 LLM）。"""
    return [Task(question=question, parent_task=question, entity_name=entity_name)]


async def planner(state: InputState) -> Dict[str, Any]:
    question = state.get("question", "")
    try:
        planner_output = await planner_chain.ainvoke({"question": question})
        entity_count = planner_output.entity_count
    except Exception:
        # L0：调用/结构化输出异常 → 整句。loguru 记录堆栈用 logger.exception
        logger.exception("planner 调用失败，回退单分支整句")
        entity_count, planner_output = -1, None

    # 信任拆解的条件：2<=任务数<=MAX_TASKS、sub_query 均非空、互不重复。
    # 不做 len==entity_count 硬校验——列表型拆解（entity_count=1 拆 2）合法。
    if (
        planner_output is not None
        and 2 <= len(planner_output.tasks) <= MAX_TASKS
        and all(t.sub_query.strip() for t in planner_output.tasks)
        and len({t.sub_query.strip() for t in planner_output.tasks})
        == len(planner_output.tasks)
    ):
        task_list = [
            Task(question=t.sub_query.strip(), parent_task=question,
                 entity_name=t.name.strip())
            for t in planner_output.tasks
        ]
        logger.info(
            "planner_decision: split={} (entity_count={}, names={})",
            len(task_list), entity_count, [t.entity_name for t in task_list],
        )
    else:
        # 单任务场景：LLM 的单任务文本一律丢弃（防改写，用原 query 原文），
        # 但保留其 name 供检索侧实体约束（单实体事实查询同样受益，见 §4.6）
        single_name = ""
        if planner_output is not None and len(planner_output.tasks) == 1:
            single_name = planner_output.tasks[0].name.strip()
        task_list = _fallback(question, single_name)
        reason = "llm_error" if planner_output is None else f"tasks={len(planner_output.tasks)}"
        logger.info("planner_decision: fallback (reason={}, name='{}')", reason, single_name)

    logger.info("Total Sub Task: {}", len(task_list))
    for i, task in enumerate(task_list):
        logger.info("Sub Task[{}]: {}", i + 1, task.question)
    return {"tasks": task_list}
```

**校验规则**：

| 场景 | 处置 |
|---|---|
| LLM 调用异常 / 结构化输出解析失败 | `_fallback`，`reason=llm_error`，`entity_name=""` |
| 任务数 < 2（含 LLM 返回单任务文本） | `_fallback`，**任务文本 = 原 query 原文**（不采用 LLM 文本），保留 LLM 的 `name` |
| 任务数 > MAX_TASKS（3） | `_fallback`，`reason=tasks=N` |
| 任一 `sub_query` 为空白 | `_fallback` |
| 任务 `sub_query` 重复（strip 后集合去重计数不等） | `_fallback` |
| 任务数 ∈ [2,3] 且全部通过 | 采用拆解，`split=N` |

> `parent_task` 由节点注入（= 原 query），不再由 LLM 生成；`Task.model_dump()` 仍含该字段，`edges.py` 发送逻辑不变。

### 4.4 planner 温度收敛

**文件**：`app/core/config.py`、`app/lg_agent/lg_builder.py`、`.../workflows/multi_agent/multi_tool.py`

```python
# —— config.py 新增（置于 LLM_TEMPERATURE 附近，对齐 ROUTER_TEMPERATURE 命名）——
PLANNER_TEMPERATURE: float = 0.0   # planner 拆解温度（确定性优先）
```

```python
# —— lg_builder.py：抽一个模型构造helper，get_research_graph 内造两个实例 ——
def _build_research_model(temperature: float):
    """子图模型构造（DEEPSEEK / OLLAMA 分支，thinking 关闭）。"""
    if settings.AGENT_SERVICE == ServiceType.DEEPSEEK:
        return ChatDeepSeek(
            api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL,
            temperature=temperature, extra_body={"thinking": {"type": "disabled"}},
        )
    return ChatOllama(
        model=settings.OLLAMA_AGENT_MODEL, base_url=settings.OLLAMA_BASE_URL,
        temperature=temperature, extra_body={"thinking": {"type": "disabled"}},
    )


# get_research_graph 内（双检锁内层）：
_research_graph = create_multi_tool_workflow(
    llm=_build_research_model(settings.LLM_TEMPERATURE),          # summarize 沿用 0.7
    planner_llm=_build_research_model(settings.PLANNER_TEMPERATURE),  # planner 收敛为 0
)
```

```python
# —— multi_tool.py 签名 ——
def create_multi_tool_workflow(llm: BaseChatModel, planner_llm: BaseChatModel):
    ...
    planner = create_planner_node(llm=planner_llm)
```

> **与 2026-09-18 流式整改的兼容性**：闸门已由"共享模型实例上的 `tags=["research_plan"]` 黑名单"改为"按 `metadata["langgraph_node"]` 节点名判定"（`stream_filter.py:19-23`），拆两个模型实例不再有任何流式副作用。**实施时不得给新实例打 tags**。

### 4.5 summarize 跨分支证据去重

**文件**：`.../components/summarize/node.py`

```python
from app.tools.doc_block_renderer import render_doc_blocks, render_dynamic_rows


def _assemble_evidence(searches: list) -> str:
    """跨分支证据装配：chunk_id 去重 + 动态行按 sku 合并 + 统一渲染。

    重复来源：跨商品混合块（docs/项目问题.md #3 实测 66%）会被 A/B 两条子 query
    各召回一次，现状原样拼接导致同一块进 prompt 两次。
    同时消除现状把 records 整体（含 hybrid_docs/dynamic_rows 原始 dict）塞进 prompt
    的冗余——渲染文本已含全部事实，原始 dict 属重复通道。
    """
    seen: set = set()
    docs: list = []
    dynamic_rows: dict = {}
    for s in searches:
        records = s.records if hasattr(s, "records") else (s or {}).get("records")
        if not records:
            continue
        for d in records.get("hybrid_docs") or []:
            key = d.get("chunk_id") or d.get("id")
            if key in seen:
                continue
            seen.add(key)
            docs.append(d)
        dynamic_rows.update(records.get("dynamic_rows") or {})

    if not docs and not dynamic_rows:
        return ""
    dynamic_text = render_dynamic_rows(dynamic_rows)
    static_text = render_doc_blocks(docs)
    return f"{dynamic_text}\n\n{static_text}" if dynamic_text else static_text
```

节点主体改为：

```python
    evidence = _assemble_evidence(state.get("searches", list()))
    if evidence:
        summary = await generate_summary.ainvoke(
            {"question": state.get("question"), "results": evidence}
        )
    else:
        summary = "No data to summarize."
```

**行为变化（需回归）**：

| 项 | 现状 | 本稿 |
|---|---|---|
| `{results}` 载荷 | `records` dict 列表的 Python repr——含已渲染文本 **+ `hybrid_docs` 原始 dict 全量 + `dynamic_rows` 全量** | 单一证据字符串（动态区 + 静态块，与两通道同款渲染） |
| 跨分支重复块 | 出现 N 次 | 1 次 |
| 动态区 | 每分支各自一份（同 sku 重复） | 全局一份，按 sku 合并 |
| `state["searches"]` | — | **不动**（`final_answer` history 与 `evaluation/runner.py:124` 继续拿到逐分支原始追溯） |
| `records["result"]` / `hybrid_docs` / `dynamic_rows` 字段 | — | **不动**（`customer_tools` 与 `test_customer_tools_node.py` 的断言继续有效） |

**保留的分组信息**：合并后不保留"哪条子问题命中该块"的分组。判断依据：每块前缀行 `【商品编码:...｜知识类型:...｜来源:...】`（`doc_block_renderer.py:53-55`）已提供商品归属，且 summarize 收到的 `question` 是完整原问题（含对比意图），分组不构成必需信息。**若回归发现对比类回答质量下降，再考虑按 `task` 分组渲染。**

### 4.6 实体约束检索（零 LLM）

**目标**：拆解分支的检索结果限定在"实体自身商品块 + 政策/通用块"，丢弃归属于其它商品的块。直接对应 `docs/项目问题.md` #2（同品牌多型号竞争导致错召回）。

**链路**：`Task.entity_name` → Send payload → customer_tools 解析 name→sku → `search(entity_skus=...)` → RRF 后、精排前过滤。

#### 4.6.1 商品名 → sku 解析

**文件**：`app/services/product_dynamic_service.py`（新增函数，与 `fetch_by_skus` 同表同域）

```python
async def resolve_skus_by_product_name(name: str) -> List[str]:
    """商品名 → sku 列表（精确优先，回退模糊）。无匹配或异常返回空列表（调用侧 fail-open）。"""
    if not name or not name.strip():
        return []
    n = name.strip()
    try:
        async with AsyncSessionLocal() as session:
            for stmt in (
                select(ProductPriceStock.sku).where(ProductPriceStock.product_name == n),
                select(ProductPriceStock.sku).where(ProductPriceStock.product_name.like(f"%{n}%")),
            ):
                rows = (await asyncio.wait_for(
                    session.execute(stmt.limit(5)), timeout=settings.TOOL_DB_TIMEOUT_SECONDS
                )).scalars().all()
                if rows:
                    return [r.upper() for r in rows]
        return []
    except Exception as e:
        logger.warning("商品名→sku 解析失败，跳过实体约束: {}", e)
        return []
```

> `product_name` 有唯一约束 `uq_product_price_stock_name`（`models/product_price_stock.py:14`），精确分支理论返回 0 或 1 条；`limit(5)` + 列表返回是为 LIKE 分支留余量。sku 统一大写（对齐 `fetch_by_skus` 的 `s.upper()` 归一）。

#### 4.6.2 检索服务签名与过滤

**文件**：`app/services/rag_retriever_service.py`

```python
async def search(
    self,
    query: str,
    top_k: Optional[int] = None,
    entity_skus: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    ...
    fused = rrf_fuse(result_lists=[vector_results, bm25_results], id_key="chunk_id", top_k=settings.RRF_TOP_K)

    # 实体约束（零 LLM）：丢弃"归属于其它商品"的候选，保留政策/通用块（sku_codes 空）。
    # 置于精排前——先净化候选池再让 reranker 排序；过滤后为空则放弃约束（fail-open）。
    if entity_skus:
        scoped = [
            d for d in fused
            if not d.get("sku_codes")
            or (set(c.upper() for c in d["sku_codes"]) & entity_skus)
        ]
        if scoped:
            if len(scoped) < len(fused):
                logger.info("实体约束过滤: {} -> {} 条候选（丢弃 {} 条他商品块）",
                            len(fused), len(scoped), len(fused) - len(scoped))
            fused = scoped
        else:
            logger.warning("实体约束过滤后候选为空，放弃约束（fail-open）")

    # ③ 精排（原逻辑不变）
    ...
```

#### 4.6.3 节点与边

**文件**：`.../workflows/multi_agent/edges.py`、`.../components/customer_tools/node.py`

```python
# edges.py：Send payload 增 entity_name（保留现有 task/question/parent_task 三键不动）
Send("customer_tools", {
    "task": task.question,
    "question": task.question,
    "parent_task": task.parent_task,
    "entity_name": getattr(task, "entity_name", ""),
})
```

```python
# customer_tools/node.py：检索前解析实体（原 query/errors 逻辑不变）
        query = state.get("task", "")
        entity_name = state.get("entity_name", "")
        if not query:
            errors.append("未提供查询文本")
        else:
            retriever = get_rag_retriever_service()
            entity_skus: set = set()
            if entity_name:
                entity_skus = set(await resolve_skus_by_product_name(entity_name))
                if not entity_skus:
                    logger.info("实体约束: 商品名 '{}' 未解析到 sku，跳过约束", entity_name)
            docs = await retriever.search(query, entity_skus=entity_skus or None)
            logger.info("检索节点返回 {} 条文档", len(docs))
```

**已知边界（不解决，如实记录）**：混合块 `sku_codes=[A, B]`（占 66%）对 A、B 两边都通过约束，因此本机制**只解决"召回成别的商品"**，不解决混合块稀释——后者属切分策略（`docs/项目问题.md` #3），不在本稿范围。

**不启用约束的情形**：`entity_name` 为空（LLM 未给出 / 解析无匹配 / LLM 调用异常）→ `entity_skus=None` → `search` 行为与现状逐字一致。

### 4.7 rerank 分数分布观测（零行为变更）

**文件**：`app/services/rag_retriever_service.py`

目的：为"是否引入 `RERANK_MIN_SCORE` 阈值过滤"提供数据。当前 `RERANKER_TOP_K=5`（config.py:163），每分支只留 5 条，**拍脑袋设阈值极易把分支砍空**；且 bge-reranker-v2-m3 输出为 logits（非 0-1 归一），阈值必须由实测分布决定。

```python
    # 精排分数分布（观测用，不改变任何过滤行为）
    scores = [d["rerank_score"] for d in fused if d.get("rerank_score") is not None]
    if scores:
        logger.info("精排分数分布: n={}, min={:.3f}, max={:.3f}, mean={:.3f}",
                    len(scores), min(scores), max(scores), sum(scores) / len(scores))
```

并把现有"最终检索结果内容预览"日志的每条前缀补上 `rerank_score`（`rag_retriever_service.py:158-161`）。

**阈值过滤的门槛**：跑满 §7.2 场景清单、样本量足够后，若分布呈现清晰的"高相关簇 / 长尾负分簇"双峰，再单独立项引入 `RERANK_MIN_SCORE`（默认关）。**本稿不实现过滤。**

### 4.8 顺带清理与守护测试

| 动作 | 文件 |
|---|---|
| 删除死 import `create_planner_node` | `lg_builder.py:25` |
| 删除死模型 `VectorSearchInputState` | `customer_tools/node.py:15-19` |
| 删除局部死类 `AgentState` | `multi_tool.py:21-29` |
| 新增守护测试：planner 节点名 ∈ `INTERNAL_NODES` | `tests/test_stream_filter.py` |

守护测试形态（实施时确认 Fake LLM 是否可复用 `tests/` 内既有 stub——需实现 `with_structured_output`；若不可用则用最小 stub）：

```python
def test_planner_node_name_in_internal_nodes():
    """planner 节点名由 add_node(函数对象) 取 __name__，与流式黑名单字符串耦合；
    重命名函数会让 planner 分片静默外泄，故断言两者一致。"""
    from app.lg_agent.stream_filter import INTERNAL_NODES
    from app.lg_agent.kg_sub_graph.agentic_rag_agents.components.planner.node import (
        create_planner_node,
    )
    assert create_planner_node(llm=_StubLLM()).__name__ in INTERNAL_NODES
```

> 清理项仅限本稿触及的 planner 链路。`customer_tools/node.py` 的 `VectorSearchInputState` 若实施时发现与 Send 结构对齐有必要，可改为按实际 payload 修正而非删除——实施时以全局 grep 结果为准（CLAUDE.md 全项目扫描规则）。

---

## 5. 回退与异常处理总表

| 场景 | 回退行为 | 位置 |
|---|---|---|
| planner LLM 调用失败 / 结构化输出解析失败 | `_fallback` → 单任务 = 原 query 原文，`entity_name=""`，日志 `reason=llm_error` | 4.3 |
| planner 返回单任务 | 丢弃 LLM 文本，任务 = 原 query 原文；保留其 `name` 作实体约束 | 4.3 |
| planner 返回空列表 / 空串 sub_query / 重复任务 / >3 条 | 同上，日志 `reason=tasks=N` | 4.3 |
| `name` 为空（拆解分支） | 该任务不带实体约束，正常检索；日志记录空名 | 4.3 / 4.6.3 |
| 商品名未解析到 sku / DB 查询异常 | `entity_skus=None`，`search` 行为与现状逐字一致（fail-open） | 4.6.1 |
| 实体约束过滤后候选为空 | 放弃约束，用原候选池继续精排 | 4.6.2 |
| 精排失败/关闭 | 沿用融合 top-K（现状逻辑不变） | 4.6.2 |
| 检索全空（某分支） | 该分支 `records.result=""`；summarize 装配后若整体为空 → "No data to summarize." | 4.5 |
| 检索全空（全部分支） | 同上，`_assemble_evidence` 返回 `""` → 现状兜底分支 | 4.5 |
| 证据装配异常 | 不预期（纯函数、无 IO）；如发生由子图外层 TimeoutGuard 兜底 | — |
| 子图整体超时 | TimeoutGuard 30s 降级回答（现状保留） | `lg_builder.py:473-486` |

**原则**：任何拆解或检索增强环节失败都降级为"整句单分支"或"不加约束的检索"，**绝不让异常中断 SSE 流**。

---

## 6. 分阶段实施步骤

> 每阶段独立可验证、可提交。提交信息遵循项目规范 `[类型] 简述`；推送前先 `git branch --show-current` 确认分支（项目主分支为 `main`，远程无 dev 分支）。

### 阶段 1：planner 节点改造（本稿核心）

**Files**：`components/planner/models.py`、`components/models.py`、`planner/node.py`、`planner/prompts.py`、`kg_sub_graph/prompts/kg_prompts.py`、`app/core/config.py`、`lg_builder.py`、`workflows/multi_agent/multi_tool.py`

- [ ] **Step 1**：`planner/models.py` 新增 `EntitySubQuery`、重写 `PlannerOutput`（§4.1）；`components/models.py` 的 `Task` 增 `entity_name`（默认空）
- [ ] **Step 2**：`kg_prompts.py` 的 `PLANNER_SYSTEM_PROMPT` 整体替换为 §4.2 全文；`planner/prompts.py` 的 human 模板缩减为仅 `问题: {question}`
- [ ] **Step 3**：`planner/node.py` 按 §4.3 重写节点体（`_fallback` 单一出口 + try/except + MAX_TASKS + 非空/去重 + 三态日志 + `entity_name` 注入）
- [ ] **Step 4**：`config.py` 增 `PLANNER_TEMPERATURE: float = 0.0`；`lg_builder.py` 抽 `_build_research_model` 并造两个实例；`multi_tool.py` 签名增 `planner_llm`
- [ ] **Step 5**：验证（§7.1 的 planner 断言）→ 提交 `[feat] planner 改造：商品子问拆解提示词 + 实体名回填 + 一致性校验单分支回退 + 拆解温度收敛为 0`

### 阶段 2：检索侧零 LLM 闭环

**Files**：`components/summarize/node.py`、`app/services/product_dynamic_service.py`、`app/services/rag_retriever_service.py`、`workflows/multi_agent/edges.py`、`components/customer_tools/node.py`

- [ ] **Step 1**：`summarize/node.py` 增 `_assemble_evidence` 并替换节点体（§4.5）
- [ ] **Step 2**：`product_dynamic_service.py` 增 `resolve_skus_by_product_name`（§4.6.1）
- [ ] **Step 3**：`rag_retriever_service.search` 增 `entity_skus` 参数与 RRF 后过滤（§4.6.2）
- [ ] **Step 4**：`edges.py` Send payload 增 `entity_name`；`customer_tools/node.py` 解析并传入（§4.6.3）
- [ ] **Step 5**：`search` 增精排分数分布日志与预览补分（§4.7）
- [ ] **Step 6**：验证（§7.2/§7.3）→ 提交 `[feat] 检索侧零 LLM 闭环：summarize 跨分支证据去重 + 商品实体约束检索 + 精排分数观测`

### 阶段 3：清理、守护测试与文档收尾

- [ ] **Step 1**：删除 §4.8 三项死代码（删除前全局 grep 确认零引用）
- [ ] **Step 2**：新增 planner 节点名守护测试（§4.8）
- [ ] **Step 3**：全量 `pytest` 回归（基线：75/76，`test_bm25_retriever.py::test_bm25_recalls_docs_with_partial_terms` 为既有失败——`docs/项目问题.md` #8）
- [ ] **Step 4**：更新 `docs/PROJECT_ANALYSIS.md` §4.4.2（planner 现状边界改为落地后描述）与本稿归档状态行
- [ ] **Step 5**：提交 `[docs] planner 改造与检索闭环落地后文档同步`

### 阶段 4：本稿归档（阶段 1~3 全部落地后执行）

> 两份源稿已随本稿定稿 `git rm`（2026-09-18，未实施内容全部移交本稿、已落地记录转记于本稿头部「已落地前置」），无需再走归档流程。本阶段只处理本稿自身；**阶段 1~3 未落地前不得执行**，否则构成状态不实。

- [ ] **Step 1**：本稿归档状态行更新为 ✅ 已完成（附实施提交 hash、关键文件路径、测试结果）
- [ ] **Step 2**：`git mv` 本稿至 `docs/spec_plan/已完成/`
- [ ] **Step 3**：修正仓库内对本稿的导航引用（`docs/PROJECT_ANALYSIS.md` §4.4）——spec 内部 git 命令示例等历史记录不改
- [ ] **Step 4**：提交 `[docs] planner 实体拆解与检索闭环实施完成，spec 归档`

---

## 7. 验证方案

### 7.1 planner 断言（阶段 1）

均以三态日志为口径（`reason=` 后缀）：

| 输入 | 期望日志 | 期望 `tasks` |
|---|---|---|
| "米家智能晾衣机2和米家智能晾衣机Pro哪个更实用？它们分别多少钱？" | `split=2 (entity_count=2, names=['米家智能晾衣机2', '米家智能晾衣机Pro'])` | 2 条，各含实体全名与价格意图，无"它们"指代 |
| "米家智能晾衣机有哪些型号？分别多少钱？" | `split=2 (entity_count=1, ...)` | 2 条（合法：任务数 > entity_count） |
| "小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？" | `fallback (reason=tasks=1, name='小米智能门锁M30')` | 1 条，文本 = 原 query **原文** |
| "有没有适合小户型的智能电动沙发？" | `fallback (reason=tasks=1, name='智能电动沙发')` | 1 条，文本 = 原 query 原文（非 LLM 改写） |
| mock 抛异常 | `fallback (reason=llm_error, name='')`，无异常上抛 | 1 条 = 原 query |
| mock 返回 4 条 / 空串 sub_query / 重复 sub_query / 空列表 | `fallback (reason=tasks=N)` | 1 条 = 原 query |

**机制指标**：`fallback` 率（目标 <10%）、`name` 空值率（目标 <20%，超出则需加强提示词）、`split` 分布（1/2/3 三档占比，作为 P4 后续评估依据）。

### 7.2 检索侧场景清单（阶段 2，人工回归）

| # | 场景 | 预期 |
|---|---|---|
| 1 | 双实体对比查询 | 2 个并行分支；两分支日志各出现 `实体约束过滤: x -> y 条候选`；summarize 回答以对比形式呈现两个产品 |
| 2 | 单实体事实查询 | 1 分支；若 `name` 解析到 sku 则出现约束日志；检索结果含该商品块 |
| 3 | 商品名未收录（"XX牌空气炸锅多少钱"） | `未解析到 sku，跳过约束`，检索正常进行，summarize 如实告知未收录 |
| 4 | 政策类 query（"京东自营怎么退货"） | 无实体约束或约束后政策块（`sku_codes=[]`）保留 |
| 5 | 三实体查询 | 拆 3 分支（验证不硬编码 2），各分支约束独立生效 |
| 6 | 同一混合块被两分支召回 | 日志中该 `chunk_id` 只在证据文本出现一次 |
| 7 | 检索全空 | "No data to summarize." 兜底，无异常中断 |
| 8 | 流式输出 | 逐 token 正常输出（planner 分片仍被闸门拦截）；无"整段蹦出" |
| 9 | 重复提问 | 缓存命中，0 次 LLM 调用 |

### 7.3 回归与指标

- **全量 pytest**：基线 75/76（`#8` 既有失败不算回归）
- **`test_customer_tools_node.py`**：需全绿——该测试断言 `out["searches"][0].records["result"]`，本稿不改 `searches`/`records` 结构，理论零影响；若失败说明改动越界
- **LLM 调用次数**：按 §3 口径逐场景统计，实施前后应**完全一致**（本稿不增不减）
- **summarize 输入 token**：实施前后对比（预期下降——去重 + 去掉 `hybrid_docs`/`dynamic_rows` 原始 dict 冗余），并确认回答质量不回归
- **`evaluation/runner.py`**：跑一轮确认 `searches` 逐分支追溯仍可用

---

## 8. 决策记录

| # | 决策 | 日期 | 理由 |
|---|---|---|---|
| D1 | 检索侧采用零 LLM 闭环，不实施 HyDE 与 LLM 相关性评分 | 2026-09-18 | ① 评分器已被 CrossEncoder 精排替代（`reranker_service.py:4`），重新引入是回退；② HyDE 会为每分支 +1 次 LLM，与阶段 0「customer_tools 零 LLM 收敛」方向相反；③ 拆解后子 query 已含实体全名，HyDE 的边际收益需实测才能证明 |
| D2 | 跨分支去重在 summarize 节点内完成，不新增 merge 节点 | 2026-09-18 | 图结构刚经阶段 3 简化，不再增节点；`render_doc_blocks` 本就是两通道共用渲染器，复用零漂移 |
| D3 | `state["searches"]` 结构不动 | 2026-09-18 | 有 3 个消费方（summarize / final_answer history / evaluation runner），改结构会波及逐分支追溯与评测 |
| D4 | 取消 `original_question` 字段 | 2026-09-18 | 原动机（预处理后 question 被稀释）随预处理管道 2026-08-21 删除消失；`state.question` 现即消解后完整句 |
| D5 | P4（单任务空转 1 次 LLM）保持独立调用，不改调用结构 | 2026-09-18 | Router 服务 6 种 type，扩 schema 输出实体/子 query 有伤路由准确率风险；规则直通需先实测误拆率。本稿以 §7.1 日志采集拆解分布，为后续决策积累依据 |
| D6 | `entity_name` 用 `default=""` 而非 schema 级必填 | 2026-09-18 | 无实体的合法 query（entity_count=0）若强制必填会迫使 LLM 编造，且结构化输出失败会让整条 query 回退；"拆解时必填"落在提示词 + 节点校验两层 |
| D7 | 实体约束置于 RRF 后、精排前 | 2026-09-18 | 先净化候选池再让 reranker 排序，优于精排后过滤（后者只能减少、不能改善排序）；过滤后为空即放弃约束 |
| D8 | 实体约束不做硬过滤，`sku_codes=[]` 的政策块始终保留 | 2026-09-18 | 政策/通用块无商品归属，硬过滤会让"商品+政策"混合 query 失去政策证据 |
| D9 | `RERANK_MIN_SCORE` 阈值本稿不实现，先采分布 | 2026-09-18 | `top_k=5` 下拍脑袋设阈值易砍空分支；bge-reranker 输出 logits 非归一值，阈值必须实测 |

---

## 9. 风险与避坑清单

1. **planner 节点名是隐式契约**：`multi_tool.py:62` 的 `add_node(planner)` 取函数 `__name__`，与 `stream_filter.py:20` 的 `INTERNAL_NODES` 字符串耦合。重命名节点函数（如改成 `plan_tasks`）会让 planner 分片**静默外泄给用户**。§4.8 的守护测试是唯一防线，**必须随阶段 1 一起落地**。
2. **拆解质量没有数量锚可校验**：v2 起废除 `len(tasks)==entity_count` 硬校验（列表型拆解下任务数可 > entity_count），保障只剩"提示词示例 + 上限约束 + 三态日志观测"。回退率与空名率指标必须真的看。
3. **实体约束是双刃剑**：`entity_name` 由 LLM 产出，名字错 → 解析到错误 sku → 过滤掉正确块。fail-open 只覆盖"解析不到"与"过滤后为空"，**不覆盖"解析到但解析错"**。缓解：LIKE 分支的 `limit(5)` 限制误伤面；§7.2 场景 3 专门回归；上线后观察"检索结果为空/明显不相关"的日志。
4. **不解决混合块稀释**：66% 的块跨商品（`docs/项目问题.md` #3），混合块 `sku_codes=[A,B]` 对两边都通过约束。真正的解法是章节感知切分，属另一专项，**不要在本稿里试图用过滤绕开**。
5. **summarize 输入形态变化需回归**：去掉 `hybrid_docs`/`dynamic_rows` 原始 dict 后，prompt 变短但 LLM 少看到一份（重复的）数据。虽然渲染文本已含全部事实，仍需按 §7.3 对比回答质量——**这是本稿唯一可能引起质量回归的改动点**。
6. **温度改造不得打 tags**：2026-09-18 的流式 bug 根因就是共享实例上的 `tags=["research_plan"]`。新造的 planner 模型实例**不加任何 tags**——闸门只认节点名。
7. **`parent_task` 语义变化**：从"LLM 逐任务生成、可被改写"变为"节点注入原 query"。`edges.py` 发送逻辑与 `Task` 模型不变，但要确认无其它消费方依赖其旧语义（现状仅有 `edges.py:18` 传入即弃）。
8. **删除前全局检索**：§4.8 三项死代码删除前 grep 确认零引用（CLAUDE.md 全项目扫描规则），避免复现 `error_tool_selection` 式未注册引用。
9. **`resolve_skus_by_product_name` 的 LIKE 通配符**：用户可控文本进 `LIKE '%...%'`，SQLAlchemy 已参数化（无注入风险），但名称中的 `%`/`_` 会作为通配符生效——属可接受的宽松匹配，fail-open 已兜底。
10. **每阶段独立提交**：阶段 1（planner）与阶段 2（检索侧）严格分开提交，禁止跨阶段合并——阶段 2 依赖阶段 1 产出的 `entity_name`，但阶段 1 单独可验证（日志断言）。
