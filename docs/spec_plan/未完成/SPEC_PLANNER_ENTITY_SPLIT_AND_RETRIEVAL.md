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
> 4. ~~新增实体约束检索（name→sku→sku_codes）~~ **【2026-09-19 实测后否决，见差异 7 与 §4.6/§8 D10；该序号保留仅为与历史记录对账】**
> 5. **P4（单任务空转 1 次 LLM）决策为保持独立调用**，不改调用结构（§8 D5）
> 6. **planner 配置环境变量化**（§4.4.1）——新增 `PLANNER_TEMPERATURE` / `PLANNER_MAX_TASKS` 两个 `.env` 项，并显式写明动态补全（§4.4.2）不受拆解影响；2026-09-19 据评审确认补充
> 7. **【2026-09-19 实测后回退】删除实体约束检索（原 §4.6）**——原"name→sku→RRF 后过滤"整层不做。两轮实测（A/B/C 三方案对照 + LLM 端到端作答）结论：约束对单商品事实问有收益（错召回块 25→3，动态区 4~8 行→3~4 行），但对列表/型号类问句反向误杀（丢 1~3 个 SKU，覆盖 9→6），且方向与"拆分"相悖；LLM 在动态区按【商品编码】配对取值的准确率已实测足够（见 §8 D10 与 §4.6 新文）。保留 summarize 装配层去重（§4.5）作为唯一过滤层。

> **用途**: 售前（presale）链路 planner 节点与检索侧的合并改造——planner 从 Cypher 时代通用拆分模板改为商品场景的子问单元拆解（含主题词产出与一致性校验），检索侧在不新增 LLM 调用、不削减召回的前提下完成跨分支证据去重（实体约束已于 2026-09-19 实测否决，见 §4.6）
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
| 检索召回不受损 | 拆解分支的检索行为与单分支逐字一致（同一 `search(query)` 签名、同一候选集、同一动态补全），**不施加任何实体过滤**（2026-09-19 实测回退，见 §8 D10） |
| 不新增 LLM 调用 | 单实体首轮 ≤3 次（Router + planner + summarize）；缓存命中 0 次。**本稿不引入任何新 LLM 调用点** |
| 证据不重复 | summarize 输入按 `chunk_id` 跨分支去重、动态行按 sku 合并，同一块/同一 sku 只进 prompt 一次 |

### 1.3 设计原则

1. **宁可少拆，不可错拆**：拆解校验失败一律回退单分支（只损失召回，不产生误导性对比回答）
2. **零 LLM 优先**：检索侧增强全部用确定性手段（集合去重、合并），不引入新 LLM 调用点
3. **不削减召回**：任何"收窄候选集"的增强都默认不做——收窄与拆分（跨实体/跨型号遍历）方向相悖，且实测会误杀正确块（§4.6）
4. **相关性交给精排与 LLM**：精排负责排序、LLM 负责按编码配对取值；系统不另造一套相关性判定
5. **surgical**：`state["searches"]` 结构不动（`final_answer` history 与 `evaluation/runner.py:124` 依赖它做逐分支追溯），去重结果只影响 summarize 的输入装配

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

> **本稿的对应关系（2026-09-19 回退后）**：上表 4 项中，**只有 P5c 是本稿要解决的**（由 §4.5 跨分支去重解决）。P5a/P5b 描述的是"无实体过滤"——本稿**有意维持现状**（§4.6 实测否决了加过滤的方案），故 `customer_tools/node.py` 与 `rag_retriever_service.search` 均不改。P5d 的混合块稀释属切分策略专项，不在本稿范围。

### 2.3 可用的确定性抓手（源稿未记载）

| 抓手 | 位置 | 用途 |
|---|---|---|
| 每条 doc 带 `sku_codes`（`[]` = 政策/通用块）、`chapter`、`rerank_score` | `rag_retriever_service.py:55-73`、`:109` | 相关性观测与"从 chunk 取 sku"的现成字段 |
| `product_price_stock` 同时有 `sku`（唯一键）与 `product_name`（唯一键），1:1 映射 | `models/product_price_stock.py:13-19` | 动态区渲染的商品名来源（LLM 按名取值） |
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
                   ├─ 拆解通过 → tasks=[EntitySubQuery...]，每 Task 带 name（仅日志/观测）
                   └─ 否则     → _fallback：单 Task = 原 query 原文
                 → Send ×N 并行（payload 三键不变，无实体约束传入）
                      → customer_tools（零 LLM）
                           RAGRetrieverService.search(query)      ← 签名与行为均不变
                             HNSW ∥ BM25 → RRF → 精排 → top_k=5
                           命中块 sku_codes → fetch_by_skus → 动态区
                 → summarize（LLM，T=0.7）
                      跨分支装配：chunk_id 去重 + 动态行按 sku 合并 + 统一渲染
                 → final_answer
  → SSE 流式输出（闸门按节点名判定，planner 在 INTERNAL_NODES 内）
```

**链路结构无变化**：不新增节点、不新增边，`Send` 扇出与 `searches` 累加器保持现状。改动全部落在节点内部逻辑与检索服务签名。

**LLM 调用次数**（无缓存命中）：单实体首轮 3 次（Router + planner + summarize），多轮 4 次（+入口消解），双实体 3 次（拆 N 分支不增 LLM）。**与本稿实施前完全一致**。

**动态信息通道（价格/库存）不受拆解影响**：`create_multi_tool_workflow` 拆分后，动态补全仍由 `customer_tools` 单一节点内的 `fetch_by_skus` 承担，**每个并行分支各自执行一次**（详见 §4.4.2）。本稿不新增、不删减动态检索调用点。

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
            "（仅用于日志与拆解质量观测，不流入检索侧——见 §4.1 说明）"
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
# —— components/models.py 的 Task 不改（与现状逐字一致）——
class Task(BaseModel):
    question: str = Field(..., description="The question to be addressed.")
    parent_task: str = Field(..., description="The parent task this task is derived from.")
    data: Optional[Any] = Field(default=None, description="The search result details.")
```

> **`Task` 不增字段**（2026-09-19 回退后确认）：原设计的 `entity_name` 字段随实体约束一并取消（§8 D10）。LLM 产出的主题词 `name` 仍保留在 `EntitySubQuery` 上，但**只用于节点日志与 §7.1 的空名率观测，不流入检索侧**——它不再承担任何检索语义。

> **`name` 的性质（2026-09-19 更新）**：字段用 `default=""` 而非强制必填——`entity_count=0` 的合法 query（如"有没有推荐的吗"）不存在实体，schema 级必填会迫使 LLM 编造，且 `with_structured_output` 校验失败会让**整条 query 回退单分支**，代价大于收益。
>
> **该字段现已降级为纯观测字段（2026-09-19 决策：保留）**：实体约束取消（§4.6/§8 D10）后，`name` 不参与任何检索决策，只用于日志与 §7.1 的空名率统计。因此"拆解时空名"不再是 fail-open 场景，**也不再需要节点校验**——空名只影响观测指标，不影响链路行为。
>
> **为什么保留而不是删**：删掉它能让提示词少一条要求、schema 少一个字段，但换来的是"将来做实体识别时要重新加回去"。实测确认它不产生任何链路风险（节点不校验、下游不消费），而日志里的主题词分布是后续专项的现成输入。**代价仅为每次 planner 调用多输出一个短字符串。** 若上线后发现空名率长期极高（>50%，说明 LLM 给不出稳定主题词），再考虑删除。

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
任务间互不重复、互不依赖；总数不超过 <<MAX_TASKS>>。   ← 源码中此为占位符，实施时替换为 settings.PLANNER_MAX_TASKS（§4.4.1）
每条任务同时给出主题词 name：商品全名或品类词（拆成多条时必填，将用于检索范围限定）。

> **⚠ 花括号转义是硬约束（2026-09-19 实施时踩到）**：本提示词会进 `ChatPromptTemplate`，它把任何 `{xxx}` 当模板变量索取，实测两个坑：
> 1. 示例里的字面 JSON `{name: "..."}` 被解析成变量名 `name` → `KeyError: missing variables {'name'}`；
> 2. 该异常**在 chain 的输入校验阶段抛出，绕过 planner 节点的 try/except**（节点 try 的是 `planner_chain.ainvoke`，而校验在读输入时就炸），表现为**静默回退单分支、日志无异常痕迹**——最难排查的一类。
>
> 因此：**源码中示例字典一律写成 `{{name: "..."}}`**（双花括号 → 渲染回单花括号字面文本，下例已按此形态书写），上限用不含花括号的 `<<MAX_TASKS>>` 占位符。**验收方式**：`create_planner_prompt_template().input_variables` 必须恰好等于 `['question']`。

示例（商品名取自知识库真实在售；下列 `{{ }}` 即源码转义形态）：
- 问题：米家智能晾衣机2的承重是多少公斤？
  entity_count：1 → 任务：[{{name: "米家智能晾衣机2", sub_query: "米家智能晾衣机2的承重是多少公斤？"}}]     # 单意图不拆

- 问题：米家智能晾衣机2和米家智能晾衣机Pro哪个更实用？它们分别多少钱？
  entity_count：2 → 任务：[{{name: "米家智能晾衣机2", sub_query: "米家智能晾衣机2的功能实用性、优缺点和售价是多少？"}},
                          {{name: "米家智能晾衣机Pro", sub_query: "米家智能晾衣机Pro的功能实用性、优缺点和售价是多少？"}}]
  # 多实体每实体 1 条；指代"它们"被实体全名替换

- 问题：米家智能晾衣机有哪些型号？分别多少钱？
  entity_count：1 → 任务：[{{name: "米家智能晾衣机", sub_query: "米家智能晾衣机有哪些型号？"}},
                          {{name: "米家智能晾衣机", sub_query: "米家智能晾衣机各型号的售价分别是多少？"}}]
  # 列表+详情并列拆 2（任务数可大于 entity_count）

- 问题：小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？
  entity_count：1 → 任务：[{{name: "小米智能门锁M30", sub_query: "小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？"}}]  # 属性细节问合并不拆

- 问题：有没有适合小户型的智能电动沙发？
  entity_count：1 → 任务：[{{name: "智能电动沙发", sub_query: "有没有适合小户型的智能电动沙发？"}}]  # 仅品类的选购句不拆
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


def _fallback(question: str) -> List[Task]:
    """所有回退的唯一出口：单分支整句检索（question 恒为原 query 原文，不经 LLM）。"""
    return [Task(question=question, parent_task=question)]


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
            Task(question=t.sub_query.strip(), parent_task=question)
            for t in planner_output.tasks
        ]
        logger.info(
            "planner_decision: split={} (entity_count={}, names={})",
            len(task_list), entity_count, [t.name.strip() for t in planner_output.tasks],
        )
    else:
        # 单任务场景：LLM 的单任务文本一律丢弃（防改写，用原 query 原文）。
        # name 仅作观测（§7.1 空名率），不流入检索侧——实体约束已于 2026-09-19 取消（§8 D10）。
        single_name = ""
        if planner_output is not None and len(planner_output.tasks) == 1:
            single_name = planner_output.tasks[0].name.strip()
        task_list = _fallback(question)
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
| LLM 调用异常 / 结构化输出解析失败 | `_fallback`，`reason=llm_error`，日志 `name=''` |
| 任务数 < 2（含 LLM 返回单任务文本） | `_fallback`，**任务文本 = 原 query 原文**（不采用 LLM 文本）；LLM 的 `name` 仅入日志 |
| 任务数 > MAX_TASKS（3） | `_fallback`，`reason=tasks=N` |
| 任一 `sub_query` 为空白 | `_fallback` |
| 任务 `sub_query` 重复（strip 后集合去重计数不等） | `_fallback` |
| 任务数 ∈ [2,3] 且全部通过 | 采用拆解，`split=N` |

> 全部分支的 `Task` 只带 `question` / `parent_task` 两个业务字段——回退与拆解**产出结构完全一致**，下游无法也不需要区分自己拿到的是哪种分支。

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
def create_multi_tool_workflow(
    llm: BaseChatModel,
    planner_llm: Optional[BaseChatModel] = None,
):
    """planner_llm 缺省回退 llm——兼容未同步更新的既有调用方（见下方调用点清单）。"""
    ...
    planner = create_planner_node(llm=planner_llm or llm)
```

**`create_multi_tool_workflow` 全部调用点（改签名前必须核对）**：

| 调用点 | 现状 | 处置 |
|---|---|---|
| `lg_builder.py:437` | `create_multi_tool_workflow(llm=model)` | 本稿改为传两个实例（§4.4 上文） |
| **`evaluation/__main__.py:112`** | `create_multi_tool_workflow(llm=build_agent_llm())` | **默认值兼容，可不改**；若希望评测也反映 planner 低温，可显式传 `planner_llm=build_agent_llm()` |
| `workflows/multi_agent/__init__.py:1` | 仅 re-export，无调用 | 不动 |

> **默认值不是可选项**：`evaluation/__main__.py` 是硬调用点，若无默认值，评测脚本一跑就 `TypeError`。本稿 §7.3 要求跑一轮评测验证追溯，故此处必须容错。
> **需同步补 import**：`multi_tool.py` 当前只 import 了 `BaseChatModel`（`multi_tool.py:1`），**没有 `Optional`**，实施时需补 `from typing import Optional`。

> **与 2026-09-18 流式整改的兼容性**：闸门已由"共享模型实例上的 `tags=["research_plan"]` 黑名单"改为"按 `metadata["langgraph_node"]` 节点名判定"（`stream_filter.py:19-23`），拆两个模型实例不再有任何流式副作用。**实施时不得给新实例打 tags**。

### 4.4.1 planner 配置项（config.py + .env）

**文件**：`app/core/config.py`（新增两项）、**项目根 `.env`（维护者本地补充）**、`kg_sub_graph/prompts/kg_prompts.py`（`MAX_TASKS` 派生句）

`config.py` 新增两个字段（`PLANNER_TEMPERATURE` 已见 §4.4）：

```python
# planner settings
PLANNER_TEMPERATURE: float = 0.0   # planner 拆解温度（确定性优先）
PLANNER_MAX_TASKS: int = 3         # 拆解任务数上限；超过一律回退单分支（§4.3）
```

项目根 `.env` 需由维护者补两行（**该文件在 `.gitignore` 内，改动不进 git，实施时须手工添加**），写法对齐现有温度参数分节：

```bash
# planner 拆解配置（见 docs/spec_plan/未完成/SPEC_PLANNER_ENTITY_SPLIT_AND_RETRIEVAL.md §4.4.1）
PLANNER_TEMPERATURE=0.0                        # 拆解温度（必须为 0，保证同输入同拆解）
PLANNER_MAX_TASKS=3                            # 拆解任务数上限；超过一律回退单分支（≥2；设 1 等价于关闭拆解）
```

**生效机制**：`Settings` 为 pydantic-settings，`ENV_FILE = ROOT_DIR / ".env"`（`config.py:7`）。**未在 `Settings` 声明的键即使写进 `.env` 也不会被读取**，故 `config.py` 字段是前提，`.env` 只是覆盖层；不写 `.env` 时按 `config.py` 默认值运行（`PLANNER_TEMPERATURE=0.0` / `PLANNER_MAX_TASKS=3`），两条链路等价。

**提示词口径不得分裂（关键）**：`PLANNER_SYSTEM_PROMPT` 中"总数不超过 3"（§4.2 全文）为字面文本，环境变量改后不会自动跟随。实施时该句改为由 settings 派生：

```python
# kg_prompts.py 顶部
from app.core.config import settings

# 以 §4.2 全文为基准，仅上限改为占位符替换（其余内容逐字照抄，不另起稿）
_MAX_TASKS_PLACEHOLDER = "<<MAX_TASKS>>"

PLANNER_SYSTEM_PROMPT = """你是售前商品问答的任务规划组件。……（§4.2 全文）
……
任务间互不重复、互不依赖；总数不超过 <<MAX_TASKS>>。
每条任务同时给出主题词 name：商品全名或品类词（拆成多条时必填，将用于检索范围限定）。……""".replace(
    _MAX_TASKS_PLACEHOLDER, str(settings.PLANNER_MAX_TASKS)
)
```

> **为什么不用 f-string（2026-09-19 实测修正）**：f-string 要求示例字典的花括号转义成 `{{name: ...}}`，与"提示词原文可直接比对"的意图冲突；更关键的是**本模板会进 `ChatPromptTemplate`，它会二次解析花括号**——`{max_tasks}` 会被当模板变量索取，抛 `KeyError` 且**绕过节点 try/except**（静默回退单分支）。用不含花括号的 `<<MAX_TASKS>>` 可完全避开。详见 §4.2 的转义约束说明。

节点侧同名常量同步改为派生（§4.3 的 `MAX_TASKS = 3` 字面量替换为下行），保证**提示词、节点校验、日志三处同源**：

```python
# planner/node.py 顶部（与 §4.3 其余代码一致，仅此行不同）
from app.core.config import settings

MAX_TASKS = settings.PLANNER_MAX_TASKS
```

> **常量保持模块级**：项目已有同款先例——`rrf_fusion.py:22` `DEFAULT_K = settings.RRF_FUSION_K`、`memory_cache.py:36` `DEFAULT_SUMMARY_TTL = settings.MEMORY_CACHE_TTL`，均为模块级派生一次。故 `MAX_TASKS = settings.PLANNER_MAX_TASKS` 非每次调用重读；`.env` 变更需重启进程生效。
> **可选提示（不强制）**：`PLANNER_MAX_TASKS=1` 可一键关闭拆解（校验恒不通过 → 全量单分支），作为拆解质量不达标时的回滚开关；`PLANNER_TEMPERATURE=0.0` 为硬要求，改高会让同输入产生不同拆解，破坏 §7.1 断言可复现性。

### 4.4.2 并行分支的执行契约（动态工具 × rag_tool）

**结论**：拆解不改变 `customer_tools` 节点的执行内容——**rag_tool（混合检索）与商品动态信息工具（价格/库存）照常一起跑，且每个并行分支各自独立跑一遍，行为与单 query 场景逐字一致。**

**依据链**（现有代码，本稿不改）：

| 环节 | 现状 | 依据 |
|---|---|---|
| 扇出粒度 | `Send × N` 每个 task 一条消息，payload 仅 `task/question/parent_task`（**本稿不动此结构**），**无"是否并行分支"标志位** | `multi_tool.py:59-80`、`edges.py:12-21` |
| 分支执行 | 每条 Send 独立进入 `customer_tools`，各自执行：`retriever.search(query)` → 收集本分支命中块的 `sku_codes` → `fetch_by_skus(skus)` → `render_dynamic_rows` + `render_doc_blocks` | `customer_tools/node.py:62-90` |
| 两个工具的关系 | 动态补全由检索结果门控（方案 A，2026-09-06）：**只对命中文档的 sku 候选集取动态行**，零命中/无 sku 块不查动态库；两支共享同一 `sku_codes → sku` 通道，不是两个独立并行调用 | `customer_tools/node.py:65-81` |

**为什么这条约定必须写明**：

1. **动态信息是唯一来源**——若被误改成"只在单分支路径补全"或"先合并再补全"，价格/库存会整段丢失，且 summarize 的 prompt 规则（"价格/库存只取【商品动态信息区】"）会拿到空区，LLM 只能回"动态信息暂未收录"（**是失败而非降级**）。
2. **候选 sku 来自本分支命中的全部块，不做任何收窄**——`docs` 里每块的 `sku_codes` 全部进候选集（`customer_tools/node.py:67-71`），跨商品混合块会把邻居商品的 sku 一并带进来（实测 8 行中 3~4 行与提问无关）。**这是有意保留的设计**，不是遗漏：2026-09-19 实测确认收窄候选集会误杀正确块、且对列表类问句反向有害（§4.6 / §8 D10），而 LLM 在动态区按【商品名 + 商品编码】取值的错配率实测为零（§4.6.1 实验 E）。
3. **本稿不新增动态检索调用点**——§1.2 指标"不新增 LLM 调用"由本约定保证不被打折：`fetch_by_skus` 是 DB 查询非 LLM，不触发 §3 的三次调用口径。

**跨分支去重的边界**：§4.5 的 `_assemble_evidence` 在 summarize 内对 `hybrid_docs`（按 `chunk_id`）与 `dynamic_rows`（按 sku）做跨分支合并——**去重只发生在证据装配层，不改变"每分支各跑一遍两个工具"的执行层事实**。混合块（`sku_codes=[A,B]`，实测占 66%）被 A/B 两分支各召回一次时，A、B 的动态行被查两遍、在装配处合并为一份，属已接受的少量冗余（DB 查询非 LLM，且 §4.5 合并后进 prompt 只一次）。

**回归守护（对应 §7.2）**：双实体查询下，应能在日志中看到**两条**"动态补全: 候选 N 个 sku,命中 M 行"，且 summarize 的 `{results}` 中动态区全局只有一份、每个 sku 一行。

### 4.5 summarize 跨分支证据装配（唯一过滤层）

**文件**：`.../components/summarize/node.py`

**定位（2026-09-19 起）**：实体约束取消后，本节是全链路**唯一的过滤层**，承担三件事——① 跨分支去重（chunk_id / sku）；② 两通道证据统一渲染；③ 把相关性判断完整交给 LLM（§4.6.2 证据 4）。**它不做相关性过滤**，凡是各分支检索到的证据一律保留，由 LLM 依据块前缀与动态区商品名自行取舍。

```python
from app.tools.doc_block_renderer import render_doc_blocks, render_dynamic_rows


def _assemble_evidence(searches: list) -> str:
    """跨分支证据装配：chunk_id 去重 + 动态行按 sku 合并 + 统一渲染。

    重复来源：跨商品混合块（docs/项目问题.md #3 实测 66%）会被 A/B 两条子 query
    各召回一次，现状原样拼接导致同一块进 prompt 两次。
    同时消除现状把 records 整体（含 hybrid_docs/dynamic_rows 原始 dict）塞进 prompt
    的冗余——渲染文本已含全部事实，原始 dict 属重复通道。

    注意：只做去重与合并，不做相关性过滤——相关性由精排排序 + LLM 取值判断承担
    （§4.6 / §8 D10：实测证明系统侧再做一层相关性收窄会误杀正确块）。
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
            # 无键块不参与去重：直接保留。若照原写法 key=None，首个无键块占用 None
            # 后，其余无键块会被整体丢弃（静默丢证据）。现状 chunk_id 恒在
            # （rag_retriever_service.py:60），此护栏防上游结构变动。
            if key is not None and key in seen:
                continue
            if key is not None:
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
| 相关性过滤 | 无（原样拼接） | **无**——本稿不引入任何相关性收窄（§8 D10），仅去重与合并 |
| `state["searches"]` | — | **不动**（`final_answer` history 与 `evaluation/runner.py:124` 继续拿到逐分支原始追溯） |
| `records["result"]` / `hybrid_docs` / `dynamic_rows` 字段 | — | **不动**（`customer_tools` 与 `test_customer_tools_node.py` 的断言继续有效） |

**保留的分组信息**：合并后不保留"哪条子问题命中该块"的分组。判断依据：每块前缀行 `【商品编码:...｜知识类型:...｜来源:...】`（`doc_block_renderer.py:53-55`）已提供商品归属，且 summarize 收到的 `question` 是完整原问题（含对比意图），分组不构成必需信息。**若回归发现对比类回答质量下降，再考虑按 `task` 分组渲染。**

### 4.6 实体约束检索——已实测否决（2026-09-19）

> **结论：不做。** 本节保留完整实验记录与数据，供后续复用判断；**实施阶段 2 不包含本节任何内容**。否决理由见 §8 D10。

**原设计**：`planner` 产出实体名 → Send 传入 → `customer_tools` 解析 name→sku → `search(entity_skus=...)` 在 RRF 后、精排前过滤掉"归属其它商品"的候选。目标是缓解 `docs/项目问题.md` #2（同品牌多型号竞争错召回）。

#### 4.6.1 实验方法与数据（真实库：47 商品 / 42 块，GPU 精排）

**实验 A/B/C（同一 query，三种检索策略，度量静态块与动态区覆盖）**

| query（列表+详情类） | A 单分支 | B 拆2无约束 | C 拆2带约束 |
|---|---|---|---|
| 米家智能晾衣机有哪些型号？分别多少钱？ | 8 sku | **10 sku** | 7 sku ⚠ |
| 小米智能门锁有哪些型号？分别多少钱？ | 9 sku | **9 sku** | 6 sku ⚠ |
| 电动升降桌有哪些款式？价格多少？ | 8 sku | **8 sku** | 6 sku ⚠ |

拆分本身三组全赢（不丢任何 sku，多召回 1~2 个；同一块精排分从 0.52~0.86 升到 0.92~0.99）；**约束三组全输**，最多丢掉 3 个 SKU。

**根因**：约束的优化方向（收窄到一个商品）与拆分的动机（跨型号/跨实体遍历）**正好相反**。典型误杀：实体名"电动升降桌"解析只覆盖 6 款中的 2 款（其余 4 款商品名里无此串），"小米智能门锁"受 `limit(5)` 截断只覆盖 9 款中的 5 款——剩余型号被当"别的商品"丢掉，LLM 只能答"只有 2 款"。

**实验 D（约束的收益侧：同品牌多型号竞争场景，5 条单商品事实问）**

| 用例 | 无约束 正确/错召回 | 有约束 正确/错召回 | 动态区行数 |
|---|---|---|---|
| 米家智能晾衣机3 最大承重 | 2 / 3 | **2 / 0** | 8 → 4 |
| 小米M20 大屏猫眼版 开锁方式 | 2 / 3 | **2 / 0** | 7 → 3 |
| 米家智能晾衣机2 Pro 功能特点 | 1 / 4 | **1 / 0** | 8 → 3 |
| 乐歌E2 升降范围 | 1 / 4 | **1 / 0** | 10 → 3 |
| 米家智能晾衣机2 最大承重（截断名） | 2 / 3 | **2 / 1** | 8 → 6 |
| 智能电动沙发 小户型（品类词） | 1 / 4 | **0 / 2** ⚠ | 8 → 2（目标掉出）⚠ |
| 米家智能门锁M30 续航（解析失败） | 0 / 4 | 0 / 0 | 8 → **0** ⚠ |

**约束收益真实存在**：精确解析成功时错召回块 14 → 0、正确块零损失、动态区行数减半。**但两个失败模式不可接受**：品类词解析不全时误杀目标商品（目标 sku 掉出动态区 → LLM 连价格都拿不到）；解析失败时约束空转且动态区被清空。

**实验 E（端到端 LLM 作答，真实 summarize prompt + 真实 deepseek）**

| 用例 | 正确答案 | 无约束作答 | 有约束作答 |
|---|---|---|---|
| 晾衣机3 承重 | 35kg | ✓（动态区 8 行、含 3 个门类） | ✓（动态区 4 行） |
| 小米M20 大屏猫眼版 多少钱 | ¥1999 | ✓（动态区 7 行跨门锁/窗帘） | ✓（动态区 3 行） |
| 米家智能晾衣机有哪些型号+价格 | 5 款带价 | **5 款全对 ✓** | 只给 4 款、且编出库外型号 ⚠ |

**决定性证据**：动态区的渲染格式是 `【动态|商品编码:S｜商品名:N】¥价格｜库存`——**商品名完整出现在每一行**，LLM 是对着名字取值而非靠位置猜。实测跨商品噪音下**零错配**。

#### 4.6.2 否决理由

| # | 理由 | 证据 |
|---|---|---|
| 1 | 与拆分动机方向相悖，对一个用户明确要求"遍历"的问句施加"收窄" | 实验 A/B/C：约束丢 1~3 个 SKU |
| 2 | 输入不可靠：`name` 由 LLM 产出，简称与库内全名不匹配率高 | 实验 D：`米家智能门锁M30` → `[]`；`智能电动沙发` → 仅 1/7 |
| 3 | 可靠性改造（三级匹配 + 精确/模糊分流 + scope 标记 + 品类词降级）引入的状态量大于收益 | 上述四个补丁缺一不可，且每个都有新的边界 |
| 4 | LLM 已能正确处理邻居商品噪音，**错配风险实测为零** | 实验 E：跨商品噪音下价格/参数取值全对 |
| 5 | 原始目标（问题 #2）本就不是它解决的 | 实验 D：目标块 top-5 命中 0 条的用例，约束全程空转；真因是召回质量 |

**保留的唯一收益是 token 精简**（动态区行数约减半），属优化而非正确性，不值得为其承担"误杀正确商品"的风险。

#### 4.6.3 实施范围（本稿据此调整）

| 原计划项 | 处置 |
|---|---|
| `product_dynamic_service.resolve_skus_by_product_name` | **不新增** |
| `Task.entity_name` 字段 | **不新增**（`Task` 与现状逐字一致） |
| `rag_retriever_service.search` 的 `entity_skus` 参数与过滤块 | **不新增**（签名不变） |
| `edges.py` Send payload 增 `entity_name` | **不新增**（三键不变） |
| `customer_tools/node.py` 实体解析逻辑 | **不新增**（节点体不变） |

> **遗留观察（供后续专项参考）**：`resolve_skus_by_product_name` 的 LIKE 分支带 `limit(5)`——实测该截断会让"小米智能门锁"只覆盖 9 款中的 5 款。本稿不实现该函数，但**将来若另做实体识别或定向查询，必须先解决这个截断**，否则同类误杀会重现。
> **动态区噪音的残余影响**：不做约束后，动态区可能含与提问无关的商品行（实验 E 中 8 行里 3~4 行无关）。实测不影响取值正确性；prompt 侧的兜底规则见 §4.6.4。

#### 4.6.4 summarize prompt 的取值纪律（防错配唯一防线）

> **文件与 §4.2 无关，勿混**：本节改的是 `components/summarize/prompts.py`（生成端 human 模板的规则列表）；§4.2 改的是 `kg_sub_graph/prompts/kg_prompts.py`（planner 的 system 提示词）。两处都写着"全文替换"式的清单，实施时各改各的，不要合并处理。

实体约束取消后，"价格/库存不得跨商品错配"**完全依赖 prompt 规则**。现有规则（`summarize/prompts.py:40-42`）已含三条：按【商品编码】配对取值、无归属块不得断言动态信息、未收录时如实告知。本稿**新增一条**（2026-09-19 补，理由为动态区噪音在无约束下必然出现）：

```text
* 动态区中与用户当前询问的商品/品类无关的行，不得出现在回答里（用户问 A 时不得提及 B 的价格/库存）
```

> **决策标注**：该条为**建议性补充**——实验 E 证明不加也不会错配（LLM 自行忽略无关行），加上是为了在长上下文、更多无关行的情况下加固。实施时若发现它引起"漏答"（把用户其实想问的型号漏掉），可直接删除，不影响本稿其余设计。

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

> 清理项仅限本稿触及的 planner 链路。**2026-09-19 回退后**：`edges.py` 的 Send payload 不再新增 `entity_name`，`VectorSearchInputState` 与 payload 的偏差维持现状，可直接按"零引用"处理（删除）；若实施时全局 grep 发现新引用，则按实际 payload 修正而非删除（CLAUDE.md 全项目扫描规则）。

---

## 5. 回退与异常处理总表

| 场景 | 回退行为 | 位置 |
|---|---|---|
| planner LLM 调用失败 / 结构化输出解析失败 | `_fallback` → 单任务 = 原 query 原文，日志 `reason=llm_error`、`name=''` | 4.3 |
| planner 返回单任务 | 丢弃 LLM 文本，任务 = 原 query 原文；其 `name` 仅入日志 | 4.3 |
| planner 返回空列表 / 空串 sub_query / 重复任务 / >3 条 | 同上，日志 `reason=tasks=N` | 4.3 |
| `name` 为空（拆解分支） | 无行为影响（该字段只用于观测）；日志记录空名 | 4.1 / 4.3 |
| 精排失败/关闭 | 沿用融合 top-K（现状逻辑不变，本稿不改检索服务） | — |
| 检索全空（某分支） | 该分支 `records.result=""`；summarize 装配后若整体为空 → "No data to summarize." | 4.5 |
| 检索全空（全部分支） | 同上，`_assemble_evidence` 返回 `""` → 现状兜底分支 | 4.5 |
| 证据装配异常 | 不预期（纯函数、无 IO）；如发生由子图外层 TimeoutGuard 兜底 | — |
| 子图整体超时 | TimeoutGuard 30s 降级回答（现状保留） | `lg_builder.py:473-486` |

**原则**：任何拆解或检索增强环节失败都降级为"整句单分支"或"不加约束的检索"，**绝不让异常中断 SSE 流**。

---

## 6. 分阶段实施步骤

> 每阶段独立可验证、可提交。提交信息遵循项目规范 `[类型] 简述`；推送前先 `git branch --show-current` 确认分支（项目主分支为 `main`，远程无 dev 分支）。

### 阶段 1：planner 节点改造（本稿核心）

**Files**：`components/planner/models.py`、`components/models.py`、`planner/node.py`、`planner/prompts.py`、`kg_sub_graph/prompts/kg_prompts.py`、`app/core/config.py`、`lg_builder.py`、`workflows/multi_agent/multi_tool.py`、**项目根 `.env`（本地手工，不进 git）**

> **`components/models.py` 实际不改**（`Task` 与现状一致，§4.1）；列出它是为提醒实施者"确认不需要改"，而非需要编辑。
> **`evaluation/__main__.py` 不在 Files 内**：靠 `multi_tool.py` 的 `planner_llm` 默认值兼容，无需编辑，但必须在 §7.3 实跑验证。

- [ ] **Step 1**：`planner/models.py` 新增 `EntitySubQuery`、重写 `PlannerOutput`（§4.1）；`components/models.py` 的 `Task` **不改**（2026-09-19 回退后 `entity_name` 不新增）
- [ ] **Step 2**：`kg_prompts.py` 的 `PLANNER_SYSTEM_PROMPT` 整体替换为 §4.2 全文；`planner/prompts.py` 的 human 模板缩减为仅 `问题: {question}`
- [ ] **Step 3**：`planner/node.py` 按 §4.3 重写节点体（`_fallback` 单一出口 + try/except + `MAX_TASKS = settings.PLANNER_MAX_TASKS` + 非空/去重 + 三态日志，`name` 只入日志；`multi_tool.py` 需确认 `Optional` 已 import）
- [ ] **Step 4**：`config.py` 增 `PLANNER_TEMPERATURE` / `PLANNER_MAX_TASKS` 两项；`kg_prompts.py` 提示词末句改为 `{settings.PLANNER_MAX_TASKS}` 派生（**勿逐字照抄 §4.2 的"总数不超过 3"默认值，`planner_llm` 默认值容错的调用点在下一行**）；`lg_builder.py` 抽 `_build_research_model` 并造两个实例；`multi_tool.py` 签名改为 `planner_llm: BaseChatModel | None = None`（默认回退 `llm`，兼容 `evaluation/__main__.py:112`）
- [ ] **Step 5**：项目根 `.env` 手工补 `PLANNER_TEMPERATURE` / `PLANNER_MAX_TASKS` 两行（§4.4.1；该文件 gitignore，仅本地生效，提交不含此项）
- [ ] **Step 6**：验证（§7.1 的 planner 断言）→ 提交 `[feat] planner 改造：商品子问拆解提示词 + 主题词产出 + 一致性校验单分支回退 + 拆解温度收敛为 0`

### 阶段 2：检索侧零 LLM 闭环

**Files**：`components/summarize/node.py`、`app/services/rag_retriever_service.py`、`.../components/summarize/prompts.py`

- [ ] **Step 1**：`summarize/node.py` 增 `_assemble_evidence` 并替换节点体（§4.5）
- [ ] **Step 2**：`summarize/prompts.py` 增"动态区无关行不得出现在回答里"一条（§4.6.4；建议性，可实施时删）
- [ ] **Step 3**：`search` 增精排分数分布日志与预览补分（§4.7）
- [ ] **Step 4**：验证（§7.2/§7.3）→ 提交 `[feat] summarize 跨分支证据装配：chunk_id/sku 去重 + 取值纪律 prompt + 精排分数观测`

> **不在本阶段**（2026-09-19 回退）：实体约束相关的四处改动全部取消——`product_dynamic_service.py`、`rag_retriever_service.search` 签名、`edges.py` Send payload、`customer_tools/node.py` **均不动**。本阶段只改 summarize 一个节点 + 一处日志。

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
| "米家智能晾衣机2和米家智能晾衣机Pro哪个更实用？它们分别多少钱？" | `split=2 (entity_count=2, names=['米家智能晾衣机2', '米家智能晾衣机Pro'])`（list 由 loguru `{}` 格式化，**单引号**） | 2 条，各含实体全名与价格意图，无"它们"指代 |
| "米家智能晾衣机有哪些型号？分别多少钱？" | `split=2 (entity_count=1, ...)` | 2 条（合法：任务数 > entity_count） |
| "小米智能门锁M30支持人脸识别吗？续航多久？有远程告警吗？" | `fallback (reason=tasks=1, name='小米智能门锁M30')` | 1 条，文本 = 原 query **原文** |
| "有没有适合小户型的智能电动沙发？" | `fallback (reason=tasks=1, name='智能电动沙发')` | 1 条，文本 = 原 query 原文（非 LLM 改写） |
| mock 抛异常 | `fallback (reason=llm_error, name='')`，无异常上抛 | 1 条 = 原 query |
| mock 返回 4 条 / 空串 sub_query / 重复 sub_query / 空列表 | `fallback (reason=tasks=N)` | 1 条 = 原 query |

**机制指标**：`fallback` 率（目标 <10%）、`name` 空值率（目标 <20%，超出则需加强提示词）、`split` 分布（1/2/3 三档占比，作为 P4 后续评估依据）。

**配置项生效断言（§4.4.1）**：

| 检查 | 期望 |
|---|---|
| 未写 `.env` 两项 | 走 `config.py` 默认（0.0 / 3），§7.1 全部断言行为不变 |
| `.env` 令 `PLANNER_TEMPERATURE=0.7` | 同一 query 连跑 3 次，拆解结果开始漂移（反证环境变量已生效；验完必须改回 0.0） |
| `.env` 令 `PLANNER_MAX_TASKS=2` | mock 返回 3 条 → `fallback (reason=tasks=3)`；返回 2 条 → `split=2` |
| `.env` 令 `PLANNER_MAX_TASKS=5` | mock 返回 4 条 → `split=4`（证明提示词与校验同源跟随，非硬编码 3） |
| 键名核对 | `.env` 键必须与 `Settings` 字段逐字一致（`PLANNER_MAX_TASKS`，非 `PLANNER_MAX_TASK`）——**pydantic-settings 对未声明/拼错的键既不报错、也不生效，直接按默认值跑**，拼错的表现是"改了没反应" |

### 7.2 检索侧场景清单（阶段 2，人工回归）

| # | 场景 | 预期 |
|---|---|---|
| 1 | 双实体对比查询 | 2 个并行分支；两分支日志**各出现一条 `动态补全: 候选 N 个 sku,命中 M 行`**（§4.4.2 执行契约）；summarize 回答以对比形式分别呈现两个产品 |
| 2 | 单商品事实问 + 同族干扰（"米家智能晾衣机3 四分区晾晒 超薄隐形 的最大承重是多少？"） | 1 分支；证据含多型号噪音；**最终回答只给目标商品的承重值，不出现同族其它型号的参数** |
| 3 | **错配断言（§4.6.4 唯一防线的直接验证）** | 无约束证据下（动态区必然含多商品行），回答中的每个价格/参数都能对应到正确的商品编码；**不得出现"把 B 的价格安在 A 头上"** |
| 4 | 商品名未收录（"XX牌空气炸锅多少钱"） | 检索正常进行，summarize 如实告知未收录（无约束后无"跳过约束"日志） |
| 5 | 政策类 query（"京东自营怎么退货"） | 政策块（`sku_codes=[]`）正常召回；回答不据其它商品动态行编造 |
| 6 | 三实体查询 | 拆 3 分支（验证不硬编码 2），各分支独立检索与动态补全 |
| 7 | 同一混合块被两分支召回 | 日志中该 `chunk_id` 只在证据文本出现一次（chunk_id 去重） |
| 8 | 两分支各自补全的动态区 | 合并后全局一份，同一 sku 只一行 |
| 9 | 检索全空 | "No data to summarize." 兜底，无异常中断 |
| 10 | 流式输出 | 逐 token 正常输出（planner 分片仍被闸门拦截）；无"整段蹦出" |
| 11 | 重复提问 | 缓存命中，0 次 LLM 调用 |

> **场景 3 是本轮验收的核心**：实体约束取消后，"取值不错配"由 prompt 规则唯一承担（§4.6.4）。该场景必须用**真实动态区含多商品行**的证据跑，才能验到实处；若失败则需回到 §4.6.4 的兜底规则或重新评估实体约束。

### 7.3 回归与指标

- **全量 pytest**：基线 75/76（`#8` 既有失败不算回归）
- **`test_customer_tools_node.py`**：需全绿——该测试断言 `out["searches"][0].records["result"]`，本稿不改 `searches`/`records` 结构、不改 `customer_tools` 节点，理论零影响；若失败说明改动越界
- **LLM 调用次数**：按 §3 口径逐场景统计，实施前后应**完全一致**（本稿不增不减）
- **summarize 输入 token**：实施前后对比（预期下降——去重 + 去掉 `hybrid_docs`/`dynamic_rows` 原始 dict 冗余），并确认回答质量不回归
- **`evaluation/runner.py`**：跑一轮确认 `searches` 逐分支追溯仍可用
- **`evaluation/__main__.py:112` 的调用点**：签名改造后必须先跑一次评测入口（哪怕只跑 1 题）确认不抛 `TypeError`——该调用点不传 `planner_llm`，靠 §4.4 的默认值容错；它同时也是本稿唯一一处"单测覆盖不到、只能靠实跑发现"的破坏点

---

## 8. 决策记录

| # | 决策 | 日期 | 理由 |
|---|---|---|---|
| D1 | 检索侧采用零 LLM 闭环，不实施 HyDE 与 LLM 相关性评分 | 2026-09-18 | ① 评分器已被 CrossEncoder 精排替代（`reranker_service.py:4`），重新引入是回退；② HyDE 会为每分支 +1 次 LLM，与阶段 0「customer_tools 零 LLM 收敛」方向相反；③ 拆解后子 query 已含实体全名，HyDE 的边际收益需实测才能证明 |
| D2 | 跨分支去重在 summarize 节点内完成，不新增 merge 节点 | 2026-09-18 | 图结构刚经阶段 3 简化，不再增节点；`render_doc_blocks` 本就是两通道共用渲染器，复用零漂移 |
| D3 | `state["searches"]` 结构不动 | 2026-09-18 | 有 3 个消费方（summarize / final_answer history / evaluation runner），改结构会波及逐分支追溯与评测 |
| D4 | 取消 `original_question` 字段 | 2026-09-18 | 原动机（预处理后 question 被稀释）随预处理管道 2026-08-21 删除消失；`state.question` 现即消解后完整句 |
| D5 | P4（单任务空转 1 次 LLM）保持独立调用，不改调用结构 | 2026-09-18 | Router 服务 6 种 type，扩 schema 输出实体/子 query 有伤路由准确率风险；规则直通需先实测误拆率。本稿以 §7.1 日志采集拆解分布，为后续决策积累依据 |
| D6 | ~~`entity_name` 用 `default=""` 而非 schema 级必填~~ **【已作废 2026-09-19】** | 2026-09-18 | 原为新字段必填性决策；字段本身已随实体约束取消（D10）。`EntitySubQuery.name` 保留但降级为纯观测字段（§4.1） |
| D7 | ~~实体约束置于 RRF 后、精排前~~ **【已作废 2026-09-19】** | 2026-09-18 | 该过滤层整体不实现（D10） |
| D8 | ~~实体约束不做硬过滤，`sku_codes=[]` 的政策块始终保留~~ **【已作废 2026-09-19】** | 2026-09-18 | 同上；政策块的保留语义仍由 prompt 规则承接（`summarize/prompts.py:41`） |
| D9 | `RERANK_MIN_SCORE` 阈值本稿不实现，先采分布 | 2026-09-18 | `top_k=5` 下拍脑袋设阈值易砍空分支；bge-reranker 输出 logits 非归一值，阈值必须实测 |
| **D10** | **取消实体约束检索（原 §4.6 整层不实现）** | **2026-09-19** | 两轮实测（§4.6.1 实验 A~E）结论：① 约束对列表/型号类问句**反向误杀**（丢 1~3 个 SKU，覆盖 9→6）——它与"跨实体/跨型号遍历"的拆分动机方向相悖；② 约束对单商品事实问确有收益（错召回块 25→3、动态区行数减半），但 LLM 端到端作答在**含噪音证据下零错配**（动态区每行带完整商品名，按名取值），收益仅剩 token 精简；③ 要让约束可靠需同时补四个补丁（三级匹配 + 精确/模糊分流 + scope 标记 + 品类词降级），状态量大于收益；④ 其原始目标（问题 #2）本就不由它解决——目标块 top-5 命中 0 条的用例中约束全程空转，真因是召回质量。**决定性判据：不为一个正确性收益为零、又带来误杀风险的优化层增加系统复杂度。** 相关性职责归还精排 + LLM |

---

## 9. 风险与避坑清单

1. **planner 节点名是隐式契约**：`multi_tool.py:62` 的 `add_node(planner)` 取函数 `__name__`，与 `stream_filter.py:20` 的 `INTERNAL_NODES` 字符串耦合。重命名节点函数（如改成 `plan_tasks`）会让 planner 分片**静默外泄给用户**。§4.8 的守护测试是唯一防线，**必须随阶段 1 一起落地**。
2. **拆解质量没有数量锚可校验**：v2 起废除 `len(tasks)==entity_count` 硬校验（列表型拆解下任务数可 > entity_count），保障只剩"提示词示例 + 上限约束 + 三态日志观测"。回退率与空名率指标必须真的看。
3. **无约束后的动态区噪音（本稿的有意取舍）**：不做实体约束，动态区必然含与提问无关的商品行（实测 8 行中 3~4 行无关），prompt 变长、token 成本上升。**实测错配率为零**（LLM 按【商品名 + 商品编码】取值），但这条结论建立在"动态区每行都渲染完整商品名"的前提上——**若将来改动 `render_dynamic_rows` 的格式（如去掉商品名只留编码），风险立即回归**。§7.2 场景 3 是该前提的守护者。
4. **不解决混合块稀释**（已取消过滤层，但结论不变）：66% 的块跨商品（`docs/项目问题.md` #3），问 A 时混在块里的 B 的内容会一并进 prompt。真正的解法是章节感知切分，属另一专项，**不要在本稿里试图用过滤绕开**——2026-09-19 实测已证明那类过滤会误杀正确块（§4.6/§8 D10）。
5. **summarize 输入形态变化需回归**：去掉 `hybrid_docs`/`dynamic_rows` 原始 dict 后，prompt 变短但 LLM 少看到一份（重复的）数据。虽然渲染文本已含全部事实，仍需按 §7.3 对比回答质量——**这是本稿唯一可能引起质量回归的改动点**。
6. **温度改造不得打 tags**：2026-09-18 的流式 bug 根因就是共享实例上的 `tags=["research_plan"]`。新造的 planner 模型实例**不加任何 tags**——闸门只认节点名。
7. **`parent_task` 语义变化**：从"LLM 逐任务生成、可被改写"变为"节点注入原 query"。`edges.py` 发送逻辑与 `Task` 模型不变，但要确认无其它消费方依赖其旧语义（现状仅有 `edges.py:18` 传入即弃）。
8. **删除前全局检索**：§4.8 三项死代码删除前 grep 确认零引用（CLAUDE.md 全项目扫描规则），避免复现 `error_tool_selection` 式未注册引用。
9. ~~`resolve_skus_by_product_name` 的 LIKE 通配符~~ **（已取消）**：该函数随实体约束一并取消（§4.6.3）。若后续专项重启实体识别，需重新评估 `LIKE '%...%'` 的通配符行为与 **`limit(5)` 截断**（实测后者会让"小米智能门锁"只覆盖 9 款中的 5 款，是导致误杀的直接原因）。
10. **每阶段独立提交**：阶段 1（planner）与阶段 2（检索侧）严格分开提交，禁止跨阶段合并。2026-09-19 回退实体约束后，**阶段 2 不再依赖阶段 1 的任何产出**（无 `entity_name` 传递），两阶段已完全解耦，可独立验证、独立发布。
11. **拆解上限存在三处引用，必须同源（§4.4.1）**：提示词的"总数不超过 N"、`node.py` 的 `MAX_TASKS` 校验、`.env` 的 `PLANNER_MAX_TASKS`。前两处一旦回退为字面量 3，`.env` 调大/调小就只影响其中一处——**提示词与校验打架时不会报错**：提示词仍教 LLM 拆 3 条、校验按 env 放行 5 条，或反之恒回退。实施后必须按 §7.1 配置项断言表逐档验一遍（改 env 值 → 重启 → 看 `fallback(reason=tasks=N)` 是否跟随）。
12. **`.env` 不进 git（已验证 `.gitignore:24`）**：本稿新增的两个配置项在版本库里只体现为 `config.py` 的默认值声明，`.env` 那两行**无法通过提交同步给其它环境**。部署到新机器时若未补写，行为等同默认值（0.0 / 3），不会报错——需在 README 或部署说明中同步告知，避免"我本地明明是 5"的排查弯路。
