# ShopSage 架构对比与优化参考

> ⚠️ 本文撰写于 2026-08-16 Neo4j 退役之前：文中 SmartCS 侧涉及 Neo4j/Text2Cypher/predefined_cypher/实体识别 的描述（如工作流图、bug 记录、健康检查建议）请以历史视角阅读——这些组件已于同日整体移除，当前知识库查询统一走 pgvector 向量检索。

> 本文档服务于 SmartCS-Agent 二次开发：对比分析 `D:\ShopSage` 与本项目，提炼 ShopSage 中可迁移到 SmartCS-Agent 的**架构与功能流程设计亮点**，并映射到本项目当前痛点，给出落地优先级建议。
>
> 分析方法：两份只读源码走读（SmartCS-Agent 约 1.1 万行 Python；ShopSage 全仓库），关键结论已抽样复核。
> 日期：2026-08-16

---

## 一、结论速览

| 维度 | SmartCS-Agent（本项目） | ShopSage |
|---|---|---|
| 定位 | 智能家居电商客服，LLM 智能体 + KG/文档双路 RAG | 电商智能客服，LLM 对话 + 确定性业务编排 + 人工审核闭环 |
| Agent 编排 | LangGraph 0.3.x，双图嵌套 + `Send` map-reduce，**MemorySaver（内存）** | LangGraph 1.x 单图 6 节点 + 纯函数条件路由，**AsyncRedisSaver（持久化）** |
| 业务风险控制 | 无（工具选择交给 LLM，无人工介入） | 规则引擎 + 金额分级人工审核 + 审计快照 |
| 异步任务 | 无队列，同步阻塞操作散落 async 路径 | Celery + 任务级重试 + 超时/进程保护 |
| 数据库迁移 | 无（`init_db.py` 每次启动 `drop_all`，重启即清库） | Alembic 异步迁移 + `alembic check` 一致性校验 |
| 测试 | 零测试（`app/test/` 为一次性脚本，eval 已脱节） | 11 个版本里程碑 + 专项测试（含越权场景） |
| 多租户隔离 | 无（`user_id` 走 query 参数，可伪造） | JWT + `thread_id` 用户前缀 + 数据层强制作用域 |
| 可观测性 | loguru + request_id（扎实），无 metrics | 日志较弱（print），但 /health + 生命周期钩子 |
| 文档工程 | README + PROJECT_ANALYSIS + spec_plan（规格先行，部分过期） | PROJECT_ANALYSIS（29 项问题清单）+ ISSUES_AND_SOLUTIONS 踩坑复盘 |

**一句话结论**：ShopSage 在「Agent 编排深度（多路路由、护栏、混合检索）」上远不如本项目，但在「**工程闭环**」（迁移、测试、任务队列、持久化状态、多租户安全、人机协作）上比本项目完整得多——恰好补足 SmartCS-Agent「设计强、接线弱」的短板。本文亮点清单即围绕这一互补关系展开。

---

## 二、两项目定位与技术栈对比

| 层 | SmartCS-Agent | ShopSage |
|---|---|---|
| Web | FastAPI + SSE（`llm_backend/main.py:105`） | FastAPI + SSE + WebSocket |
| Agent | LangGraph 0.3.25 StateGraph，双图嵌套 | LangGraph 1.x，单图 6 节点 |
| LLM | DeepSeek/Ollama 工厂切换，独立 Vision API | DeepSeek（chat/intent 分组），通义千问 Embedding |
| 检索 | pgvector（BM25+向量 RRF） | PostgreSQL 16 + pgvector（HNSW）余弦 Top5 |
| 缓存/队列 | Redis 语义缓存 + 摘要缓存（**同步客户端**） | Redis 7（redis-stack）+ Celery 5.6 |
| 业务库 | PostgreSQL 16（psycopg + SQLAlchemy async） | PostgreSQL（SQLModel + asyncpg） |
| 迁移 | ❌ 无 | ✅ Alembic 异步迁移（6 个版本） |
| 状态持久化 | MemorySaver（进程内存，重启丢失） | AsyncRedisSaver（thread_id 维度持久化） |
| 认证 | JWT（无 refresh/撤销），大部分端点免鉴权 | JWT 双角色 + 三级认证依赖 |
| 前端 | chat.html | Gradio C 端 7860 / B 端管理台 7861 |
| 部署 | Docker Compose 四服务 + healthcheck | Docker Compose 四服务 + 健康检查串行启动 + start.sh |

---

## 三、架构对比

### 3.1 模块划分

- **SmartCS-Agent**：`main.py` 单体路由（519 行）→ services（工厂/缓存/索引）→ lg_agent 顶层图 → kg_sub_graph 子图（38 个 components 节点）。**问题**：api 层未收敛（仅 auth 用 APIRouter）、请求模型与 schemas 混在 main.py、死代码 20+ 文件散布。
- **ShopSage**：严格四层——`api/v1`（接口）→ `graph`（节点/状态/编排/工具）→ `services`（纯逻辑规则引擎）→ `models`（SQLModel），另配 `tasks`（Celery）/`websocket`/`frontend`。职责边界清晰，**业务规则与 LLM 节点物理隔离**。

### 3.2 请求调用链

**SmartCS-Agent**（`/api/langgraph/query`）：

```
路由 → LoggingMiddleware → analyze_and_route_query（ScopeGuard → LLM 5 路意图+复杂度）
  → graphrag-query: 查询预处理 5 步 → multi_tool 子图
     guardrails → planner(Send map-reduce) → tool_selection → predefined_cypher / customer_tools
     → summarize → final_answer
  → SSE stream_mode="messages"
```

**ShopSage**（`/api/v1/chat`）：

```
JWT → app_graph.astream_events(v2) → intent_router（LLM 四分类，白名单容错）
  → route_intent（纯函数条件路由）
     ORDER → query_order（正则 SN + user_id 强制作用域）
     POLICY → retrieve（pgvector 阈值 0.5 硬过滤）
     REFUND → handle_refund → check_refund_eligibility（金额分级）
               低风险自动通过 / 中高风险 → AuditLog 快照 + Celery 通知 + WS 广播
     OTHER → generate（LLM）
  → SSE 只转发 generate 节点 token；节点直出 answer 时兜底补发
```

**核心差异**：ShopSage 把「LLM 语义理解」与「业务流程执行」分层——路由是纯函数、退款校验是纯 Python 规则引擎、审核是人工节点；SmartCS-Agent 则把工具选择与执行几乎全部交给 LLM，且无任何人工介入与审计通道。

---

## 四、功能流程设计关键差异点

| 关注点 | SmartCS-Agent | ShopSage | 启示 |
|---|---|---|---|
| 意图路由 | LLM 输出 5 字段（type/complexity/...）直接驱动路由 | LLM 只输出 4 分类标签，**白名单校验**（`nodes.py:281-283`），非法值降级 OTHER | LLM 输出驱动路由处必须加枚举白名单 + 安全默认分支 |
| 中间 LLM 输出是否污染用户流 | `stream_mode="messages"` 需逐类过滤 tool_calls/标签（`main.py:402-420`），易漏 | `astream_events` 按 `langgraph_node == "generate"` 白名单过滤（`chat.py:59-61`） | 节点级白名单过滤 + 最终状态兜底补发，更稳 |
| 确定性结论 vs LLM 生成 | 无此机制 | 节点直出 answer 时**短路 generate**（`workflow.py:26-40`），防止 LLM 覆盖规则结论 | 凡代码可确定的回复不再过 LLM |
| 会话状态 | MemorySaver 内存，重启丢全部对话 | AsyncRedisSaver 持久化，thread_id 恢复 | 生产必须换持久化 checkpointer |
| 资金/权限副作用 | 无 | 金额分级（≥2000 HIGH / ≥500 MEDIUM 进配置）→ 中高风险人工审核 + context_snapshot 审计 | 高风险动作必须人机闭环 + 可追溯 |
| 异步耗时操作 | 同步阻塞散落 async 路径（索引、Neo4j、搜索） | Celery 任务 + 任务级重试 + time_limit | 引入任务队列 |
| 租户隔离 | `user_id` 走 query 参数，可伪造 | JWT + `thread_id = f"{user_id}_{tid}"` 前缀契约 + 数据层强制过滤 | 全链路强制作用域 |

---

## 五、ShopSage 可迁移亮点清单（核心章节）

> 格式：每项含【亮点】【证据】【解决了什么】【SmartCS 如何借鉴】【优先级】。
> 优先级：🟢 直接抄（低成本高价值）/ 🟡 抄但需规避已知坑 / 🔵 中期演进。

### A. Agent 编排与 LLM 调用工程

**A1. LLM 意图路由 + 确定性图编排的混合架构** 🟢
- 证据：`D:\ShopSage\app\graph\nodes.py:268-286`（intent_router 仅输出四类标签）、`workflow.py:14-23`（route_intent 纯函数）、`app\services\refund_service.py:40`（"纯 Python 硬逻辑，不依赖 LLM"）
- 解决了什么：LLM 只做语义理解，业务流程由代码状态机强制执行，杜绝 LLM 幻觉直接操作业务数据。
- SmartCS 借鉴：当前 `tool_selection/node.py` 完全由 LLM bind_tools 决定执行路径。建议把「工具调用权限」「金额/权限/时效类规则」下沉为确定性节点，LLM 动作必须落到显式声明的图节点边界。

**A2. 节点直出 answer 短路 generate** 🟢
- 证据：`workflow.py:26-40`（`audit_required` 或已有 `answer` 则直达 END）——ShopSage 对"LLM 覆盖审核结论"P0 问题的修复。
- 解决了什么：确定性结论不被 LLM 二次生成改写，既正确又省钱。
- SmartCS 借鉴：本项目 `final_answer` 后无此类短路；凡 pre-check（ScopeGuard 降级、护栏拦截）产出的答复应直接进最终输出，不再过生成节点。

**A3. SSE 节点级白名单过滤 + 兜底补发** 🟢
- 证据：`chat.py:59-61`（`metadata.langgraph_node != "generate"` 则 continue）、`chat.py:78-80`（`not streamed_any and final_answer` 时补发完整回答）
- 解决了什么：意图识别等中间 LLM 调用的 token 不污染用户流；非 LLM 路径前端不会收到空气泡。
- SmartCS 借鉴：本项目 `main.py:402-420` 用「过滤特定类型消息」的黑名单方式，新增节点类型即可能漏滤。改为按目标节点白名单过滤 + `graph.get_state` 兜底补发更稳。同时注意本项目 `main.py:417` 对 `StateSnapshot` 做 `len()`/下标访问疑似类型错误，改造时一并修复。

**A4. 意图识别与生成使用独立模型配置** 🟢
- 证据：`D:\ShopSage\app\core\config.py:45-48`（INTENT_MODEL / INTENT_TEMPERATURE 独立配置，注释"可换成成本更低/更快的模型"）、`nodes.py:103-116`
- 解决了什么：分类任务用小/快/便宜模型，生成任务用强模型，成本延迟分别调优。
- SmartCS 借鉴：本项目已有 CHAT/REASON/AGENT 三路 Settings（`config.py`），但图内 6 处节点各自 new ChatDeepSeek（`lg_builder.py:84-89,158-161,...`）。建议：提取 `get_agent_llm(tag)` 工厂 + 意图路由单独配轻量模型。

**A5. LLM 输出白名单容错** 🟢
- 证据：`nodes.py:281-283`（`if intent not in [...] : intent = "OTHER"`）
- 解决了什么：LLM 输出不可信，非白名单值强制降级到安全分支。
- SmartCS 借鉴：本项目 `Router` 已用 `with_structured_output`，但下游（tool_selection、summarize 的 JSON 解析）建议同样加枚举校验 + 默认分支，避免解析异常中断 SSE。

**A6. RAG 三重防幻觉（阈值硬过滤 + prompt 约束 + temperature=0）** 🟢
- 证据：`nodes.py:25,168`（SIMILARITY_THRESHOLD=0.5 硬过滤）、`nodes.py:119-132,178-186`（"context 为空直接回答暂无规定，严禁编造"）、`models/knowledge.py:16-23`（HNSW m=16, ef_construction=64）
- 解决了什么：检索结果低于阈值直接不注入，prompt 层再兜底，三层防线防幻觉。
- SmartCS 借鉴：本项目 customer_tools 已有 LLM 相关性评分，但建议在注入前同样加**硬阈值**（目前仅靠 LLM 判断），并统一 prompt 中"仅依据检索上下文"的约束。

**A7. 图状态契约显式声明 + JSON 序列化边界收口** 🟡
- 证据：`D:\ShopSage\app\graph\state.py:39`（refund_data 注释：必须声明在 schema，否则新版 LangGraph 静默丢弃）、`nodes.py:28-41`（order_to_json_dict 单点转换 Numeric→float、datetime→ISO）
- 解决了什么：依赖升级导致状态 key 被静默丢弃、Decimal/datetime 序列化崩溃两类隐性耦合（ISSUES_AND_SOLUTIONS 问题 4）。
- SmartCS 借鉴：本项目状态分散在 `lg_states.py` 与 `components/state.py` 两套，且存在重复定义（`CypherHistoryRecord` 两份）。建议收敛状态契约 + 统一序列化转换函数；升级 LangGraph 版本时尤其注意。

### B. 人机协作 / 审计（SmartCS 完全缺失的能力）

**B1. 风险分级 + 完整上下文快照审计** 🔵
- 证据：`nodes.py:469-477`（金额分级）、`nodes.py:511-517`（context_snapshot = 问题+refund_data+order_data+history 全量快照）、`models/audit.py:63-75`（admin_id/admin_comment/reviewed_at + JSON 快照列）、阈值进配置 `config.py:87-88`
- 解决了什么：审核员决策时可回放完整上下文，决策过程可追溯。
- SmartCS 借鉴：本项目虽无资金操作，但可把此模式用于**高风险动作**（如 Cypher 写操作放行、外部搜索触发、订单类查询）：建 audit 表 + 决策上下文快照 + 审批人字段。CypherSafetyValidator 拦截写操作后「拒绝/人工放行」即可复用该闭环。

**B2. 并发审批状态机保护** 🟡
- 证据：`D:\ShopSage\app\api\v1\admin.py:118-122`（`action != PENDING` 则 400 "already been processed"）
- 解决了什么：已处理任务拒绝二次决策。注意 ShopSage 无行锁仍有竞态（PROJECT_ANALYSIS §6.2 #15）。
- SmartCS 借鉴：直接上「读-判-写前检查状态 + `SELECT ... FOR UPDATE`」的完整版。

### C. 异步任务 / Celery

**C1. 任务级差异化重试策略** 🟢
- 证据：`D:\ShopSage\app\tasks\refund_tasks.py:27-33,65-71,124-129`（短信 max_retries=3/delay=60，支付 3/120，通知 2）
- 解决了什么：不同任务对失败敏感度不同，重试节奏按业务定。
- SmartCS 借鉴：本项目 `/api/upload` 的解析/分块/入库为同步阻塞（`indexing_service.py:81-142`），大文件卡 worker。引入 Celery 后：索引任务（重试 3 次、长间隔）、通知类（轻量）、LLM 回调类各配独立策略。

**C2. Celery 全局超时与进程保护** 🟢
- 证据：`D:\ShopSage\app\celery_app.py:17-28`（JSON 序列化、task_time_limit=300、soft=240、worker_max_tasks_per_child=1000、prefetch=4）
- 解决了什么：LLM/外部网关调用可能挂死 worker；子进程限任务数防内存泄漏。
- SmartCS 借鉴：LLM 场景必须 time_limit（外部 API 不可控）。

**C3. Celery 注册规避 autodiscover 陷阱 + 显式任务名** 🟡
- 证据：`celery_app.py:31-37`（模块底部显式 import 注册 + `# noqa: F401`）、`refund_tasks.py` 每任务显式 `name=`
- 解决了什么：注册不依赖文件名约定、不产生 import 循环；重命名函数不破坏 broker 消息。
- SmartCS 借鉴：建 Celery 时照抄该模式；Windows 开发环境用 `pool='solo'`（`celery_worker.py:6-16` 自适应）。

**C4. 任务只传业务 ID，不传对象** 🟢
- 证据：`refund_tasks.py` 任务签名仅 refund_id / audit_log_id
- 解决了什么：消息体小、幂等友好、重试安全。

### D. 数据模型 / 多租户安全（SmartCS 最薄弱环节）

**D1. 全链路 user_id 作用域 + thread_id 前缀契约** 🟢（最高优先级）
- 证据：`chat.py:35`（`thread_id = f"{current_user_id}_{request.thread_id}"`）、`status.py:38`、`websocket.py:31`（三处统一前缀）、`nodes.py:311-313`（订单查询强制 `Order.user_id == user_id`）
- 解决了什么：横向越权被数据层强制拦截；不同用户同 thread_id 不串会话（曾因前缀不统一出 P0，ISSUES_AND_SOLUTIONS 问题 7）。
- SmartCS 借鉴：本项目 `/api/conversations/{id}/messages` 用 query 参数传 user_id 归属校验（`main.py:260`）可被任意伪造；`/api/chat`、`/api/search`、`/api/upload` 完全免鉴权。建议：JWT 依赖注入 user_id → thread_id 加前缀 → 所有数据访问节点强制作用域过滤，三处契约统一。

**D2. 外键 RESTRICT + 枚举状态机** 🟢
- 证据：`models/refund.py:34-37`（`ondelete="RESTRICT"`）、`models/order.py:9-14`（OrderStatus 枚举）
- SmartCS 借鉴：涉及资金/核心业务表外键一律 RESTRICT；状态字段用 str Enum。

**D3. 向量维度从配置读取 + 索引参数模型层声明** 🟡
- 证据：`models/knowledge.py:16-23,32`（`Vector(settings.EMBEDDING_DIM)`、HNSW 参数在 Index 声明）
- SmartCS 借鉴：本项目 embedding 维度（1024）散落各处，建议收敛到 settings 单点。

### E. 数据库迁移（SmartCS 当前最大工程隐患）

**E1. Alembic 异步迁移 + URL 自动注入** 🟢
- 证据：`D:\ShopSage\migrations\env.py:32-41,74-85`（get_url 从 Settings 取、`postgresql://`→`postgresql+asyncpg://` 自动替换、在线迁移）
- 解决了什么：迁移无需手传 URL，兼容同步/异步驱动。
- SmartCS 借鉴：本项目 `init_db.py:24-27` 每次启动 `drop_all` + Dockerfile CMD 每次启动执行 → **重启即清库**（`llm_backend/scripts/init_db.py`、`Dockerfile:31`）。应改为：init_db 仅建表当不存在（或只建扩展），表结构演进交给 Alembic。可直接照抄 ShopSage 的 env.py（改为 psycopg 协议）。

**E2. env.py 显式导入全部模型 + compare_type=True** 🟢
- 证据：`migrations/env.py:14-19,66`
- 解决了什么：autogenerate 不漏模型、不忽略类型变化；上线前跑 `alembic check` 校验模型 vs 库一致性（ShopSage 曾因漏迁移崩溃整条链路，ISSUES 问题 3）。

### F. 配置管理

**F1. computed_field 派生 URL** 🟢
- 证据：`D:\ShopSage\app\core\config.py:16-27,33-40,74-84`（DATABASE_URL/REDIS_URL 由原子值 build 派生，CELERY_BROKER 默认回退 REDIS_URL）
- 解决了什么：环境变量只存原子值，组合派生计算，一处改处处生效。
- SmartCS 借鉴：本项目 `config.py` 已集中但存在重复（经营范围字符串在 `lg_builder.py:211-221` 与 `:471-481` 两份；SECRET_KEY 默认 `"your-secret-key"`）。照抄派生模式 + 清理重复。

**F2. 业务阈值全部进配置** 🟢
- 证据：`config.py:87-95`（HIGH_RISK_REFUND_AMOUNT=2000 等）
- 解决了什么：改策略不动代码。

### G. 部署运维

**G1. 健康检查串行启动** 🟢
- 证据：`D:\ShopSage\docker-compose.yaml:15-19,30-34,41-45,59-65`（pg_isready / redis-cli ping / depends_on `condition: service_healthy`）
- 解决了什么：容器启动顺序由健康检查驱动而非裸等待。
- SmartCS 借鉴：本项目 compose 已有 healthcheck 雏形，但 Neo4j healthcheck 用 `wget`（`docker-compose.yml:46`）——neo4j:5-community 镜像默认未必带 wget，需改为 `cypher-shell` 或 curl。

**G2. lifespan 启动钩子：重型资源启动期完成，失败即失败** 🟢
- 证据：`D:\ShopSage\app\main.py:24-32`（init_db + compile_app_graph）、`main.py:61-76`（/health 返回 status/version/features）
- SmartCS 借鉴：本项目图在请求期编译/每请求重建客户端（`main.py:103` 每请求 `LLMFactory.create_chat_service()`）。建议：图、LLM 客户端、Embedding、缓存全部在 lifespan 单例化，/health 输出版本与依赖连通性。

**G3. Windows UTF-8 控制台防护** 🟢
- 证据：`app/main.py:4-8`（GBK 控制台 reconfigure UTF-8，防 emoji 日志 UnicodeEncodeError）
- 解决：Windows 中文环境日志崩溃提前规避。SmartCS-Agent 同样在 Windows 开发（本项目即 Windows 11），直接照抄。

### H. 数据工程

**H1. 知识库 ETL 生产级骨架** 🟢
- 证据：`D:\ShopSage\scripts\etl_policy.py:32-64`（按后缀选加载器、中文标点切分 500/50）、`:42-45`（tenacity 重试 3 次指数退避）、`:79-123`（50 条/批 Embedding、100 条/批入库、空文本过滤、分批 commit）、`:71-75`（按 source 幂等清理重灌）
- 解决了什么：批量防 API 超时、防网络抖动、可重复执行。
- SmartCS 借鉴：本项目 `indexing_service.py` 无重试、无分批、无幂等。改造 `/api/upload`（移入 Celery 后）可直接复用该骨架。

**H2. 造数脚本分级（开发/压测）** 🟡
- 证据：`scripts/seed_data.py`（3 用户 + 越权测试场景）、`seed_large_data.py`（200 用户/500 订单，可重复跑）
- 借鉴：小数据验证功能、大数据验证性能；造数脚本内置越权用例。

### I. 测试组织

**I1. 版本里程碑测试 + 专项测试分层** 🟢（高价值）
- 证据：`D:\ShopSage\test\test_v1_agent.py` ~ `test_v4_complete.py`（按功能版本演进沉淀）、`test_refund_rules.py`（规则专项）、`test_users.py:171` 起（横向越权）、`test_infra.py`（连通性）
- 解决了什么：每个里程碑可独立验收回归，测试文件即"可运行的验收文档"。
- SmartCS 借鉴：本项目零测试。建议按「v1 对话链路 → v2 检索 → v3 多工具 → v4 流式/中断」组织验收测试；优先补**关键链路冒烟**（路由→检索→回答）与**已确认 bug 的回归用例**（如 tool_selection 空结果分支）。

**I2. pytest-asyncio 会话级 loop** 🟡
- 证据：`D:\ShopSage\pyproject.toml:44-46`（`asyncio_default_test_loop_scope = "session"`）；dev 依赖与运行时依赖分离（`:36-42`）
- 解决：异步引擎/图在多个 loop 间重建问题。

### J. 文档工程

**J1. 「深度分析 + 踩坑复盘」双文档体制** 🟢
- 证据：`D:\ShopSage\PROJECT_ANALYSIS.md`（29 项分级问题清单 P0-P2 + 修复路线）、`ISSUES_AND_SOLUTIONS.md`（现象→排查→根因→解决→可展开点统一格式）
- SmartCS 借鉴：本项目已有 PROJECT_ANALYSIS.md（部分过期）与 spec_plan（规格先行，优秀）。建议补 ISSUES_AND_SOLUTIONS 踩坑记录（如本项目已踩的：`REDIS_CACHE_THRESHOLD` 默认值与 README 不一致、predefined_cypher 空参数 bug）。

---

## 六、落地优先级路线图（结合本项目痛点）

### P0 — 先修必炸的运行期问题（与 ShopSage 无关，但阻塞一切优化）
1. `/chat-rag` 端点引用不存在的 `RAGChatService`（`main.py:226`，已 grep 确认无定义）→ 删除端点或补实现。
2. `tool_selection` 无工具可选时 `Send("error_tool_selection")`（`tool_selection/node.py:126-137`）但节点从未 `add_node`（`multi_tool.py:107-113`，已确认）→ 补节点或改跳转。
3. `predefined_cypher` 快路径空参数恒失败（`lg_builder.py:444-446` 传 `query_parameters:{}`，`predefined_cypher/node.py:53-57` 用 `params.get("query")` 查表）→ 修复参数链路。
4. `main.py:417` 对 `StateSnapshot` 做 `len()`/下标访问的疑似类型错误 → 改用 `.values`。
5. Dockerfile 启动即 `init_db` drop_all（`scripts/init_db.py:24-27`）→ **重启即清库**，改为仅建缺失表/交给迁移。

### P1 — 工程闭环补课（按 ShopSage 模式，逐项对应亮点编号）
1. **状态持久化**：MemorySaver → AsyncRedisSaver（对应 ShopSage 会话持久化模式；本项目 spec_plan/SPEC_CONTEXT_ENGINEERING.md 已有规划，落地即可）。
2. **多租户安全**：JWT 强制注入 user_id + thread_id 前缀 + 数据层作用域（亮点 D1）——最高性价比的安全改造。
3. **数据库迁移**：引入 Alembic 异步 env（照抄 E1/E2，MySQL 改 psycopg 协议），废除 drop_all。
4. **异步任务队列**：Celery 接管 `/api/upload` 索引与耗时操作（C1/C2/C3），LLM 调用配 time_limit。
5. **SSE 事件过滤重构**：按节点白名单 + 兜底补发（A3），顺带修复 main.py:417。
6. **测试起步**：pytest + 版本里程碑测试（I1），先补关键链路冒烟与 P0 bug 回归。

### P2 — 架构优化（中期，成本较高）
1. **人机闭环**：audit 表 + 上下文快照（B1），用于 Cypher 写操作放行/高风险查询审批。
2. **意图模型拆分**：路由用轻量模型、生成用强模型（A4），并清掉 6 处重复的 LLM 实例化代码。
3. **确定性短路**：护栏/预检产出直接出答案，不过生成节点（A2）。
4. **ETL 生产化**：`indexing_service` 重试/分批/幂等改造（H1）。
5. **metrics/追踪**：Prometheus 指标（缓存命中率、LLM token、延迟）+ 可选 OpenTelemetry——ShopSage 此块也弱，本项目 loguru 底子更好，可领先一步。
6. **清理死代码**：约 20+ 文件已定义未接线（check_hallucinations、validate_final_answer、visualization、evaluation、BGEReranker、MaxIterationGuard 等），同步修正 README 与代码脱节处。

---

## 七、不要抄的反面清单

ShopSage 本身也有工程欠账（其 PROJECT_ANALYSIS 29 项问题清单），迁移时需注意：

1. **print 日志**：ShopSage 缺结构化日志/追踪——本项目 loguru + request_id 体系更优，不要倒退。
2. **tools.py 与 nodes.py 双实现退货逻辑**：同一业务两套实现必然漂移——SmartCS 也要警惕 `kg_sub_graph/planner/planner_node.py` 与 `components/planner/node.py` 的重复。
3. **WS 连接池纯进程内存**：多实例丢推送，升级方向 Redis Pub/Sub。
4. **admin API 实际未用 get_admin_user_id**（`admin.py:51,93` 的 TODO）：SmartCS 若做管理端，鉴权要真正挂上。
5. **CORS 全放开**：与 SmartCS 现状（`allow_origins=["*"] + allow_credentials=True`）同病，双方都要改白名单。
6. **Celery 内用废弃的 `asyncio.get_event_loop()`**：迁移时用 `asyncio.run()` 或独立事件循环。
7. **审批并发无行锁**：直接上 `SELECT ... FOR UPDATE` 完整版。

---

## 八、附录：关键文件对照表

| 关注点 | SmartCS-Agent | ShopSage |
|---|---|---|
| 应用入口 | `llm_backend/main.py`（519 行单体路由） | `app/main.py`（lifespan + /health） |
| 配置 | `app/core/config.py`（Pydantic Settings） | `app/core/config.py`（computed_field 派生 URL） |
| Agent 图 | `app/lg_agent/lg_builder.py`（顶层图）、`kg_sub_graph/.../multi_tool.py`（子图） | `app/graph/workflow.py` + `nodes.py` + `state.py` |
| 业务规则 | ❌ 无独立层 | `app/services/refund_service.py`（纯逻辑规则引擎） |
| 异步任务 | ❌ 无 | `app/tasks/refund_tasks.py` + `app/celery_app.py` + `celery_worker.py` |
| 数据库迁移 | ❌ `scripts/init_db.py`（drop_all） | `migrations/env.py`（异步 Alembic）+ 6 个 versions |
| 认证 | `app/api/auth.py` + `core/security.py`（大部分端点免鉴权） | `app/core/security.py`（三级认证依赖） |
| 审计 | ❌ 无 | `app/models/audit.py`（JSON 快照列） |
| WS/状态同步 | ❌ 无（SSE + resume 轮询雏形） | `app/websocket/manager.py` + `api/v1/status.py`（双通道） |
| ETL | `app/services/indexing_service.py`（无重试/分批/幂等） | `scripts/etl_policy.py`（生产级骨架） |
| 测试 | ❌ 零测试 | `test/` 11 个版本里程碑 + 专项测试 |
| 文档 | `docs/PROJECT_ANALYSIS.md` + `spec_plan/`（规格先行，优秀） | `PROJECT_ANALYSIS.md`（问题清单）+ `ISSUES_AND_SOLUTIONS.md`（踩坑复盘） |

---

## 附：本分析引用的关键证据速查

- SmartCS-Agent 痛点证据：`llm_backend/main.py:226`（RAGChatService 未定义，已 grep 确认）、`main.py:417`（StateSnapshot 下标访问）、`scripts/init_db.py:24-27`（drop_all）、`tool_selection/node.py:126-137` vs `multi_tool.py:107-113`（Send 到未注册节点，已确认）、`lg_builder.py:444-446` + `predefined_cypher/node.py:53-57`（快路径空参数）、`redis_semantic_cache.py:27,36,143-157`（同步客户端/任务泄漏/O(N) 扫描）、`customer_tools/node.py:126-151`（每请求重建模型）、`lg_builder.py:84-89,158-161,195-198,386-389,429-432,581-584`（LLM 选择 if/else 复制 6 份）、`main.py:47-53`（CORS 风险配置）、`main.py:260`（user_id query 参数伪造）。
- ShopSage 亮点证据：见第五节各条目所附 `D:\ShopSage\...` 路径。
