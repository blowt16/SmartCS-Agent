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
| PDF 解析(原外部仓库本地 MinerU 流水线 → 云端 MinerU API) | ✅ 迁移(改用 API 版) | 扫描件/复杂版面/中文表格全场景覆盖;免本地 5-10GB 模型与 GPU;免费额度 1000 页/天足够运营小批量 |
| 本地 MinerU 部署(模型 + GPU + 视觉管线) | ❌ 排除 | 重依赖(5-10GB 模型、PaddlePaddle 生态);云端 API 已达到同等解析能力,无自建必要 |
| PDF 多模态/VL 图片描述 | ❌ 排除 | 商品说明书以文本+表格为主,无需图片理解 |
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
  3. 解析: pdf→MinerU 云端 API(is_ocr=true,轮询取 full.md) / docx→段落+表格(body 顺序) / txt,md→编码降级链
  4. 清洗(TEXT_CLEAN_ENABLED 开关):
     控制字符清理 → 空白规范化 → 页码行清理 → 目录行清理
  5. 分块: RecursiveCharacterTextSplitter(500/50, 不变)
     + 过滤 < CHUNK_MIN_SIZE 的噪声块
     + 注入标准元数据(见 §5 元数据设计)
  6. Embedding: 分批 10 条 + 指数退避重试 3 次 + 全零检测
     (必须全部批次成功,不允许部分批次先行写入)
  7. 单事务原子写入: documents(文件记录) + document_chunks(块)
  8. 返回 success / duplicate / failed(含错误分类与原因)

GET  /api/documents?user_id=      文档列表(来自 documents 表)
DELETE /api/documents/{md5}?user_id=  按文件删除(chunks + 记录,同事务)
```

### 原子性与失败恢复(根本性保证)

**设计原则:全链路零中间态** —— 校验/解析/清洗/分块/嵌入全部在内存完成,**任何 DB 写入只发生在最后一步且只有一个事务**:

- 任一阶段失败(校验、MD5、解析——含 MinerU API 不可达/超时/解析失败、清洗、分块、嵌入超限、事务回滚)→ **DB 零写入**(无 documents 行、无 chunk、MD5 未登记)→ 返回 failed + 明确错误分类
- **失败后重传同一文件 = 全新重试**:因为失败无痕,MD5 查重不会命中,不会出现"库里没数据却提示已存在"的死锁
- **并发防重(竞态兜底)**:`documents (user_id, md5)` 唯一约束;两个并发上传同文件时,后提交方 INSERT 触发 IntegrityError → 捕获后整体回滚 → 返回 duplicate(查重 SELECT 只是快速路径,唯一约束才是最终防线)
- 嵌入分批:**全部批次成功才进事务**;任何一批重试 3 次仍失败即整体失败,无部分写入
- 磁盘文件:`uploads/` 下源文件保留(失败也保留,供排查),**一致性以 DB 为准**,不参与原子性
- documents 表**不含 status/error 列**(失败不落行,状态通过 HTTP 响应与日志表达),从结构上杜绝"半成功"记录

## 4. 关键技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 去重粒度与存储 | 文件级 MD5;存 PostgreSQL `documents` 表 | 外仓库用 JSONL(md5_store),本项目已有 PG,表存储天然支持事务原子与查询 |
| 去重触发时机 | 解析前(先 MD5 后解析) | 命中即跳过,省解析/嵌入成本(与外仓库一致) |
| 并发防重 | `documents (user_id, md5)` 唯一约束,INSERT 冲突 → 回滚 → duplicate | 查重 SELECT 只是快速路径,唯一约束是并发下的最终防线 |
| 原子性实现 | 全链路零中间态:内存完成解析/嵌入,最后一步 `documents` + `document_chunks` 同一 SQLAlchemy 事务 | 外仓库需"写库→记 MD5→失败手动回滚"三步,PG 事务一步完成;失败无痕 → 重传即重试 |
| 失败语义 | 失败不落任何 DB 记录(无 documents 行/MD5 未登记),返回 failed + 错误分类 | 保证"回滚后重新上传"语义:同文件重传不会被残留记录拦截 |
| PDF 解析器 | PyPDF2 → MinerU 云端 API(标准 API v4) | 扫描件(OCR)/复杂版面/中文表格全场景覆盖;免本地模型与 GPU;免费额度 1000 页/天;与 qwen 嵌入 API 同为云端依赖,有先例 |
| MinerU 接入方式 | 异步客户端:批量上传(`/file-urls/batch` + PUT)→ 提交任务 → 轮询 `state` → 取 `full.md`;`is_ocr=true`、`language=ch` | 标准 API 为官方稳定路径;免 SDK,复用现有 aiohttp;失败分类对齐 API 错误码(A0202 鉴权 / -60002 格式 / 超时) |
| PDF 元数据 | MinerU 输出 markdown 无页边界 → `page`/`page_count` 置 null;演进:需要时解析结果 zip 中 `layout.json` | MVP 不做 zip 解包;页码溯源记为演进项,不阻塞 |
| docx 提取 | python-docx 遍历 `element.body` 按原序取段落+表格(合并单元格跳过续格) | `doc.paragraphs`/`doc.tables` 分离列表丢失文档顺序;body 迭代零新依赖 |
| docx/md 元数据 | `chapter` 章节上下文:docx 取 Heading 样式,md 取 `#` 标题正则 | 商品文档按产品/系列分节,章节溯源价值高;实现为 ~20 行纯函数 |
| 删除语义 | `DELETE /documents/{md5}` 级联删 chunks | 外仓库三层联动(向量→MD5→图片)在 PG 下简化为级联删除 |
| 元数据列 | documents 表:md5/original_filename/file_type/file_size/page_count;chunk 表:chunk_id/md5/file_type/page/chapter | 支撑去重、按文件删除、文档列表、按页/按章节溯源;存量数据列可空,不阻塞 |
| RRF 去重键 | 检索结果 doc 携带 `chunk_id`,`rrf_fuse` 的 `id_key` 从 PK 切换为 `chunk_id` | PK 是 DB 行号(删除重传后漂移/复用);chunk_id 内容确定性(同文件→同键,跨环境稳定);消除 `hash()` 进程随机兜底导致静默去重失效的隐患 |
| 重试 | `embed_in_batches` 内加 3 次指数退避([1,2,4]s) | 瞬断自愈;超过即返回失败+明确错误,不做自动重试队列 |
| 配置入口 | 全部入 `.env`(settings 增加字段) | 遵循本项目"配置统一入 env"的既有约定(见近期 commit 系列) |

## 5. 主要改动清单

### 数据模型
- 新增 `app/models/document.py`:`documents` 表
  - 列:`id`、`md5`(String(32))、`original_filename`、`user_id`(String(50))、`file_type`(String(20))、`file_size`(Integer)、`page_count`(Integer, 可空, MVP 全为 null,演进从 MinerU layout.json 解析)、`chunk_count`(Integer)、`created_at`
  - 约束:`UNIQUE (user_id, md5)`(并发防重最终防线)
- `app/models/document_chunk.py`:增加 `chunk_id`(String(64), 可空存量, **UNIQUE**, 生成规则 `f"{user_id}_{md5}_{chunk_index:04d}"`)、`md5`(String(32), 可空)、`file_type`(String(20), 可空)、`page`(Integer, 可空, PDF 页号)、`chapter`(String(255), 可空, 章节路径如 "一、智能沙发系列 > 云享智能沙发 SF-2000")

### 标准元数据设计(§3 流程第 5 步注入)

| 字段 | 级别 | 提取来源 |
|---|---|---|
| `md5` | 文件/块 | 上传时对文件字节计算 |
| `original_filename` | 文件 | 上传原始文件名 |
| `file_type` | 文件/块 | 扩展名(txt/md/pdf/docx) |
| `file_size` | 文件 | 上传字节数 |
| `page_count` | 文件 | 仅 md/txt/docx 为 null;PDF 经 MinerU 无页边界,同样为 null(演进:解析 layout.json) |
| `chunk_count` | 文件 | 分块完成后回填 |
| `page` | 块 | MinerU markdown 无页边界 → PDF 为 null(演进:解析 layout.json);其他格式为 null |
| `chapter` | 块 | md:`^(#{1,6})\s+` 标题正则维护章节栈;docx:段落 Heading 样式;txt/pdf 为 null |
| `chunk_id` | 块 | 生成规则 `f"{user_id}_{md5}_{chunk_index:04d}"`;**RRF 融合去重键**(双路检索 doc 均携带,`rrf_fuse` 按它去重),兼作幂等引用/日志关联;存量数据为 null,重跑 ingest 回填 |
| `chunk_index` | 块 | 文件内块序号(已有) |
| `created_at` | 文件/块 | 入库时间(已有) |

### 索引服务(重构 indexing_service.py)
- 新增 `app/services/mineru_client.py`:MinerU API 客户端(异步,上传→轮询→full.md;失败分类:auth_error / unsupported_format / api_timeout / parse_error;指数退避重试)
- `_parse_document`:按类型分发 → pdf(MinerU API,is_ocr=true) / docx(body 顺序段落+表格)/ txt、md(编码降级链 + 标题正则)
- `_clean_text`:升级为开关控制的多步流水线(控制字符、空白、页码行、目录行)
- `process_file` 重排:校验 → MD5 查重 → 解析 → 清洗 → 分块(min_size 过滤)→ 注入元数据(page/chapter/md5/file_type)→ 嵌入(退避重试,全部成功才进事务)→ 单事务入库
- 新增:空文件检测、错误分类返回(empty_file / parse_error / unsupported / embedding_failed / duplicate);任何失败路径零 DB 写入

### API(扩展 main.py)
- `/api/upload`:返回增加 `status`(success/duplicate/failed)与 `md5`;校验失败 400
- 新增 `GET /api/documents`、`DELETE /api/documents/{md5}`

### 检索侧(配合改动)
- `rag_retriever_service.py` / `bm25_sql_retriever.py`:检索结果 doc 增加 `chunk_id` 字段(从 ORM 行读取)
- `rrf_fuse` 调用处:`id_key="id"` → `id_key="chunk_id"`(`rrf_fuse` 的 hash 兜底保留但实际不再触发)

### 种子导入脚本(新增)
- `llm_backend/scripts/ingest_knowledge.py`:遍历 `knowledge_data/` 目录,调用 IndexingService 批量入库(解决 P0-1,当前 10 条语料唯一来源是丢失的一次性操作,必须有可复现脚本)

### 初始化脚本(修复 P0-2)
- `scripts/init_db.py`:移除 `drop_all`,改为幂等建表(`CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`),扩展/索引语句不变

### 配置(.env + config.py)
- `MAX_FILE_SIZE=30`(MB)、`ALLOWED_FILE_TYPES=txt,md,pdf,docx`、`TEXT_CLEAN_ENABLED=true`、`CHUNK_MIN_SIZE=5`、`EMBEDDING_MAX_RETRIES=3`
- `MINERU_API_TOKEN`(必填)、`MINERU_BASE_URL=https://mineru.net/api/v4`(默认)、`MINERU_POLL_INTERVAL=3`(轮询间隔,秒)、`MINERU_TIMEOUT=300`(轮询总超时,秒)、`MINERU_MAX_RETRIES=3`(提交失败重试)

### 死代码清理(修复 P3-10)
- 删除 `app/services/embedding_service.py`(全项目零引用,与 qwen 切换不同步的遗留)

### 依赖
- **无新依赖**:MinerU 走 HTTP API,复用现有 aiohttp;移除 `PyPDF2`(确认无其他引用后);不安装 mineru 本地包

## 6. 验收标准(逐项映射核查清单全部问题,全部实测)

### 问题回归验收(对应 docs/索引链路核查问题清单.md)

| # | 验收用例 | 预期 | 对应问题 |
|---|---|---|---|
| 1 | `init_db.py` 连续运行两次(含有数据时) | 数据不丢、无报错、幂等 | P0-2 |
| 2 | `ingest_knowledge.py` 跑通;再跑第二次 | 首次全部 success;二次全部 duplicate(chunks 不翻倍) | P0-1 |
| 3 | 上传 `智能家具产品知识文档.txt` 两次 | 首次 success;第二次返回 `duplicate`,库内 chunks 不翻倍 | P2-6 |
| 4 | 上传 .exe / .png 伪改名 / 超 MAX_FILE_SIZE 文件 | 400 明确报错(unsupported/too_large),库零写入 | P2-7 |
| 5 | 上传 GBK 编码 txt | 解析成功进库(编码降级链) | P2-9 |
| 6 | 上传"段落-表格-段落"交替的 docx | 表格文本进库且位置顺序正确(body 迭代),合并单元格不重复 | P2-8 |
| 7 | 上传扫描版 PDF | MinerU OCR 解析成功进库(非静默报错,非空 chunk) | P1-4 |
| 8 | 上传损坏 PDF(非法文件) | 返回 failed + 明确分类(auth_error/unsupported_format/parse_error),库零写入 | P1-4 |
| 9 | 上传含 `##` 标题的 md | 每块 `chapter` 字段为所在章节路径 | P1-5 |
| 10 | 上传多页 PDF | 成功入库,chunks 正常;`page`/`page_count` 为 null 不阻塞(演进项,见 §8) | P1-5 |
| 11 | `GET /api/documents` | 每条记录含 md5/original_filename/file_type/file_size/page_count/chunk_count | P1-5 |
| 12 | `DELETE /api/documents/{md5}` | chunks 与记录同事务删除,库内无残留;删后再传同文件成功(success) | — |
| 13 | 全库 grep 无 `embedding_service` 引用 | 死代码已删,导入不报错 | P3-10 |
| 14 | 存量 10 条无 md5 数据 | 不阻塞新链路;重跑 ingest 后 md5 补全,列表正确 | P1-3 |

### 原子性专项验收(根本性问题)

| # | 验收用例 | 预期 |
|---|---|---|
| 15 | 故障注入:嵌入 API 强制失败(打桩,重试 3 次全挂) | 返回 failed(embedding_failed),**documents 表 0 行、chunks 0 行**;同文件重传 → 全新成功 |
| 16 | 故障注入:入库事务提交前抛错(模拟 DB 断连/唯一冲突) | 返回 failed,**库零残留**;同文件重传成功 |
| 17 | 故障注入:解析中途抛错(损坏文件) | 返回 failed(parse_error),库零残留;同文件重传可重试 |
| 18 | 并发上传同一文件 ×2(asyncio.gather 双请求) | 恰好 1 个 success + 1 个 duplicate,chunks 不重复(唯一约束兜底) |
| 19 | 上传失败后 `GET /api/documents` | 列表不含失败文件(失败无痕) |
| 20 | 端到端回归:上传 → `/api/langgraph/query` 检索命中新内容 | 原 RAG 链路不受影响,新 chunk 可检索(BM25 + 向量双路径) |
| 21 | 故障注入:MinerU API 不可达 / Token 无效 | 返回 failed + 明确分类(api_unreachable / auth_error),库零写入;同文件重传可重试 |

### 元数据与检索专项验收

| # | 验收用例 | 预期 |
|---|---|---|
| 22 | 构造同时被 BM25 与向量召回的同一 chunk(如查询"SF-2000 价格") | 两路 doc 均含相同 `chunk_id`;RRF 融合该 chunk **计 1 次**(分数 = 双路 rank 之和),非重复两条 |
| 23 | 存量 10 条数据重跑 `ingest_knowledge.py` 后 | 全部 chunk 的 `chunk_id`/`md5` 按规则回填,UNIQUE 无冲突;`GET /documents` 列表完整 |

## 7. 明确不做(防过度设计)

- 不做本地 MinerU 部署(5-10GB 模型 / GPU / PaddlePaddle 生态)
- 不做多模态 PDF 图片理解(商品说明书以文本+表格为主,云端 MinerU OCR 已覆盖扫描件)
- 不做语义合并、不做 zip 批量导入、不做 SSE 进度
- 不做 MIME 魔数检测、不做文档多租户权限
- 不做增量同步/文件变更监听(商品文档由运营显式上传/重传)

## 8. 已知风险与备注

- **存量数据**:现有 10 条 chunk 无 md5/chunk_id,加列后可空;`documents` 表从零开始,旧数据在文档列表不可见,建议审核后重跑一次 `ingest_knowledge.py`(同 md5 命中去重,天然不产生重复,chunk_id 一并回填)
- **MinerU API 依赖(已确认)**:PDF 解析新增云端依赖——网络抖动/Token 失效/额度耗尽(免费 1000 页/天,超出降级低优先级)时上传返回明确错误并可重传,不影响已入库数据;批量 ingest 注意提交限流(50 次/分)
- **PDF 页码元数据**:MinerU markdown 无页边界,`page`/`page_count` 暂为 null;需要页码溯源时演进解析结果 zip 中 `layout.json`
- **md 解析**:MVP 不做 mistune 结构化解析(外仓库主路径),按 txt 编码链处理即可——商品文档多为纯文本/markdown 简单结构,结构化解析收益有限
- 本文档不涉及检索侧(混合检索/RRF/精排)改动;documents 表为文件级入口,检索仍走 document_chunks
