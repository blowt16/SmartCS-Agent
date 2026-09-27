# SPEC：售后 Agent 对齐与整改（提案，待审核）

> **归档状态**：⏳ **待审核**（2026-09-27 提案）
> 本文是**待决策的整改提案**，不是已批准的实施方案。§7 决策表与 §8 待确认项需要你裁决后才进入实施。
>
> **前置依赖（重要）**：本 spec 描述的目标态**依赖物流模块先落地** —— `shipments` 表与 `models/shipment.py` 目前**只存在于 `feat/admin-logistics` 分支，`main` 上没有**（实测 `git ls-tree main` 无 shipment/logistics 文件）。物流模块未合并前，本 spec 中涉及 `shipments` 的部分（§3.2 的 `logistics_query`、§7 的 D3）无法实施。
>
> **关联**：`docs/项目问题.md`（#17a 停用过滤已解决）、`docs/superpowers/specs/2026-09-27-管理端物流模块-design.md`、`llm_backend/app/lg_agent/intent_rules.py`、`SPEC_INTENT_RULE_LAYER.md`（二级场景的来源）

---

## 1. 背景与目标

### 1.1 背景

意图识别层**已经能把售后诉求分成 6 个二级场景**（`intent_rules.py:48-67` 规则层 + `lg_prompts.py:49-56` LLM 层，两层口径一致），并且经评测验证（golden 9/9）。但**下游什么都没有**：

```python
# llm_backend/app/lg_agent/lg_builder.py:318-323
async def aftersale_placeholder(state, *, config):
    """售后占位节点：返回"服务升级中"提示（静态话术）。
    接口与 multi_tool 子图同构（question+history → answer），
    后续售后 agent 子图就位后仅替换路由目的地。"""
```

也就是说：**识别层已经把用户想要什么分得很细，但没有任何数据表能承接这些诉求。**

### 1.2 目标

让售后 Agent 能**真正执行业务操作**（查订单、查物流、建售后单、改订单状态），而不是只回一句"服务升级中"。

### 1.3 本文要回答的问题

对齐 **意图层的 6 个二级场景** ↔ **现有数据表结构**，找出缺口并给出整改方案。

---

## 2. 售后业务全景（逐字取自代码，非推测）

### 2.1 六个二级场景

来源 `intent_rules.py:48-67`；语义描述来源 `lg_prompts.py:49-56`。

| sub_type | 规则层词表 | LLM 侧语义 | 需要执行**什么业务动作** |
|---|---|---|---|
| `return_refund` | 退货、退款、退钱、退了、退掉、不要了、无理由退、申请退、退单、取消订单 | 要退货、要退款、问退货流程与运费 | **建退货单 → 算退款额 → 等用户寄回 → 退款 → 回写订单** |
| `exchange` | 换货、换一个、换个、换新、换一款、换成、想换、以旧换新 | 要换商品/型号/颜色 | **建换货单 → 校验库存 → 指定新 SKU → 等寄回 → 寄出** |
| `reship` | 补发、少发、漏发、缺件、少了一件、少东西、没发、少给 | 少发/漏发/缺件，要求补寄 | **建补寄单 → 生成/关联运单** |
| `logistics_query` | 物流、快递、发货、什么时候到、到哪了、到哪、签收、派送、运单、配送、几天到、送到了吗、我的包裹 | 问包裹/快递/发货/配送/签收 | **按订单号查运单 + 轨迹** |
| `order_query` | 订单状态、查订单、查下订单、我的订单、订单号、订单记录、订单进度 | 查订单本身的状态/进度/订单号 | **按 user_id / order_no 查订单** |
| `other`（兜底） | 坏了、损坏、故障、质量问题、售后、退换、维修、返修、三包、过保、**价保** | 归不进上面的（如"东西坏了"要维修） | **建工单 → 转人工** |

> **按业务性质分三类**：
> - **查询类**（`order_query` / `logistics_query`）—— 只读，不产生单据
> - **受理类**（`return_refund` / `exchange` / `reship` / `other`）—— 需要落单据、有流转
> - **纯人工**（`other` 里的维修/三包/过保）—— 建单转人工，Agent 不做业务判定

### 2.2 关键约束（来自 prompt 与规则层注释）

- 含投诉语义的一律 `type=complaint`、`sub_type=none` —— **投诉不走售后 Agent**（`lg_prompts.py:63`）
- `sub_type` 是**最后**才判断的维度（`lg_prompts.py:66`）
- 「保修」有意不收词表（与"质保"歧义，`intent_rules.py:70`）

---

## 3. 现状实测取证

> 全部为真实库（`smartcs_agent`）与真实代码的实测结果，非推测。

### 3.1 相关表现状

```
users               5-6 行
orders              18 行（ORD-001..018，全有 user_id，分布 3→5 / 4→5 / 5→4 / 6→4）
tickets             8 行
shipments           0 行          ← 空表
product_price_stock 47 行
conversations       14 行
messages            66 行
```

**工单实际数据**：

```
TK-20260914000001  售后服务  已解决  "如果我买不合适，怎么去退货呢"     → 实为 return_refund
TK-20260914000002  其他      已解决  "ORD-001这个"                     → 订单号埋在用户原话里
TK-20260914000003  投诉      待处理  "我要投诉"
TK-20260914000004  退货咨询  待处理  "怎么退货？"                       → 实为 return_refund
TK-20260914000005  售后服务  待处理  "订单一直没发货，都一周了"          → 实为 logistics_query
TK-20260914000006  售后服务  已解决  "买贵了能退差价吗"                 → 实为 price_protection
TK-20260914000007  投诉      待处理  "收到货是坏的"
TK-20260914000008  其他      低/已解决  "发票怎么开"
```

### 3.2 逐场景能力对比

| 场景 | 需要的操作 | 现有表 | 判定 |
|---|---|---|---|
| `order_query` | 按 `user_id`/`order_no` 查订单 | `orders` | 🔶 **表够，链路断**（见下） |
| `logistics_query` | 按订单号查运单+轨迹 | `shipments` | ❌ **空表**：0 行；13/18 已发货以上订单无运单 |
| `return_refund` | 建退货单、算退款、回写订单 | **无** | ❌ 无表可落 |
| `exchange` | 建换货单、校验库存、指定新 SKU | **无** | ❌ 无表可落 |
| `reship` | 建补寄单、关联运单 | **无** | ❌ 无表 + **与运单表 1:1 冲突**（§3.4） |
| `other` | 建工单、转人工 | `tickets` | 🔶 **能存不能写**（§3.3） |

### 3.3 `tickets` 表的能力缺口（实测）

```
端点清单 （grep @router app/api/admin/tickets.py）:
  37: @router.get("")
  78: @router.put("/{ticket_id}")
  → 【没有 POST】

schema （app/schemas/admin.py）:
  class TicketUpdate     ← 存在
  class TicketCreate     ← 不存在（全仓 grep 零命中）
```

字段缺口：

| 缺什么 | 为什么必须 |
|---|---|
| `order_no` | 退货/换货/补发**全部要挂着订单**；现在只能靠裸文本猜（`TK-...0002` 的 `user_query='ORD-001这个'`） |
| `user_id` | 无法回答"我的售后记录" |
| 可执行的**类型** | 现在 `category` 是自由文本（`售后服务/退货咨询/投诉/其他`），**不携带动作信息**；`TicketUpdate.category` 是 `Optional[str], max_length=50`，无枚举、无 CHECK |
| 业务载荷 | 退款金额、换货目标 SKU、寄回/补寄运单号，**一个都没有** |
| 状态表达力 | `status` 仅 `待处理\|已解决`（`schemas/admin.py:62` 的 pattern）。退货要走「受理→待寄回→待退款→完成」，2 态表达不了 |
| `conversation_id` 实际全空 | 8/8 为 NULL（`seed_tickets.py:101` 写死），与对话流完全没接上 |

**接口层的现状**（`list_tickets`）：筛选条件只有 `status`（原值直传）与关键词（`ilike` 于 `ticket_no`/`summary`/`user_query`）。**改 status 语义会影响管理端工单页的筛选。**

### 3.4 `reship` 与运单表的**硬冲突**

补发的本质 = 「这单少发了，**再发一次**」→ 需要给**同一个订单**建**第二个运单**。

```python
# llm_backend/app/models/shipment.py:16-21
order_no = Column(String(32), ForeignKey("orders.order_no", ondelete="CASCADE"),
                  nullable=False, unique=True)
#                                 ^^^^^^^^^^^^
# 注释原文:"unique=True 不能省 —— 这是'一单一个运单'的强制点"
```

而物流模块 spec v2 又**主动把"一单可多运单"砍掉改成一对一**。

**⇒ `reship` 这个子场景在当前数据模型上根本落不了地。** 必须裁决（§7 D3）。

### 3.5 `order_query` 的四处断链

```
① orders.user_id         无索引 → 查"我的订单"全表扫
                         （实测 10 万单：无索引 6.436ms → 复合索引 0.040ms，快 161×）
② orders.buyer_name      是订单序号，与 users.username 零重合 → Agent 会念错买家名（§3.6）
③ AgentState             没有 user_id（grep 零命中）→ Agent 拿不到"我是谁"
④ /api/langgraph/query   无鉴权，user_id 是表单字段（main.py:216-221）→ 拿到也不可信
```

### 3.6 `orders` 的买家身份是**两套互不相干的数据**

```python
# llm_backend/scripts/seed_orders.py:59-61, 79-81
user_ids = (await s.execute(select(User.id).order_by(User.id).limit(4))).scalars().all()
# 注释原文:"user_id 现查轮转,不写死 [3,4,5,6] —— 库一旦重建/清过,写死会让外键直接报错"
...
"buyer_name": BUYER_NAMES[index % len(BUYER_NAMES)],   # 18 个编造名字，18 单各用一次
"buyer_code": f"P{index + 1:03d}",                      # P001..P018
"user_id":    user_ids[index % len(user_ids)],          # 前 4 个账号轮转
```

实测：

```
distinct buyer_name = 18   distinct buyer_code = 18   distinct user_id = 4   总单数 = 18
orders.buyer_name = users.username 的订单数 = 0
同一 user 名下订单的买家名: user 3 -> [沈七, 陈三, 周九, 李四, 陈静]
```

**结论**：
- `buyer_code` 是**订单序号**（P001↔ORD-001），不是买家标识
- `buyer_name` **也是**订单序号（18 名字 × 18 单，每个只用一次）—— **两列零身份信息量**
- `user_id` 取「前 4 个账号轮转」，**与角色无关** → **4 单挂在 `admin_test`（管理员）名下**

### 3.7 其他实测事实

| 事实 | 证据 |
|---|---|
| **全库 0 个 CHECK 约束** | `SELECT count(*) FROM pg_constraint WHERE contype='c'` = **0** |
| 客服侧完全不读订单 | `app/tools/` + `app/lg_agent/` + `app/services/` 里 grep `Order` **零命中** |
| `orders.status` 无状态机 | 接口层无流转校验（`orders.py:179-183` 只做 `signed_date` 与 `已签收` 的绑定）；物流 spec §9③ 确认「任意跳转是有意的」 |
| 两个签收日无一致性校验 | `orders.signed_date` 与 `shipments.signed_at` 各写各的，无校验 |
| 测试污染 | `orders_id_seq.last_value` = **1,203,814**，实际 18 行 —— 订单表被插删过 120 万次 |

---

## 4. 用户已决策：售后单据**统一写入工单表**

> 用户决策（2026-09-27）：**`return_refund` / `exchange` / `reship` 等业务，统一先创建工单写入工单表。**

### 4.1 这个决策的直接影响

**✅ 顺带解决了一个冲突**：补发的运单号存进工单（`reship_tracking_no`），**不用动 `shipments` 的 1:1** —— 避开了与物流模块（另一个窗口正在进行）的协调成本。

**代价**：补发的包裹**在物流页看不到**（物流页以 `shipments` 为数据源）。这个取舍需要确认（§8 Q4）。

### 4.2 需要把 `tickets` 从「人工工单」升级为「售后单据 + 人工工单」

现有 `tickets` 的设计目的是"人工处理的工单"，字段全是给**人**看的（`summary`/`detail`/`suggestion`/`handler`）。现在要让它同时承载**机器可执行的业务单据**，必须补四类东西：

1. **锚定关系**（现在工单是悬空的）
2. **机器可执行的类型**（现在 `category` 是给人看的自由文本）
3. **业务载荷**（退款额、换货 SKU、运单号）
4. **业务流转态**（现在 2 态）

---

## 5. 整改方案

### 5.1 表结构改造 —— `tickets`

```sql
-- ① 锚定关系（当前完全缺失）
ALTER TABLE tickets
  ADD COLUMN order_no VARCHAR(32) REFERENCES orders(order_no) ON DELETE SET NULL,
  ADD COLUMN user_id  INTEGER     REFERENCES users(id)        ON DELETE SET NULL;

-- ② 机器可执行的售后类型（与意图模块 sub_type 同源，零映射成本）
ALTER TABLE tickets
  ADD COLUMN after_sales_type VARCHAR(20);

-- ③ 业务载荷（按类型各自使用，其余为 NULL）
ALTER TABLE tickets
  ADD COLUMN refund_amount      NUMERIC(10,2),   -- 退款额 / 价保差价（return_refund / price_protection）
  ADD COLUMN new_sku            VARCHAR(32) REFERENCES product_price_stock(sku) ON DELETE SET NULL,  -- 换货目标
  ADD COLUMN return_tracking_no VARCHAR(50),     -- 用户寄回的运单（return_refund / exchange）
  ADD COLUMN reship_tracking_no VARCHAR(50);     -- 补发/换货寄出的运单（reship / exchange）

-- ④ 售后业务流转态（与通用 status 分开，见 §7 D2）
ALTER TABLE tickets
  ADD COLUMN after_sales_status VARCHAR(20);

-- ⑤ 索引：Agent 按这两个查（现在都没有）
CREATE INDEX ix_tickets_order_no         ON tickets(order_no);
CREATE INDEX ix_tickets_user_id          ON tickets(user_id);
CREATE INDEX ix_tickets_conversation_id  ON tickets(conversation_id);   -- 顺带补，Agent 建单会填它

-- ⑥ CHECK：全库 0 个，新建列时一并加（当前数据实测无违反，可直接加）
ALTER TABLE tickets ADD CONSTRAINT ck_tickets_urgency
  CHECK (urgency IS NULL OR urgency IN ('低','中','高'));
ALTER TABLE tickets ADD CONSTRAINT ck_tickets_status
  CHECK (status IN ('待处理','处理中','已解决','已关闭'));
ALTER TABLE tickets ADD CONSTRAINT ck_tickets_after_sales_type
  CHECK (after_sales_type IS NULL OR after_sales_type IN
         ('return_refund','exchange','reship','price_protection','other'));
ALTER TABLE tickets ADD CONSTRAINT ck_tickets_after_sales_status
  CHECK (after_sales_status IS NULL OR after_sales_status IN
         ('待受理','处理中','待用户寄回','待退款','待补发','待审核','已完成','已驳回'));
```

### 5.2 服务层与接口

**新建 `llm_backend/app/services/ticket_service.py`**

现在 `app/api/admin/tickets.py` 里**直接写 ORM**。Agent 在 `lg_agent` 内部，**不可能走 HTTP 调自己的服务** —— 必须有两边都能调的 service 层：

```python
async def create_ticket(
    *,
    user_query: str,
    after_sales_type: str | None = None,
    order_no: str | None = None,
    user_id: int | None = None,
    conversation_id: int | None = None,
    summary: str | None = None,
    urgency: str | None = None,
    payload: dict | None = None,      # refund_amount / new_sku / *_tracking_no
    db: AsyncSession,
) -> Ticket: ...

async def transition(ticket: Ticket, *, status: str | None = None,
                     after_sales_status: str | None = None, db: AsyncSession,
                     handler: str | None = None) -> Ticket: ...
```

调用方：管理端 `POST /api/admin/tickets` 与**售后 Agent 的节点**。

**接口改动**：

```
① 新增 POST /api/admin/tickets        —— 管理端手工建单 + Agent 建单的 HTTP 入口
② PUT /{id} 增加状态流转校验          —— 现在无任何约束
③ GET "" 增加按 after_sales_type / order_no 筛选（可选）
④ _serialize 增加新字段（前端处理弹窗要用）
```

### 5.3 三条共性前置（**6 个场景全卡在这，不做则后面全白做**）

| # | 缺口 | 整改 |
|---|---|---|
| **P1** | `/api/langgraph/query` **无鉴权**，`user_id` 是表单字段（`main.py:216-221`） | 加 `current_user: User = Depends(get_current_user)`，**用服务端解析的 id 覆盖表单值**。不做的话，Agent 一旦能做业务操作就是越权通道 |
| **P2** | `AgentState` **没有 `user_id`**（grep 零命中） | state 加 `user_id: int`，`langgraph_query` 构造初始 state 时从已验证的 `current_user.id` 注入。（`user_id` 已在 `thread_config.configurable` 里，透传即可） |
| **P3** | **全库 0 个 CHECK** | 见 §5.1 ⑥。Agent 直写库时，所有 Pydantic pattern 全部失效 |

### 5.4 `orders` 索引与买家身份收敛

**索引（实测 161× 收益）**：

```sql
CREATE INDEX ix_orders_user_id_date ON orders(user_id, order_date DESC, id DESC);
```

> 实测（10 万订单 / 2000 用户）：`WHERE user_id=? ORDER BY order_date DESC, id DESC LIMIT 12`
> 无索引 **6.436ms** → 单列 `(user_id)` 0.151ms → **复合 0.040ms**。
> 复合索引的**最左前缀**即 `user_id`，故**不需要再建单列**。
> 按订单号查已由 `orders_order_no_key`（UNIQUE）覆盖，**无需改动**。

**买家身份收敛**（§3.6 的问题）—— 待决策，见 §7 D6。

---

## 6. 非目标（明确不做）

| 不做 | 理由 |
|---|---|
| 新建 `after_sales_requests` 独立表 | 用户已决策统一进工单表 |
| 放开 `shipments` 的 1:1 | 改工单承载补发运单即可，避免与物流模块协调 |
| 售后 Agent 子图本身 | 本文只做**对齐与整改**，Agent 实现是后续独立任务 |
| 订单状态机（`ALLOWED_TRANSITIONS`） | 物流 spec §9③ 确认「任意跳转是有意的」；本文只补 CHECK，不改流转语义 |
| `orders.buyer_name` 的 `ilike` 搜索优化 | 前后模糊用不上 btree，数据量也不值得上 `pg_trgm` |
| 测试与生产共库的隔离 | 已由 `SPEC_BM25_TEST_ISOLATION` 单独跟踪；订单表 `id_seq` 已到 120 万是同一问题的新证据，但属独立课题 |

---

## 7. 决策表

| # | 决策 | 状态 | 取舍 |
|---|---|---|---|
| **D1** | 售后单据统一落在 `tickets` | ✅ **用户已决策** | 顺带解决 reship 的 1:1 冲突；代价是补发包裹在物流页不可见 |
| **D2** | **状态用单字段还是双字段？** | ⏳ **待裁决** | 见下 |
| **D3** | **`reship` 的运单归属** | ⏳ **待裁决** | 见下 |
| **D4** | **`price_protection` 是否提为独立二级场景** | ⏳ **待裁决** | 见下 |
| **D5** | `category` 收敛为枚举，取值与意图模块 `sub_type` 同源 | ⏳ 待确认 | 需处理 8 条存量数据的映射 |
| **D6** | **买家身份怎么收敛** | ⏳ **待裁决** | 见下 |
| **D7** | `orders` 建复合索引 `(user_id, order_date DESC, id DESC)` | 建议采纳 | 已实测；**同时解决"删除用户"的级联全表扫**（382ms→12ms） |
| **D8** | 三条共性前置 P1/P2/P3 必做 | 建议采纳 | P1（鉴权）是售后 Agent 的上线前提 |

### D2：状态单字段 vs 双字段

「工单处理完了吗」（管理视角）与「退货走到哪一步」（业务视角）**是两个不同问题**。

| | 做法 | 影响 |
|---|---|---|
| **甲** | `status` 扩成超集：`待受理\|处理中\|待用户寄回\|待退款\|待补发\|已完成\|已驳回` | 少一个字段；但**破坏现有管理端工单页的筛选语义**（"已解决"对一个还在等寄回的退货单意味着什么？），且非售后工单（发票、投诉）被迫用售后态 |
| **乙** ✓ | `status` 扩为 `待处理\|处理中\|已解决\|已关闭`（**向后兼容**，现有两态是子集）+ 新增 `after_sales_status` 承载业务流转 | 多一个字段；但两个问题各归各，**现有工单页不用改** |

**建议乙**。依据：现有 8 条工单里 **3 条根本不是售后动作**（投诉×2、`其他`"发票怎么开"），甲会逼它们用售后状态。

### D3：`reship` 的运单归属

| | 做法 | 影响 |
|---|---|---|
| **甲** ✓ | 补发运单号存工单的 `reship_tracking_no`，**不动 `shipments`** | 零协调成本；但补发包裹在物流页看不到 |
| **乙** | 放开 `shipments.order_no` 的 unique，加 `shipment_type` 区分首发/补发 | 物流页完整；但要改**另一个窗口正在做的表**，且推翻其 spec v2 的决策 |

**建议甲**（与 D1 一致，成本最低）。若你要求物流页完整，则选乙 —— 但那需要与物流模块负责人协调。

### D4：`price_protection`（价保）是否提为独立二级场景

**现状**：价保埋在 `AFTERSALE_GATE` 兜底词里 → 归 `other`。

**但它可能是最容易做出真实价值的一类**：

- **数据现成可算**：`orders.amount` vs `product_price_stock.current_price` → 直接得差价（实测现在 18/18 相等，商品一调价就有真实差价）
- **业务规则已存在**：`seed_tickets.py:63-66`「签收后 7 日内可申请，需提供新价格截图，审核通过后原路退回差价」
- **已有真实样例**：`TK-20260914000006`「买贵了能退差价吗」（现归在 `category='售后服务'`）

**建议**：提为独立子场景（改 `intent_rules.py` 的 `AFTERSALE_RULES` + prompt + `after_sales_type` 枚举加值）。**但这会改意图模块，属独立改动，可拆。**

### D6：买家身份怎么收敛

现状见 §3.6（两套买家数据零重合，订单还挂在管理员名下）。

| | 做法 | 影响 |
|---|---|---|
| **甲** | seed 改成 `buyer_name = 该 user 的 username`、`buyer_code = U{user_id:03d}`；user 轮换加 `WHERE role='user'` | 改动最小（只动 `seed_orders.py`）；但 `test_user`/`blowt` 当买家名不好看 |
| **乙** | 把两列改名为 `receiver_name`/`receiver_code`，**明确降级为"收货人"**，身份一律走 `user_id` | 语义最清晰；需 `ALTER TABLE ... RENAME COLUMN` + 全链路改名（接口/前端/测试） |

**建议甲**起步（先让数据自洽），乙作为后续演进。

---

## 8. 待确认项

| # | 事项 | 建议 |
|---|---|---|
| **Q1** | D2 状态模型选甲还是乙？ | 乙 |
| **Q2** | D3 reship 运单归属选甲还是乙？ | 甲（存工单） |
| **Q3** | D4 价保是否提为独立子场景？本次做还是拆独立任务？ | 拆独立任务（要改意图模块） |
| **Q4** | 接受"补发包裹在物流页不可见"吗？ | 若不能接受则 D3 选乙 |
| **Q5** | D6 买家身份选甲还是乙？ | 甲 |
| **Q6** | 实施时机：**等物流模块合并后再做**，还是现在并行？ | 涉及 `logistics_query` 的部分必须等；工单改造可并行（不碰 logistics 文件） |

---

## 9. 实施顺序

> **依赖**：§1 前置依赖 —— 涉及 `shipments` 的部分需物流模块先合并。**工单改造与前置 P1/P2 不依赖它，可先行。**

```
阶段一：共性前置（不依赖任何新表，可立即做）
  1. P1 /api/langgraph/query 加鉴权
  2. P2 AgentState 加 user_id
  3. D7 orders 复合索引（模型 index=True + init_db 迁移）

阶段二：工单表改造（不碰 logistics，可并行）
  4. §5.1 表结构 + 索引 + CHECK（一条迁移）
  5. §5.2 ticket_service.py（管理端与 Agent 共用）
  6. §5.2 POST 端点 + PUT 流转校验 + _serialize 扩字段
  7. D5 category 存量映射 + 收敛枚举

阶段三：数据自洽
  8. D6 买家身份收敛（seed_orders.py）

阶段四：依赖物流模块
  9. logistics_query 链路（等 shipments 落地 + 有数据）
  10. 明确"权威签收日"口径（orders.signed_date vs shipments.signed_at）
```

**阶段一、二完全独立于物流模块，可以现在开始。**

---

## 10. 验收断言

| # | 断言 |
|---|---|
| **A1** | `POST /api/admin/tickets` 能建单，且 `order_no`/`user_id`/`after_sales_type` 落库正确 |
| **A2** | 按 `user_id` 建单后，`GET /api/admin/tickets?keyword=<order_no>` 能查到 |
| **A3** | 非法 `after_sales_type` / `after_sales_status` 被 DB CHECK 拒绝（**绕过 Pydantic 直插也应被拒**） |
| **A4** | `orders` 复合索引生效：`EXPLAIN SELECT * FROM orders WHERE user_id=? ORDER BY order_date DESC, id DESC LIMIT 12` 走 Index Scan（非 Seq Scan + Sort） |
| **A5** | `AgentState` 里能读到 `user_id`，且**与登录令牌一致**（不是表单自报值） |
| **A6** | 伪造 `user_id` 调 `/api/langgraph/query` **不生效**（服务端以令牌为准） |
| **A7** | 迁移幂等：连跑两次 `init_db.py` 无报错 |
| **A8** | 存量 8 条工单的 `category` 映射后，`after_sales_type` 取值合法且非空（或明确为空） |

---

## 11. 风险

| # | 风险 | 处置 |
|---|---|---|
| **R1** | **与物流模块冲突** | §5.1 不碰 `shipments`/`logistics.py`（D3 选甲即无冲突）；但 `init_db.py` 与 `schemas/admin.py` 两边都会改 → **合并时有冲突**，建议物流先合并 |
| **R2** | 改 `status` 语义影响管理端工单页 | D2 选乙可避免（向后兼容超集） |
| **R3** | 存量 8 条工单 `category` 映射有歧义 | 「售后服务」这个值不携带动作信息，需人工判定（数据量小，可接受） |
| **R4** | `after_sales_type` 与意图模块 `sub_type` 漂移 | 两处枚举必须同源；建议在 `intent_rules.py` 注释里互相指路 |
| **R5** | 测试与生产共库污染（`orders_id_seq` 已 120 万） | 本次不处理，但**售后 Agent 的评测若也打同一张表会更严重**；参见 `SPEC_BM25_TEST_ISOLATION` |
| **R6** | Agent 绕过 service 层直写库 | 靠 CHECK 兜底（P3）；若允许直写，`signed_date` 等接口层规则会失效 |

---

## 12. 附：本 spec 的取证清单

| 结论 | 取证方式 |
|---|---|
| 6 个二级场景与词表 | `intent_rules.py:48-67` 逐字读取 + `lg_prompts.py:49-56` 交叉核对 |
| `tickets` 无 POST / 无 TicketCreate | `grep @router` + `grep TicketCreate` 全仓零命中 |
| 工单 8 行 / conversation_id 8/8 NULL | 直接 SQL |
| `shipments` 0 行 / 13 单无运单 | 直接 SQL |
| 买家身份两套零重合 | SQL 比对 `orders.buyer_name` vs `users.username`；读 `seed_orders.py:59-61,79-81` |
| 全库 0 CHECK | `SELECT count(*) FROM pg_constraint WHERE contype='c'` |
| 无鉴权 | 读 `main.py:216-221` 与全文件 grep `Depends` |
| `AgentState` 无 user_id | `grep user_id lg_states.py` 零命中 |
| orders 索引收益 161× | TEMP 表造数 10 万单实测（EXPLAIN ANALYZE） |
| shipments 仅存在于 feat/admin-logistics | `git ls-tree main` |
