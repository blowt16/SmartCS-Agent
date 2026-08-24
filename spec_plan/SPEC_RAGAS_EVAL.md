# RAGAS 评测模块实施规格（方案A · MVP）

> **用途**: 引入业界标准 RAG 评测工具 ragas，为 graphrag-query 检索模块产出四指标（faithfulness / answer relevancy / context precision / context recall），解决"检索链路多轮调优（BM25 AND→OR、全文递归分块、RRF 融合、Reranker 精排）无客观可复现评测手段"的问题，指标用于后续调优前后对比决策
> **技术栈**: ragas 0.4.3（dev 依赖，2026-08-23 已装并核实 API）+ langchain-openai 0.3.35（DashScope OpenAI 兼容接口，judge LLM）+ LangGraph 0.3.25 子图 + PostgreSQL（document_chunks 生产分块）+ uv
> **状态**: **待实施**（2026-08-23 设计定稿，方案 A 经用户确认，MVP 先行）
> **关联文档**: [[SPEC_RAG_RETRIEVAL_CONVERGENCE.md]] [[SPEC_BM25_QUERY_SEMANTICS_FIX.md]] [[SPEC_CHUNK_MERGE_STRATEGY.md]]（近期检索调优，本模块为其提供客观评测口径）

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与可测性分析](#2-现状链路与可测性分析)
3. [方案选型决策记录](#3-方案选型决策记录)
4. [目标架构](#4-目标架构)
5. [配置扩充](#5-配置扩充)
6. [关键坑与对策](#6-关键坑与对策)
7. [边界情况处理表](#7-边界情况处理表)
8. [影响面分析](#8-影响面分析)
9. [实施步骤](#9-实施步骤)
10. [验证方案](#10-验证方案)
11. [决策记录](#11-决策记录)
12. [风险与避坑清单](#12-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 项目近期对检索链路连续调优（2026-07 至 08）：BM25 全文检索替代 rank_bm25、分块改全文统一递归切分（310→31 块）、BM25 查询语义 AND→OR（`OR` 召回）、RRF 融合 + bge-reranker-v2-m3 精排，每次改动靠 `llm_backend/app/test/` 临时脚本肉眼比对结果，无标准指标、不可复现
2. 旧的评测资产 `llm_backend/eval_rag.py`（自研 LLM 评判 + 关键词匹配，10 题 golden set）已在提交 `a3f1d7d`（"彻底移除 Neo4j 与 Cypher"）中整体删除；自研指标无业界对照，难以判断"调优生效/回退"
3. ragas 是 RAG 评测事实标准：四指标覆盖检索（context precision/recall）与生成（faithfulness/answer relevancy）两段，且允许自带 LLM 评判器（本项目不可用 OpenAI 默认评判，需接阿里云百炼）
4. 知识语料（`京东智能家具产品知识文档.docx`，31 块入库）真实且可全文自检，具备合成评测集（TestsetGenerator）和人工标注的替代路径；MVP 选择合成（用户确认）

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 四指标可复现输出 | 每轮评测产出 faithfulness / answer relevancy / context precision / context recall 的 mean±std 与逐题明细 |
| 评测链路隔离 | 直调 graphrag-query 子图（绕过路由/语义缓存/指代消解），指标干净可归因于检索+生成模块 |
| 结果可对比 | 报告按时间归档（`evaluation/results/ragas_*.json`），`--skip-synthesize` 复用缓存重跑，judge temperature=0 保证复现 |
| 生产零侵入 | ragas 仅 dev 依赖（生产 Docker `uv sync --no-dev` 不携带）；评测代码不挂 FastAPI、不改生产链路 |
| MVP 成本可控 | 20 题全流程 < 30 分钟、约 ¥2-4（qwen-plus judge + DeepSeek 生成） |

### 1.3 设计原则

1. **评测即生产链路**：评测调用的子图工厂 `create_multi_tool_workflow`、检索服务 `RAGRetrieverService`、agent LLM 构造与生产完全一致（同一个函数、同一份配置），唯一绕开的是会话层前置（路由/缓存/指代消解）
2. **语料复用生产分块**：TestsetGenerator 的输入文档从 DB `document_chunks` 读取（已清洗 + 500/50 分块），而非重新解析 docx——保证合成器产出的 reference_contexts 在生产检索中"找得到"，避免 context_recall 系统性失真
3. **最小 CLI、不进 API**：评测先验为耗时/烧 token 的批处理任务（每轮 20 题 × 400-700 次 LLM 调用），用 CLI 驱动，不需要前端/HTTP 交互
4. **失败不静默**：子图超时/异常、空检索上下文、judge 失败均显式标记（failures / empty_context），均值只统计完成题——不静默丢分导致指标虚高
5. **评测入口与生产同构（含指代消解）**：真实系统中进入 RAG 子图的 query 是"入口指代消解后的 query"（main.py:389-395）。评测 runner 必须复刻同一入口——`detect_pronoun → (有历史且 NEED_RESOLVE 时) resolve_pronouns → 子图`，报告记录原始 question 与实际进入子图的 resolved_question——确保评测流程与真实系统流程一致（详见 §4.2）

---

## 2. 现状链路与可测性分析

### 2.1 评测目标链路（graphrag-query 执行体）

```
planner（LLM 任务分解）
  → customer_tools / vector_search_query（每子任务一次 RAGRetrieverService.search）
  → summarize（基于检索上下文生成）
  → final_answer
```

关键代码位置（已核实）：

| 环节 | 位置 | 说明 |
|---|---|---|
| 子图工厂 | `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py:32` | `create_multi_tool_workflow(llm) -> CompiledStateGraph` |
| 子图调用先例 | `llm_backend/app/lg_agent/lg_builder.py:437-451` | `TimeoutGuard`(30s 降级) + `config={"configurable": {"__pregel_checkpointer": None}}`（禁 checkpointer，规避 `Send is not JSON serializable` 序列化问题，lg_builder.py:438-441 注释）。**注：评测脚本应直接把降级答案视为失败，而非计时兜底** |
| 子图输出结构 | `.../components/state.py:68-75` | `OutputState{answer, question, steps, searches, history}` |
| 检索上下文出处在 `searches` | `.../components/customer_tools/node.py:66-80` | `searches[*].records.hybrid_docs`（doc 字段：text/chunk_id/source/rrf_score/rerank_score，见 rag_retriever_service.py:54-69） |
| 检索服务 | `llm_backend/app/services/rag_retriever_service.py:103` | `async search(query, top_k) -> List[Dict]`，纯 async，仅依赖 DB + embedding API |

### 2.2 可测性现状

- **前置链路会污染评测**：主入口 `/api/langgraph/query`（main.py:321）有指代消解（main.py:389-395）、语义缓存短路（main.py:403-411，命中则不检索）——评测必须绕过
- **路由层是独立关注点**：general-query（闲聊）/ graphrag-query（RAG）由 `analyze_and_route_query`（lg_builder.py:55）分类，非本次评测目标（评测 RAG 模块本身，用户确认）
- **依赖就绪**：`RAGRetrieverService.search` 曾被 `tests/test_bm25_retriever.py` 直接调用，DB + embedding 可用即可跑，无需启动 HTTP 服务

### 2.3 指代消解与评测一致性（关键约束）

**生产入口行为**（main.py:380-398，已核实）：

```python
decision = detect_pronoun(query, skip_filler=settings.RESOLVE_SKIP_FILLER)   # main.py:389
if history_messages and decision == DetectionDecision.NEED_RESOLVE:           # main.py:390
    resolved_query = await resolve_pronouns(_get_resolve_llm(),
        history_messages + [{"role": "user", "content": query}], query)
    decision = detect_pronoun(resolved_query)
# 消解后或其他情况：resolved_query 原样进入主图 InputState(messages=resolved_query)
```

**一致性论证**：

1. TestsetGenerator 合成的评测题是**单轮独立完整问题**（无对话历史）→ `history_messages == []` → **生产也不会触发指代消解**，`resolved_query == query` 原样进子图（即使问题含"该产品/这款"等书面指代，detect 判定 NEED_RESOLVE——test_pronoun_resolve.py:74 已证——但无历史时不可消解、直接原样传递）
2. 因此 MVP 下"直调子图"与本系统真实 RAG 链路的输入**等价**
3. **但为保证严格一致性与可审计性**（且为将来多轮/指代评测集留好扩展位），评测 runner 仍复刻入口：每题 `detect_pronoun → 有历史且 NEED_RESOLVE 时 resolve_pronouns（同款 resolve LLM：`LLMFactory.create_chat_service()`，main.py:48-53）→ resolved_query 进子图`；报告逐题记录 `question`（原始）与 `resolved_question`（实际进子图）及 `was_resolved` 标记——二者不同时高亮
4. 被测 resolve LLM 构造复用 `app.services.llm_factory.LLMFactory`（与生产同一个类，保证消解行为一致）；**禁止 import `main.py`**（module-level 会创建 FastAPI app，见 §12 风险 11）

---

## 3. 方案选型决策记录

### 3.1 模块形态

| 候选 | 结论 | 理由 |
|---|---|---|
| **`llm_backend/evaluation/` 独立包 + CLI（方案 A）** | ✅ 采纳 | 直接 import 生产服务（检索/子图/配置），零重写；与 `llm_backend/scripts/` 独立运行先例一致；合成/评测/报告三个关注点分文件，后续扩展友好 |
| 单个 CLI 脚本（`evaluate_rag.py`） | 否决（够用但不优） | 300+ 行单文件，评测链路迭代难维护 |
| 接入 FastAPI（`/api/evaluate` + 前端） | 否决 | 批处理任务不适合 HTTP 交互；前端额外开发；违反 YAGNI |

### 3.2 评测数据来源

| 候选 | 结论 | 理由 |
|---|---|---|
| **TestsetGenerator 从知识文档合成** | ✅ 采纳（用户确认） | 项目无现成 golden set（旧 10 题随 a3f1d7d 删除）；合成自动化、动量足；参考上下文随题生成，context_precision/recall 有金标。代价：问题可能偏浅、reference 为 LLM 表述非逐字抽取——绝对分偏低属正常，MVP 关注相对变化与对比口径 |
| 手工黄金集（30-50 题） | 备选 | 质量最高但需人工，后续如果需要更高区分度再加 |

### 3.3 judge LLM

| 候选 | 结论 | 理由 |
|---|---|---|
| **阿里云百炼 DashScope（qwen-plus / qwen-max）** | ✅ 采纳（用户确认） | 项目已有 DashScope 使用先验（`QWEN_EMBEDDING_*`，config.py:88-91，compatible-mode 端点验证过）；langchain-openai 0.3.35 已是传递依赖，`ChatOpenAI(base_url=...)` 直连；独立 `RAGAS_*` 配置与生产隔离 |
| DeepSeek 同模型评判 | 备选 | 与生成同模型有偏袒风险 |
| Ollama 本地评判 | 备选 | 免费但 32B 模型慢，拖长整轮评测 |

### 3.4 评测入口

| 候选 | 结论 | 理由 |
|---|---|---|
| **直调 `create_multi_tool_workflow` 子图** | ✅ 采纳（用户确认） | 评测目标即"graphrag-query 的 RAG 模块"；绕过路由/缓存/指代消解，指标可归因于检索+生成本身 |
| 完整主图 | 备选 | 引入路由波动与缓存短路，指标噪声大；路由质量另属独立关注点 |

---

## 4. 目标架构

### 4.1 目录结构

```
llm_backend/evaluation/
├── __init__.py          # 包标记
├── llm_factory.py       # judge LLM / judge embeddings / 被测 agent LLM 构造（合成与评测共用）
├── testset_builder.py   # 从 document_chunks 读语料 → TestsetGenerator 合成 QA（jsonl 缓存可复用）
├── runner.py            # 逐题跑 multi_tool 子图取 answer+contexts；构建 EvaluationDataset；跑四指标
├── report.py            # 汇总四指标 mean/std + 明细 + 失败列表 → JSON 归档 + 控制台摘要
└── __main__.py          # CLI：python -m evaluation <args>
```

### 4.2 各文件要点

**`llm_factory.py`**

```python
def build_judge_llm() -> BaseChatModel
    # ChatOpenAI(api_key=RAGAS_JUDGE_API_KEY, base_url=RAGAS_JUDGE_BASE_URL,
    #            model=RAGAS_JUDGE_MODEL, temperature=RAGAS_JUDGE_TEMPERATURE,
    #            timeout=RAGAS_JUDGE_TIMEOUT, max_retries=2)
def build_judge_embeddings() -> Embeddings
    # OpenAIEmbeddings(...)，**完全独立配置 RAGAS_EMBEDDING_*（必填，不回退生产 QWEN 配置**
    # ——评测配置与生产隔离，避免生产 embedding 配置变更静默影响评测复现性）；缺失直接报错
def build_agent_llm() -> BaseChatModel
    # 与 lg_builder.py:416-419 同构：AGENT_SERVICE==DEEPSEEK →
    # ChatDeepSeek(api_key=settings.DEEPSEEK_API_KEY, model_name=settings.DEEPSEEK_MODEL,
    #              temperature=settings.LLM_TEMPERATURE,
    #              extra_body={"thinking": {"type": "disabled"}})
```

**`testset_builder.py`**

```python
async def load_corpus_documents(max_docs: int, user_id: str) -> list[Document]
    # SELECT content, chunk_id, source FROM document_chunks [WHERE user_id=...]
    # LIMIT max_docs → Document(page_content=content, metadata={"chunk_id","source","user_id"})
def synthesize_testset(documents, size, output_path)
    # TestsetGenerator.from_langchain(build_judge_llm(), build_judge_embeddings())
    #   .generate_with_langchain_docs(documents, testset_size=size,
    #     query_distribution=[SingleHopSpecificQuerySynthesizer(llm=judge_llm)])   # 单跳具体查询（中文知识为主）
    #   → to_jsonl 落盘
def load_cached_testset(path) -> Testset   # --skip-synthesize 复用
```

**`runner.py`**（含指代消解入口复刻 + 评测批处理配置）

```python
@dataclass
class SubgraphResult:
    question: str                        # 原始测评问题
    resolved_question: str               # 实际进入子图的 query（指代消解后）
    was_resolved: bool                   # 是否发生过指代消解
    answer, contexts: list[str], elapsed, ok, error, timed_out, empty_context

async def apply_entry_resolution(question, history=None) -> tuple[str, bool]
    # 复刻生产入口（main.py:389-395，与系统真实流程一致）：
    #   decision = detect_pronoun(question, skip_filler=settings.RESOLVE_SKIP_FILLER)
    #   if history and decision == DetectionDecision.NEED_RESOLVE:
    #       resolved = await resolve_pronouns(LLMFactory.create_chat_service(),
    #                     history_messages + [{"role":"user","content":question}], question)
    #   return resolved, (decision == NEED_RESOLVE and history)
    # MVP 无历史（history=None）→ 恒原样通过，与生产单轮行为一致（§2.3）
async def run_one(question, workflow, history=None) -> SubgraphResult
    # apply_entry_resolution → resolved_question 进子图：
    # workflow.ainvoke({"question": resolved_question, "data": [], "history": []},
    #   config={"configurable": {"__pregel_checkpointer": None}})
    # 外包 TimeoutGuard(settings.RAG_TIMEOUT_SECONDS)；降级文案/异常 → ok=False
    # contexts = [d["text"] for s in searches for d in s.records.hybrid_docs]
async def run_all(questions, workflow, concurrency=4, history=None) -> list[SubgraphResult]
def build_eval_dataset(results, references) -> EvaluationDataset
    # EvaluationDataset / SingleTurnSample 位置（ragas 0.4.3 实测）：ragas.dataset_schema
    # SingleTurnSample(user_input=question, response=answer,
    #   retrieved_contexts=contexts, reference=..., reference_contexts=...)
    #   user_input 用原始 question（用户实际问的）；contexts/answer 来自 resolved_question 的链路
    # 失败/超时/空上下文题不进入 dataset（单列 failures）
async def run_metrics(dataset, metrics) -> Result
    # 优先 aevaluate（异步原生，本环境 asyncio.run 主导；sync evaluate 会 patch 事件循环且
    #   emit deprecation warning）——实现时按安装版本核实存在性，否则 fallback asyncio.to_thread(evaluate)
    # aevaluate(dataset, metrics, llm=LangchainLLMWrapper(judge_llm),
    #   embeddings=LangchainEmbeddingsWrapper(judge_embeddings),
    #   run_config=RunConfig(timeout=judge_timeout), batch_size=RAGAS_BATCH_SIZE,
    #   raise_exceptions=False)   # 失败行 NaN，由报告单列，不中断
```

**`report.py`**：`summarize(raw, results, meta) -> dict`（`{"meta", "metrics": {四指标 mean/std}, "samples": [{question, resolved_question, was_resolved, scores, answer 截断}], "failures"}`）、`dump_report(path)`（UTF-8）、`print_summary()`（中文控制台摘要；四指标 mean±std + 失败/超时明细 + 发生过指代消解的题高亮）。

**`__main__.py`**：`sys.path.insert` + `asyncio.run(main(parse_args()))`（沿用 `scripts/ingest_knowledge.py:10-11` 模式）。

### 4.3 CLI

```bash
cd llm_backend
python -m evaluation --testset-size 20 --user 1                       # 全流程（MVP 默认）
python -m evaluation --only-synthesize --testset-size 10              # 仅合成并缓存
python -m evaluation --skip-synthesize --testset-file <缓存>.jsonl    # 复用缓存重跑（省钱、可复现）
python -m evaluation --metrics faithfulness,context_precision --max-docs 50
```

参数：`--testset-size`（默认 `RAGAS_DEFAULT_TESTSET_SIZE=20`）、`--user`（默认 "1"）、`--testset-file`、`--max-docs`（默认 `RAGAS_MAX_CORPUS_DOCS=100`）、`--metrics`、`--concurrency`、`--results-dir`、`--only-synthesize`、`--skip-synthesize`。

### 4.4 数据流

```
document_chunks（生产分块：清洗 + 500/50，31 块）
  ├─ load_corpus_documents（async，DB 读）─→ ragas Document 列表
  ├─ TestsetGenerator.from_langchain(judge_llm, judge_embeddings)
  │    .generate_with_langchain_docs(docs, testset_size=20, query_distribution=[单跳])
  ├─ Testset（user_input + reference + reference_contexts）→ to_jsonl 缓存
  │    └─ to_evaluation_dataset()（预填 3 字段）
  ├─ runner.run_all：每题 →
  │     ① apply_entry_resolution(question)     # 生产同构：detect_pronoun → (有历史且NEED_RESOLVE时) resolve
  │     ② multi_tool_workflow.ainvoke(resolved_question, ...)（并发 4，TimeoutGuard 30s）
  │        answer → response；searches[*].hybrid_docs[*].text → retrieved_contexts
  │        （question / resolved_question / was_resolved 全部入报告）
  └─ 合并 → EvaluationDataset（5 字段齐：user_input=原 question，contexts/answer 来自消解后链路）
        → aevaluate(四指标, judge LLM, judge embeddings, batch_size, raise_exceptions=False)
        → Report：JSON 归档 + 控制台摘要
```

---

## 5. 配置扩充

### 5.1 `llm_backend/app/core/config.py`（追加于 QWEN_EMBEDDING_* 之后，config.py:91 后）

```python
# ============ RAGAS 评测配置（评测专用，与生产链路隔离） ============
# 隔离原则：所有评测变量以 RAGAS_ 为唯一命名空间；judge/embedding 的 key、
# 模型、端点全部独立填写，不与任何生产配置（QWEN_EMBEDDING_*/DEEPSEEK_*）混用或回落
RAGAS_JUDGE_API_KEY: str = ""          # 阿里云百炼 DashScope API Key（必填，评测专用 key）
RAGAS_JUDGE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
RAGAS_JUDGE_MODEL: str = "qwen-plus"   # 评判模型（qwen-plus 性价比 / qwen-max 更严）
RAGAS_JUDGE_TEMPERATURE: float = 0.0   # 评判低温保证可复现
RAGAS_JUDGE_TIMEOUT: int = 60
RAGAS_EMBEDDING_API_KEY: str = ""      # 必填（评测专属，不回退生产配置）——评测 embedding 独立于检索 embedding
RAGAS_EMBEDDING_BASE_URL: str = ""     # 必填，默认同 DashScope compatible-mode
RAGAS_EMBEDDING_MODEL: str = ""        # 必填，默认 text-embedding-v4
RAGAS_DEFAULT_TESTSET_SIZE: int = 20   # 合成题数（MVP）
RAGAS_MAX_CORPUS_DOCS: int = 100       # 喂合成器的分块上限
RAGAS_BATCH_SIZE: int = 10             # 评测批处理并行度（ragas 内部 LLM 调用批量，主流实践 10-25）
RAGAS_RESULTS_DIR: str = "evaluation/results"
```

### 5.2 `.env`（需用户填入 key，不入 git；评测配置/密钥与生产分开填写）

```
# ===== RAGAS 评测专用（与生产 key 区分：建议独立百炼子账号/API key，评测消耗单独计费）=====
RAGAS_JUDGE_API_KEY=sk-ragas-xxx
RAGAS_EMBEDDING_API_KEY=sk-ragas-xxx
# RAGAS_JUDGE_MODEL=qwen-plus          # 默认已定，可按需覆盖
```

> **配置隔离边界**（评测模块的配置依赖表）：
> - **评测专用（RAGAS_*，任何生产配置变更不影响评测复现性）**：judge LLM、评测 embedding、合成题数、批大小、报告目录
> - **评测仍依赖的生产基础设施（只读，属"被测对象"而非"评测配置"）**：PostgreSQL DSN（读语料 + 检索）、被测 agent LLM（DeepSeek/Ollama，测的就是生产链路本身）、`RERANKER_ENABLED` 等检索行为开关——这些变更**应当**影响评测结果（这正是指标存在的意义）
> - **禁止**：评测进程读写生产配置（不改 .env、不写缓存到生产键空间、不新增环境变量到生产路径（评测仅经 `config.py` 只读访问））

### 5.3 `pyproject.toml` dev 依赖

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "ragas>=0.3.0"]
```

生产镜像（`uv sync --frozen --no-dev`，Dockerfile）不携带 ragas，评测与生产隔离。

---

## 6. 关键坑与对策

| # | 坑 | 对策 |
|---|---|---|
| 1 | **ragas 版本冲突（最高风险）**：0.1.x 锁定 langchain<0.3 硬冲突；较高版本可能拉入 langchain 元包顶掉 `langchain-core 0.3.86` / `langchain-openai 0.3.35` | `uv add --dev ragas` 前 `--dry-run` 预览；装后 `uv tree` 核对核心包未被顶到 0.4+/1.x，被顶即换版本（回退 0.2.x 需同时验证）；`python -c "import ragas"` 冒烟 |
| 2 | **Windows 事件循环**：psycopg async 拒用 Proactor（database.py:4-7 已全局补丁）；ragas 合成/评测为同步执行而子图为 async | 主流程单一 `asyncio.run`；DB 读取 await；合成与 `evaluate()` 经 `asyncio.to_thread`；子图直接 await——单一事件循环无嵌套 |
| 3 | **合成 API 参数名随版本漂移**（`testset_size` vs `test_size`） | 实施时 `inspect.signature` 校验实际参数名 |
| 4 | **指标类名版本漂移**：0.3.x 新旧并存（`ResponseRelevancy`/`AnswerRelevancy`、`LLMContextPrecisionWithReference`/`ContextPrecision`、`LLMContextRecall`/`ContextRecall`） | 按安装版本核实；import 用 try/except 兼容 |
| 5 | **子图 30s 超时降级是生产兜底**（lg_builder.py:442-450），评测中降级文案即失败 | 评测侧自行 TimeoutGuard 且判定：answer 含降级文案或超时 → `timed_out=True`，不参与均值 |
| 6 | **成本**：20 题全流程约 400-700 次 LLM 调用（合成 100-150 + 子图 40-80 + 四指标 240-440），qwen-plus + DeepSeek 约 ¥2-4 | MVP 20 题；jsonl 缓存合成集，`--skip-synthesize` 重跑不再合成；评测 LLM 调用经 batch_size=10 批量并行（主流实践省 40-50% 时间） |
| 7 | **合成集与检索语料同源**：question 由目标 chunk 生成，context_recall 存在天然偏移 | 如实记录口径：指标用于**相对比较**（调优前后、配置切换），不与其他项目/文献绝对分对标 |
| 8 | **evaluate 事件循环/sync 兼容**：sync evaluate 会 patch 事件循环（Jupyter 设计），且当前版本 emit deprecation warning（倾向 `@experiment`/aevaluate） | 本环境 async 主导 → 优先 **aevaluate**（异步原生、不阻塞循环）；实现时核实安装版本，无 aevaluate 则 fallback `asyncio.to_thread(evaluate)` |
| 9 | **不得 import `main.py`**：module-level 创建 FastAPI app（main.py:75）、lifespan 副作用 | 评测只 import `app.services.pronoun_detector/pronoun_resolver/llm_factory` 等纯服务模块——均已验证无 FastAPI 依赖 |
| 10 | **指代消解一致性**：用户明确要求"确保评测流程与真实系统流程一致" | 评测 runner 复刻入口（§2.3/§4.2 `apply_entry_resolution`）；报告逐题记录原 query/消解后 query/是否消解——消解发生时高亮，避免"实际评测的不是 user 问的问题"而不自知 |
| 11 | **batch_size 与 API 限流**：ragas 评测内部批量 10 并发 judge 调用，加上子图侧 `--concurrency 4` 的 DeepSeek/embedding 调用 | 实测遇 429 先降 `RAGAS_BATCH_SIZE`；百炼与 DeepSeek 为独立服务的各自限流，互不影响 |

---

## 7. 边界情况处理表

| 情形 | 行为 |
|---|---|
| 子图超时（>30s） | `timed_out=True`，记 failures，不参与均值 |
| 子图抛异常（embedding API 抖动、DB 波动） | 捕获记 `error`，跳过该题不中断批处理 |
| 检索返回空（retrieved_contexts=[]） | `empty_context=True`，faithfulness/context_precision 必然 NaN——不静默，failures 单列 |
| judge 单请求失败 | `ChatOpenAI(max_retries=2, timeout=60)` 兜底；evaluate 级失败则该题 NaN，报告标注 |
| `--metrics` 子集 | 未选指标不实例化（省 LLM 调用） |
| 合成时无语料 chunks | 明确报错退出（SIG 提示先 `python -m scripts.ingest_knowledge`） |
| `--skip-synthesize` 缓存文件缺失 | 报错提示先跑 `--only-synthesize` |
| 无 GPU 环境 | 可设 `RERANKER_ENABLED=false` 跳过 CrossEncoder（检索服务已支持降级），减少子图耗时 |
| 合成题含书面指代（如"该产品支持快充吗"）但无历史 | 生产入口同样不消解（main.py:390 条件 `history_messages and ...`）——评测原样通过，与真实系统行为**一致** ✓ |
| 未来评测集含多轮历史/指代词 | `--history` 传会话历史 → `resolve_pronouns(LLMFactory.create_chat_service(), history, query)` 与生产同款消解；报告 `was_resolved=True` 高亮。MVP 不提供历史参数（YAGNI，流程已预留） |
| 消解后 query 与原始 query 不同 | 检索/生成用消解后 query，`user_input` 用原始 question（用户实际问题）；报告逐题记录两值，置信度对比时可按 `was_resolved` 分组 |

---

## 8. 影响面分析

| 组件 | 状态 | 依据 |
|---|---|---|
| `config.py` | **新增字段** | RAGAS_* 仅追加，现有字段零改动 |
| `pyproject.toml` | **dev 依赖扩充** | 生产依赖不变 |
| `.env` | 新增 1 行 | 用户填 key；确认 .gitignore 已覆盖 .env |
| `evaluation/` 包（6 文件） | **全新增** | 与生产代码同包但不被生产 import |
| 生产链路（检索/子图/API） | **零改动** | 评测只 import，不修改任何生产文件 |
| 前端 | 无引用 | 纯后端 CLI |
| 测试 | 可选新增 | 评测模块以"能跑通 + 报告结构正确"验收（§9），不另行 pytest（评测依赖真实 LLM/DB，不适合 CI 单元化） |

---

## 9. 实施步骤

1. **依赖与版本验证**：`uv add --dev ragas`（先 0.3.x）→ `uv tree` 核对 langchain-core/langchain-openai → `python -c "import ragas; print(ragas.__version__)"` 冒烟 → `inspect.signature(TestsetGenerator.generate_with_langchain_docs)` 核实参数名
2. **配置扩充**：config.py 追加 RAGAS_* 字段；确认 .env 有 `RAGAS_JUDGE_API_KEY`（用户提供）→ 验证：`uv run python -c "from app.core.config import settings; print(settings.RAGAS_JUDGE_MODEL)"`
3. **`llm_factory.py` + `testset_builder.py`** → 先跑通 `python -m evaluation --only-synthesize --testset-size 10` → 验证：jsonl 文件含 10 题（question/reference/reference_contexts 齐全）
4. **`runner.py`**：先用 1 题验证输出提取（answer 非空、contexts=searches 展开的 hybrid_docs 文本）→ 再 `--concurrency 4` 跑 20 题 → 验证：SubgraphResult 全量提取；**另用含书面指代的题（如"该产品支持快充吗"）验证 `apply_entry_resolution` 无历史时原样通过（与生产一致）**，并核对报告 `was_resolved` 字段
5. **`report.py` + `__main__.py`** 组装 → 全流程 `python -m evaluation --testset-size 20 --user 1` → 对照 §10 验证方案逐条核验
6. **git 提交推送**：`spec_plan/SPEC_RAGAS_EVAL.md` + `evaluation/` + config.py + pyproject.toml + .env.example（若存在）.env 不入库；推送用 `-c http.sslBackend=schannel`（本机 openssl 证书包失效，见操作记忆）

---

## 10. 验证方案

1. **合成**：`--only-synthesize --testset-size 10` 落盘 jsonl，检查每行含 `user_input`/`reference`/`reference_contexts`
2. **子图提取**：1 题直调，answer 非空，contexts 与检索结果一致（非空、顺序为 searches 顺序）
3. **全流程**：20 题全流程报告四指标 mean±std 非全 NaN；完成题 ≥ 16（失败率 ≤ 20%）；failures 单列详情
4. **复现**：`--skip-synthesize --testset-file <缓存>` 重跑，指标一致（judge temperature=0）
5. **侵入检查**：`git status` 生产代码零改动（仅 evaluation/ + config.py 新增 + pyproject + spec 文档）；`uv run pytest llm_backend/tests -q` 全绿

---

## 11. 决策记录

| 决策点 | 决议（2026-08-23） |
|---|---|
| 模块形态 | **方案 A：`llm_backend/evaluation/` 独立包 + CLI**（用户确认） |
| 评测指标 | ragas 四指标全量（faithfulness / answer relevancy / context precision / context recall），后续调优据此对比 |
| 数据来源 | **TestsetGenerator 从知识文档合成**（用户确认）；注：合成 reference 为表述性金标，绝对分偏低属正常，指标用于相对对比 |
| judge LLM | **阿里云百炼 DashScope**（OpenAI 兼容接口，qwen-plus 默认），独立 `RAGAS_JUDGE_*` 配置入 .env（用户确认） |
| judge embeddings | **完全独立配置 `RAGAS_EMBEDDING_*`（必填，不回退生产 QWEN 配置）**——评测配置与生产隔离（用户明确要求"env 单独配置，不与项目配置混淆"）：评测 embedding 与检索 embedding 本就是两个用途（相似度评判 vs 向量检索），独立后生产 embedding 变更不影响评测复现性 |
| 配置隔离原则 | **RAGAS_* 独占命名空间**；评测 key 建议独立百炼子账号（评测消耗单独计费/限流）；评测只读生产基础设施（DB、被测 agent），不读写任何生产配置 |
| 评测入口 | **直调 `create_multi_tool_workflow` 子图**（用户确认），绕过路由/缓存/指代消解 |
| 语料来源 | DB `document_chunks` 生产分块，不重新解析 docx（一致性优先） |
| 合成集缓存 | to_jsonl 落盘 + `--skip-synthesize` 复跑（省钱、可复现） |
| 报告形态 | JSON 归档（meta/metrics/samples/failures）+ 控制台摘要；MVP 不做 HTML/趋势曲线 |
| ragas 版本 | **0.4.3（2026-08-23 实测安装）**：langchain-core 0.3.86 / langchain-openai 0.3.35 未被顶掉；指标新旧类名并存；`EvaluationDataset` 位于 `ragas.dataset_schema`；`aevaluate` 含 `run_config/batch_size/raise_exceptions` 参数 |
| **评测入口与指代消解** | **复刻生产入口（detect_pronoun → resolve_pronouns → 子图），报告记录原/消解后 query**——用户明确要求评测流程与真实系统一致；MVP 合成题无历史使消解天然为 no-op，但流程不绕过（可审计+未来多轮扩展） |
| **调用 API 形态** | **aevaluate 优先**（async 原生、省事件循环 patch），sync evaluate 为 fallback |
| **评测批处理** | `batch_size=10` + RunConfig(timeout)（主流实践：批量并行省 40-50% 时间） |
| **5 指标 stack（主流）** | 对比结论：业界常加 `answer_correctness` 作 headline（四指标之外的第 5 项）。**MVP 保持四指标**（用户确认，成本与复杂度），记录为后续可选扩充；`context_entity_recall`（客服/电商领域友好）同样留作后续 |
| **阈值门槛 / CI gating** | 对比结论：主流在 CI 中对每指标设 floor（如 faithfulness ≥ 0.8）。MVP 不做（指标需先跑出基线再定阈值），报告 meta 预留 `thresholds` 字段位 |
| **judge 校准** | 对比结论：建议 50-100 题人工评分 + Cohen's kappa 校准 judge。记录为后续项（MVP 先用 qwen-plus + temperature=0） |

---

## 12. 风险与避坑清单

1. **版本冲突**：`uv add --dev ragas` 可能顶掉 langchain-core 0.3.86（raises `version resolution` error 或运行时行为异常）——装后必须 `uv tree` 核对并跑 `uv run pytest llm_backend/tests -q` 全量回归；被顶即换 ragas 版本或手工 pin langchain-core 共存版本
2. **Windows 事件循环**：评测脚本 import 链需带上 `app.core.database`（WindowsSelectorEventLoopPolicy 补丁生效）；ragas 同步调用一律 `to_thread`，禁止在 async 内裸调同步 evaluate 阻塞循环
3. **ragas 同步 LLM 调用**：`ChatOpenAI`（langchain）为同步 invoke，ragas evaluate 内部同步——`to_thread` 后线程内完成，注意 judge 超时（60s）与隔离
4. **checkpointer 禁止启用**：子图 ainvoke 配置必须 `{"configurable": {"__pregel_checkpointer": None}}`，否则 map-reduce Send 序列化抛错（lg_builder.py:438-441 已实测）
5. **检索上下文展开顺序**：`searches` 按子任务顺序，`hybrid_docs` 为空（检索失败）须合并时保序并打空标记——reorder 会破坏 context_precision 的排序语义
6. **reference_contexts 与检索上下文文本不完全一致**：合成器 grounding 分块为切片语义，评测指标按"语义包含"评判，不要求逐字一致（ragas 内建处理）
7. **.env 密钥安全**：`RAGAS_JUDGE_API_KEY`/`RAGAS_EMBEDDING_API_KEY` 不入 git；提交前确认 `git status` 无 .env 泄漏；评测 key 与生产 key 物理分离（独立百炼子账号），禁用后不影响生产能力
7.5 **配置隔离约束**：`build_judge_embeddings()` 若实现为"回退 QWEN"将被视为违反隔离原则——必须全部走 `RAGAS_*`；`config.py` 中 RAGAS_* 字段默认值均为空串/非生产值，缺失时评测入口显式报错（列明缺哪个变量），绝不静默回落生产配置
8. **judge 与生成并发**：`--concurrency 4` 控制子图并发（embedding/DeepSeek API 限流），升高需验证无 429
9. **语料规模**：`RAGAS_MAX_CORPUS_DOCS=100` 上限控制合成器 docstore 建立成本；当前库 31 块，默认值可覆盖
10. **合成集质量**：MVP 合成 20 题若出现大量"无意义浅题"或 reference 与问题不符（人工抽检 jsonl），后续按决策记录 3.2 换人工黄金集——不阻塞 MVP
11. **禁止 import `main.py`**：`main.py` module-level 创建 FastAPI app（main.py:75）并注册 lifespan——评测 import 它=拉起整个 HTTP 服务。评测所有生产复用点（detect_pronoun/resolve_pronouns/LLMFactory/create_multi_tool_workflow/RAGRetrieverService）均从 `app.services.*`/`app.lg_agent.*` 导入，实现后加一句注释固化约束
12. **指代消解一致性回归**：`apply_entry_resolution` 逻辑必须与 main.py:389-395 严格同步——若生产入口变更（如消解条件放宽、消解模型更换），评测模块须同步更新；spec 此处与 main.py 互为参照，实施时在注释中双向交叉引用
13. **aevaluate 版本核实**：安装后 `inspect.signature(ragas.evaluate)` 与 `hasattr(ragas, "aevaluate")` 核对——若当前版本 aevaluate 参数与 evaluate 有差异（如 run_config 合并位置），以实际版本为准，spec 的签名按实现修正并回填本文档
