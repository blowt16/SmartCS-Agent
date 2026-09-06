# 商品 SKU 确定性对齐改造（阶段 A：TSV 与动态库数据源优化）实施规格
> **归档状态**: ✅ 已完成（2026-09-06 归档 `已完成/`）——落地证据：TSV sku 列 50 编码固化 + add_sku_column 生成器（8305b99）；model sku unique 双约束/导入校验 fail-fast/upsert 键切换（7bfb4dc）；验证期修复三处（8a2abd5：validate_sku 读键、JSONB none_as_null、spans 测试迁移）。验收全过：A1/A2（编码全覆盖幂等）A3（47 行入库=50−3 行价格"—"跳过[spec 风险 #3 预期]、零空 sku、零重复、sku↔名称与 TSV 逐字符一致）A4（重跑幂等）A5（改名沿 sku 延续无新行，实测 JD-DRY-003）单测 17+13 passed；A6（回归：名称通道 ok）A7（DB 层 structural fail-fast 实测拦截真实错误零写入——验证期发现键名 bug 即由其拦截）。本机 PG 由 docker compose 拉起验证

> **用途**: 解决 RAG 静态知识（rag_retrieval）与商品动态信息（product_stock_lookup）两 tool 检索结果不一致的问题——不一致根因是两侧对齐依赖"商品名称文本匹配"（LLM 从片段抽名 → ILIKE 子串模糊），存在同前缀多商品歧义、名称抽取失败、名称变更失联三类失效。本方案引入**全局唯一商品编码 sku 作为确定性对齐键**，从数据源（TSV）与存储层（动态库表）开始改造，docx 文本标注、RAG chunk metadata、rag 返回层透出、product_tool 精确入参为后续阶段。
> **技术栈**: 纯数据层改造——TSV（单一数据源，header 名读取）+ PostgreSQL（product_price_stock）+ SQLAlchemy 2.x + 幂等导入脚本（`import_product_price_stock.py`），不涉及 LLM 链路
> **状态**: 部分实施（TSV/DB 代码已落地，DB 侧断言待环境补跑；全链路方案沿用既有预研 [[SPEC_PRODUCT_STOCK_TOOL.md]] §12.1 演进方向设计，本次落地其中 TSV/DB 两项并修正其一决策——sku 不进 docx 的裁定已在 [[SPEC_RAG_SKU_METADATA.md]] P0 推翻（2026-09-06））
> **关联文档**: [[SPEC_PRODUCT_STOCK_TOOL.md]]（§12.1 SKU 对齐演进方向，本 spec 为其承接）[[SPEC_RAG_TOOL_OPTIMIZATION.md]]（决策 #10 片段前缀扩展约定）[[SPEC_ENTITY_PARALLEL_RAG.md]] [[PROJECT_ANALYSIS.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状核查](#2-现状核查)
3. [方案决策记录](#3-方案决策记录)
4. [阶段 A 详细设计](#4-阶段-a-详细设计)
5. [验证方案与验收断言](#5-验证方案与验收断言)
6. [分阶段实施步骤](#6-分阶段实施步骤)
7. [待确认事项](#7-待确认事项)
8. [风险与避坑清单](#8-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 商品知识分两层存储（CLAUDE.md 分层原则）：**静态信息**（参数/功能/政策）→ docx → RAG 检索；**动态信息**（价格/库存）→ `product_price_stock` 表 → `product_stock_lookup` 工具。两 tool 尚未在 Agent 编排层接线（生产链路 customer_tools 直连 RAG），但消费约定已成型（SPEC_PRODUCT_STOCK_TOOL §4.4）
2. 两 tool 对齐键 = **商品名称文本匹配**：TSV 商品名是"品牌+型号+品类+卖点"超长营销名（含空格），动态表按 `product_name` ILIKE 子串模糊命中——"米家智能晾衣机2"可同时命中 2 / 2 Pro 等多款变体，排序键 `updated_at DESC` 与静态检索命中的具体变体**无绑定关系**，Agent 组装回答时静态信息与动态价格指向不同商品（不一致根因 1）
3. LLM 从用户口语/检索片段抽取完整名失败、或运营改名后名称失联（不一致根因 2/3，SPEC_PRODUCT_STOCK_TOOL §12.1 已落档："当前两 tool 对齐靠商品名文本匹配……名称歧义/提取失败场景无结构化对齐键"）
4. RAG 侧 chunk 归属商品目前仅有 `chapter` 单值文本（块首字符归属），且 rag 返回层丢弃全部元数据——即使动态表有键，静态侧也无键可对齐（不一致根因 4，详见后续阶段 spec）

### 1.2 目标（阶段 A 范围）

| 目标 | 量化验收 |
|---|---|
| TSV 具备稳定商品编码列 | 表头 9 列含 `sku`，50 行全非空、格式统一、无重复 |
| 动态表具备确定性对齐键 | `product_price_stock.sku` unique 非空，与 TSV 逐字符一致 |
| 导入链路以 sku 为身份键幂等 | 重复执行行数不变；商品改名后价格/库存沿 sku 延续（不产生新行） |
| 数据源结构性错误 fail-fast | sku 缺失/非法/重复 → 导入整体终止，无部分入库 |
| 不回归 | 现有 `product_stock_lookup` 按名称模糊查询行为与返回字段零变化 |

### 1.3 明确不做（本期/本 spec）

- 不改 `product_price_stock` 既有列名（product_name/current_price/stock_quantity 保持，拒绝为命名美观做破坏性更名，见决策 D5）
- 不加 `brand` 列（静态信息归 docx 的分层原则；如需"按品牌泛查询"另行评估，见 §7-4）
- 不动 docx 生成/切分/RAG 返回/tool 入参（阶段 B/C/D，§6.2 仅列纲领，待后续 spec 详设）
- 不引入第二标识字段（决策 D1：单键 sku）

## 2. 现状核查

### 2.1 TSV（scripts/data/jd_smart_furniture.tsv，2026-09-06 实测）

- 表头 8 列，**无任何 ID/SKU/编码列**：`品类 商品名称 品牌 参考价格(元) 功能特点 规格参数 售后服务 来源`
- 数据 50 行商品，9 品类分布：智能门锁 12、电动智能沙发 7、电动升降桌 6、智能窗帘 6、智能电动床 5、智能晾衣架 5、按摩椅 4、智能床垫 3、智能床头柜 2
- `来源` 列 URL 为第三方比价页（smzdm/manmanbuy），其参数 id **非京东商品 SKU，不可复用**
- 商品名称长名风格（示例）：`芝华仕 50611B 头等舱智能电动沙发 小三座双电 2.43M 赤霞橙`、`米家智能晾衣机2 隐形超薄隐藏式升降`
- 消费方全量扫描（2026-09-06 实测，共 3 个读写方 + 文档引用）：`scripts/build_smart_furniture_docx.py`、`llm_backend/scripts/import_product_price_stock.py`、`scripts/enrich_product_tsv.py`（DictReader/DictWriter **header 往返——保留 sku 列与行序，零改动**，但注意其全量重写行为，勿与 sku 固化流程并发运行）；`docs/PROJECT_ANALYSIS.md` 与归档 design 文档仅文字引用，不改。三方均 header 名读取，无按列序位置解析 → **表尾追加列安全**（结论闭环，替代原开放扫描项 §8-2）

### 2.2 动态库表 product_price_stock（llm_backend/app/models/product_price_stock.py）

| 列 | 类型 | 约束/备注 |
|---|---|---|
| id | Integer PK 自增 | DB 行号，删行重灌漂移，**非业务键** |
| product_name | String(255) | **NOT NULL + UNIQUE**（uq_product_price_stock_name），注释"与 TSV 商品名称完全一致(查询/join 键)"——现状对齐键 |
| category | String(50) | NOT NULL |
| current_price | Numeric(10,2) | NOT NULL |
| stock_quantity | Integer | NOT NULL（0=无货） |
| updated_at | DateTime | server_default now, onupdate |

### 2.3 导入脚本（llm_backend/scripts/import_product_price_stock.py，2026-09-06 读码）

- 列映射按 header 名：`COL_NAME/COL_CATEGORY/COL_PRICE/COL_AFTERSALES`（`商品名称/品类/参考价格(元)/售后服务`）
- `parse_price`：正则提取数字（含范围/券后），**多值取均值** quantize 0.01；解析失败整行跳过（记警告）
- `assign_stock`：品类行序第一款填 0/少量（ZERO_STOCK_CATEGORIES=["智能晾衣架","智能门锁"]；LOW_STOCK_CATEGORIES 映射），其余 50——测试用固定规则，非真实库存
- upsert：`pg_insert(...).on_conflict_do_update(index_elements=[product_name], set_={category, current_price, stock_quantity})`——**冲突键 = product_name**
- 单事务提交（循环逐行 execute 后一次 commit）；重复执行幂等

### 2.4 本方案与归档 spec 的关系

SPEC_PRODUCT_STOCK_TOOL.md §12.1 已完整预研 SKU 对齐五步（TSV 增列 → chunk 多值挂载 → DB 增列 → 工具参数 → 消费约定），前置核查结论与本 spec §2 一致，标记"未实施，待 SKU 数据源 + 混合块占比实测"。**本 spec 阶段 A 落地其 TSV/DB 两步**；数据源问题（sku 值从哪来）由决策 D2（自造稳定编码）关闭。

## 3. 方案决策记录

| # | 决策 | 结论 | 理由 |
|---|---|---|---|
| D1 | TSV 新增标识字段数量 | **仅加一列 `sku`，不另加 `id`** | 全链路消费方（docx 标注/DB 键/chunk metadata/tool 入参）只需一个全局唯一编码；`product_price_stock.id` 为自增行号会漂移不可作业务键；双键引入同步与选择负担。既有 spec §12.1 用词"SKU"泛指唯一商品编码，自造编码合法（D2） |
| D2 | sku 值来源 | **自造稳定编码**（`JD-{CLASS}-{NNN}`），非真实京东 SKU | 本方案目的为内部确定性对齐，不与京东外部系统 join；真实 SKU 需 50 款逐个采集（归档 spec 待确认前置）。编码一旦固化**禁止变更**（变更=全链路身份变更，见 §8-1） |
| D3 | sku 编码格式 | `JD-[A-Z]{3}-[0-9]{3}`，3 位品类前缀 + 品类内 3 位序号 | 可读（日志/排查一眼归品类）、定长、正则校验简单；前缀表见 §4.1.2（待确认项 §7-1） |
| D4 | DB 改造路径 | 加 `sku` 列（unique NOT NULL）；**本环境清空重建 + 重跑导入**为主路径，另提供存量保留环境回填路径（§4.3.3） | 表数据全部由导入脚本生成（测试库存规则），清空重建零损失；NOT NULL 语义干净 |
| D5 | 列名更名 / 加 brand | **不更名、不加 brand** | product_name→name 等更名无功能收益，却牵动 ORM/导入/工具返回/单测/归档文档全链；brand 属静态信息（分层原则）。用户原始目标格式中 brand 待确认（§7-4） |
| D6 | 双唯一约束 | **sku unique 与 product_name unique 并存** | product_name 唯一保留作：人性化标识 + 现状名称通道查询的确定性保障（1:1 映射由 TSV 源头保证）；sku 为身份键（不可变），product_name 为可变展示名（可运营改名） |
| D7 | upsert 冲突键 | **从 product_name 切换为 sku** | 身份语义：改名不改码 → 价格/库存沿 sku 延续、不产生新行（验收 A5）；product_name unique 冲突（TSV 内重名改名）由源数据校验拦截 |
| D8 | 校验时机与策略 | **导入前置全量校验，fail-fast 整体终止**（区别于现状"单行价格解析失败跳过"） | sku 缺失/非法/重复属**数据源结构性错误**，部分入库会静默制造对齐键空洞；结构性校验失败不写任何行（先校验后写，保持 §2.3 单事务语义） |
| D9 | sku 列追加位置 | TSV 表尾第 9 列 | 消费方全为 header 名读取（§2.1），尾部追加对既有消费零影响 |

## 4. 阶段 A 详细设计

### 4.1 TSV 改造

#### 4.1.1 新列定义

表尾追加第 9 列，header 名为 `sku`（与 docx 标注格式、DB 列名、后续阶段统一用词）。示例（示意，非真实行数据；实际 50 行由生成器填充后人工审阅固化）：

```
品类          商品名称    品牌    参考价格(元)  功能特点  规格参数  售后服务  来源    sku
智能门锁      ××××       ×××     ×××          ×××      ×××      ×××      ×××    JD-LCK-001
```

- 列值规则：非空；格式 `^JD-[A-Z]{3}-\d{3}$`；文件内全局唯一；与商品名称一一映射（同 sku 不得出现在两行）
- **纪律**：编码一经固化进 TSV 即不可变更、不可重排（§8-1）；新商品入库只允许在品类内取当前 max 序号 +1（由生成器保证）

#### 4.1.2 品类前缀映射表（推荐值，待确认 §7-1）

| 品类 | CLASS | 备注 |
|---|---|---|
| 电动智能沙发 | SOF | sofa |
| 电动升降桌 | DSK | desk |
| 智能门锁 | LCK | lock |
| 智能电动床 | BED | bed |
| 智能床垫 | MTR | mattress |
| 智能床头柜 | NST | nightstand |
| 智能窗帘 | CUR | curtain |
| 智能晾衣架 | DRY | dryer rack |
| 按摩椅 | MAS | massage |

新增品类时扩展本表（后缀字母不重复），校验器随映射表同步。

#### 4.1.3 生成器脚本 scripts/add_sku_column.py（新增，一次性工具）

- 职责：对**缺失 sku 的行**回填编码，`--write` 写回 TSV（默认 dry-run 打印待填行与拟定编码）
- 填充规则（幂等，二次执行零 diff）：
  1. 已有合法 sku 的行**永不重写**
  2. 缺 sku 行按（品类，商品名称）升序；每品类内取现有最大 NNN + 1 续编（无现存则 001 起）
- **写回行序纪律**：只逐行**原位补 sku 值**，禁止整表排序/重排——TSV 行序驱动导入脚本 `assign_stock`"品类第一款=0/少量"库存规则（`import_product_price_stock.py:43-49`），重排会使零库存分布漂移、A3~A5 断言失真
- 完成后人工 `git diff` 审阅 50 行编码 → 提交固化；后续手工新增商品行时 sku 留空、由生成器补
- 附带输出自检：全量 sku 非空/格式/唯一/映射唯一，违规即报错退出

### 4.2 DB 模型改造（llm_backend/app/models/product_price_stock.py）

```python
from sqlalchemy import Column, Integer, String, Numeric, DateTime, func, UniqueConstraint

sku = Column(String(32), nullable=False)          # 商品编码:确定性对齐键(查询/join 主键)
# product_name 列注释更新: 展示名(可改,改名不产生新身份;1:1 映射由 TSV 保证)
# 既有:
#   UniqueConstraint("product_name", name="uq_product_price_stock_name")
# 新增:
UniqueConstraint("sku", name="uq_product_price_stock_sku")
```

- sku 长度 32 远大于 `JD-XXX-999`(10)，留足真实京东 SKU 替换余量（真 SKU 为 8~16 位数字）
- 注释同步更新：原"product_name 与 TSV 商品名称完全一致(查询/join 键)"改为——**sku 为 join/查询主键，product_name 为可变展示名**（保留一致性约束注释）

### 4.3 导入脚本改造（llm_backend/scripts/import_product_price_stock.py）

#### 4.3.1 列读取与前置校验（新增步骤，先校验后写）

```python
COL_SKU = "sku"
SKU_RE = re.compile(r"^JD-[A-Z]{3}-\d{3}$")

def validate_sku(rows: list[dict]) -> None:
    """结构性校验:任一违规抛 ValueError(整体终止,不写任何行)"""
    seen_sku: dict[str, str] = {}          # sku -> product_name(查一列多值/重复)
    seen_name: dict[str, str] = {}         # product_name -> sku(同名双 sku 违规提前拦截,防 DB 层撞 unique 整体回滚)
    for row in rows:
        sku, name = row["sku"].strip(), row[COL_NAME].strip()
        if not sku:
            raise ValueError("存在空 sku 行: {}".format(name))
        if not SKU_RE.match(sku):
            raise ValueError("sku 格式非法(期望 JD-XXX-000): {} = {}".format(name, sku))
        if sku in seen_sku:
            raise ValueError("sku 重复: {} 与 {} 同为 {}".format(seen_sku[sku], name, sku))
        if name in seen_name:
            raise ValueError("商品名重复(同名双 sku 违规): {} 同时为 {} 与 {}".format(name, seen_name[name], sku))
        seen_sku[sku] = name
        seen_name[name] = sku
```

- 校验放 `read_tsv_rows` 完成后、任何 DB 写之前；若表头缺 `sku` 列（`row["sku"]` KeyError）同样整体终止并提示"TSV 未升级，先执行 add_sku_column"
- 价格解析失败跳行的**现状策略不变**（内容性单行问题），但被跳行若带 sku 不影响其余行入库——注意：跳行不会造成 sku 空洞，因为该行本就未入库

#### 4.3.2 upsert 冲突键切换

```python
stmt = pg_insert(ProductPriceStock).values(**r)   # r 含 sku/product_name/category/current_price/stock_quantity
stmt = stmt.on_conflict_do_update(
    index_elements=[ProductPriceStock.sku],       # ← 原 [product_name]
    set_={
        "product_name": stmt.excluded.product_name,      # 展示名可随 TSV 更新
        "category": stmt.excluded.category,
        "current_price": stmt.excluded.current_price,
        "stock_quantity": stmt.excluded.stock_quantity,
    },
)
```

- 语义变化：按 sku 冲突更新，**改名不产生新行**（验收 A5）；同名不同 sku（改名成现存行名）触发 product_name unique 冲突 → IntegrityError → 事务回滚整体终止（源数据 1:1 性由 §4.3.1 校验 + TSV 唯一名约束兜底，属异常态人工处理）
- `read_tsv_rows` 返回 dict 追加 `"sku": row[COL_SKU].strip()`

### 4.4 数据迁移/重建路径

#### 4.4.1 主路径：清空重建（本环境 dev/test，数据为脚本生成）

```bash
# 1. 数据源升级
python -m scripts.add_sku_column --write        # llm_backend 同级 scripts/ 下执行或按脚本指引
git diff scripts/data/jd_smart_furniture.tsv    # 人工审阅 50 行编码(含行序未变校验)
# 2. DB 结构 + 数据重建 —— 顺序约束:先清空后加列(或 DROP 重建表)
#    注意:sku 列 NOT NULL 无 default,表内有行时 ALTER ADD COLUMN 直接失败——严禁"先加列后清空"
#    方式 A(本环境,结构由 create_all 管理): DROP TABLE product_price_stock; 改 model 后建表
#    方式 B(保留表): TRUNCATE product_price_stock; 再 ALTER ADD COLUMN sku VARCHAR(32) NOT NULL + unique 约束
#    执行前确认: 建表机制(create_all/迁移)与目标库无真实业务数据
python -m scripts.import_product_price_stock    # 50 行全部入库
```

#### 4.4.2 存量保留路径（有需保留数据的部署）

1. ORM 先以 `nullable=True` 加列（或 SQL `ALTER TABLE ... ADD COLUMN sku VARCHAR(32)`）
2. 回填脚本：读 TSV（商品名称→sku），按 product_name `UPDATE product_price_stock SET sku = :sku WHERE product_name = :name`；TSV 中不存在/名称不匹配的行输出人工清单
3. 回填完成后 `ALTER TABLE ... ALTER COLUMN sku SET NOT NULL` + 加 unique 约束；重复 sku/空值先清理
4. 之后导入流程与主路径一致（upsert 键为 sku）

> 本期开发/测试环境按主路径执行即可；此小节供部署方按环境选择，不做代码实现（若需可后续补脚本）。

### 4.5 影响面清单（执行时按 CLAUDE.md 全项目扫描约束逐项核对）

| 文件 | 改动 |
|---|---|
| scripts/data/jd_smart_furniture.tsv | 表尾加 sku 列 + 50 行编码（生成器+人工审阅） |
| scripts/add_sku_column.py | **新增**生成器（dry-run 默认） |
| llm_backend/app/models/product_price_stock.py | 加 sku 列 + unique；注释更新 |
| llm_backend/scripts/import_product_price_stock.py | COL_SKU、前置校验、upsert 冲突键切换、日志含 sku 计数 |
| scripts/enrich_product_tsv.py | **不改**（已实测 DictReader/DictWriter header 往返保留 sku 列与行序）；注意勿与 sku 固化流程并发运行 |
| product_stock_lookup（app/tools/product_stock_tool.py） | **本期不改**（名称通道返回字段不变，新增列不影响现有查询/返回）；回归验证 A6 |
| 引用 product_price_stock 模型的测试/建表 fixture | 同步加 sku 列（新建表路径自然生效；测试若用独立 schema 需重建） |
| SPEC_PRODUCT_STOCK_TOOL.md（已完成/） | **不改历史**，仅在需要处可加一行指向本 spec 的承接注记（CLAUDE.md §6 规则 4） |

## 5. 验证方案与验收断言

### 5.1 数据源

- **A1** TSV：表头 9 列含 `sku`；行数 51（含表头）；`sku` 列全非空、全匹配 `^JD-[A-Z]{3}-\d{3}$`、无重复
- **A2** 幂等：`add_sku_column`（dry-run 与 --write 各一次）二次执行输出"无缺失行"，`git diff` 为空

### 5.2 DB

- **A3** 清空重建后导入：日志"入库完成 50 行"；SQL 断言 `SELECT count(*) = 50`、`SELECT count(*) WHERE sku IS NULL = 0`
- **A4** 幂等：再次执行导入，行数仍 50、全部 sku 值不变（与首次逐字符一致）
- **A5** 改名延续：临时改某行 `商品名称`（sku 不动）→ 重跑导入 → 行数仍 50、该行 sku 不变、product_name 更新、无新行；改回后重跑复原
- **A6** 结构性校验 fail-fast：构造坏 TSV（空 sku / 格式非法 / sku 重复各一例，仅本地临时文件测试）→ 导入整体终止退出码非 0，DB 行数与执行前一致（无部分写入）
- **A7** 回归：`product_stock_lookup(product_name="<原商品简称>")` 结果与改造前一致（三态 ok/empty/error 行为不变）

### 5.3 验收命令（参考）

```bash
# TSV 校验(临时一段 python 或并入生成器自检)
awk -F'\t' 'NR==1{print NF; for(i=1;i<=NF;i++) print i,$i}' scripts/data/jd_smart_furniture.tsv   # 表头 9 列
python -m scripts.import_product_price_stock   # llm_backend 下执行
# DB 断言走 psql/既有测试框架;单测补 sku 相关用例(见 §5.2)
```

## 6. 分阶段实施步骤

### 6.1 本期（阶段 A）实施顺序

1. 新增 `scripts/add_sku_column.py` → 生成 50 行编码 → 人工审阅 → TSV 提交
2. model 加 sku 列/unique → 重建或 TRUNCATE 动态表
3. 导入脚本：COL_SKU + 校验 + upsert 键切换
4. 回归测试（A3~A7）→ 更新受影响测试
5. 按 CLAUDE.md §6 更新本 spec 归档状态行 → 归档 `已完成/`
6. git 提交推送（[docs]+[config] 分类按实际）

### 6.2 后续阶段纲领（另行起草 spec，不在本期实施）

- **阶段 B：知识文档侧**——`build_smart_furniture_docx.py` H3 商品标题标注 `(SKU:xxx)`（推翻归档 spec"sku 不进 docx"裁定，理由与代价在阶段 B spec 论证）；docx 全量重建
- **阶段 C：RAG chunk metadata**——`document_chunks` 加 `sku_codes` 多值列；parse 阶段 sku 随章节栈挂段；切分后按字符轴区间多值收集（替换块首单值归属的 sku 部分）；构建时统计多 sku 块占比（归档前置验证项）；存量重灌
- **阶段 D：检索与工具侧**——`_to_doc`/BM25 doc 透出 `sku_codes`；`rag_retrieval` 输出前缀行带商品归属与 sku 多值（知识类型/无商品语义；对齐 SPEC_RAG_TOOL_OPTIMIZATION 决策 #10）；`product_stock_lookup` 加可选 `sku` 精确参数（精确优先、名称/品类模糊兜底保留，覆盖无 sku 场景）+ 返回含 sku；customer_tools/summarize 双消费通道同步；汇总"先 rag 取键 → 再精确查动态"的对齐约定
- 阶段 A 与 B/C/D **可独立实施**（A 不破坏现状，B/C/D 依赖 A 的 TSV sku 列）

## 7. 待确认事项

| # | 事项 | 默认/建议 | 说明 |
|---|---|---|---|
| 1 | 品类前缀映射表文案（§4.1.2） | 表内推荐值 | 可整体替换（自定义缩写亦可），校验正则与 docx 标注格式随之统一；确认后固化 |
| 2 | 编码格式定长 `JD-{3 字母}-{3 位}` | 采用 | 若期望更大容量改 4 位序号需同步改校验与正则 |
| 3 | DB 重建方式授权 | 主路径清空重建（§4.4.1） | 需确认目标库可 TRUNCATE（无真实业务数据）；有保留需求走 §4.4.2 |
| 4 | brand 列本期是否加入 | 不加 | 用户原始目标格式含 brand；加入成本低（model+import 各一行）但属静态信息入动态表，违背分层原则——除非存在"按品牌查价格"的明确消费需求 |
| 5 | product_name unique 保留 | 保留 | 若认为名称通道可弃、欲简化约束，需连同工具名称通道决策一起评估（阶段 D） |
| 6 | 校验失败策略（fail-fast 整体终止 vs 仿价格解析失败行跳过） | fail-fast（D8） | 结构性错误与内容性错误区分对待 |

## 8. 风险与避坑清单

1. **sku 编码稳定性纪律**：编码一经固化禁改禁重排。改动 sku = 商品身份变更，会传播到 docx 文本、chunk metadata、DB 全量重建（阶段 B~D 落地后成本更高）。新增商品只走生成器续编。TSV 手工编辑只允许动其他 8 列
2. **TSV 消费方扫描（已闭环，2026-09-06 实测）**：3 个读写方（docx 构建/导入/enrich_product_tsv，均 header 名读取，enrich 往返保留新列与行序）——表尾追加列安全结论成立，无需再扫；文档引用（PROJECT_ANALYSIS/归档 design）不改
3. **price 解析失败跳行 vs 校验**：校验只拦结构性错误；价格解析失败跳行现状保留——跳行后 DB 行数 < 50 属预期（日志有 warning），勿与 A3 混判
4. **并发/重复上传无关**：本阶段不触碰 RAG 入库链路（document_chunks 未改），无 md5/重灌联动
5. **upsert 键切换的隐性行为**：改名后旧行不再按 name 命中新名——`product_stock_lookup` 名称模糊通道仍按新名可查（工具不依赖冲突键），无用户可见差异
6. **模型/表不同步风险**：执行顺序严格按 §6.1（先 TSV+生成器 → 再 model → 再 import），表结构未变时旧导入脚本新 TSV（含 sku 列）会 KeyError 读列？——不会：旧脚本不读 sku 列，仅新脚本校验；反之新脚本旧 TSV 会终止（§4.3.1 提示语）
7. **归档文档关系**：SPEC_PRODUCT_STOCK_TOOL.md §12.1 属已完成归档历史，本 spec 实施后不改其原文（CLAUDE.md §6 规则 3），承接关系以本 spec §2.4 表述为准
8. **商品下线/删除语义（未设计，需产品/运营确认）**：导入 upsert 只增改不删——TSV 删行后 DB 残留旧行，sku 精确通道会把已下线商品当在售（比名称模糊通道更隐蔽）。后续需下线清理流程（按 TSV diff 删除/人工清理）或明确定义下线运营流程
9. **真实京东 SKU 替换 = 全链路一次性重建流程（演进注记，非逐行零散替换）**：现校验正则 `JD-[A-Z]{3}-\d{3}`（本 spec）与 RAG 侧锚点正则（SPEC_RAG_SKU_METADATA D9）均以自造格式为前提；真实 SKU 为纯数字串，替换时同步放宽两处正则 + docx 重灌 + DB 重建一次完成，与本条 #1"禁改"纪律不矛盾（#1 禁的是无流程的零散改动）
