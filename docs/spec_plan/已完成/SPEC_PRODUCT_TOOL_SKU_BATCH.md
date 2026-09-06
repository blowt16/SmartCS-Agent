# product_stock_lookup 多 sku 批量检索优化实施规格
> **归档状态**: ✅ 已完成（2026-09-06 归档 `已完成/`）——落地证据：tool 双参(sku/skus 合并去重)+单条 WHERE IN 保序+部分命中 ok/全缺失 empty+上限 20+归一去重（提交见下）；验收全过：mock 82 passed（含批量 A1~A5/schema A7/单码回归，单码 SQL 形态从 `=` 变单元素 `IN` 语义等价）、DB 冒烟 B1~B5（2 码保序/部分命中/全缺列码/单码与批量单元素一致/与节点 fetch_by_skus 同键同数据）；节点链路与方案 A 纪律零改动

> **用途**: 方案 A 后 tool 契约为单码单查。真实场景 rag top-k 混合块返回 3~10 个候选 sku（92.1% 混合块），LLM 若需"候选全查"（对比意图/清单意图）只能逐码串行调用——N 次往返 + N 次跨快照（价格窗口内变动 → 行间不一致，破坏原子性）。本 spec 为 @tool 增加批量路径，**一次 IN、同一快照、按入参序返回**；节点链路已具备等价能力（`fetch_by_skus`），本 spec 不动节点、仅工具层扩展，两处行为约定统一
> **依赖前置**: 方案 A（sku-only + RAG 门控）已实施（9b4ebcc/e3514a8）；DB `product_price_stock.sku` unique 与 47 行数据在库
> **技术栈**: langchain @tool + Pydantic v2 args_schema + SQLAlchemy（select WHERE IN）+ 既有 _query_with_retry（超时/瞬时重试/异常分类三态协议不变）
> **状态**: 已完成 —— 范围：仅 `product_stock_tool.py` + 服务/节点零改动（回归验证）+ 测试
> **关联文档**: [[SPEC_SKU_ALIGNMENT.md]]（数据键）[[SPEC_RAG_SKU_METADATA.md]]（rag 侧多码元数据）[PRODUCT_TOOL_SKU_GATE 方案 A（对话决策 2026-09-06）] [[SPEC_PRODUCT_STOCK_TOOL.md]]

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [现状与问题](#2-现状与问题)
3. [方案决策记录](#3-方案决策记录)
4. [详细设计](#4-详细设计)
5. [验证方案与验收断言](#5-验证方案与验收断言)
6. [实施步骤](#6-实施步骤)
7. [待确认事项](#7-待确认事项)
8. [风险与避坑清单](#8-风险与避坑清单)

---

## 1. 背景与目标

### 1.1 背景

1. 方案 A 后 rag 返回的【商品编码:】前缀可为多值（混合块 92.1%、top-k 跨块去重后 3~10 码）；tool 一次只能查一个码
2. 未来 agent bind_tools 场景：LLM 拿到多码候选后，对"对比意图（A vs B 哪个性价比高）""清单意图（有哪些 X 在售）"需要候选全查；若逐码串行 → N 次 DB 往返 + N 次独立快照（每次查询间价格可能更新，行间价格来自不同时刻——与"两 tool 信息一致性"同源的快照不一致问题）
3. 节点链路已用 `product_dynamic_service.fetch_by_skus`（单次 IN、保序、缺失静默）解决了等价需求——工具层应具备同级能力，且**行为约定与节点一致**（防两通道语义分叉）

### 1.2 目标

| 目标 | 验收 |
|---|---|
| tool 支持多码批量 | `skus=[...]` 一次精确返回全部映射行，单次查询同一快照 |
| 保序 | 返回 data 顺序 = 入参顺序（LLM 可按码对位） |
| 部分命中语义明确 | ≥1 命中 → ok（data 仅含命中行）；全缺失 → empty（文案列缺失码） |
| 单码路径零破坏 | 现 `sku=` 单查行为、三态协议、返回字段完全不变（回归） |
| 与节点约定一致 | 两处批量均"IN 一次 + 入参序 + 缺失静默"，由同一组断言交叉覆盖 |

### 1.3 明确不做

- 不改节点链路（`fetch_by_skus` 保持，`customer_tools` 零改动）
- 不做并行 gather N 次单查（跨快照 + N 往返，中间态无价值）
- 不做分页/游标（候选上限 20，远低于参数上限）
- 不恢复 product_name/category/limit 参数（方案 A 决策不变）

## 2. 现状与问题

### 2.1 现状（2026-09-06 读码）

- `product_stock_tool.py`：`ProductStockLookupInput{sku: str 必填}`；`WHERE sku = :sku`（upper 归一）；`_query_with_retry`（超时 10s + 瞬时重试 1 次）；三态 JSON：ok(data 数组) / empty（编码不存在/已下线 + 防编造指引）/ error（error_type/retryable）；docstring 负向段（rag 未命中不调用、禁按名称猜测、混合块定位后传对应码）
- `product_dynamic_service.fetch_by_skus(skus) → dict[sku, row]`：IN 批量、入参序 ordered、异常内兜底 `{}`（节点用）
- 工具当前无批量路径；LLM 多码候选只能逐码调用

### 2.2 问题

1. 串行 N 查：延迟 N×、跨快照不一致（行间价格时刻不同）
2. LLM 需自行拼接 N 次结果做清单/对比——上下文碎片化，无"一次取齐"语义
3. 与节点通道能力不对齐（同数据同键，语义应一致）

## 3. 方案决策记录

| # | 决策 | 结论 | 理由 |
|---|---|---|---|
| D1 | 入参形态 | **双参并存**：`sku: str`（单码快捷）+ `skus: list[str]`（批量），**至少其一** | 单码场景（主流：定位单商品）保持最简调用；批量场景显式表达"候选全查"。不采用"单 list 参兼容单值"——单码高频场景让 LLM 每次包 list 反而别扭，且 sku 单参描述与方案 A 已落测试零迁移 |
| D2 | 双参同时提供 | **合并去重**（code 层），非互斥报错 | 语义自然（都想查就都查），避免 LLM 边界歧义；合并后统一去重保序 |
| D3 | 查询实现 | 单条 `WHERE sku IN (...)`，**不 gather 并行单查** | 1 往返 + 同快照（DB 语句级一致性）；并行单查跨快照且 N 往返 |
| D4 | 保序 | 代码层按入参序映射（SQL 不 ORDER BY） | 与 fetch_by_skus 同约定，LLM 按码对位零歧义 |
| D5 | 部分命中语义 | ≥1 命中 → `ok`（data 仅含命中行，count=命中数）；全缺失 → `empty`（message 列出全部入参码 + 已下线/不存在指引） | 缺失静默与节点一致；全空才 empty，避免"半 ok 半 empty"的双状态拼接 |
| D6 | 上限 | 合并去重后 ≤20 码，超限 → `invalid_argument`（retryable=true） | 防 LLM 全库扫描式滥用（与旧 limit≤20 语义一致）；20 码单 IN 参数量安全 |
| D7 | 归一 | 逐码 strip + upper + 去重 | 与现单码归一一致，防大小写差异稳定 empty |
| D8 | 实现归属 | 批量逻辑**内置于 tool**（复用现 `_query_with_retry` 骨架），**不强抽公共函数** | 节点 fetch_by_skus 的降级语义（静默 {}）与 tool 的错误语义（error JSON/empty）本就不同；公共化会引入 session 注入/重试策略参数化，得不偿失。行为约定（IN 一次/入参序/缺失静默）由两处各自实现 + 统一断言保证一致 |
| D9 | docstring | 增补批量指引段 | "何时用 skus：rag 多码候选需一次全查（清单/对比）；何时用 sku：已定位单商品" |

## 4. 详细设计

### 4.1 args_schema（product_stock_tool.py）

```python
class ProductStockLookupInput(BaseModel):
    sku: Optional[str] = Field(
        default=None,
        description="商品编码（单码，来自 rag_retrieval 返回段的【商品编码:】前缀，如 JD-LCK-001）。"
        "已定位单一目标商品时使用；sku 与 skus 至少提供一个",
    )
    skus: Optional[List[str]] = Field(
        default=None,
        description="商品编码列表（批量，一次检索全部候选的动态信息，单次查询同一时刻快照）。"
        "rag 返回段含多个编码且需一次全查（清单/对比/混合块候选）时使用；"
        "最多 20 个；与 sku 同时提供则合并去重",
    )
```

### 4.2 函数体（查询与三态）

```python
async def product_stock_lookup(sku: Optional[str] = None, skus: Optional[List[str]] = None) -> str:
    # 校验与归一(代码层,与现单码一致的手动校验路径)
    codes: list[str] = []
    if sku:
        codes.append(sku)
    if skus:
        codes.extend(skus)
    codes = list(dict.fromkeys(c.strip().upper() for c in codes if c and c.strip()))  # strip+upper+去重保序
    if not codes:
        return _error("invalid_argument", True, "参数错误：sku 与 skus 至少提供一个。…(引导先 rag 拿编码)")
    if len(codes) > 20:
        return _error("invalid_argument", True,
                      f"参数错误：skus 数量 {len(codes)} 超过上限 20。请按 rag 返回的候选范围收窄后重试。")

    stmt = select(ProductPriceStock).where(ProductPriceStock.sku.in_(codes))
    rows = await _query_with_retry(stmt)      # 超时/瞬时重试沿用;异常→ error JSON 不变

    if not rows:
        return _empty(codes)                  # 全缺失:message 列出入参码
    by_sku = {r.sku: r for r in rows}
    records = [ _record(by_sku[c]) for c in codes if c in by_sku ]   # 入参序 + 部分命中静默
    return _ok(records)
```

- `_empty` 文案扩展：批量时列出入参码（f"编码 {codes} 均未找到：…不存在或已下线…"），单码路径文案与现完全一致（回归 A6 用）
- `_record`：现 records dict 构建提取为小函数（字段不变：sku/product_name/category/current_price/stock_quantity/updated_at）
- docstring 增补（Returns 前插入）：
  ```
  批量指引: rag 返回多码候选需一次全查时传 skus=[...]（清单/对比/混合块）;
  已定位单一商品用 sku=。返回 data 顺序与入参一致,缺失的编码不报错(部分命中 ok,
  全部缺失 empty),勿据缺失码推断商品下线之外的结论。
  ```

### 4.3 一致性约定（与节点 fetch_by_skus 对齐，双实现同断言）

| 约定 | 节点 fetch_by_skus | tool skus 批量 |
|---|---|---|
| 查询 | 一次 IN | 一次 IN |
| 快照 | 语句级一致 | 语句级一致 |
| 顺序 | 入参序 | 入参序 |
| 缺失 | 静默（dict 少键） | 静默（data 少行；全缺才 empty） |
| 归一 | upper | upper |

### 4.4 影响面

| 文件 | 改动 |
|---|---|
| llm_backend/app/tools/product_stock_tool.py | args_schema 双参 + 批量查询 + empty 扩展 + docstring |
| llm_backend/tests/test_product_stock_tool.py | 增批量用例（见 §5），单码用例零改写（回归即验证） |
| product_dynamic_service / customer_tools / rag_tool / summarize | **零改动**（回归验证） |

## 5. 验证方案与验收断言

### 5.1 mock 单测（test_product_stock_tool.py 增补）

- **A1** 批量 SQL：`skus=["JD-DRY-003","JD-LCK-001"]` → 编译 SQL 含 `sku IN ('JD-DRY-003', 'JD-LCK-001')`；返回 data 序 = 入参序（mock 乱序返回也按入参序重排）
- **A2** 归一去重：`skus=["jd-dry-003", " JD-DRY-003 ", "JD-LCK-001"]` → 入参 2 码、SQL IN 无重复
- **A3** 部分命中：mock 仅返回 1/2 行 → `ok` count=1，data 为该命中行（缺失静默）
- **A4** 全缺失 → `empty`，message 含全部入参码
- **A5** 超限：21 码 → `invalid_argument` retryable=true，SQL 未执行
- **A6** 单码回归：现全部 `sku=` 用例不改动照跑（含 upper 归一/SQL/empty/error/重试/schema required）
- **A7** schema：`required` 为空数组、props 含 sku+skus、skus description 含"批量/一次/快照"

### 5.2 DB 冒烟（真实库，docker PG）

- **B1** `{"skus": ["JD-DRY-003", "JD-LCK-001"]}` → ok count=2，data[0].sku=JD-DRY-003（保序），价格与单查一致
- **B2** `{"skus": ["JD-DRY-003", "JD-NONE-999"]}` → ok count=1（部分命中静默）
- **B3** `{"skus": ["JD-NONE-999", "JD-XXX-000"]}` → empty
- **B4** 单码 `{"sku": "JD-DRY-003"}` 与批量单元素 `{"skus": ["JD-DRY-003"]}` 结果一致
- **B5** 与节点 `fetch_by_skus` 同一组 sku 返回行集合一致（两通道同键同数据）

### 5.3 回归

- **B6** 全量 mock 套件（方案 A 相关 75 项）零失败；`customer_tools` 节点用例不动照绿

## 6. 实施步骤

1. product_stock_tool.py：args_schema/函数体/empty/docstring（§4）
2. test_product_stock_tool.py 增补 A1~A7（单码用例零改写）
3. 跑 mock 全量 → DB 冒烟 B1~B5 → 回归 B6
4. 按 CLAUDE.md §6 更新本 spec 归档状态 → 归档 `已完成/`
5. git 提交推送（[feat]）

## 7. 待确认事项

| # | 事项 | 默认 | 说明 |
|---|---|---|---|
| 1 | 双参（sku/skus）并存 vs 单 list 参 | 双参并存（D1） | 单码高频场景调用最简；若审阅认为参数面冗余可改单 `skus` list（单元素即单查），改动集中在 schema+2 测试 |
| 2 | 双参同时提供=合并去重 | 合并（D2） | 亦可互斥校验（报 invalid 引导只用其一），保守选项 |
| 3 | 批量上限 20 | 20（D6） | 与旧 limit 上限一致；可下调为 10（对应 top-k 候选典型规模） |
| 4 | 部分命中 ok vs 逐码独立 empty | 部分命中 ok（D5） | 与节点缺失静默一致；若希望 LLM 感知缺失码可在 message 中附"未命中: [...]"提示字段（不改变 status） |
| 5 | 是否在 rag 侧 docstring 加"多码可批量 skus"引导 | 加一句 | 联动小改 rag_tool docstring（规则 4 后补"候选全查可用 skus=批量一次取回"） |

## 8. 风险与避坑清单

1. **契约稳定性**：tool 尚未 bind_tools，无历史调用方——本次 schema 扩展零兼容负担；一旦 agent 接线后参数形态冻结，改动成本上升（本 spec 应在此前落地）
2. **两通道语义分叉**：tool 与节点都实现"IN+保序+缺失静默"，靠各自测试断言锚定（A1/A3 + B5 交叉验证），不抽公共函数（D8）的前提是断言足够——代码评审关注点
3. **IN 参数上限**：PG 参数上限 ~65535，20 码无虞；防的是 LLM 滥用（D6），不是 DB 限制
4. **保序依赖代码层映射**：SQL `IN` 不保证序——records 必须按 codes 序重排（A1 mock 乱序返回即测此路径），勿依赖 DB 返回序
5. **与方案 A 纪律的张力**：批量是"候选全查"能力，不改变"未命中不调用/禁名称猜测"门控——docstring 负向段原样保留
