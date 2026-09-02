# RAG 检索收敛实施规格：pgvector HNSW 单路径 + pg_jieba 数据库内 BM25 + Reranker 精排 + Tool 化封装
> **归档状态**: ✅ 已完成（2026-09-02 审计，依据 main 代码与 git 历史）
> 检索收敛落地：cf9e37b（pgvector HNSW + pg_jieba 库内 BM25 + Reranker 精排 + Tool 化），§5.6 已追加 tools 迁入注记（2026-08-31）。

> **用途**: 消除混合检索链路的"全量拉库 + 内存重建"性能瓶颈，并补齐精排与工具化——①向量检索收敛为 pgvector HNSW 单路径（移除内存暴力检索）；②BM25 从应用内存（jieba + rank_bm25）迁移至数据库内（pg_jieba + ts_rank_cd + GIN 倒排索引）；③检索后接入 bge-reranker-v2-m3 精排（top-20 → top-5，替代 LLM 相关性评分，支持 GPU 加速）；④全部检索/精排参数统一由 .env 全局配置；⑤RAG 检索封装为 langchain @tool 供 agent 使用  
> **技术栈**: pgvector/pgvector:pg16（自定义镜像）+ pg_jieba（cppjieba）+ PostgreSQL 16.14 + LangGraph 0.3.x + qwen text-embedding-v4（DashScope API）+ sentence-transformers 5.7.0 CrossEncoder（BAAI/bge-reranker-v2-m3）+ torch CUDA（RTX 3060）  
> **状态**: **已实施**（2026-08-20 全链路验证通过）—— 决策已与需求方逐项确认（含第二轮需求：env 全局配置 / reranker 精排替代 LLM 评分 / tool 封装）  
> **关联文档**: [[SPEC_ENTITY_PARALLEL_RAG.md]] [[SPEC_ENTITY_RECOGNITION_AND_RAG_RETRIEVAL.md]] [[PLAN_GraphRAG_TO_StandardRAG.md]] [[SPEC_CONTEXT_ENGINEERING.md]]

---

## 实施记录（2026-08-20 落地补充）

1. **镜像构建 vendor 化**：容器内访问 GitHub/gitee/git clone 与 curl 下载均不稳定（CA 缺失、403、exit 22/128），最终改为宿主机预下载（gitee zip + PyPI sdist）放入 `docker/postgres/vendor/`（.gitignore 排除，README 记录获取命令）；apt 源清华镜像对部分 deb 403，改阿里云
2. **pg_jieba 编译链**：CMake 需 libpq-dev（find_package(PostgreSQL) 缺 server 头）；CMakeLists include 相对路径在 CMake 3.25 报错需 sed 改绝对；词典目录（libjieba/dict/）从 PyPI sdist 补齐后 `cmake --install` 自动部署到 tsearch_data（jieba_base.utf8 / jieba_hmm.utf8）
3. **torch CUDA**：Windows 默认锁 CPU wheel，`[tool.uv.sources] torch = { index = "pytorch-cu126" }` + `uv lock --upgrade-package torch` 切到 2.13.0+cu126（RTX 3060 fp16 验证通过）；注意环境变量 `UV_DEFAULT_INDEX` 指向清华镜像对个别包 403，需 `--default-index https://pypi.org/simple` 覆盖
4. **reranker 模型本地化**：本机存在 SteamTools 中间人证书（huggingface.co 流量被本地代理 MITM），certifi 校验必然失败、系统存储"信任"的是 MITM 根——模型改为本地目录加载（`llm_backend/models/bge-reranker-v2-m3/`，.gitignore 排除，约 2.3GB 用 urllib 系统存储断点续传下载），`RERANKER_MODEL` 指向本地路径，彻底绕开 HF 网络
5. **BM25 查询语义**：`plainto_tsquery` 为 AND 严格语义（"SF-2000 多少钱" 分词后 AND 匹配 0 条属预期），向量路补充召回；如需宽松召回可改 `websearch_to_tsquery`（未实施）——⚠️ 2026-08-23 已实施 OR 语义修复（jiebacfg ∪ jiebamp 双分词并集 + ts_rank_cd，详见 [[SPEC_BM25_QUERY_SEMANTICS_FIX.md]]），本节"AND 严格语义属预期"不再适用
6. **精排模型加载失败自动降级**：验证过程中曾因模型缺失连续触发降级（融合结果直接输出），确认降级链路健壮

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状链路与问题](#2-现状链路与问题)
3. [方案选型决策记录](#3-方案选型决策记录)
4. [目标架构总览](#4-目标架构总览)
5. [模块详细设计](#5-模块详细设计)
6. [回退与异常处理总表](#6-回退与异常处理总表)
7. [分阶段实施步骤](#7-分阶段实施步骤)
8. [验证方案](#8-验证方案)
9. [风险与避坑清单](#9-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 当前 `vector_search_query` 节点（`customer_tools/node.py:138`）每次 RAG 查询执行两次语义检索 + 一次全量语料搬运：
   - 先做 pgvector HNSW 检索（`VectorStoreQuery.search`，node.py:72-91，DB 内完成，有 HNSW 索引）
   - 再 `get_all_documents()` 无分页拉全表（含 1024 维 embedding，node.py:93-99），用 `HybridRetriever` 在应用内存重建向量矩阵 + 暴力点积（`_vector_search`，hybrid_retriever.py:89-119）——**与上一步重复的语义检索**，且是最慢的实现
   - `HybridRetriever` 构造时同步重建 BM25 索引（`bm25_retriever.py:89-98`）：jieba 全量分词 O(corpus) 单线程 CPU
2. planner map-reduce 并行分支（`edges.py:10-22`）下，上述成本 ×N（每子任务独立全量拉库 + 重建）
3. 语料规模预期：JDDC 对话（约 12 万条，筛选后 MAX 5 万）、电商 FAQ、商品知识文档——目标 **1 万 ~ 10 万 chunk 级**。此规模下全量拉库 + jieba 分词重建可达 30s+，直接触发 `TimeoutGuard`（lg_builder.py:475）超时降级
4. 依赖缺口：`rank_bm25` 未声明于 pyproject.toml（仅 jieba），缺失时 BM25 静默返回空列表
5. 精排能力缺失：`RERANKER_*` 三项配置（MODEL/TOP_K/MAX_LENGTH）在 config.py:109-111 与 .env 中**已声明但全项目零消费**——reranker 从未接线
6. 工具化缺失：全项目无 LLM 工具绑定概念（`customer_tools` 是固定检索节点，"multi_tool"名不副实），唯一工具形态是隔离的 `search_service.py` OpenAI 风格 ToolRegistry

### 1.2 目标

| 目标 | 量化指标 |
|---|---|
| 向量检索单路径化 | 内存暴力检索（`_vector_search`/`_get_doc_embeddings`）从生产链路移除，全项目调用点归零 |
| BM25 数据库化 | 应用内存无 BM25 索引与语料驻留；`get_all_documents` 全量拉库调用点归零 |
| 检索延迟可预期 | 两路 SQL 并行执行，单次检索耗时 = max(两路)；语料规模增长不再线性放大应用侧成本 |
| 增量维护 | 新 chunk 入库后 GIN 索引自动维护，无重建窗口 |
| 精排接入 | 融合 top-20 → bge-reranker-v2-m3 精排 top-5；替代 LLM 相关性评分（grade_relevance 移除）；GPU 加速（cuda 可用即用，fp16） |
| Tool 化 | 核心检索抽为 `RAGRetrieverService` + langchain `@tool` 薄封装，graph 节点与未来 agent 共用同一服务 |
| 配置统一 | 全部检索/精排参数（top-k、RRF k、模型、设备、批量）收敛到 .env，代码零硬编码 |
| 行为一致性 | BM25 分词与现网 jieba 精确模式同源（cppjieba）；精排替代 LLM 评分后质量回归对比（需确认无回退） |

### 1.3 设计原则

1. **检索计算全部下沉数据库**：应用层只发 SQL 收 top-k 排名，语料（文本 + 向量）不再流入应用进程
2. **两路独立、并行、可降级**：BM25 与向量检索无数据依赖，`asyncio.gather` 并行；任一路失败不影响另一路与融合
3. **精排可关闭、可降级**：`RERANKER_ENABLED=false` 或模型加载失败时，跳过精排直接用融合 top-K 进生成，不阻塞主链路
4. **配置语义收敛 .env**：所有检索/精排可调参数以 `settings.*` 为唯一入口，.env 全覆盖
5. **单一服务入口**：`RAGRetrieverService.search()` 是唯一检索核心，graph 节点与 `@tool` 都是薄调用方
6. **分词配置统一 jiebacfg（精确模式）**：索引与查询同一配置，保证 `@@` token 对齐；不使用 jiebaqry 全模式

---

## 2. 现状链路与问题

### 2.1 现状调用链（一次 graphrag-query 子任务）

```
planner 拆解 → Send("customer_tools") ×N 并行
  vector_search_query（每子任务独立执行）：
    ① vector_store.search()        HNSW SQL top-10          —— 快路径（有索引）
    ② get_all_documents()          全表无分页拉取（含向量）  —— 慢：O(corpus) 网络+物化
    ③ HybridRetriever(documents)
       ├─ __init__ → BM25Retriever._build_index()            —— 慢：jieba 全量分词重建
       └─ search()
           ├─ bm25.search()         内存 BM25（同步）
           ├─ _get_doc_embeddings() 堆叠 N×1024 float32 矩阵 —— 慢：O(corpus) 内存
           └─ _vector_search()      np.dot 暴力点积 + 全排序  —— 与①重复的语义检索
    ④ rrf_fuse([vector, bm25])      top-5
    ⑤ merged 补充：把①的 top-10 补进融合结果（去重）
    ⑥ grade_relevance（LLM 评分）  → response_text
```

### 2.2 问题清单

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | 内存暴力检索与 HNSW 检索重复执行同一语义检索 | node.py:154 + hybrid_retriever.py:89-119 | 双倍计算，慢实现 |
| 2 | 全量拉库（无 LIMIT/分页，含 4KB×N 向量） | node.py:93-99 | 每请求 O(corpus) 网络 + ORM 物化，×N 并行分支 |
| 3 | BM25 索引每请求全量重建（jieba 分词 1~3MB/s） | bm25_retriever.py:89-98 | 10 万 chunk ≈ 30s+，超时降级 |
| 4 | 向量矩阵常驻/峰值内存 | hybrid_retriever.py:83 | N×4KB×并行分支数 |
| 5 | rank_bm25 依赖未声明 | pyproject.toml | 新环境 BM25 静默降级为空 |
| 6 | 单例只缓存无状态客户端，最贵部分照跑 | node.py:108-119 | "消除重型资源重建"目标未达成 |
| 7 | RERANKER_* 死配置（零消费） | config.py:109-111 | 精排能力缺失，LLM 评分每查询烧 1 次调用 |
| 8 | 检索无工具化封装 | customer_tools/ | agent 无法自主调用检索 |

---

## 3. 方案选型决策记录

### 3.1 向量检索：pgvector HNSW 单路径（已确认）

- 语义：归一化向量下 `cosine_distance = 1 − 余弦相似度`，`ORDER BY 距离 + LIMIT k` 触发 HNSW 近似最近邻（`idx_document_chunks_embedding`，已建）
- 理由：RRF 只消费排名（近似 top-k 的轻微扰动可接受）；下游精排 + 生成 LLM 双兜底；准确性旋钮 `SET hnsw.ef_search = N`，替代换算法

### 3.2 BM25：pg_jieba + ts_rank_cd（已确认）

| 候选 | 结论 | 理由 |
|---|---|---|
| **pg_jieba + ts_rank_cd** | ✅ 采纳 | cppjieba 与现网 jieba 同源算法；阿里云 PolarDB 官方支持；GIN 索引增量维护 |
| zhparser + ts_rank_cd | 否决 | SCWS 最大匹配与现网统计分词差异大，排名行为漂移不可预知 |
| pg_bestmatch / pg_bm25 | 否决 | 标准 BM25/BM25F 但生态过新、成熟度存疑 |
| pg_search (ParadeDB) | 否决 | AGPL-3.0 商业硬约束 + 2026-03 Neon 官方弃用 |
| 内存 BM25 + 语料缓存 | 否决 | 语料必须流入应用进程并常驻；IDF/avgdl 全局统计无法增量，变更后仍全量重建 |
| ES/OpenSearch/Manticore | 否决 | 新增容器 + 同步管道，与 compose 收敛方向相悖 |

- 已接受代价：排名公式由 BM25Okapi（k1=1.5, b=0.75）换为 ts_rank_cd（BM25 变体）——排名行为需回归
- 分词配置：索引与查询统一 `jiebacfg`（精确模式）

### 3.3 精排：bge-reranker-v2-m3 CrossEncoder 替代 LLM 相关性评分（已确认）

| 候选 | 结论 | 理由 |
|---|---|---|
| **sentence-transformers CrossEncoder（bge-reranker-v2-m3）** | ✅ 采纳 | 依赖已具备（sentence-transformers 5.7.0 自带 CrossEncoder，无需 FlagEmbedding）；RERANKER_MODEL/TOP_K/MAX_LENGTH 配置已存在；本地推理零 API 成本，GPU 可加速 |
| FlagEmbedding FlagReranker | 否决 | 新增依赖包，功能与 CrossEncoder 等价，无必要 |
| 精排 API（如 Jina 等） | 否决 | 引入外部依赖与成本，与"本地收敛"方向相悖 |

- **替代关系**（用户确认）：reranker 精排输出 top-5 直接进生成，`grade_relevance` 及其 LLM 调用移除；`RELEVANCE_GRADING_ENABLED` 弃用，由 `RERANKER_ENABLED` 接管"筛选开关"
- **GPU**（用户确认）：torch 本次升级 CUDA 构建（当前 Windows 锁 CPU-only，RTX 3060 6GB，fp16 精排 ~1.2GB 显存），设备由 `RERANKER_DEVICE` 控制（auto/cuda/cpu）
- 管线位置：`检索 top-20（每路）→ RRF 融合 → 取融合前 RERANKER_INPUT_TOP_K=20 → 精排 top-5`

### 3.4 执行结构：并行 + 统一 RRF（已确认）

- 两路均异步 SQL，`asyncio.gather` 并行，耗时 = max 而非 sum；RRF 只消费排名
- 单路失败 → 该路返回空列表，融合仅用成功路

### 3.5 Tool 封装：核心服务 + langchain @tool 薄封装（已确认）

| 候选 | 结论 | 理由 |
|---|---|---|
| **RAGRetrieverService 核心 + langchain `@tool` 薄封装** | ✅ 采纳 | 单一检索核心，graph 节点与未来 agent 共用；langchain @tool 是生态标准，后续 `bind_tools` 直接可用 |
| 项目 FunctionTool + ToolRegistry 风格 | 否决 | 与 OpenAI Chat Completions 风格绑定（search_service 专用），不适合 LangGraph 侧复用 |

---

## 4. 目标架构总览

```
RAGRetrieverService.search(query)          ← 唯一检索核心（graph 节点与 @tool 共用）
    ① asyncio.gather（并行，互不依赖）：
       ├─ HNSW 向量检索 SQL   → top-20（HYBRID_RETRIEVAL_TOP_N）
       └─ pg_jieba BM25 SQL   → top-20
    ② rrf_fuse([vector, bm25], id_key="id", k=60, top_k=RERANKER_INPUT_TOP_K=20)
    ③ RerankerService 精排    → top-5（RERANKER_TOP_K）
    ④ 返回 docs（rrf_score + rerank_score）

调用方：
  ├─ graph 节点 vector_search_query → records.hybrid_docs（summarize 消费）
  └─ @tool rag_retrieval（query → docs 文本，供 agent bind_tools）

数据侧：
  document_chunks 表
    ├─ embedding vector(1024)      （既有，HNSW 索引）
    ├─ content_tsv tsvector        （新增生成列，jiebacfg 分词）
    └─ GIN 索引 (content_tsv)      （新增，DB 内增量维护）
```

**移除项**：`_vector_search`、`_get_doc_embeddings`、`get_all_documents`、`bm25_retriever.py`（jieba + rank_bm25）、`relevance_grader.py`（LLM 评分）、merged 补充逻辑、`jieba` 依赖、`RELEVANCE_GRADING_ENABLED`。

---

## 5. 模块详细设计

### 5.1 容器镜像（`docker/postgres/Dockerfile` 新增）

```dockerfile
FROM pgvector/pgvector:pg16
RUN apt-get update && apt-get install -y --no-install-recommends \
        git gcc g++ make postgresql-server-dev-16 \
    && git clone --recursive --depth 1 https://github.com/jaiminpan/pg_jieba /tmp/pg_jieba \
    && cd /tmp/pg_jieba && make && make install \
    && rm -rf /tmp/pg_jieba \
    && apt-get purge -y git gcc g++ make postgresql-server-dev-16 \
    && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
```

- `--recursive` 必带（cppjieba 子模块）；PGDG apt 无 pg_jieba 包（已实测），源码编译是唯一路径
- `docker-compose.yml`：postgres 服务 `image:` → `build: ./docker/postgres`；容器名/端口/`pg_data` 卷/healthcheck 全不动；重建后数据卷保留

### 5.2 扩展初始化与 schema 迁移

> 数据卷已初始化 → docker-entrypoint 跳过 initdb 脚本，须手动执行一次；同时同步进 `init_db.py` 保证新环境可复现。

```sql
CREATE EXTENSION IF NOT EXISTS pg_jieba;        -- 自带 jiebacfg（精确模式）配置
ALTER TABLE document_chunks ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('jiebacfg', content)) STORED;
CREATE INDEX ix_chunks_tsv ON document_chunks USING GIN (content_tsv);
```

- 生成列 ALTER 时自动回填存量 chunk；INSERT 自动维护，无需触发器
- 同步写入 `llm_backend/scripts/init_db.py`（`CREATE EXTENSION IF NOT EXISTS pg_jieba` + content_tsv 列 + GIN 索引，显式文本 SQL 与现有 HNSW 索引模式一致）

### 5.3 BM25 SQL 检索器（`hybrid_retrieval/bm25_sql_retriever.py` 新增）

```python
async def search(query: str, top_k: int) -> List[Dict[str, Any]]:
    query_tsv = func.plainto_tsquery("jiebacfg", query)
    stmt = (
        select(DocumentChunk, func.ts_rank_cd(DocumentChunk.content_tsv, query_tsv).label("bm25_score"))
        .where(DocumentChunk.content_tsv.op("@@")(query_tsv))
        .order_by(desc("bm25_score"))
        .limit(top_k)
    )
    # 结果映射复用 _to_doc 结构（id/source/text/file_path/user_id/chunk_index），
    # 附加 bm25_score；embedding 不返回（已无消费方）
```

### 5.4 RAGRetrieverService 核心（`app/services/rag_retriever_service.py` 新增）

- 组合：`VectorStoreQuery.search`（HNSW，从 customer_tools 上移复用）+ `BM25SQLRetriever` + `rrf_fuse` + `RerankerService`
- `search(query, top_k=None) -> List[Doc]`：并行 gather 两路（各自 try/except 降级为空）→ RRF 融合 top-`RERANKER_INPUT_TOP_K` → 精排 top-`RERANKER_TOP_K`（`RERANKER_ENABLED=false` 或加载失败时直接取融合 top-`RERANKER_TOP_K`）→ 返回 docs
- 模块级懒加载单例（沿用 `get_vector_store()` 的 lock + double-check 模式，node.py:104-119）
- 下游消费字段不变：`records.hybrid_docs` 仍为完整 doc 列表，`records.result` 仍为拼接文本（summarize 零改动）

### 5.5 RerankerService 精排（`app/services/reranker_service.py` 新增）

- 实现：`CrossEncoder(settings.RERANKER_MODEL, device=resolved_device, max_length=settings.RERANKER_MAX_LENGTH)`，`half_precision=config.RERANKER_HALF_PRECISION`（fp16）
- 设备解析：`RERANKER_DEVICE` ∈ {auto, cuda, cpu}——auto = `torch.cuda.is_available()` 判断；单例懒加载（首次调用触发模型下载 + 加载）
- 评分：`model.predict([(query, doc_text) for doc in candidates], batch_size=RERANKER_BATCH_SIZE)` → 按分数降序取 top-`RERANKER_TOP_K`，doc 附加 `rerank_score`
- 输入截取：融合结果前 `RERANKER_INPUT_TOP_K` 条（默认 20，与"检索 top-20 → 精排 top-5"口径一致）

### 5.6 Tool 封装（`app/tools/rag_tool.py` 新增；2026-08-31 目录调整：自 `app/services/` 迁入 `app/tools/`，详见 SPEC_RAG_TOOL_OPTIMIZATION 决策 #9）

```python
@tool
def rag_retrieval(query: str) -> str:
    """从企业知识库检索与问题相关的文档片段（产品参数/故障排查/政策）。输入用户问题，返回相关文档文本。"""
    docs = asyncio.run(RAGRetrieverService().search(query))   # 或 async tool
    return "\n\n".join(d.get("text", "") for d in docs)
```

- 核心逻辑全部在 `RAGRetrieverService`，tool 仅为薄封装；后续 agent 经 `llm.bind_tools([rag_retrieval])` 即可使用

### 5.7 customer_tools/node.py 简化

- `vector_search_query` 节点改为直接调用 `RAGRetrieverService.search()`，输出落 `records.hybrid_docs` / `records.result`
- 删除：`get_all_documents()`、merged 补充逻辑、`grade_relevance` 调用、内存向量导入、`get_vector_store()` 单例（由 RAGRetrieverService 单例接管）
- 保留：HNSW SQL 检索实现（上移至 service）、`_to_doc`、错误处理与降级

### 5.8 配置统一清单（config.py + .env）

| 配置项 | 现状 | 动作 |
|---|---|---|
| `VECTOR_SEARCH_TOP_K` | 有/有消费 | 保留（HNSW 路 top-k 语义随 HYBRID_RETRIEVAL_TOP_N 收口，视实施定存废） |
| `HYBRID_RETRIEVAL_TOP_K=5` | 有/有消费 | 保留（融合最终输出） |
| `HYBRID_RETRIEVAL_TOP_N=20` | 有/有消费 | 保留（每路候选数） |
| `RRF_FUSION_K=60` | 有/有消费 | 保留（平滑常数） |
| `RRF_TOP_K=20` | **无** | **新增**（融合输出候选数，精排输入） |
| `RERANKER_ENABLED` | **无** | **新增**（默认 true，精排开关） |
| `RERANKER_MODEL=BAAI/bge-reranker-v2-m3` | 有/零消费 | 接线 |
| `RERANKER_TOP_K=5` | 有/零消费 | 接线 |
| `RERANKER_MAX_LENGTH=512` | 有/零消费 | 接线 |
| `RERANKER_INPUT_TOP_K=20` | **无** | **新增**（精排输入候选数） |
| `RERANKER_DEVICE=auto` | **无** | **新增**（auto/cuda/cpu） |
| `RERANKER_BATCH_SIZE` | **无** | **新增**（默认 8） |
| `RERANKER_HALF_PRECISION=true` | **无** | **新增**（fp16，6GB 显存必需） |
| `RELEVANCE_GRADING_ENABLED` | 有 | **弃用删除**（LLM 评分移除） |
| `HF_HOME` | 无 | **新增**（模型缓存路径约定，默认不设走系统缓存） |
| `LLM_GRADER_TEMPERATURE` | 有 | **弃用删除** |

### 5.9 依赖变更

- **torch → CUDA 构建**（用户确认本次升级）：`uv add torch --index-url https://download.pytorch.org/whl/cu126`（3060 Ampere 兼容；Windows wheel ~2.5GB）；失败回退 CPU-only torch（精排 CPU 模式可用，仅速度差异）
- 移除 `jieba>=0.42.1`；`relevance_grader` 删除后无新增 LLM 依赖
- sentence-transformers 5.7.0 已有（CrossEncoder 内置），无新增

---

## 6. 回退与异常处理总表

| 场景 | 处理 |
|---|---|
| BM25 SQL 异常（扩展缺失/查询失败） | 该路返回空列表，仅向量路参与融合；日志 warning |
| HNSW SQL 异常 | 该路返回空列表，仅 BM25 路参与融合；日志 warning |
| query 向量全零（Embedding API 失败） | 跳过向量路，BM25 独立出结果 |
| **reranker 模型加载失败**（下载中断/显存不足） | `RERANKER_ENABLED` 逻辑降级：跳过精排，直接用融合 top-K；日志 error 但主链路不阻塞 |
| reranker 单次评分异常 | 同上降级 |
| torch CUDA 升级失败 | 回退 CPU-only torch（uv lock 回滚），精排 CPU 模式运行（慢但可用） |
| pg_jieba 扩展未安装 | `@@` 查询报错 → BM25 降级分支；补执行 5.2 初始化 |
| 镜像构建失败 | compose 回退 `image: pgvector/pgvector:pg16`，内存 BM25 代码暂保留至切换完成 |
| 精排质量回归 | 8.3 回归对比；旋钮：`RERANKER_INPUT_TOP_K` / `RERANKER_TOP_K` / fp16 关 / `hnsw.ef_search` |

---

## 7. 分阶段实施步骤

| 阶段 | 内容 | 验收 |
|---|---|---|
| 0 | 镜像：Dockerfile + compose build，`docker compose up -d --build postgres` | 容器 healthy；`pg_available_extensions` 见 pg_jieba |
| 1 | 扩展 + schema：手动 SQL + 同步 init_db.py | `\d document_chunks` 见 content_tsv 生成列 + GIN 索引；存量回填正确 |
| 2 | 依赖：torch CUDA 升级（uv add --index-url）+ uv lock；RERANKER/HF 配置入 config.py + .env | `torch.cuda.is_available()` = True；配置读取得出 |
| 3 | 核心检索：bm25_sql_retriever 新增 → RAGRetrieverService（并行 + RRF）→ node.py 改调服务 | 冒烟：两路并行、融合去重、单路降级正常 |
| 4 | 精排：RerankerService 新增 → 接入 RAGRetrieverService → node.py 移除 grade_relevance | 冒烟：精排 top-5 输出、GPU 生效、关闭开关走降级 |
| 5 | Tool：rag_tool.py @tool 封装 → 验证调用 | tool 直接调用返回 docs |
| 6 | 清理：删 bm25_retriever.py / relevance_grader.py、移除 jieba、RELEVANCE_GRADING_ENABLED/LLM_GRADER_TEMPERATURE、uv lock | grep 调用点归零（除 spec/文档） |
| 7 | 回归验证 + git 提交 | 见 §8；git 提交推送（schannel） |

## 8. 验证方案

### 8.1 数据层

```sql
SELECT count(*) FROM document_chunks WHERE content_tsv @@ plainto_tsquery('jiebacfg','沙发');  -- >0，回填正确
EXPLAIN SELECT ... ORDER BY ts_rank_cd DESC LIMIT 5;  -- 走 ix_chunks_tsv（Bitmap Index Scan）
EXPLAIN SELECT ... ORDER BY embedding <=> :v LIMIT 5; -- 走 HNSW 索引
```

### 8.2 端到端冒烟（临时脚本，跑完删除）

- 语义匹配："智能灯泡不亮了" → 命中故障排查章节（对比旧链路 top-1）
- 精确型号："SF-2000 多少钱" → 命中产品信息/选购章节（BM25 路有贡献，验证 jiebacfg 切词）
- 无结果查询 → 融合 + 精排返回空，链路不抛错
- 单路降级：临时让 BM25 路抛错 → 仅向量结果正常返回
- 精排降级：`RERANKER_ENABLED=false` → 直接用融合 top-K 出结果
- 并行性：日志确认两路并发（耗时 ≈ max 非 sum）

### 8.3 回归对比（关键）

| 项 | 方法 |
|---|---|
| 召回对比 | 旧内存 BM25 vs pg_jieba 各跑 3 类查询 top-5/top-20，对比命中文档 id 集合 |
| 精排质量 | 对比"仅融合 top-5"与"融合→精排 top-5"的文档集合与顺序；抽查精排剔除的明显不相关项 |
| 分词一致性 | 抽查 "SF-2000"、"SN-2024-089"、"扫地机器人X1" 在 cppjieba 与 jieba 的切词 |
| GPU 生效 | 日志/`torch.cuda.is_available()` 确认精排跑在 cuda；对比 cpu/cuda 单次精排耗时 |

### 8.4 Tool 验证

- 直接调用 `rag_retrieval("SF-2000 多少钱")` → 返回相关文档文本（含 source/评分）
- 与 graph 节点路径结果一致性（同一 RAGRetrieverService）

### 8.5 性能

- `EXPLAIN ANALYZE` 记录两路 SQL 耗时（毫秒级）；精排单次耗时（cuda，top-20 输入）；确认整体在 TimeoutGuard 30s 内富余

---

## 9. 风险与避坑清单

1. **PG 16.14 与 pg_jieba 编译兼容性**：Pigsty 已打包 PG12~17 支持，风险低；构建失败走 6 节回退
2. **cppjieba 子模块拉取**：`git clone` 不带 `--recursive` 会编译失败——Dockerfile 已含
3. **存量卷不跑 initdb 脚本**：5.2 的"手动执行一次"不可省略
4. **生成列与 `to_tsvector` 不可变约束**：固定配置 `'jiebacfg'` 满足 immutable 要求
5. **torch CUDA 升级体积与破坏性**：~2.5GB 下载、uv.lock 大改；升级后全项目 torch 相关代码（embedding 本地路径等）需冒烟
6. **reranker 模型首查下载延迟**：bge-reranker-v2-m3 ~2.3GB，首次查询触发下载可能超时——预下载步骤（阶段 2 顺手下载或脚本预热）避免线上首查卡住
7. **6GB 显存限制**：fp16 必需（默认开启）；显存不足异常走降级分支
8. **精排替代 LLM 评分无 LLM 兜底**：质量依赖 reranker + 生成 LLM 自身判断；8.3 精排质量回归不可跳过
9. **jiebaqry 诱惑**：查询配置与索引不一致 → 召回丢失，坚持双端 jiebacfg
10. **镜像升级路径**：后续 PG 小版本升级需重建自定义镜像
11. **embedding 返回裁剪**：检索结果不再携带 1024 维向量进下游，重构时保持
