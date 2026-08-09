# GraphRAG → 标准 RAG 改造实施计划

## Context

### 为什么要做这个改造

SmartCS-Agent 的核心业务场景是**智能家居电商客服**，用户问题以事实查询为主（价格、库存、故障排查、退换货政策）。当前使用 Microsoft GraphRAG 做文档检索，存在以下问题：

1. **索引进度慢**：GraphRAG 构建索引需要 LLM 抽取实体/关系/社区（5-30 分钟/次），而标准 RAG 只需要分块+Embedding（10-60 秒）
2. **LLM Token 消耗大**：GraphRAG 索引阶段每次构建调用大量 LLM，成本高
3. **内嵌源码**：`graphrag/` 目录包含 80+ 文件，依赖 `graphrag==2.1.0`，版本管理困难
4. **能力过剩**：Global Search、DRIFT 多跳推理、社区摘要等 GraphRAG 高级功能在电商客服场景下极少用到
5. **已有 Neo4j**：实体关系已经在 Neo4j 知识图谱中管理，GraphRAG 的实体抽取是冗余的

### 改造成什么

替换为标准 RAG 管道：**文档解析 → 清洗 → 语义分块 → Embedding → 向量库存储 → 相似度检索**

### 目标成果

- ✅ 索引速度从分钟级降到秒级
- ✅ 消除 GraphRAG 依赖（80+ 源码文件、`graphrag` pip 包）
- ✅ 事实查询效果持平（向量检索 + 混合检索 + 相关性评分不受影响）
- ✅ 降低维护复杂度
- ✅ 保留现有的 Neo4j 知识图谱（Text2Cypher/PredefinedCypher 不受影响）
- ✅ 保留现有的混合检索（BM25 + 向量 + RRF）、相关性评分、语义缓存

---

## 影响范围

### 需要修改的文件 (5 个 + 1 个可选)

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `requirements.txt` | 修改 | 删除 `graphrag==2.1.0`，新增 `chromadb>=0.4.0`、`langchain-text-splitters>=0.3.0` |
| `llm_backend/app/core/config.py` | 修改 | 删除 8 项 `GRAPHRAG_*` 配置，新增 `VECTOR_DB_PATH` 等向量库配置 |
| `llm_backend/app/services/indexing_service.py` | 重写 | GraphRAG `build_index()` → 解析→清洗→分块→Embedding→入库管道 |
| `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/customer_tools/node.py` | 重写 | `GraphRAGAPI` → `VectorStoreQuery`，去掉 parquet 初始化 |
| `llm_backend/app/lg_agent/kg_sub_graph/kg_tools_list.py` | 修改 | 工具名 `microsoft_graphrag_query` → `vector_search_query`，描述更新 |
| `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/tool_selection/node.py` | 修改 | `microsoft_graphrag_query` → `vector_search_query` |

### 需要删除的文件/目录

| 路径 | 大小 | 说明 |
|------|------|------|
| `llm_backend/app/graphrag/` | ~80+ 文件 | 内嵌的 Microsoft GraphRAG 源码 |
| `scripts/build_graphrag_index.py` | - | GraphRAG 手动构建脚本，不再需要 |
| `.env.example` (部分) | - | 删除 `GRAPHRAG_*` 配置段 |

### 不动的文件

- `llm_backend/main.py` — API 端点不变
- `llm_backend/app/lg_agent/lg_builder.py` — Agent 编排不变
- `llm_backend/app/lg_agent/lg_states.py` — 状态定义不变
- `llm_backend/app/lg_agent/lg_prompts.py` — 提示词不变
- `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py` — 工作流结构不变
- `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/hybrid_retrieval/` — 完全复用
- `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/relevance_grader.py` — 完全复用
- `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/cypher_tools/` — 不受影响
- `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/predefined_cypher/` — 不受影响
- `llm_backend/app/services/deepseek_service.py` — 不受影响
- `llm_backend/app/services/redis_semantic_cache.py` — 不受影响
- `llm_backend/app/services/conversation_service.py` — 不受影响
- `llm_backend/app/core/database.py` — 不受影响
- `docker-compose.yml` — 不变（无需新增服务）
- `Dockerfile` — 基本不变

---

## 技术方案

### 1. 向量库选型：ChromaDB

选择 ChromaDB 的理由：

- **无需新增 Docker 服务**：ChromaDB 支持 persist 模式，数据存本地文件，零运维
- **与现有技术栈契合**：Python 原生，异步支持，LangChain 官方集成
- **轻量级**：pip install 即可，不增加部署复杂度
- **备选方案**：如果后续需要分布式，可平滑迁移到 Milvus 或 Qdrant，API 相似

### 2. 文档解析管道

```
PDF      → PyPDF2 (已有依赖)  → 文本
TXT/MD   → 直接读取           → 文本
DOCX     → python-docx (已有依赖) → 文本
CSV      → pandas             → 文本
图片     → (暂不支持，Vision API 单独处理)
```

### 3. 分块策略

复用 `langchain-text-splitters` 的 `RecursiveCharacterTextSplitter`：

```python
RecursiveCharacterTextSplitter(
    chunk_size=500,        # 与现有 _preprocess_text_file 一致
    chunk_overlap=50,      # 上下文重叠
    separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""]
)
```

对于有 `##` 标题结构的电商产品文档，增加 `MarkdownHeaderTextSplitter` 保留标题层级。

### 4. Embedding 模型

复用现有 SiliconFlow BAAI/bge-m3 或本地 Ollama bge-m3，与语义缓存使用同一套 Embedding，**不需要新配置**。

### 5. 查询接口

`VectorStoreQuery` 替代 `GraphRAGAPI`：

```
原: GraphRAGAPI.initialize() → 加载 6 个 parquet → local/global/drift/basic_search()
新: VectorStoreQuery.search(query) → ChromaDB.similarity_search() → 返回文档列表
```

查询结果直接流入现有的 `HybridRetriever` + `RelevanceGrader` 管道。

---

## 分步实施计划

### Step 1: 更新依赖和配置

**文件**: `requirements.txt`, `llm_backend/app/core/config.py`, `.env.example`, `.env.docker`

具体操作：
- `requirements.txt`: `graphrag==2.1.0` → `chromadb>=0.4.0` + `langchain-text-splitters>=0.3.0`
- `config.py`: 删除 8 个 `GRAPHRAG_*` 字段，新增：
  ```python
  VECTOR_DB_PATH: str = str(ROOT_DIR / "vector_db")  # ChromaDB 持久化目录
  VECTOR_DB_COLLECTION: str = "smartcs_agent_docs"   # 集合名称
  ```
- `.env.example` + `.env.docker`: 删除 `# Microsoft GraphRAG 配置` 段

**验证**: `python -c "import chromadb; from langchain_text_splitters import RecursiveCharacterTextSplitter; print('OK')"`

---

### Step 2: 重写 IndexingService

**文件**: `llm_backend/app/services/indexing_service.py`

重构逻辑：

```
原 (GraphRAG): 复制文件 → (可选预处理) → api.build_index() → parquet 输出
新 (标准RAG): 复制文件 → 解析文本 → 清洗 → 分块 → Embedding → ChromaDB 入库
```

关键代码结构：

```python
class IndexingService:
    def __init__(self):
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500, chunk_overlap=50,
            separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""]
        )
        self.embeddings = SiliconFlowEmbeddings(
            api_key=settings.SILICONFLOW_API_KEY,  # 复用或新增配置
            model="BAAI/bge-m3"
        )
        self.vector_store = Chroma(
            collection_name=settings.VECTOR_DB_COLLECTION,
            embedding_function=self.embeddings,
            persist_directory=settings.VECTOR_DB_PATH
        )

    def _parse_document(self, file_path: str) -> str:
        """根据文件类型解析文档"""
        if file_path.endswith('.pdf'):
            reader = PdfReader(file_path)
            return "\n".join(page.extract_text() for page in reader.pages)
        elif file_path.endswith('.docx'):
            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
        else:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()

    def _clean_text(self, text: str) -> str:
        """清洗文本"""
        # 去掉多余空行、统一换行符、去掉噪声字符
        ...

    async def process_file(self, file_info: Dict) -> Dict:
        # 1. 解析
        text = self._parse_document(file_path)
        # 2. 清洗
        text = self._clean_text(text)
        # 3. 分块
        chunks = self.text_splitter.split_text(text)
        # 4. 入库
        self.vector_store.add_texts(
            texts=chunks,
            metadatas=[{"source": file_path, "user_id": user_id, "chunk_index": i}
                       for i in range(len(chunks))]
        )
        return {"status": "success", "chunks": len(chunks)}
```

**验证**: 运行 `IndexingService().process_file(test_file)` 确认分块入库成功

---

### Step 3: 重写 customer_tools/node.py

**文件**: `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/customer_tools/node.py`

核心变更：

```
原: GraphRAGAPI 类 (~100 行) + graphrag_query 函数
新: VectorStoreQuery 类 (~20 行)  + vector_search_query 函数
```

关键代码结构：

```python
class VectorStoreQuery:
    """向量库查询封装"""
    def __init__(self):
        self.vector_store = Chroma(
            collection_name=settings.VECTOR_DB_COLLECTION,
            embedding_function=self._get_embeddings(),
            persist_directory=settings.VECTOR_DB_PATH
        )
        self.hybrid_retriever = None  # 懒加载

    def search(self, query: str, top_k: int = 5):
        """执行向量检索"""
        return self.vector_store.similarity_search_with_score(query, k=top_k)
```

`create_graphrag_query_node()` 函数重命名为 `create_vector_search_query_node()`，内部逻辑简化为：

```
1. 向量库检索 → 得到 top-K 文档
2. 混合检索 (HybridRetriever) → BM25+向量+RRF 融合
3. 相关性评分 (RelevanceGrader) → 过滤 + 不足时重检索
4. 合并 GraphRAG 结果 → 现在改为合并向量库结果
```

> **关键不变项**：`HybridRetriever` 只需要一个文档列表（`text_units`），之前从 GraphRAG 的 parquet 获取，现在从向量库查询获取。它的 BM25+向量+RRF 内部逻辑不需要任何修改。

**验证**: 调用 `VectorStoreQuery().search("扫地机器人故障排查")` 确认返回结果

---

### Step 4: 更新工具 Schema 定义

**文件**: `llm_backend/app/lg_agent/kg_sub_graph/kg_tools_list.py`

```python
# 修改前
class microsoft_graphrag_query(BaseModel):
    """如果用户问的问题是关于产品的故障、售后、保修、维修、退换货以及评价等，则使用这个工具"""
    query: str = Field(...)

# 修改后
class vector_search_query(BaseModel):
    """如果用户问的问题是关于产品的故障、售后、保修、维修、退换货以及评价等，则使用这个工具进行向量检索"""
    query: str = Field(...)
```

同名引用同步更新：`tool_schemas` 列表中的类名。

---

### Step 5: 更新 tool_selection/node.py

**文件**: `llm_backend/app/lg_agent/kg_sub_graph/agentic_rag_agents/components/tool_selection/node.py`

```python
# 修改前
elif tool_preference == "microsoft_graphrag_query":
    ...
    query_name: "microsoft_graphrag_query"

# 修改后
elif tool_preference == "vector_search_query":
    ...
    query_name: "vector_search_query"
```

---

### Step 6: 更新 lg_builder.py 中的 tool_preference 常量

**文件**: `llm_backend/app/lg_agent/lg_builder.py`

```python
# 修改前 (第 458 行)
tool_preference = "microsoft_graphrag_query"

# 修改后
tool_preference = "vector_search_query"
```

同步更新日志中的描述文字。

---

### Step 7: 清理删除

- 删除 `llm_backend/app/graphrag/` 整个目录（~5MB, 80+ 文件）
- 删除 `scripts/build_graphrag_index.py`
- 删除 `fix_nb.py`（Jupyter Notebook 修复脚本，与 GraphRAG 相关）
- 更新 `Dockerfile`：移除 pip 安装时可能的 GraphRAG 特殊处理

**验证**: `grep -r "graphrag" --include="*.py" llm_backend/app/` 确认无残留引用（`"graphrag-query"` 路由名除外，那个是业务意图分类，不需要改）

---

### Step 8: 端到端测试

1. **索引测试**：上传一个 PDF 文档，验证分块入库
2. **查询测试**：通过 `/api/langgraph/query` 发送业务问题，验证向量检索结果
3. **混合检索测试**：验证 BM25+向量+RRF 融合正常工作
4. **相关性评分测试**：验证相关性评分+重检索逻辑
5. **回归测试**：验证闲聊、追问、Text2Cypher、PredefinedCypher、图片分析路由均正常
6. **Docker 构建测试**：`docker compose build app` 确认无 GraphRAG 依赖错误

---

## 裁剪项（不在此次改造范围）

| 项目 | 原因 |
|------|------|
| Neo4j 知识图谱 | Text2Cypher/PredefinedCypher 查的是结构化数据，与文档检索是独立管线 |
| 语义缓存 | Redis 层，与文档检索无关 |
| 混合检索 | BM25+向量+RRF 是独立组件，接收文档列表即可，来源无关 |
| 相关性评分 | 独立组件，不依赖 GraphRAG |
| Vision API 图片分析 | 独立路由，与文档检索无关 |
| 联网搜索 | SerpAPI，独立功能 |
| 前端 UI | 不受影响 |

---

## 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| ChromaDB 持久化目录权限问题 | 低 | 中 | Dockerfile 中预创建 `vector_db/` 目录并设权限 |
| Embedding 模型调用失败（SiliconFlow API 不可用） | 中 | 高 | 降级到本地 Ollama bge-m3，已有依赖 |
| 已有 GraphRAG 索引数据无法迁移 | 高 | 低 | 不需要迁移，重新上传文档即可重建索引 |
| `graphrag-query` 路由名硬编码在其他地方 | 低 | 中 | 全局 grep 验证，Step 7 已包含 |
| 分块后检索精度不如 GraphRAG 的实体链接 | 中 | 中 | 混合检索 + 相关性评分可弥补；且 Text2Cypher/PredefinedCypher 负责结构化查询 |

---

## 工时估算

| 步骤 | 预估时间 | 说明 |
|------|---------|------|
| Step 1: 依赖配置 | 15 min | 改 requirements.txt + config.py + .env |
| Step 2: IndexingService | 45 min | 重写核心管道逻辑 |
| Step 3: customer_tools/node.py | 30 min | GraphRAGAPI → VectorStoreQuery |
| Step 4: kg_tools_list.py | 5 min | 重命名类 + 描述 |
| Step 5: tool_selection/node.py | 5 min | 常量替换 |
| Step 6: lg_builder.py | 5 min | 常量替换 |
| Step 7: 清理删除 | 10 min | 删除目录 + grep 验证 |
| Step 8: 端到端测试 | 30 min | 6 项测试 |
| **总计** | **~2.5 小时** | |

---

## 验证清单

- [ ] `docker compose build app` 成功
- [ ] `docker compose up -d` 全部 healthy
- [ ] 上传 PDF/TXT 文档 → 返回分块数和入库状态
- [ ] `/api/langgraph/query` 发送产品问题 → 返回流式向量检索结果
- [ ] 闲聊路由正常（"你好"）
- [ ] Text2Cypher 路由正常（"扫地机器人X1多少钱"）
- [ ] 图片分析路由正常（上传图片）
- [ ] 混合检索 + 相关性评分流水线正常
- [ ] `grep -ri "graphrag" llm_backend/app/ --include="*.py" | grep -v "\.pyc" | grep -v "/graphrag/"` 输出为空（除 `graphrag-query` 路由名外）
