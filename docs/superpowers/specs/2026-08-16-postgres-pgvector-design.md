# 设计文档：技术选型迁移 PostgreSQL + pgvector

> 日期：2026-08-16 ｜ 状态：✅ 已实施并验证

## 1. 目标

将项目技术选型统一到 PostgreSQL 生态：

- 关系数据库：**MySQL 8.0 → PostgreSQL 16**（官方 `pgvector/pgvector:pg16` Docker 镜像）
- 向量库：**ChromaDB（本地持久化）→ pgvector**（与业务库共用同一 PostgreSQL 实例）
- 部署：PostgreSQL(pgvector) 与 Redis 一起跑在 Docker 中，FastAPI 应用本地/容器均可运行
- 附加：LangGraph checkpointer 从 MemorySaver 切换为 **PostgresSaver**（会话状态持久化，重启不丢失）

**决策依据（假设）**：开发期项目，`init_db.py` 本为 drop_all+create_all，`vector_db/` 磁盘上尚未建索引 → 全新开始，不写 MySQL→PostgreSQL 数据迁移脚本；Neo4j 不动。

## 2. 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 驱动 | **psycopg v3**（`psycopg[binary,pool]`） | SQLAlchemy 异步（`postgresql+psycopg://`）与 PostgresSaver（psycopg_pool）共用同一驱动，只加一个依赖 |
| 向量镜像 | `pgvector/pgvector:pg16` | 官方镜像，一条 compose 服务替代 ChromaDB + MySQL 两个存储 |
| 向量表 | `document_chunks`（SQLAlchemy `Vector(1024)` 列） | 元数据列与 Chroma metadata 一一对应（source/file_path/user_id/chunk_index），检索用 `cosine_distance` |
| 索引 | HNSW `vector_cosine_ops`（init_db 中创建） | 对齐 SHOP_SAGE_ANALYSIS 的目标形态 |
| Checkpointer | `AsyncPostgresSaver`（aio）+ `AsyncConnectionPool` | langgraph 0.3.25 异步循环直接调用 `aget_tuple`，sync saver 无异步桥接；saver 须在事件循环内构造 → graph 延迟编译（lifespan 中初始化） |
| 扩展创建 | init_db 先 `CREATE EXTENSION IF NOT EXISTS vector` 再建表 | Vector 类型在 DDL 前必须存在 |

## 3. 主要改动清单

- `pyproject.toml` / `uv.lock`：移除 aiomysql、chromadb、langgraph-checkpoint-sqlite；新增 psycopg[binary,pool]、pgvector、langgraph-checkpoint-postgres（192 → 161 包）
- `app/core/config.py`：`DATABASE_URL` 改 `postgresql+psycopg://`，新增 `POSTGRES_DSN`（供 psycopg_pool），`VECTOR_DB_PATH/VECTOR_DB_COLLECTION/CHROMADB_ANONYMIZED_TELEMETRY` → `VECTOR_TABLE_NAME`
- `app/core/database.py`：Windows 下全局设置 `WindowsSelectorEventLoopPolicy`（psycopg 拒绝 ProactorEventLoop）
- `app/models/document_chunk.py`（新）：向量表模型
- `app/scripts/init_db.py`：扩展 + 建表 + HNSW 索引
- `app/services/indexing_service.py`：写入侧 ChromaDB → SQLAlchemy 批量插入
- `customer_tools/node.py`：`VectorStoreQuery` 查询侧改 pgvector（异步 session + cosine_distance Top-K），`search/get_all_documents` 变 async
- `app/lg_agent/lg_builder.py`：MemorySaver → AsyncPostgresSaver + 延迟编译 graph（`_LazyGraph` 代理 + `init_checkpointer/close_checkpointer`）
- `main.py`：FastAPI lifespan 挂接检查点初始化；两处同步 `get_state` → `await aget_state`
- `app/lg_agent/main.py`（CLI 测试入口）：同步更新
- `run.py`：Windows 下给 uvicorn loop 工厂打补丁（SelectorEventLoop）
- `docker-compose.yml`：mysql 服务 → `pgvector/pgvector:pg16`（`pg_isready` healthcheck、`pg_data` 卷、app 环境变量与 depends_on 同步）
- `.env` / `.env.docker` / `.dockerignore` / `.gitignore` / `chat.html` 注释同步
- 文档：README（根 + llm_backend）、PROJECT_ANALYSIS、OPTIMIZATION_QA、CLAUDE_WORKFLOW_MIRROR、SHOP_SAGE_ANALYSIS、STUDY_NOTES 中 MySQL/ChromaDB 描述全部同步

## 4. 验证结果（2026-08-16 实测）

1. `docker compose up -d postgres redis` → 两容器 healthy
2. `python -m scripts.init_db` → 4 张业务表 + `vector` 扩展 + HNSW 索引创建成功（psql 确认）
3. 端到端向量验证：随机 1024 维向量写入/余弦 Top-K 查询，自身相似度 = 1.000
4. uvicorn 启动（含 loop 补丁）→ `/health` 200，lifespan 自动创建 checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_migrations 4 张检查点表
5. Redis 语义缓存/摘要缓存逻辑零改动（纯 Redis，不受迁移影响）

## 5. 已知风险与后续

- `init_db.py` 仍保留 drop_all 语义（容器每次启动清业务表；检查点表不受影响）——生产化应改 create-only + Alembic（SHOP_SAGE_ANALYSIS 已有建议）
- 本地 Windows 开发必须经 `run.py` 启动（loop 补丁）；Docker/Linux 无此问题
- 混合检索仍走「全量拉取语料本地编码」的旧路径，后续可改为直接复用 pgvector 已存向量，省一次全量编码
- 本地直接 `uvicorn main:app` 不经过 run.py 补丁，Windows 下会报 ProactorEventLoop 错误——README 已注明用 run.py 启动
