# SmartCS-Agent - 智能电商客服系统

<div align="center">

![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-005571?logo=fastapi)
![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-green)
![DeepSeek](https://img.shields.io/badge/DeepSeek-V3-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)

*基于 FastAPI + LangGraph 的智能电商客服系统，集成向量知识检索（pgvector）与多轮对话管理*

</div>

---

## 项目亮点

| 特性 | 说明 |
|------|------|
| **规则层 + 三维意图识别** | 前置零延迟**规则判定层**：明确意图（售前/售后+二级场景/闲聊）关键词直接短路、省一次 LLM 调用，未命中才降级 LLM；四道让行闸门（风险信号词/多意图/二级撞车）保证安全——**规则层不判风险**，含风险信号词的消息一律交回 LLM。LLM 层单次合并识别三维：场景（售前/售后/投诉安抚/闲聊/图片/意图不明澄清）驱动分支 + **售后二级场景**（物流查询/退货退款/换货/补发/订单查询/兜底） + 风险意图（违规拦截/高风险转人工）独立判断、拦截优先；意图不明时以电商统一风格询问用户真实意图 |
| **向量知识库检索** | pgvector（HNSW）Top-K 检索 + BM25 混合检索 + LLM 相关性评分，文档由管理端上传后秒级建索引 |
| **混合检索 + 相关性评分** | BM25 + 向量检索 RRF 融合，LLM 逐条评分过滤不相关结果，不足时自动切换策略重检索 |
| **文档向量检索管道** | 解析 → 清洗 → 语义分块 → Embedding → pgvector 入库（HNSW 索引），秒级索引，配合混合检索增强召回 |
| **语义缓存** | 基于 Embedding 向量余弦相似度（阈值 0.90），相同语义问题直接返回缓存，降低 LLM 调用成本 |
| **幻觉检测** | LLM 校验生成回答是否基于事实数据，为回答质量提供最后保障 |
| **Docker Compose 一键部署** | PostgreSQL(pgvector) + Redis + App，healthcheck 保障启动顺序，开箱即用 |
| **丰富的知识库** | 内置产品知识文档 + 1,800 条电商 FAQ + 2,600+ 条真实客服对话（JDDC 数据集） |

## 系统架构

```
用户请求 → FastAPI API 层
  │
  ├─ /api/langgraph/query ──→ LangGraph Agent（规则层前置 + 三维意图识别）
  │    │                        意图规则层（零延迟）命中即短路 → 未命中降级 LLM
  │    │                        risk 拦截优先级最高
  │    ├─ risk=violation → 风险拦截（明确拒绝 + 合规引导）
  │    ├─ risk=high_risk → 转人工（说明无法在线直接处理）
  │    ├─ 售前 presale → RAG 子图（Multi-Tool Workflow）
  │    │    └─ 向量检索（pgvector）→ 混合检索(BM25+向量) → 相关性评分(LLM)
  │    ├─ 售后 aftersale → 售后占位节点（二级场景物流/退货/换货/补发/订单查询已落 state，售后 Agent 接口预留，后续接入）
  │    ├─ 投诉安抚 complaint → 投诉安抚占位节点（安抚 Agent 接口预留，后续接入）
  │    ├─ 闲聊 general → 纯 LLM 闲聊
  │    ├─ 意图不明 clarify → 澄清节点（电商风格询问真实意图）
  │    └─ 图片 image → 视觉模型（Qwen-VL）图片分析
  │
  ├─ /api/upload ────────→ 文档解析 → 向量入库（pgvector）
  │
  └─ 前端（Vue3 SFC，frontend → dist）─→ SSE 流式响应
       └─ Redis 语义缓存检查（命中时模拟流式短路返回）
```

## 技术栈

| 层次 | 技术 | 作用 |
|------|------|------|
| 后端框架 | FastAPI | REST API，原生异步，SSE 流式响应 |
| 智能体 | LangGraph | StateGraph 多路由 Agent 编排，会话检查点持久化（PostgresSaver） |
| LLM | DeepSeek API | 对话、推理、Agent（工厂模式可切换 Ollama） |
| 文档检索 | pgvector | 语义分块 + 向量入库（HNSW）+ 混合检索（BM25 + 向量 RRF） |
| Embedding | SiliconFlow (BAAI/bge-m3) | 语义向量生成，免费 API |
| 向量缓存 | Redis | 语义缓存（余弦相似度 >= 0.90 命中） |
| 数据库 | PostgreSQL（pgvector） | 用户、会话、消息持久化 + 向量检索 + LangGraph 检查点 |
| 前端 | Vue3 SFC（frontend → dist） | 客户端聊天界面（登录/注册、SSE 流式）+ 管理端控制台（`admin.html`，含知识库管理） |

## 快速开始

### 环境要求

- Python 3.13+
- uv（Python 包管理器，用于本地安装依赖）
- Docker & Docker Compose（仅用于启动 PostgreSQL / Redis 基础服务）

### 本地运行（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/blowt16/SmartCS-Agent.git
cd SmartCS-Agent

# 2. 安装依赖（uv 自动创建 .venv 并按 uv.lock 锁定版本）
uv sync

# 3. 配置环境变量
#    编辑项目根目录 .env，填入 API Key 和数据库连接信息
#    DB_HOST / REDIS_HOST 保持 localhost（对应下方 Docker 启动的基础服务）

# 4. 启动 PostgreSQL、Redis（Docker 仅承载基础服务，应用在本地运行调试）
docker compose up -d

# 5. 初始化数据库（建表 + 启用 pgvector 扩展 + HNSW 索引 + 增量列迁移）
#    ⚠️ 必须在启动服务【之前】跑：模型里的列（users.role / documents.description、status）
#    与表（orders / tickets）都靠它创建。顺序反了会出现"聊天能用、但登录全 500"的迷惑现象
#    ——SQLAlchemy 的 select(User) 会显式列出 role 列，库缺列直接抛 UndefinedColumn。
cd llm_backend
python -m scripts.init_db

# 6. 启动服务（uvicorn，开发模式热重载）
cd llm_backend
python run.py
```

### 初始化管理端数据（首次运行 / 演示前）

```bash
# 顺序不能换：init_db 最前，种子脚本依赖它建的列与表
cd llm_backend
python scripts/init_db.py                 # 1. 建 orders/tickets 表 + 加 4 个增量列
python scripts/seed_admin_account.py      # 2. 管理员账号 admin_test@test.com（role=admin）
python scripts/seed_orders.py             # 3. 18 条订单（upsert，可反复跑刷新演示日期）
python scripts/seed_tickets.py            # 4. 8 条工单（ticket_no 幂等键，跨天重跑仍为 8 条）

# 5. 商品占位图 —— 必须在 npm run build 之前！
#    这些 SVG 是生成物、不入库（frontend/.gitignore 忽略），新克隆的仓库没有它们；
#    不先跑这一步，构建后商品列表图片会全走 onerror 兜底（灰块 + 箱子图标）。
cd ..
python scripts/build_product_placeholders.py   # 47 个 SVG → frontend/public/products/

# 6. 构建前端（客户端 + 管理端两个入口）
cd frontend && npm install && npm run build
```

### 管理端（Admin Console）

入口：**<http://127.0.0.1:8000/admin.html>**（开发模式 `http://localhost:5173/admin.html`），或从客户端登录页底部的「进入管理端」链接进入。
管理员账号由 `seed_admin_account.py` 创建（默认 `admin_test@test.com` / `admin`，已存在的账号只补 `role='admin'`、不改密码）。管理端登录页可勾选「记住账号」——**只记住邮箱，不保存密码**（密码不入任何浏览器存储）。

五个模块：控制台（统计卡 + 四张图）、商品管理、订单管理、知识库（两阶段上传 + SSE 实时进度条）、工单管理。

两点须知：

- **管理端与客户端共用 `localStorage` 的 `token` 键**（有意的：从管理端点「体验客服」跳过去不用重新登录）。反过来说，在管理端「退出登录」会把客户端的登录态一并清掉。
- 知识库上传是**两阶段**：点「上传文件」只暂存到磁盘（不解析、不写库），点「取消」能真正撤销；点「保存」才走完整的解析 → 分块 → 嵌入 → 入库链路，进度条实时推进。未提交的暂存文件由 `stage` 触发的机会式清理回收（TTL 24 小时，`KNOWLEDGE_STAGE_TTL_HOURS`）。

### 准备知识库数据

```bash
# 生成三类知识库数据（产品知识 / 电商 FAQ / 客服对话），
# 输出到 llm_backend/knowledge_data/ 目录
python scripts/generate_product_knowledge.py
python scripts/download_datasets.py
python scripts/download_jddc.py
```

文档通过 `/api/upload` 上传后自动完成解析 → 分块 → Embedding → pgvector 入库（`document_chunks` 表），无需手动建索引。

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/langgraph/query` | POST | Agent 多路由查询（SSE 流式 + 语义缓存） |
| `/api/upload` | POST | 上传文件 -> 解析分块 -> 向量入库（pgvector） |
| `/api/conversations` | POST | 创建会话 |
| `/api/conversations/{id}/messages` | GET | 获取历史消息 |
| `/api/register` | POST | 用户注册 |
| `/api/token` | POST | 登录获取 JWT |

## 项目结构

```
├── llm_backend/                      # 后端服务
│   ├── main.py                       # FastAPI 入口，所有 API 端点
│   ├── app/
│   │   ├── core/                     # 配置、数据库、安全、日志
│   │   ├── services/                 # 业务服务层
│   │   │   ├── llm_factory.py        # LLM 工厂模式（DeepSeek/Ollama）
│   │   │   ├── deepseek_service.py   # DeepSeek API + 语义缓存
│   │   │   ├── redis_semantic_cache.py # Redis 语义缓存
│   │   │   └── indexing_service.py   # 文档解析 → 分块 → 向量入库
│   │   ├── lg_agent/                 # LangGraph 智能体
│   │   │   ├── lg_builder.py         # StateGraph 构建与路由
│   │   │   ├── lg_states.py          # 状态定义（Router/AgentState）
│   │   │   ├── intent_rules.py       # 意图规则判定层（前置，零延迟短路）
│   │   │   └── kg_sub_graph/         # 知识图谱子图
│   │   │       └── agentic_rag_agents/
│   │   │           └── components/
│   │   │               ├── customer_tools/  # 向量检索 + 混合检索
│   │   │               ├── relevance_grader.py  # LLM 相关性评分
│   │   │               └── hybrid_retrieval/    # BM25+向量 RRF 融合
│   │   │   ├── models/                   # SQLAlchemy 数据模型（含 document_chunks 向量表）
│   │   ├── api/                      # 认证路由
│   │   └── prompts/                  # 提示词模板
│   └── frontend/                     # Vue3 SFC 前端工程（npm run build → dist/，主入口 http://127.0.0.1:8000）
├── scripts/                          # 工具脚本
│   ├── generate_product_knowledge.py # 从 CSV 生成产品知识文档
│   ├── download_datasets.py          # 下载开源电商 FAQ 数据集
│   └── download_jddc.py              # 下载 JDDC 客服对话数据集
├── docker-compose.yml                # Docker Compose 配置（仅 postgres/redis）
└── STUDY_NOTES.md                    # 项目学习文档（面试准备）
```

## 设计模式

| 模式 | 应用位置 | 说明 |
|------|---------|------|
| **工厂模式** | `LLMFactory` | 通过 `.env` 配置切换 DeepSeek/Ollama，不改代码 |
| **策略模式** | `config.py` | CHAT/REASON/AGENT 可分别选不同 LLM 服务 |
| **回调模式** | `deepseek_service.py` | `on_complete` 回调触发消息持久化，解耦 LLM 和存储 |
| **状态图** | LangGraph | `StateGraph` + 条件边实现多路由 Agent |

## 数据集说明

本项目内置三类知识库数据（由 `scripts/` 脚本生成，位于 `llm_backend/knowledge_data/`）：

| 数据类型 | 文件数 | 来源 | 说明 |
|---------|--------|------|------|
| 产品知识文档 | 10 | 从项目 CSV 聚合生成 | 涵盖 8 个品类 10 款智能家居产品 |
| 电商 FAQ | 81 | [Chinese-EcomQA](https://huggingface.co/datasets/OpenStellarTeam/Chinese-EcomQA) | 1,800 条品牌/商品/推荐问答 |
| 客服对话 | 85 | [JDDC](https://github.com/SimonJYang/JDDC-Baseline) | 2,600+ 条真实电商客服对话 |

## 许可证

MIT License

## 致谢

本项目基于原项目 [wang219416/GraphRAG-](https://github.com/wang219416/GraphRAG-) 二次开发，在此向原作者 [wang219416](https://github.com/wang219416) 的开源贡献表示感谢。

- [LangGraph](https://github.com/langchain-ai/langgraph) - Agent 编排框架
- [JDDC](https://jddc.jd.com/) - 京东客服对话数据集
- [Chinese-EcomQA](https://huggingface.co/datasets/OpenStellarTeam/Chinese-EcomQA) - 中文电商问答数据集
- [SiliconFlow](https://siliconflow.cn/) - 免费 Embedding API
