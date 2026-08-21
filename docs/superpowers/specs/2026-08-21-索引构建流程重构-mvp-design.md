# 设计文档：索引构建流程重构(MVP,迁移自 Knowledge_rag_system)

> 日期：2026-08-21 ｜ 状态：待审核
> 参考：[docs/索引链路核查问题清单.md](../../索引链路核查问题清单.md)（现状 10 项问题）

## 1. 目标

基于外部项目 [Knowledge_rag_system](https://github.com/blowt16/Knowledge_rag_system.git) 的索引构建实践,重构本项目"上传 → 解析 → 清洗 → 分块 → 向量化 → 入库"链路,修复核查清单中的 P0/P1/P2 问题。

**场景约束(电商客服)**:语料为商家商品文档(智能家具产品知识、说明书),文件量级小(单文件 KB~MB 级)、格式固定(txt/md/pdf/docx)、由运营人员上传,无需多租户隔离、无需批量导入、无需文档级权限。

**原则**:只迁移对当前场景有实际收益、且实现简单的部分;外部仓库的复杂能力(多模态 PDF、语义合并、异步队列、zip 批量)明确排除,列入"不迁移清单"防止未来误加。

## 2. 迁移取舍总览

| 外部仓库能力 | 决策 | 理由 |
|---|---|---|
| 文件级 MD5 去重(md5_manager) | ✅ 迁移 | 直接修复 P2-6(重复上传);实现简单 |
| 文件校验:扩展名白名单 + 大小限制 | ✅ 迁移 | 直接修复 P2-7;纯配置 |
| 编码降级链 txt: utf-8→gbk→gb2312→latin-1 | ✅ 迁移 | 直接修复 P2-9;约 10 行 |
| docx 段落+表格提取 | ✅ 迁移 | 直接修复 P2-8(商品参数常放表格);用现有 python-docx 实现,零新依赖 |
| 清洗流水线:控制字符/空白/页码/目录行 | ✅ 迁移(开关控制) | 修复"清洗过弱";全部为纯函数,独立可测 |
| chunk 元数据注入(md5/file_type/filename) | ✅ 迁移 | 修复"零元数据"(P1-5);支撑按文件删除/列表 |
| 批量嵌入指数退避重试 | ✅ 迁移(简化) | 嵌入 API 瞬断是 8/20 PDF 未入库的疑似主因;3 次退避即可 |
| 原子入库(外仓库:写库→记 MD5→失败手动回滚) | ✅ 迁移(简化) | 本项目 PG 单事务天然原子,比外仓库手动回滚更简单 |
| 失败诊断(外仓库:魔数表+用户提示) | 🔶 简化迁移 | 只做空文件/解析失败/格式不支持三类分类;魔数表对来源可控的运营上传过度设计 |
| 文件列表/删除接口 | ✅ 迁移(新增) | 基于 documents 表实现,运营可删除错误上传 |
| 商品知识种子导入脚本 | ✅ 新增(外仓库无此能力) | 修复 P0-1(语料无自动导入入口),本项目独有需求 |
| init_db 幂等化(去 drop_all) | ✅ 新增(外仓库无此能力) | 修复 P0-2(初始化即清库) |
| PDF 多模态/视觉理解(MinerU 流水线、VL 图片描述) | ❌ 排除 | 重依赖(视觉 API+图片存储+哈希去重),商品手册 MVP 无此需求 |
| 语义合并(SentenceTransformer 二次嵌入) | ❌ 排除 | 外仓库默认关闭;增加本地模型依赖,收益不明确 |
| SSE 异步进度(single_upload_tracker) | ❌ 排除 | 单文件同步上传对运营场景足够;是 zip 批量场景的配套 |
| zip 批量导入 | ❌ 排除 | 未要求 |
| MIME 魔数检测(python-magic) | ❌ 排除 | 新依赖;扩展名+大小校验已覆盖风险 |
| chunk 批量缓冲池(跨文件双阈值) | ❌ 排除 | 单文件处理+PG 事务已覆盖;外仓库的缓冲是为了批量场景 |
| 图片提取/imagehash | ❌ 排除 | 同多模态 |
| pptx 支持(需 python-pptx 依赖) | ❌ 排除 | 商品文档极少用 pptx;白名单不含,后续可加 |

## 3. 目标架构

```
POST /api/upload
  1. 校验: 扩展名白名单 + 大小上限(env 配置)           → 400
  2. MD5 计算 → documents 表查重(同 user_id)            → 命中返回 duplicate,跳过
  3. 解析: pdf→PyMuPDF / docx→段落+表格 / txt,md→编码降级链
  4. 清洗(TEXT_CLEAN_ENABLED 开关):
     控制字符清理 → 空白规范化 → 页码行清理 → 目录行清理
  5. 分块: RecursiveCharacterTextSplitter(500/50, 不变)
     + 过滤 < CHUNK_MIN_SIZE 的噪声块
  6. Embedding: 分批 10 条 + 指数退避重试 3 次 + 全零检测
  7. 单事务原子写入: documents(文件记录) + document_chunks(块)
  8. 返回 success / duplicate / failed(含错误分类与原因)

GET  /api/documents?user_id=      文档列表(来自 documents 表)
DELETE /api/documents/{md5}?user_id=  按文件删除(chunks + 记录,同事务)
```

## 4. 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 去重粒度与存储 | 文件级 MD5;存 PostgreSQL `documents` 表 | 外仓库用 JSONL(md5_store),本项目已有 PG,表存储天然支持事务原子与查询 |
| 去重触发时机 | 解析前(先 MD5 后解析) | 命中即跳过,省解析/嵌入成本(与外仓库一致) |
| 原子性实现 | `documents` + `document_chunks` 同一 SQLAlchemy 事务 | 外仓库需"写库→记 MD5→失败手动回滚"三步,PG 事务一步完成,更简单 |
| PDF 解析器 | PyPDF2 → PyMuPDF(fitz) | 外仓库实践:提取能力显著强于 PyPDF2;商品说明书主力格式;单函数内替换 |
| docx 提取 | python-docx 直接读 paragraphs + tables | 零新依赖(Docx2txtLoader 需 langchain-community);段落+表格按原序拼接 |
| 删除语义 | `DELETE /documents/{md5}` 级联删 chunks | 外仓库三层联动(向量→MD5→图片)在 PG 下简化为级联删除 |
| 元数据列 | document_chunks 增加 `md5`、`file_type`;新增 documents 表 | 支撑去重、按文件删除、文档列表;存量数据列可空,不阻塞 |
| 重试 | `embed_in_batches` 内加 3 次指数退避([1,2,4]s) | 瞬断自愈;超过即返回失败+明确错误,不做自动重试队列 |
| 配置入口 | 全部入 `.env`(settings 增加字段) | 遵循本项目"配置统一入 env"的既有约定(见近期 commit 系列) |

## 5. 主要改动清单

### 数据模型
- 新增 `app/models/document.py`:`documents` 表(id, md5 唯一, original_filename, user_id, file_type, chunk_count, status, error, created_at)
- `app/models/document_chunk.py`:增加 `md5`(String(32), 可空)、`file_type`(String(20), 可空)列

### 索引服务(重构 indexing_service.py)
- `_parse_document`:按类型分发 → pdf(PyMuPDF)/docx(段落+表格)/txt、md(编码降级链)
- `_clean_text`:升级为开关控制的多步流水线(控制字符、空白、页码行、目录行)
- `process_file` 重排:校验 → MD5 查重 → 解析 → 清洗 → 分块(min_size 过滤)→ 嵌入(退避重试)→ 单事务入库
- 新增:空文件检测、错误分类返回(empty_file / parse_error / unsupported / embedding_failed / duplicate)

### API(扩展 main.py)
- `/api/upload`:返回增加 `status`(success/duplicate/failed)与 `md5`;校验失败 400
- 新增 `GET /api/documents`、`DELETE /api/documents/{md5}`

### 种子导入脚本(新增)
- `llm_backend/scripts/ingest_knowledge.py`:遍历 `knowledge_data/` 目录,调用 IndexingService 批量入库(解决 P0-1,当前 10 条语料唯一来源是丢失的一次性操作,必须有可复现脚本)

### 初始化脚本(修复 P0-2)
- `scripts/init_db.py`:移除 `drop_all`,改为幂等建表(`CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`),扩展/索引语句不变

### 配置(.env + config.py)
- `MAX_FILE_SIZE=30`(MB)、`ALLOWED_FILE_TYPES=txt,md,pdf,docx`、`TEXT_CLEAN_ENABLED=true`、`CHUNK_MIN_SIZE=5`、`EMBEDDING_MAX_RETRIES=3`

### 依赖
- `pyproject.toml`:`pymupdf`(替代 PyPDF2;PyPDF2 若无其他引用则移除)

## 6. 验证计划(全部实测)

1. `init_db.py` 连续运行两次 → 数据不丢、无报错(修复 P0-2)
2. 上传 `智能家具产品知识文档.txt` 两次 → 第二次返回 `duplicate`(修复 P2-6)
3. 上传 GBK 编码 txt → 解析成功(修复 P2-9)
4. 上传含表格的 docx → 表格文本进库(修复 P2-8)
5. 上传非白名单扩展名/超限文件 → 400 明确报错(修复 P2-7)
6. `ingest_knowledge.py` 跑通 → 商品语料可复现入库(P0-1),且入库后 `GET /documents` 可见
7. `DELETE /documents/{md5}` → chunks 与记录同事务删除,库内无残留
8. 存量 10 条无 md5 数据:不阻塞新链路,文档列表正确(旧数据标记,待重传)
9. 端到端:上传→检索,原 RAG 查询链路不受影响

## 7. 明确不做(防过度设计)

- 不做多模态 PDF(视觉理解/图片描述/扫描件 OCR)
- 不做语义合并、不做 zip 批量导入、不做 SSE 进度
- 不做 MIME 魔数检测、不做文档多租户权限
- 不做增量同步/文件变更监听(商品文档由运营显式上传/重传)

## 8. 已知风险与备注

- **存量数据**:现有 10 条 chunk 无 md5,加列后可空;`documents` 表从零开始,旧数据在文档列表不可见,建议审核后重跑一次 `ingest_knowledge.py`(同 md5 命中去重,天然不产生重复)
- **PyMuPDF 替换(已确认)**:替换 PyPDF2,需在验证计划中确认既有 pdf 样例解析行为;PyPDF2 若无其他引用则随依赖清理移除
- **md 解析**:MVP 不做 mistune 结构化解析(外仓库主路径),按 txt 编码链处理即可——商品文档多为纯文本/markdown 简单结构,结构化解析收益有限
- 本文档不涉及检索侧(混合检索/RRF/精排)改动;documents 表为文件级入口,检索仍走 document_chunks
