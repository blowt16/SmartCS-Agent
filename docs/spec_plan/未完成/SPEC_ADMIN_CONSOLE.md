# 管理端（Admin Console）实施规格

> **用途**: 为 SmartCS-Agent 增加一个独立的管理员端，含控制台、商品管理、订单管理、知识库管理、工单管理五个模块。管理员登录后进入，风格与客户端一致（同套 Tailwind + 品牌绿 `#16a34a`）。
> **依赖前置**: 无阻塞依赖。可复用的既有件：`users` 表 + JWT 登录链路（`app/api/auth.py`）、`product_price_stock` 表（47 行真实数据）、`POST /api/upload` 索引链路（`app/services/indexing_service.py`）、`documents`/`document_chunks` 表。
> **技术栈**: FastAPI + SQLAlchemy async + psycopg + PostgreSQL/pgvector（后端）；Vue3 + Vite + Tailwind + ECharts（前端）。
> **状态**: ⏳ 待实施（设计已评审，2026-09-22）
> **修订④**: 2026-09-22 用户变更知识库上传方案——由"上传即入库、取消不撤销"改为**两阶段暂存制**（D7/D17/D18/D19）：`stage` 只存文件 + 取基本信息（**不解析内容、不清洗、不分块、不嵌入、不写 DB**），**「取消」调 `unstage` 真撤销**，点「保存」才 `commit` 走完整索引链路。**连带推翻初稿"复用 `/api/upload`"**（该接口没有"只存不索引"的模式），知识库端点 4 → 6；并删掉初稿计划的"单条详情"端点（`commit` 响应直接返回完整行）。§5.7 / §6.4.5 / §6.7 / §8.5 / §9 / §附 已同步。**暂存残留补了清理方案**（§6.4.5.1，机会式清理 + `KNOWLEDGE_STAGE_TTL_HOURS=24`），风险清单增至 34 条。
> **修订③**: 2026-09-22 用户评审确认全部 5 项待确认事项，其中**订单管理页由卡片网格改为表格**（与工单页同风格，D20）——这也澄清了最初需求里"订单管理参考工单管理界面"指的是表格而非卡片版式。正文 §6.4.4 已重写，§11 改为确认记录，§6.5 注明 `ProductThumb` 收窄为商品页专用。
> **修订②**: 2026-09-22 按用户要求重构知识库模块——**`documents` 不加 `title` 列**（D16，列表直接用 `original_filename`）、**新增文档改为"上传驱动"表单**（D17：点「上传文件」→ 立即入库解析 → 基本信息自动回填只读区 → 补描述 → 保存）、**「创建时间」为纯展示项**（不在表单字段中，上传成功后出现；为此新增 `GET /api/admin/knowledge/{md5}` 详情端点，D18）。连带更新 §4.4 / §5.7 / §5.9 / §6.4.5 / §8.5 / §9-14 / §附，风险清单增至 32 条。
> **修订①**: 2026-09-22 完成一轮对抗性审计（自查 + 前端/后端两路独立复核，实测方式：起真实 FastAPI 复现路由与鉴权、连真实库核对数据、建 Vite 探针工程验证多页构建与 Tailwind content 行为、直接构造 pydantic 模型验证空串/None 行为）。结论已合入正文：**2 处阻断**（`require_admin` 缺 `User` 导入 → 服务起不来；`src/admin/main.js` 导入清单缺失 → 管理端无样式无图标）、8 处高危、20+ 处中低，风险清单从 16 条扩到 29 条。**其中 4 处是我原稿的事实错误**（`Decimal` 手搓 dict 其实能序列化 / `App.vue` 行数 / 测试文件数 / 知识库新增表单的标题自相矛盾），已在正文更正并保留了"原稿错在哪"的说明，便于后续复核。
> **关联文档**: `CLAUDE.md` §6（spec 生命周期）§商品知识文档编写规范（知识分层原则）、[[SPEC_SKU_ALIGNMENT.md]]（商品 sku 对齐键，本 spec 风险 §12-1 的依据）、[[SPEC_FRONTEND_VUE3_REFACTOR.md]]（客户端前端结构 = 本 spec 的隔离对象；亦是 D8 历史注脚的出处）、`docs/项目问题.md` #14（知识库归属）/#15（浏览器实测）、`docs/PROJECT_ANALYSIS.md` §10.1（业务端点鉴权现状）

---

## 目录

1. [范围与明确不做](#1-范围与明确不做)
2. [背景与现状（实测）](#2-背景与现状实测)
3. [决策记录](#3-决策记录)
4. [数据表设计](#4-数据表设计)
5. [后端接口设计](#5-后端接口设计)
6. [前端设计](#6-前端设计)
7. [种子数据规范](#7-种子数据规范)
8. [测试方案](#8-测试方案)
9. [验证方案（按序执行）](#9-验证方案按序执行)
10. [实施步骤](#10-实施步骤)
11. [评审确认事项（5 项已全部落定）](#11-评审确认事项5-项已全部落定)
12. [风险与避坑清单](#12-风险与避坑清单)

---

## 1. 范围与明确不做

### 1.1 本次做

| # | 模块 | 交付 |
|---|---|---|
| 1 | 数据层 | 新建 2 表（`orders` / `tickets`）+ 改 2 表（`users` 加 `role`；`documents` 加 `description` / `status` **两列，不加 `title`**） |
| 2 | 鉴权 | `users.role` 列 + `require_admin` 依赖 + 管理端接口组级鉴权 |
| 3 | 后端接口 | **19 个**新端点（`/api/admin/*`）+ 2 个既有响应扩展（`Token`/`UserResponse` 加 `role`）。**`/api/upload` 保持原样但管理端不再调用**（D7/D17） |
| 4 | 前端 | 新入口 `admin.html` + `src/admin/` 目录，5 个页面 + 登录页 + 通用弹窗/分页/图表组件 |
| 5 | 数据 | 4 个幂等脚本：管理员账号、订单种子、工单种子、商品占位图生成 |
| 6 | 测试 | 5 个测试文件（auth / products / orders / knowledge / console）+ conftest 补 4 个 fixture，重点覆盖越权（401/403）与 CRUD 契约 |

### 1.2 明确不做

| 项 | 理由 |
|---|---|
| 用户管理页 | 用户明确要求只做 5 个模块（参考图导航里的「用户管理」不做） |
| 智能客服页 | 用控制台的「体验客服」按钮跳转到客户端替代（`window.open('/')`） |
| 工单从对话流真实生成 | 用户选择"只做种子数据"。表已预留 `conversation_id` 字段，将来接驳不改表 |
| 订单与下单链路打通 | 项目无购物车/结算链路，订单只能是种子数据（见 §2.2） |
| 商品图片入库 | TSV/表/脚本均无图片来源，改用本地生成的 SVG 占位图按 sku 路径寻址（见 §6.5） |
| 商品写入时同步更新 docx | 违反 `CLAUDE.md` 知识分层原则（动态信息禁止写 docx）。新增商品只有价格库存，无静态知识 |
| 管理端操作审计日志 | 无人要求；不引入 `admin_logs` 表 |
| 客户端回归改动 | `index.html` / `App.vue` / `main.js` / `global.css` 一行不动（见 §6.1） |
| 补全既有端点的鉴权 | `/api/upload`、`/api/documents` 不校验令牌是**既有缺陷**（`docs/项目问题.md` #14）。本次只保证管理端端点有鉴权，不动既有端点行为 |
| 数据库时区迁移 | `timestamptz` 迁移是独立议题（`docs/项目问题.md` #15 附带发现①），本 spec 沿用"约定库时区为 UTC" |

---

## 2. 背景与现状（实测）

> 以下数据均为 2026-09-22 实连数据库 + 全仓检索所得，非推断。

### 2.1 数据库现状

```
TABLES: checkpoint_blobs, checkpoint_migrations, checkpoint_writes, checkpoints,
        conversations, document_chunks, documents, messages, product_price_stock, users

users: 4 行        → (3, test_user), (4, 浏览器测试), (5, blowt), (6, admin_test)
conversations: 13 行
messages: 64 行
documents: 2 行    → 都挂在 user_id='6'（京东自营售后政策.docx 4块 / 京东智能家具产品知识文档.docx 38块）
document_chunks: 42 行
product_price_stock: 47 行
```

商品品类分布（真实）：

| 品类 | 数量 | 品类 | 数量 | 品类 | 数量 |
|---|---|---|---|---|---|
| 智能门锁 | 12 | 电动升降桌 | 6 | 智能床垫 | 3 |
| 电动智能沙发 | 7 | 智能晾衣架 | 5 | 智能床头柜 | 2 |
| 智能窗帘 | 6 | 智能电动床 | 5 | 按摩椅 | 1 |

**注意**：TSV `scripts/data/jd_smart_furniture.tsv` 有 50 条商品，DB 只有 47 行——3 条因价格解析失败被 `import_product_price_stock.py:81-104` 整行跳过。控制台统计以 DB 为准（47），不是 TSV（50）。

### 2.2 能力空白（决定了本 spec 的形态）

| 能力 | 现状 | 证据 |
|---|---|---|
| 订单 | **零**。无表、无模型、无 mock、无接口 | `grep "class Order" llm_backend/` → 0 命中 |
| 工单 | **零**。唯一"转人工"是静态话术，不落库 | `lg_builder.py:208-216` `transfer_human` 直接 return 一条 AIMessage |
| 商品图片 | **零**。TSV 9 列无图片列、表无列、无图片目录、生成脚本无 `add_picture` | `head -1 scripts/data/jd_smart_furniture.tsv` |
| 权限/RBAC | **零**。`users` 无 role/is_admin；`status` 列无任何代码读它做鉴权 | `app/models/user.py:5-18` |
| 业务端点鉴权 | **零**。全仓仅 `/api/users/me` 挂了 `Depends(get_current_user)` | `grep -rn get_current_user` → 仅 `auth.py:5,45` / `security.py:23-24` |
| 前端路由 | 无 vue-router、单 HTML 入口、vite 未配多页 | `frontend/package.json:11-24`、`vite.config.js` |
| 图表库 | 无 | 依赖仅 vue/marked/highlight.js/fontawesome/inter |

### 2.3 两条对设计有决定性影响的既有事实

**A. 检索侧不按 `user_id` 过滤 → 管理员上传的知识全局可见。**

`document_chunks` 的向量路（`rag_retriever_service.py` 全表 `order_by(distance).limit(top_k)`）与 BM25 路（`bm25_sql_retriever.py:46` `WHERE document_chunks.content_tsv @@ tsq.q`）**均无 user_id 谓词**。所以管理端上传的文档，客户端聊天**立刻可检索**——这正是知识库管理模块成立的前提。反过来，管理端列表若沿用 `/api/documents?user_id=` 的过滤，就会像 `docs/项目问题.md` #14 那样"看不到全平台文档"。故管理端用独立接口、不做 user_id 过滤（§3 D6）。

**B. 前端传的是明文密码，`hashing.py` 的注释是过期的。**

`hashing.py` 的 docstring 写"plain_password: 前端已经做过 SHA256 的密码"，但**前端已不再做 SHA256**：

- `LoginView.vue` `submit()` → `apiLogin(email.value, password.value)`（原样传 `ref` 值）
- `api/auth.js` `login()` → `JSON.stringify({ email, password })`（无哈希）
- `grep -rn "sha256|SHA256|crypto.subtle" frontend/src/` → **0 命中**

因此种子脚本建管理员账号时**必须 `get_password_hash(明文密码)`**，绝不能自作主张先算一遍 SHA256——否则该账号永远登不上。这是本 spec 最容易踩的坑，已列入 §12 风险清单。

---

## 3. 决策记录

| # | 决策 | 选定方案 | 备选与否决理由 |
|---|---|---|---|
| **D1** | 管理员身份 | `users` 表加 `role VARCHAR(20) DEFAULT 'user'` | 备选"独立 `admins` 表 + `/api/admin/login`"：物理隔离更干净，但要复制一整套登录/JWT/校验代码，且管理员的聊天记录、知识库归属仍需回到 `users` |
| **D2** | 登录接口 | **复用** `POST /api/token`，不分接口；管理端有**自己的**登录页（§6.3.1） | 一套登录逻辑。管理员在 `/admin.html` 登录进控制台，在 `/` 登录进聊天（两边都能用，对应"体验客服"） |
| **D3** | 工单数据来源 | **只做种子数据**（用户指定） | 备选"从对话流真实生成"：需改 `lg_builder.py` 三个节点 + 流式事件协议，工程量翻倍且本轮不需要 |
| **D4** | 订单数据来源 | 新建 `orders` 表 + 种子脚本（用户指定） | 备选"把商品当订单展示"：订单号/买家/状态全是编的，语义对不上，否决 |
| **D5** | 商品写权限 | **完整 CRUD**（用户指定） | 备选"只改价格库存"：参考图的新增按钮就没了 |
| **D6** | 管理端知识库列表 | 新建 `/api/admin/knowledge`，**不做 user_id 过滤** | 备选"复用 `/api/documents`"：该接口强制 `user_id` 过滤（`main.py:191`），管理员看不到别人的文档，也看不到 `docs/项目问题.md` #14 修复后挂在 id=6 的那 2 份 |
| **D7** | 知识库上传 | **不复用 `POST /api/upload`**，管理端新增**两阶段**端点：`stage`（暂存，只存文件+基本信息）与 `commit`（索引，走完整链路） | **此项推翻了初稿"复用当前接口"的说法**（用户 2026-09-22 变更需求）。理由：`/api/upload` 的语义就是"上传即索引"——收文件 → 解析 → 清洗 → 分块 → 嵌入 → 落库，**没有"只存不索引"的模式**。而新需求要求"取消能撤销"，只有在索引前拦一道才可能。详见 D17 |
| **D8** | 前端组织 | 独立 `admin.html` 多页入口（用户指定） | 备选"装 vue-router 统一 SPA"：要重构 `App.vue`（**实测 330 行**，登录态/会话/文档/聊天状态全堆在一个文件），客户端有回归风险 |

**D8 的历史注脚（值得知道）**：这个项目**曾经有过两个前端入口**，并在 `SPEC_FRONTEND_VUE3_REFACTOR.md`（已完成）里**特意收敛成一个**。但那次收敛的理由与本方案无关——当时是"两个**互相独立、其中一个仓库里连源码都没有**的代码库"（旧 Vue 产物 `/` + 手写 1434 行 `/chat.html`），收敛是为了消灭不可维护的重复代码库，并把 `/chat.html` 的 `FileResponse` 路由一并删除。本方案的两个入口是**同一个标准 Vite 工程内的两个页面**，共享 `src/api/`、`package.json`、Tailwind 配置，不存在重复代码库问题。区别记在这里，免得后来者看到"又变回两个入口了"困惑。
| **D9** | 商品图片 | 脚本生成本地 SVG 占位图（用户指定） | 备选"外部占位图服务"：国内网络可能加载不出，演示不可靠 |
| **D10** | 图表 | 引入 ECharts（用户指定） | 只打管理端的包，客户端 bundle 不受影响（Vite 按入口分包） |
| **D11** | 导航范围 | 5 项 + 「体验客服」按钮 | 不做用户管理（§1.2） |
| **D12** | 后端代码组织 | 路由按模块拆 `app/api/admin/` 5 个文件，**不建 service 层** | 遵循 `main.py` 既有做法（`/api/documents` 等端点直接 `select`），CRUD 逻辑薄，建 service 层是纯样板 |
| **D13** | 工单状态取值 | `待处理` / `已解决`（两值） | 对齐参考图（列表徽章 + 环形图图例均只此两值），不擅自加"处理中" |
| **D14** | 订单状态取值 | `处理中` / `已发货` / `已送达`（三值） | 对齐参考图（徽章 + 环形图图例；本实现用表格行内徽章，取值与配色不变） |
| **D15** | 管理端入口的**发现路径** | 写进 README + 控制台不重复提供入口；**客户端一行不改**（登录成功不按 role 跳转） | 备选"客户端登录后 `if (role==='admin') location.href='/admin.html'`"：要改 `LoginView.vue`，与 §6.1 的"客户端零改动"直接冲突（§6.1 冻结了 `App.vue`/`main.js`/`components/*`，改了就没有文件能承载这个跳转）。管理端登录页已有「返回客服端」链接，反向路径是通的；正向路径靠 README 与书签 |
| **D16** | 文档标识 | **不给 `documents` 加 `title` 列**，列表直接用 `original_filename`，列头叫「文件名」 | 备选"加 title 列"：存量 2 行无标题要靠 `title \|\| original_filename` 回退；上传表单里"标题"与"文件名"语义重叠；列表两列显示同一内容浪费宽度（详见 §4.4） |
| **D17** | 新增文档的**交互形态** | **暂存制（两阶段）**：点「上传文件」→ **只存文件 + 取基本信息（文件名/类型/大小/MD5），不解析内容、不清洗、不分块、不嵌入、不落库** → 基本信息回填只读区 → 补描述 → 点「保存」才走完整索引链路 → 成功后才建 `documents` 行 | **用户方案（2026-09-22）**，取代初稿的"上传即入库"。核心收益：**「取消」能真正撤销**（删掉暂存文件即可，什么都没留下）。备选"上传即索引 + 取消时反向删除"：要在取消路径上调索引删除接口，一旦删除失败就留下半成品，且索引已经跑完（PDF 走 MinerU 云端，白花钱和时间） |
| **D18** | 「片段数」「创建时间」何时可得 | **只有 `commit` 之后才有**——它们是索引链路的产物（片段数来自分块，创建时间来自 `documents` 行）。表单在保存成功后切到「完成」态补齐这两项 | 备选"暂存时就解析出片段数"：**与 D17 直接冲突**（分块依内容而定，要分块就得先解析；PDF 解析是 MinerU 云端调用，用户一取消就白跑）。所以暂存态只显示"文件的基本信息"三项，片段数/创建时间标注为"保存后生成" |
| **D19** | 「创建时间」的接口来源 | **`commit` 的响应直接返回完整文档行**（含 `created_at` / `chunk_count`），**不需要单独的详情接口** | 初稿曾计划加 `GET /api/admin/knowledge/{md5}` 详情接口，用于上传后回填 `created_at`。改成两阶段后，`commit` 本来就由管理端控制、本来就在写这一行，**顺手返回它即可**——详情接口因此删除（编辑弹窗的数据来自列表行，也不需要它） |
| **D20** | 订单管理页版式 | **表格，与工单页同风格**（用户指定） | 备选"参考图的卡片网格"：**已否决**。注意与 D5/§6.4.3 的区别——**商品管理页仍是卡片网格**（用户另有明确要求「商品管理界面呈现该效果」），只有订单页是表格。两页版式不同是有意为之，不是遗漏 |

**D15 的代价（明说）**：`Token.role` 在客户端侧**没有任何消费者**（`src/api/auth.js:44-46` 的 `login()` 只取 `access_token`），是给管理端与外部联调用的。若将来想在客户端做"管理员登录自动跳管理端"，需要动 `LoginView.vue`，届时另开一条改动，不在本次范围。

---

## 4. 数据表设计

### 4.1 新建 `orders`（订单）

文件：`llm_backend/app/models/order.py`

```python
from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, func

from app.core.database import Base


class Order(Base):
    """订单表:项目无下单链路,数据由 seed_orders.py 从 product_price_stock 生成"""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(32), nullable=False, unique=True)   # ORD-001 形式
    sku = Column(String(32), nullable=False)                     # 关联商品(product_price_stock.sku)
    product_name = Column(String(255), nullable=False)           # 下单时快照,商品改名不影响历史订单
    category = Column(String(50), nullable=False)
    buyer_name = Column(String(50), nullable=False)              # 展示名
    buyer_code = Column(String(20), nullable=True)               # P001 形式
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Numeric(10, 2), nullable=False)              # 下单金额
    status = Column(String(20), nullable=False, default="处理中")  # 处理中/已发货/已送达
    order_date = Column(Date, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

**设计说明**

| 字段 | 为什么这么设计 |
|---|---|
| `product_name` 快照 | 商品改名后历史订单应保留原名。若直接 join `product_price_stock` 取名字，改一次商品名会改写所有历史订单的展示 |
| `amount` 独立列 | 商品改价后历史订单金额不应跟着变。种子取下单时刻的真实价格写入 |
| `user_id` 可空 | 参考图 15 个买家只有 4 个真实用户，种子轮转填充；管理端新增订单时可不选（填买家名即可）。`ON DELETE SET NULL` 保证删用户不炸订单 |
| 不建 `sku` 外键 | `product_price_stock` 的 sku 是唯一键但**删商品不应连带删订单**（订单是历史凭证）。用软引用 |

### 4.2 新建 `tickets`（工单）

文件：`llm_backend/app/models/ticket.py`

```python
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func

from app.core.database import Base


class Ticket(Base):
    """工单表:本轮数据由 seed_tickets.py 生成;conversation_id 为将来接驳对话流预留"""

    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_no = Column(String(32), nullable=False, unique=True)  # TK-20260914161654
    user_query = Column(Text, nullable=False)                    # 用户原话(处理弹窗只读)
    summary = Column(String(200), nullable=True)                 # 摘要(弹窗 200 字计数)
    category = Column(String(50), nullable=True)                 # 售后服务/退货咨询/投诉/其他
    urgency = Column(String(10), nullable=True)                  # 低/中/高
    status = Column(String(20), nullable=False, default="待处理")  # 待处理/已解决
    detail = Column(Text, nullable=True)                         # 问题详情
    suggestion = Column(Text, nullable=True)                     # 处理建议
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="SET NULL"),
                             nullable=True)
    handler = Column(String(50), nullable=True)                  # 处理人
    resolved_at = Column(DateTime, nullable=True)                # 结单时间
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

**`conversation_id` 的说明（唯一一处"为将来预留"）**

本轮种子数据全部填 `NULL`。该字段存在的唯一理由是：将来若要"用户问售后/投诉时自动建单"，只需在 `lg_builder.py` 的 `transfer_human` / `aftersale_placeholder` / `complaint_placeholder` 三个节点各插一句写库代码，**前端页面一行不用改**。除此之外不引入任何为将来服务的设计。

### 4.3 改 `users`：加 `role`

文件：`llm_backend/app/models/user.py`，在 `status` 后追加一行：

```python
    role = Column(String(20), nullable=False, default="user", server_default="user")  # user/admin
```

**`default` 与 `server_default` 分工不同，两个都要写**：

| 参数 | 生效场景 | 不写的后果 |
|---|---|---|
| `default="user"`（Python 侧） | ORM 走 `s.add(User(...))` 时，在 **flush 时**填值并写进 INSERT | **`/api/register` 会 500**：`user_service.py:34-42` 的 `create_user` 构造 `User(...)` 时没传 `role`，缺 Python 侧 default 则属性为 `None`，而 `UserResponse.role` 是必填 `str` → pydantic `ValidationError` → 500 |
| `server_default="user"`（DDL 侧） | `create_all` 建表时把默认值写进表定义；`init_db.py` 的 `ALTER ... DEFAULT 'user'` 靠它给存量 4 行填值 | 存量行虽因 ALTER 语句里的 `DEFAULT` 仍是 `'user'`，但新建库缺兜底 |

即：**`default=` 是保证 register/me 不 500 的关键，`server_default=` 是第二道保险**——两个都不能省。

同时 `app/schemas/user.py`：

```python
class UserResponse(UserBase):
    id: int
    status: str
    role: str                       # 新增
    created_at: datetime
    last_login: Optional[datetime] = None

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = "user"              # 新增,前端据此决定进哪个端
```

`app/api/auth.py` 的 `login` 返回值补 role：

```python
    return {"access_token": access_token, "token_type": "bearer", "role": user.role}
```

### 4.4 改 `documents`：加 `description` / `status`（**不加 `title`**）

文件：`llm_backend/app/models/document.py`，在 `chunk_count` 后追加**两列**：

```python
    description = Column(Text, nullable=True)        # 文件描述(参考图"内容"列 → 改为"文件描述"列)
    status = Column(String(20), nullable=False, default="enabled", server_default="enabled")  # enabled/disabled
```

**为什么不加 `title` 列**（用户明确要求）：文件名本身就是最好的标识，`documents.original_filename` 已经存在且是上传时的原始文件名。再加一个 `title` 列会带来三个问题——① 存量 2 行没有标题，列表要靠 `title || original_filename` 回退，展示逻辑多一层分支；② 上传表单里"标题"和"文件名"两个字段语义重叠，用户实际只会填一个；③ 管理端列表的"标题"列与"文件名"列会显示同一个东西，浪费一列宽度。**直接用 `original_filename`，列头改叫「文件名」**。

**为什么 `description` / `status` 可空/带默认**：

| 列 | 可空性设计 | 理由 |
|---|---|---|
| `description` | `nullable=True` + ORM `default` 不设 | 存量 2 行为 NULL；上传那一刻也还没有描述——由上传成功后的 `PATCH` 补写（§5.7）。**允许为 NULL 是上传路径零改动的前提**（`indexing_service.py:183-186` 构造 `Document(...)` 时不传这两列） |
| `status` | `nullable=False` + `default="enabled"` + `server_default="enabled"` | 必须有 Python 侧 default：否则上传链路构造的对象该属性为 `None`，写库撞 NOT NULL |

**状态列是否影响检索**：本轮**不影响**。`status='disabled'` 只改管理端展示，检索侧仍会召回。理由：检索侧加 `WHERE status='enabled'` 要改 `bm25_sql_retriever.py` + `rag_retriever_service.py` 两处 SQL，属检索链路改动，超出本模块范围。此限制已列入 §12 风险清单（#5）。

### 4.5 建表与迁移：`scripts/init_db.py` 增量改动

新建表由 `Base.metadata.create_all` 自动处理（幂等，只建不存在的表），**前提是新模型已导入**。改动两处：

**(1) `llm_backend/app/models/__init__.py`**

```python
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.document_chunk import DocumentChunk
from app.models.document import Document  # noqa: F401
from app.models.product_price_stock import ProductPriceStock  # noqa: F401
from app.models.order import Order  # noqa: F401
from app.models.ticket import Ticket  # noqa: F401

__all__ = ["User", "Conversation", "Message", "DocumentChunk", "Document",
           "ProductPriceStock", "Order", "Ticket"]
```

**(2) `llm_backend/scripts/init_db.py`**，在既有 ALTER 块之后追加（与现有 `ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS ...` 写法完全同构）：

```python
            # 管理端增量列(幂等)
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user'"
            ))
            await conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS description TEXT"
            ))
            await conn.execute(text(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'enabled'"
            ))
```

并把 `from app.models import ...` 那行补上 `Order, Ticket`。

**执行顺序**：`create_all` 在前（建 `orders`/`tickets`），ALTER 在后（改存量表）——与现有代码顺序一致，无需调整。

---

## 5. 后端接口设计

### 5.1 鉴权：`require_admin`

文件：`llm_backend/app/core/security.py`（追加，不动既有 `get_current_user`）

```python
# ⚠️ 必须补这一行导入:security.py 现有 44 行里没有 User 的 import,
# 而函数注解在 Python 3.13(无 from __future__ import annotations)下于"定义时"求值,
# 缺它 → NameError → security 导入即抛 → auth → main 全链失败 → uvicorn 起不来,
# 且 tests/ 里所有 `from main import app` 在 collection 阶段全灭。
from app.models.user import User


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """管理端依赖:复用既有 JWT 校验,再查 role。非管理员 403。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user
```

**同样的导入坑还有一处**：`app/api/admin/tickets.py` 的 `PUT /{id}` 签名里写 `current_user: User = Depends(require_admin)`——该文件也要 `from app.models.user import User`（`require_admin` 的返回值类型是 `User`，注解同样在定义时求值）。

**顺带一条既有小瑕疵（本次不修，知道即可）**：`security.py:11` 的 `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")`，而实际端点是 `/api/token` → Swagger `/docs` 里点 Authorize 会打到不存在的 `/token`（实测返回 **405**，不是 404，因为被 `/` 的 StaticFiles 接住了）。**不影响 401 语义**（状态码与 `WWW-Authenticate: Bearer` 都由代码决定，与 tokenUrl 无关），所以 16 个新端点的鉴权行为完全正确——只是没法在 Swagger UI 里点着测。**联调一律用 curl 带 `Authorization: Bearer <token>`**。改 `tokenUrl` 属既有代码调整，不在本模块范围。

**三态语义**（测试必须逐条覆盖，见 §8）：

| 请求 | 结果 | 触发点 |
|---|---|---|
| 无 `Authorization` 头 | `401` | `oauth2_scheme` 抛出，早于 `require_admin` |
| 令牌无效/过期/用户不存在 | `401` | `get_current_user` |
| 令牌有效但 `role != 'admin'` | `403` | `require_admin` |
| 令牌有效且 `role == 'admin'` | 正常 | — |

### 5.2 路由注册

文件：`llm_backend/app/api/admin/__init__.py`

```python
from fastapi import APIRouter, Depends

from app.core.security import require_admin
from app.api.admin import console, products, orders, knowledge, tickets

# 组级依赖:一条声明覆盖全组,避免每个端点各写一遍(漏写即越权的风险点)
admin_router = APIRouter(dependencies=[Depends(require_admin)])

admin_router.include_router(console.router, prefix="/console", tags=["admin-console"])
admin_router.include_router(products.router, prefix="/products", tags=["admin-products"])
admin_router.include_router(orders.router, prefix="/orders", tags=["admin-orders"])
admin_router.include_router(knowledge.router, prefix="/knowledge", tags=["admin-knowledge"])
admin_router.include_router(tickets.router, prefix="/tickets", tags=["admin-tickets"])
```

文件：`llm_backend/app/api/__init__.py`（改 2 行）

```python
from fastapi import APIRouter
from app.api.auth import router as auth_router
from app.api.admin import admin_router

api_router = APIRouter()

api_router.include_router(auth_router, tags=["authentication"])
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])
```

`main.py` 已有 `app.include_router(api_router, prefix="/api")`，故最终路径为 `/api/admin/*`。**`main.py` 不需改动**。

### 5.3 通用约定

**分页请求参数**（所有列表接口一致）

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `page` | int | 1 | `ge=1` |
| `page_size` | int | 12 | `ge=1, le=100` |
| `keyword` | str | `""` | 去空格后为空则不过滤 |
| `status` 等筛选 | str | `None` | 空串/null 视为不过滤 |

**分页响应体**（所有列表接口一致）

```json
{
  "total": 47,
  "page": 1,
  "page_size": 12,
  "items": [ /* ... */ ]
}
```

`total` 必须是**过滤后**的全量条数，实现上用同一组 `where` 另跑一次 count（`select(func.count()).select_from(Model).where(*conds)`），**不能取 `len(items)`**——那样分页下 total 恒等于 `page_size`，前端分页条会永远只有一页。

**错误响应**：沿用项目既有约定（`main.py` 现状）——校验类错误 `400`，不存在 `404`，越权 `403`，未登录 `401`，未知异常 `500 + detail`。

**时间字段序列化**：沿用 `main.py:209` 的 `d.created_at.isoformat() if d.created_at else None` 写法。**注意**：该写法不带时区标记，前端 `new Date()` 会按本地时间解析（`docs/项目问题.md` #15a 的已知问题）。管理端展示日期一律用**字符串切片 `s.slice(0, 10)`** 取 `YYYY-MM-DD`，不经过 `new Date()`——从源头绕开该坑。

**数值字段序列化**：`Numeric` 列在 SQLAlchemy 里是 `Decimal`，fastapi 默认无法 JSON 序列化 → 一律显式 `float(row.current_price)`。

**数据库会话**：路由内用 `Depends(get_db)`（`app/core/database.py` 已有），不学 `main.py` 里 `AsyncSessionLocal()` 的直接用法——router 文件里 DI 更贴合 FastAPI 惯例，且 `get_db` 自带 commit/rollback。

### 5.4 控制台（2 个端点）

文件：`llm_backend/app/api/admin/console.py`

#### `GET /api/admin/console/stats`

六张统计卡片的数字。全部实时 `COUNT`，返回结构：

```json
{
  "products":      { "total": 47, "in_stock": 45 },
  "orders":        { "total": 18 },
  "knowledge":     { "total": 2 },
  "tickets":       { "total": 8, "pending": 5 },
  "users":         { "total": 4 },
  "conversations": { "total": 13, "messages": 64 }
}
```

口径定义（全为实测可复现的确定性定义，无歧义）：

| 字段 | SQL 口径 |
|---|---|
| `products.total` | `COUNT(*) FROM product_price_stock` |
| `products.in_stock` | `COUNT(*) WHERE stock_quantity > 0` |
| `orders.total` | `COUNT(*) FROM orders` |
| `knowledge.total` | `COUNT(*) FROM documents`（**不按 user_id 过滤**，与 §3 D6 一致） |
| `tickets.total` | `COUNT(*) FROM tickets` |
| `tickets.pending` | `COUNT(*) WHERE status = '待处理'` |
| `users.total` | `COUNT(*) FROM users` |
| `conversations.total` | `COUNT(*) FROM conversations` |
| `conversations.messages` | `COUNT(*) FROM messages` |

**判据**：`products.total` 实测应为 47（不是 TSV 的 50，见 §2.1）。

#### `GET /api/admin/console/charts?days=7`

四张图的数据。`days` 参数 `ge=1, le=30`，默认 7。

```json
{
  "trend": {
    "days":          ["09-16", "09-17", "09-18", "09-19", "09-20", "09-21", "09-22"],
    "orders":        [2, 2, 2, 2, 1, 1, 1],
    "conversations": [0, 1, 4, 0, 2, 3, 3]
  },
  "order_status":     [ { "name": "处理中", "value": 6 },
                        { "name": "已发货", "value": 6 },
                        { "name": "已送达", "value": 6 } ],
  "product_category": [ { "name": "智能门锁", "value": 12 },
                        { "name": "电动智能沙发", "value": 7 } /* ... */ ],
  "ticket_status":    [ { "name": "待处理", "value": 5 },
                        { "name": "已解决", "value": 3 } ]
}
```

实现要点：

**(a) 日期基准统一用 UTC**（本节与 §7.2 必须一致，否则折线永远错位一天）：

```python
from datetime import datetime, timezone, timedelta

today = datetime.now(timezone.utc).date()          # 不用已弃用的 datetime.utcnow()
days_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
labels = [d.strftime("%m-%d") for d in days_list]  # 升序,最后一项=今天
```

理由：`conversations.created_at` 是 `func.now()`，库时区为 `Etc/UTC`（`docs/项目问题.md` #15a），所以会话分组天然是 UTC 日期；`orders.order_date` 由 §7.2 的种子脚本用同一个 UTC 基准写入。两边一致才不会错位。

**已知取舍**：UTC 日期在本地 00:00~08:00 这段时间比本地日期早一天（同一个既有问题，`docs/项目问题.md` #15a）。管理端**不为此引入时区转换**——本项目全链路统一 UTC，只在那一段时间里"今天"显示为本地昨天。若要改，应作为全局议题（迁 `timestamptz`）单独处理，不在本模块开小灶。

**(b) 日期轴必须补齐空日**：

- `orders` 按 `orders.order_date` 分组计数；`conversations` 按 `conversations.created_at::date` 分组计数
- **两个分组结果都要左连接到 `days_list` 轴上，缺失日补 0**——不能把 SQL 的 `GROUP BY` 结果直接返回，那样数组里只有有数据的日期，长度对不上 `days`，折线图 X 轴会跳日

**(c) 其余三张图**：

- `order_status` / `ticket_status`：`GROUP BY status`，`ORDER BY` 固定顺序（订单按 `处理中→已发货→已送达`，工单按 `待处理→已解决`），与前端图例颜色顺序绑定（§6.6）
- `product_category`：`GROUP BY category ORDER BY COUNT(*) DESC`
- 状态分布即使某状态计数为 0 **也要返回该项**（值为 0），否则图例会随数据消失

**上例 `orders` 的数字形状说明**：`[2,2,2,2,1,1,1]` 是 §7.2 的「`index % 14` 铺开」规则跑出来的真实形状（实测），**不是**把 18 条堆在一天。实现时不要照抄常量，按 §7.2 的规则算——但要用它核对：折线应当是**有起伏的多点**，若跑出来是 `[0,0,0,0,0,0,18]` 说明种子的日期铺开没生效（与 §12-9 对应）。

### 5.5 商品管理（5 个端点）

文件：`llm_backend/app/api/admin/products.py`

#### `GET /api/admin/products`

`keyword` 的过滤条件（`ILIKE` 即 SQLAlchemy 的 `.ilike(f"%{kw}%")`）：

```python
if keyword:
    stmt = stmt.where(or_(
        ProductPriceStock.product_name.ilike(f"%{keyword}%"),
        ProductPriceStock.sku.ilike(f"%{keyword}%"),
    ))
if category:
    stmt = stmt.where(ProductPriceStock.category == category)
```

按 `sku` 升序。`total` 用**同一组 where 条件**另跑一次 `select(func.count()).select_from(...)`（不是 `len(items)`，否则分页下 total 恒等于 page_size）。

分页响应 `items` 元素结构：

```json
{
  "sku": "JD-BED-001",
  "product_name": "8H 智能电动床 6电机护腰悬浮升降 1.8m MTSD按摩",
  "category": "智能电动床",
  "current_price": 9957.50,
  "stock_quantity": 50,
  "image": "/products/JD-BED-001.svg",
  "updated_at": "2026-09-19T20:06:11"
}
```

`image` 由后端拼接 `/products/{sku}.svg`（不需要查文件系统是否存在，前端 `onerror` 兜底，见 §6.5）。

#### `POST /api/admin/products` — 新增

请求体（`app/schemas/admin.py` 的 `ProductCreate`）：

```python
class ProductCreate(BaseModel):
    sku: str = Field(..., pattern=r"^JD-[A-Z]{3}-\d{3}$")
    product_name: str = Field(..., min_length=1, max_length=255)
    category: str = Field(..., min_length=1, max_length=50)
    current_price: Decimal = Field(..., gt=0, le=99999999.99)
    stock_quantity: int = Field(..., ge=0)
```

**前端只提交这 5 个字段**，`updated_at` 由 DB 默认值维护。

校验与错误：

| 情况 | 响应 |
|---|---|
| `sku` 格式不符 | `422`（pydantic pattern 自动） |
| `sku` 已存在 | `400 {"detail": "商品编码已存在: JD-BED-001"}` |
| `product_name` 已存在 | `400 {"detail": "商品名称已存在: <name>"}`（表上有 `uq_product_price_stock_name` 唯一约束，不先查会抛 IntegrityError→500） |

**成功响应体（对齐 §5.5 列表元素结构，与 orders 的新增保持一致）**：

```json
{ "sku": "JD-TST-901", "product_name": "...", "category": "...",
  "current_price": 199.00, "stock_quantity": 10,
  "image": "/products/JD-TST-901.svg",
  "updated_at": "2026-09-22T10:30:00" }
```

`image` 按 sku 拼接（新商品没有对应 SVG，前端 `onerror` 兜底，见 §6.5）——**接口侧不检查文件是否存在**，保持无文件系统依赖。

**路由注册顺序纪律**：`GET /products/categories` 必须注册在**任何** `GET /products/{sku}` 形态的路由**之前**。当前设计里没有 `GET /products/{sku}`（只有 PUT/DELETE），所以不存在冲突；但将来若加详情端点，Starlette 是"路径+方法"匹配，`/products/categories` 会被 `{sku}="categories"` 抢先命中，必须把它排在前面。

**sku 规则说明**：格式 `JD-{3位大写字母}-{3位数字}`，与现有 47 行完全一致（`import_product_price_stock.py:31` 同款正则）。**不自动生成**——自动生成需要维护"品类→3字母码"映射表，属额外复杂度；手填 + 格式校验 + 唯一性校验已足够，且与现有数据规范一致。

#### `PUT /api/admin/products/{sku}` — 编辑

请求体 `ProductUpdate`：四个可选字段（`product_name` / `category` / `current_price` / `stock_quantity`），全部 `None` 时不更新任何列。

**`sku` 不可改**（它是 `document_chunks.sku_codes` 的对齐键，改了会让知识块的 sku 指向不存在的商品）。仅在路径参数里定位，不接受请求体改 sku。

`sku` 不存在 → `404 {"detail": "商品不存在: <sku>"}`

#### `DELETE /api/admin/products/{sku}` — 删除

`404` 若不存在；成功返回 `{"sku": "...", "deleted": true}`（与 `main.py:233` 删文档的返回形状一致）。

**不做级联**：只删 `product_price_stock` 一行。`document_chunks` 里该商品的静态知识块**保留**（用户仍能查到参数/售后，只是查不到价格库存）。这是有意为之，理由见 §12-1。

#### `GET /api/admin/products/categories` — 品类下拉选项

```json
["智能门锁", "电动智能沙发", "智能窗帘", "电动升降桌", "智能晾衣架",
 "智能电动床", "智能床垫", "智能床头柜", "按摩椅"]
```

`SELECT DISTINCT category FROM product_price_stock ORDER BY category`。

### 5.6 订单管理（4 个端点）

文件：`llm_backend/app/api/admin/orders.py`

#### `GET /api/admin/orders`

`keyword` 匹配 `order_no` / `product_name` / `buyer_name`；`status` 精确匹配。按 `order_date DESC, id DESC` 排序。

`items` 元素结构：

```json
{
  "id": 18,
  "order_no": "ORD-018",
  "sku": "JD-BED-001",
  "product_name": "8H 智能电动床 6电机护腰悬浮升降 1.8m MTSD按摩",
  "category": "智能电动床",
  "buyer_name": "沈七",
  "buyer_code": "P001",
  "amount": 9957.50,
  "status": "已发货",
  "order_date": "2026-09-14",
  "image": "/products/JD-BED-001.svg"
}
```

`order_date` 直接 `isoformat()` → `"2026-09-14"`（`Date` 列无时间部分，不经 `new Date()`，无时区风险）。

#### `POST /api/admin/orders` — 新增

```python
class OrderCreate(BaseModel):
    product_sku: str                        # 从商品下拉选择,后端据此回填 name/category/amount
    buyer_name: str = Field(..., min_length=1, max_length=50)
    buyer_code: Optional[str] = Field(None, max_length=20)
    user_id: Optional[int] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    status: str = Field("处理中", pattern=r"^(处理中|已发货|已送达)$")
    order_date: Optional[date] = None
```

行为：

1. 按 `product_sku` 查 `product_price_stock`，不存在 → `400 {"detail": "商品不存在: <sku>"}`
2. `user_id` 传了就先校验存在性（`SELECT 1 FROM users WHERE id=:id`），不存在 → `400 {"detail": "用户不存在: <id>"}`。**不能省**：`orders.user_id` 有外键，传一个不存在的 id 会抛 `IntegrityError` → 500（而不是一个可读的 400）
3. `product_name` / `category` 从商品表**回填快照**
4. `amount` 未传则取 `current_price`，传了则用传入值
5. `order_date` 未传则取 **`datetime.now(timezone.utc).date()`**（UTC，与 §5.4(a) 的图表日期轴同基准；**不用** `date.today()`——那是本地日期，与 UTC 轴会在早 8 小时窗口内错位一天）
6. `order_no` 由后端生成：`ORD-{max(现有序号)+1:03d}`。序号从现有 `order_no` 里解析（`ORD-(\d+)` 取最大值），**不是 `COUNT(*)+1`**——删除过订单后 `COUNT+1` 会撞唯一键
7. 返回完整订单对象（同列表元素结构），状态码 `200`

**并发下的撞号处理（必须写，否则是 500 而不是 400）**：两个请求同时读到 `max=18` → 都算 `ORD-019` → 后者撞 `order_no` 唯一约束 → `get_db`（`database.py:42-44`）rollback → 未捕获的 `IntegrityError` → **500**。处理方式：

```python
for attempt in range(3):          # 重算 max 再试,最多 3 次
    try:
        ...                       # 生成 order_no + INSERT + flush
        break
    except IntegrityError:
        await db.rollback()
        if attempt == 2:
            raise HTTPException(409, "订单号生成冲突，请重试")
```

演示场景下并发概率极低，但这 6 行能把它从"500 未知错误"变成"409 请重试"。`ticket_no` 由种子脚本写死，无此问题。

#### `PUT /api/admin/orders/{id}` — 编辑

可改 `buyer_name` / `buyer_code` / `status` / `amount` / `order_date` / `user_id`。

**`sku` / `product_name` / `category` 不可改**：换商品等于换订单，语义上应删了重建。若前端需要换商品，走"删除 + 新增"。

`id` 不存在 → `404`

#### `DELETE /api/admin/orders/{id}` — 删除

`404` 若不存在；成功 `{"id": 18, "deleted": true}`。

### 5.7 知识库管理（6 个新端点，**不复用 `/api/upload`**）

文件：`llm_backend/app/api/admin/knowledge.py`

#### `GET /api/admin/knowledge` — 全平台文档列表

**不接收 `user_id` 参数**（§3 D6）。

`keyword` 的匹配口径（**刻意不匹配 `md5`**）：`original_filename` ILIKE / **`id` 等值（关键词是纯数字时）**。

```python
if keyword:
    conds = [Document.original_filename.ilike(f"%{keyword}%")]
    if keyword.isdigit():
        conds.append(Document.id == int(keyword))   # 对应前端"文档编号"列的搜索
    stmt = stmt.where(or_(*conds))
```

**为什么把 `md5` 从匹配范围里拿掉**：前端表格第 1 列「文档编号」展示的是 `id`（§6.4.5），搜索框 placeholder 也写「搜索文档编号/文件名」。若 `keyword` 匹配 `md5`，用户搜「1」会命中**几乎所有行的十六进制 md5**（几乎每个 md5 都含 `1`）→ 返回全表 → 用户以为搜索坏了。改成 `id` 等值后，搜「1」精确命中 id=1 那行，与列头语义一致。

按 `created_at DESC`。**本轮不支持按 `status` 筛选**（前端工具条只放搜索框 + 查询按钮，状态只做展示徽章；要加筛选就同步加 `status` 参数与下拉）。

`items` 元素结构：

```json
{
  "id": 1,
  "md5": "a1b2c3d4e5f6...",
  "original_filename": "京东智能家具产品知识文档.docx",
  "description": "京东智能家具 50 款商品的静态知识：品类、品牌、功能特点、规格参数、售后服务",
  "file_type": "docx",
  "file_size": 28416,
  "chunk_count": 38,
  "status": "enabled",
  "owner_id": "6",
  "created_at": "2026-09-06T08:54:50"
}
```

**没有 `title` 字段**——表格的首列标识直接用 `original_filename`（§4.4 的决策）。

`description` 为 NULL 时前端显示 `—`（存量 2 行就是这种情况）。

`owner_id` 一并返回（管理端要知道这份文档属于谁），前端在本轮不做展示，留作信息完整性。

#### `POST /api/admin/knowledge/stage` — 暂存文件（**不索引**）

**D17 两阶段设计的第 1 步**：只把文件落到磁盘并取出基本信息，**不解析内容、不清洗、不分块、不嵌入、不写任何 DB 行**。

请求：`multipart/form-data`

| 字段 | 类型 | 说明 |
|---|---|---|
| `file` | UploadFile | 必填 |
| `user_id` | Form(str) | 管理员自己的 id（从 `/api/users/me` 拿），与 `/api/upload` 同口径 |

**做什么**（对应 `indexing_service.process_file` 前 4 步的校验，但**在本端点独立实现，不调用 `process_file`**）：

0. **先做机会式清理**：`_cleanup_stale_staging()` 删掉 `_staging/` 里超 TTL 的残留文件（§6.4.5.1）。放第一行——把"上一次没取消干净的东西"先扫掉，再进新文件
1. 扩展名白名单校验（`settings.allowed_extensions` = `txt,md,pdf,docx`）→ 不符 `400 {"detail": "不支持的文件格式: .exe"}`
2. 大小校验（≤ `settings.MAX_FILE_SIZE_MB` = 30MB）→ 超限 `400`
3. 空文件校验 → `400`
4. 读一次文件字节，算 MD5
5. 落盘到暂存目录 `llm_backend/uploads/_staging/{md5}{ext}`
6. 查重：`SELECT id, created_at, chunk_count, description FROM documents WHERE user_id=:uid AND md5=:md5`
7. 返回

**返回体**（无重复）：

```json
{
  "md5": "a1b2c3d4e5f6...",
  "original_filename": "京东智能家具产品知识文档.docx",
  "file_type": "docx",
  "file_size": 28416,
  "duplicate": false,
  "existing": null
}
```

**返回体**（命中重复——查重是**为了提前告知**，不是阻止）：

```json
{
  "md5": "a1b2c3d4e5f6...", "original_filename": "...", "file_type": "docx", "file_size": 28416,
  "duplicate": true,
  "existing": { "id": 1, "created_at": "2026-09-06T08:54:50", "chunk_count": 38, "description": null }
}
```

**暂存目录为什么是 `uploads/_staging/{md5}{ext}`**（而不是照搬 `/api/upload` 的 `{uuid5(user_id)}/{timestamp}/`）：

| 理由 | 说明 |
|---|---|
| **无状态** | 服务端没有会话。用 md5 当键，`commit` / `unstage` 只需带上 md5 就能定位文件，不必为此引入 session 表 |
| **无路径穿越** | md5 是 32 位十六进制，服务端用 `^[0-9a-f]{32}$` 校验后才拼路径——**绝不接受客户端回传任意路径**。若照搬带时间戳的目录结构，就必须让客户端回传路径，那是个目录穿越面 |
| **天然幂等** | 同一文件重复暂存 = 覆盖同一路径，不产生多份 |
| **与正式文件分流** | 暂存文件在 `_staging/` 下，一眼可辨哪些没提交；`/api/upload` 的正式文件仍在 `{uuid5}/{timestamp}/`，互不干扰 |

`_staging/` **已在 gitignore 覆盖范围内**（根 `.gitignore:66` 的 `llm_backend/uploads/`），无需新增规则。

#### `POST /api/admin/knowledge/commit` — 提交索引（**走完整链路**）

**D17 两阶段设计的第 2 步**：这一步才真正解析、清洗、分块、嵌入、落库。

请求体：

```python
class KnowledgeCommit(BaseModel):
    md5: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    original_filename: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
```

**做什么**：

1. 校验暂存文件存在（`uploads/_staging/{md5}{ext}`）→ 不存在 `404 {"detail": "暂存文件不存在或已被清理: <md5>"}`
1b. **对暂存文件 `os.utime(path)` 刷新 mtime**——防止提交过程中被并发的 `stage` 机会式清理误删（§6.4.5.1 的 30b 风险，一行解决）
2. 调**既有** `IndexingService().process_file({"path": staged_path, "original_name": original_filename, "user_id": str(admin_id)})`——**索引链路全量复用，一行不改**
3. 按 `process_file` 的返回分三种情况：

| `status` | 处理 | HTTP |
|---|---|---|
| `success` | 查回 `Document` 行 → 写 `description`（非空时）→ 返回完整行 | `200` |
| `duplicate` | 该 `(user_id, md5)` 已有行（可能是暂存期间别处建的）→ **不重复索引**，查回那行 + 更新 `description` → 返回完整行，`duplicate: true` | `200` |
| `failed` | 按 `error` 转 4xx：`unsupported`/`too_large`/`empty_file` → `400`；`parse_error`/`embedding_failed` → `400` | `4xx` |

> **注意 `failed` 的 HTTP 码与 `/api/upload` 不同**：共享端点沿用"处理类错误 200 + `status=failed`"契约（客户端依赖它）。管理端端点**没必要继承这种别扭语义**——管理员调一个端点做索引，失败了就该是 4xx。这是两个端点的**有意差异**，不是不一致。

4. **暂存文件的清理规则**：

| 结果 | 暂存文件 | 为什么 |
|---|---|---|
| `success` | **删** | 已入索引，`_staging` 里那份没用了 |
| `duplicate` | **删** | 同上（该文件的内容已在库里） |
| `failed` | **保留** | 让"重试保存"不必重传文件（PDF 可能几 MB，MinerU 还可能瞬时失败）。此时弹窗仍停在暂存态，用户可再点保存，或点「取消」走 `stage` 删除兜底 |

**返回体（完整文档行，与列表 `items` 元素同结构）**：

```json
{
  "id": 3,
  "md5": "a1b2c3d4e5f6...",
  "original_filename": "京东智能家具产品知识文档.docx",
  "description": "本次上传时填的描述",
  "file_type": "docx",
  "file_size": 28416,
  "chunk_count": 38,
  "status": "enabled",
  "owner_id": "6",
  "created_at": "2026-09-22T10:30:15",
  "duplicate": false
}
```

**`created_at` / `chunk_count` 就在这个响应里**——写这一行的代码本来就在这个函数里，顺手返回即可。这正是 D19 删掉"单条详情接口"的依据。

#### `DELETE /api/admin/knowledge/stage/{md5}` — 撤销暂存

「取消」调它：删掉 `uploads/_staging/{md5}{ext}`。

| 情况 | 响应 |
|---|---|
| 文件存在，已删 | `200 {"md5": "...", "deleted": true}` |
| 文件不存在 | `200 {"md5": "...", "deleted": false}` |

**为什么"文件不存在"返回 200 而不是 404**：取消是**幂等**操作——"文件本来就不在"和"刚被我删掉"对调用方是同一个结果（都已达成"不留暂存文件"的目标）。前端一律当成功处理，不弹错误。

**只删暂存文件，不碰任何 DB**——这正是两阶段设计的全部意义：**撤销时数据库里从来就没有过痕迹**。

#### `POST /api/upload` — 保留原样，**管理端不再调用**

该端点仍是**客户端**的知识库上传接口（`docs/项目问题.md` #14 记录其不校验令牌是既有缺陷，本次不修，见 §12-6）。管理端改用上面的两阶段端点，**不再复用**——理由见 D7 / D17。

#### `PATCH /api/admin/knowledge/{md5}` — 写描述/状态

```python
class KnowledgeUpdate(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern=r"^(enabled|disabled)$")
```

按 `md5` 全表查找（**不加 user_id 过滤**，与列表口径一致）。`404` 若不存在。返回更新后的完整对象（同列表元素结构）。

**只有两个可写字段**——`original_filename`（文件名）、`file_type`、`file_size`、`chunk_count`、`created_at` 全是上传解析出的客观事实，**不允许人工改写**；`status` 是管理端的启停开关；`description` 是唯一的人工描述字段。

**「未传字段」与「显式传 null」必须区分开**——这不是洁癖，是 §8.5 的测试**能不能写出来**的前提：

```python
# ✅ 正确:用 exclude_unset 区分"没传"和"传了 null"
for field, value in payload.model_dump(exclude_unset=True).items():
    setattr(doc, field, value)

# ❌ 错误:这种最常见写法会让 {"description": null} 被当成"没传",
#    于是 description 永远回不到 NULL(存量 2 行正是 NULL 状态,改一次就再也恢复不了)
if payload.description is not None:
    doc.description = payload.description
```

同样的问题存在于 `OrderUpdate` 的 `buyer_code` / `user_id`（想清空买家编码时无法清空）。**两个 Update schema 一律用 `exclude_unset=True` 逐字段赋值**。

`ProductUpdate` / `TicketUpdate` 不受影响（它们没有"需要清空"的可空字段，`ticket` 的 `resolved_at`/`handler` 由 status 副作用控制，见 §5.8）。

**同 md5 多归属的处理**：`documents` 的唯一约束是 `(user_id, md5)`，理论上同 md5 可挂多个 user_id。本接口若命中多行，**只更新 `id` 最小的那一行**并在响应里返回该行。当前库中 `md5` 无重复（2 行 2 个 md5），此分支不会触发，但代码不能因为 `scalar_one_or_none()` 抛 `MultipleResultsFound` 而 500。

#### `DELETE /api/admin/knowledge/{md5}` — 按 md5 全量删

与 `main.py:215-233` 的既有删除逻辑同构，但**去掉 user_id 过滤**，且**删掉该 md5 的所有行**（不区分归属）：

```python
@router.delete("/{md5}")
async def delete_knowledge(md5: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Document).where(Document.md5 == md5))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"文档不存在: {md5}")
    await db.execute(delete(DocumentChunk).where(DocumentChunk.md5 == md5))
    for r in rows:
        await db.delete(r)
    return {"md5": md5, "deleted": True, "documents": len(rows)}
```

**注意会话来源**：本模块所有路由统一用 `Depends(get_db)`（见 §5.3），**不学** `main.py:218` 删文档端点里的 `async with AsyncSessionLocal() as session` 写法——那是不走 DI 的另一套。`get_db` 在 `yield` 后自动 commit（`app/core/database.py`），故路由内不写 `await db.commit()`。

删除同时清理 `document_chunks` 与 `documents`（同事务），否则检索侧会召回已删文档的孤儿块。

### 5.8 工单管理（2 个端点）

文件：`llm_backend/app/api/admin/tickets.py`

#### `GET /api/admin/tickets`

`keyword` 匹配 `ticket_no` / `summary` / `user_query`；`status` 精确匹配。按 `created_at DESC`。

**响应形状与 §5.3 一致，是分页对象**（`{total, page, page_size, items}`）——与商品/订单/知识库列表同构，**不是裸数组**。前端 §6.4.6 也照样有分页条（种子 8 条一页放得下，但接口与 UI 都不做特例，否则将来工单涨到 50 条就要改三处）。

`items` **返回全字段**（含 `detail` / `suggestion`）——数据量小（种子 8 条），列表直接带全字段，处理弹窗不需要二次请求：

```json
{
  "id": 3,
  "ticket_no": "TK-20260914161654",
  "summary": "智能手表S3退货流程咨询",
  "category": "售后服务",
  "urgency": "中",
  "user_query": "如果我买不合适，怎么去退货呢",
  "status": "待处理",
  "detail": "用户已选定智能手表S3（¥699），关注购买后不合适时的退货政策与操作流程。",
  "suggestion": "转接人工客服，提供清晰退货指引：7天无理由退换（商品未拆封/配件齐全）、寄回方式（平台预付快递单）、退款时效（签收后1-3工作日原路退回），并主动协助生成退货单。",
  "handler": null,
  "created_at": "2026-09-14T08:16:58",
  "resolved_at": null
}
```

#### `PUT /api/admin/tickets/{id}` — 保存处理结果

```python
class TicketUpdate(BaseModel):
    summary: Optional[str] = Field(None, max_length=200)
    category: Optional[str] = Field(None, max_length=50)
    urgency: Optional[str] = Field(None, pattern=r"^(低|中|高)$")
    status: Optional[str] = Field(None, pattern=r"^(待处理|已解决)$")
    detail: Optional[str] = None
    suggestion: Optional[str] = None
```

**不改 `ticket_no` / `user_query` / `conversation_id`**（前者是主键式标识，后两者是原始事实，人工处理不应篡改）。

副作用规则：

| 条件 | 行为 |
|---|---|
| `status` 改为 `已解决` 且原状态非 `已解决` | 写 `resolved_at = now()`、`handler = 当前管理员 username` |
| `status` 改为 `待处理` 且原状态为 `已解决` | 清空 `resolved_at` 与 `handler`（重开单） |
| `status` 未变或未传 | `resolved_at`/`handler` 不动 |

`handler` 从 `require_admin` 拿到的当前用户填（虽然组级依赖只做校验不注入，本端点额外加 `current_user: User = Depends(require_admin)` 参数——FastAPI 同依赖同请求会复用缓存，不会重复查库）。

`id` 不存在 → `404`

### 5.9 请求/响应 schema 汇总

文件：`llm_backend/app/schemas/admin.py`（新建）

```python
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    sku: str = Field(..., pattern=r"^JD-[A-Z]{3}-\d{3}$")
    product_name: str = Field(..., min_length=1, max_length=255)
    category: str = Field(..., min_length=1, max_length=50)
    current_price: Decimal = Field(..., gt=0, le=99999999.99)
    stock_quantity: int = Field(..., ge=0)


class ProductUpdate(BaseModel):
    product_name: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, min_length=1, max_length=50)
    current_price: Optional[Decimal] = Field(None, gt=0, le=99999999.99)
    stock_quantity: Optional[int] = Field(None, ge=0)


class OrderCreate(BaseModel):
    product_sku: str
    buyer_name: str = Field(..., min_length=1, max_length=50)
    buyer_code: Optional[str] = Field(None, max_length=20)
    user_id: Optional[int] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    status: str = Field("处理中", pattern=r"^(处理中|已发货|已送达)$")
    order_date: Optional[date] = None


class OrderUpdate(BaseModel):
    buyer_name: Optional[str] = Field(None, min_length=1, max_length=50)
    buyer_code: Optional[str] = Field(None, max_length=20)
    user_id: Optional[int] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    status: Optional[str] = Field(None, pattern=r"^(处理中|已发货|已送达)$")
    order_date: Optional[date] = None


class KnowledgeCommit(BaseModel):
    md5: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    original_filename: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class KnowledgeUpdate(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern=r"^(enabled|disabled)$")


class TicketUpdate(BaseModel):
    summary: Optional[str] = Field(None, max_length=200)
    category: Optional[str] = Field(None, max_length=50)
    urgency: Optional[str] = Field(None, pattern=r"^(低|中|高)$")
    status: Optional[str] = Field(None, pattern=r"^(待处理|已解决)$")
    detail: Optional[str] = None
    suggestion: Optional[str] = None
```

**刻意不用 `response_model`**，但理由要说准：

- 事实是**两种风格项目里都有**——`main.py` 的端点全部手搓 dict；`api/auth.py:16,27,44` 三处用了 `response_model`（正是本次要改的 `UserResponse` / `Token`）。
- 管理端 19 个端点**统一手搓 dict**，与 `main.py` 那批保持一致（管理端更接近 `main.py` 的业务端点，而非 auth 的 schema 端点）。
- **不是为了绕开序列化问题**——`Decimal` / `datetime` 在 FastAPI 手搓 dict 返回路径上是**能正常序列化**的。实测（FastAPI 0.141.1 + `TestClient`）：返回 `{"price": Decimal("9957.50"), "when": datetime(...), "day": date(...)}` → `HTTP 200`，body 为 `{"price":9957.5,"when":"2026-09-22T10:30:00","day":"2026-09-14"}`，**不会抛 `Object of type Decimal is not JSON serializable`**。
- 代价：手搓 dict **漏字段不会报错**。所以 §8 的测试必须逐字段断言关键字段存在（尤其 `current_price` / `amount` 是 number 而不是字符串）。

**由此带出的真正前端责任**：`Decimal("9957.50")` 序列化成 **`9957.5`**（尾零被吃掉）。前端展示金额/价格**必须** `Number(v).toFixed(2)`，否则页面上会出现 `¥9957.5` 这种少一位的写法。这条已写入 §6.4.8 的通用交互约定。

---

## 6. 前端设计

### 6.1 构建与入口（客户端零改动）

**新建** `frontend/admin.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SmartCS-Agent 管理端</title>
</head>
<body>
  <div id="admin-app"></div>
  <script type="module" src="/src/admin/main.js"></script>
</body>
</html>
```

**改** `frontend/vite.config.js`（加 `rollupOptions.input`）：

```js
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        admin: fileURLToPath(new URL('./admin.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
});
```

**为什么不用 `resolve(__dirname, ...)`**：`frontend/package.json` 里有 `"type": "module"`，Vite 会把这个配置文件当 **ESM** 加载，而 ESM 里 **`__dirname` 未定义**，直接写会报 `__dirname is not defined in ES module scope`，构建启动即失败。`fileURLToPath(new URL(...))` 是 ESM 下的等价写法。

**改** `frontend/tailwind.config.cjs` 的 `content` 数组，加 `'./admin.html'`：

```js
  content: ['./index.html', './admin.html', './src/**/*.{vue,js}'],
```

`./src/**/*.{vue,js}` 已覆盖 `src/admin/`，无需再加一条。

**不动**的文件：`index.html`、`src/main.js`、`src/App.vue`、`src/components/*`、`src/composables/useChat.js`、`src/style/global.css`。

**访问地址**：
- 构建后（生产/演示）：`http://127.0.0.1:8000/admin.html`（`main.py` 末尾的 `StaticFiles(directory=frontend/dist, html=True)` 挂在 `/`，直接命中 `dist/admin.html`）
- 开发：`http://localhost:5173/admin.html`

**按入口分包**：ECharts 只被 `src/admin/**` 引用，Rollup 只把"多入口共同可达"的模块提成共享 chunk，所以 ECharts 落在 admin 专属 chunk，`dist/index.html` 那一侧**不会加载 ECharts**（D10 的 JS 结论成立，实测确认）。

**但 CSS 不成立，要说清楚**：Tailwind 的 `content` 是**全局并集**——`tailwind.config.cjs` 的 `content` 加上 `'./admin.html'` 后，**`dist/assets/main-*.css`（客户端那份）里会同时包含只出现在管理端文件里的工具类**，反之亦然。实测现有客户端 CSS 约 103KB，会因此长胖几 KB 量级。**单独给 admin 配一份 content 需要引入第二套 Tailwind 构建，不值得**——接受这个互相包含，只是别指望 CSS 也完全隔离。

### 6.2 目录结构

```
frontend/
  admin.html                          ← 新建
  public/products/{sku}.svg           ← 新建(脚本生成,47 个;gitignore 内,不入库,见 §6.5)
  src/admin/
    main.js                           ← 见下方完整导入清单(漏一样就少一块样式,不是可选项)
    admin.css                         ← Tailwind 三行 + 管理端专用类
    AdminApp.vue                      ← 顶部导航 + 页面切换 + 登录态
    api.js                            ← 管理端接口封装
    views/
      ConsoleView.vue
      ProductView.vue
      OrderView.vue
      KnowledgeView.vue
      TicketView.vue
    components/
      AdminLogin.vue
      AdminModal.vue                  ← 通用弹窗
      Pagination.vue                  ← 通用分页条
      StatusBadge.vue                 ← 状态徽章
      ProductThumb.vue                ← 商品缩略图(含 onerror 兜底)
      KnowledgeFormModal.vue          ← 知识库新增/编辑弹窗(上传驱动,mode 区分,见 §6.4.5.1)
      charts/
        LineChart.vue
        DonutChart.vue
        BarChart.vue
```

**通用组件契约**（被 5 个页面共 7 处使用，不定义就会各写各的）：

```js
// AdminModal.vue
props: {
  visible: Boolean,          // 受控显隐;父组件 v-if 之外再用它控制内部动画时不必须
  title:   String,           // 弹窗标题(「新增商品」/「处理工单」…)
  width:   { type: String, default: '560px' },
}
emits: ['close']             // 不 emit 'update:visible';父组件用 @close 关自己
// 行为约定:
//   - 点遮罩【不关闭】(表单填了一半误点会丢数据),只有右上角 × 与「取消」按钮触发 close
//   - ESC 关闭
//   - 打开时 body 加 overflow:hidden,关闭时移除(与客户端 docs-panel 同样的处理)
//   - 表单内容全部由父组件通过默认插槽传入,弹窗自身不持有表单状态
//   - z-index: 50(高于导航栏的 40,低于图片预览类的 100)

// StatusBadge.vue
props: {
  text:  String,             // 徽章文字:已发货 / 待处理 / 低 …
  color: { type: String, default: 'gray' },  // gray|green|blue|amber|red —— 映射到 bg-{c}-100 text-{c}-600
}
// 用法:<StatusBadge text="已发货" color="blue" />
// 各处颜色映射在【页面里】决定(订单状态→blue/green/amber、工单状态→red/green、紧急度→green/amber/red),
// 不把它做成"传 status 自动配色"的智能组件 —— 三种状态集(订单/工单/紧急度)取值完全不同,硬合并要传枚举类型

// ProductThumb.vue
props: { src: String, alt: String, size: { type: Number, default: 96 } }
// 内部 onerror 兜底,见 §6.5
```

**复用客户端既有件**（相对路径 `../api/auth.js`）：`getToken` / `setToken` / `clearToken` / `authHeaders` / `handleUnauthorized`。token 存 `localStorage` 的 `token` 键——与客户端**同一个键**，故管理端登录后客户端也是登录态（对应"体验客服"点过去不用再登）。这是有意为之。

**副作用（须知）**：反过来说，**在管理端登出会把客户端的登录态一并清掉**（同一个 token 键）。演示时若两个标签页都开着，在管理端点「退出登录」后切回客户端标签页会发现需要重新登录。属预期行为，写进 README。

**`src/admin/main.js` 的完整内容**（对照客户端 `src/main.js:1-11`，这是**必须逐行写全**的——漏一行就少一块样式，且不报错）：

```js
import { createApp } from 'vue';
import AdminApp from './AdminApp.vue';
import './admin.css';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fortawesome/fontawesome-free/css/all.min.css';

createApp(AdminApp).mount('#admin-app');
```

| 导入 | 漏掉的后果 |
|---|---|
| `./admin.css` | **整个管理端无样式**（Tailwind 指令不落地）——阻断级 |
| `@fortawesome/.../all.min.css` | §6.4.1 导航 5 个图标、§6.4.2 六张卡片图标、§6.5 logo 的 `fa-headset`、`ProductThumb` 兜底的 `fa-box` **全部渲染成空白** |
| `@fontsource/inter/*` | `tailwind.config.cjs:7` 的 `fontFamily.sans: ['Inter', ...]` 静默回退 `system-ui`，与客户端并排看字体明显不同 |

**不需要** `highlight.js`（管理端无 markdown 渲染，那是聊天消息专用的）。

**不复用** `global.css`——但代价不只是 `overflow`，**要手工补齐 3 项**，否则与客户端并排看观感不一致：

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

/* 1. body 背景色:Tailwind preflight 不设 body 背景色,不补则页面是纯白底,
      而卡片是 bg-white —— 白底卡片贴白底页面几乎看不出边界 */
/* 2. 文字色:同 preflight 不设,Tailwind 默认继承浏览器黑 */
body {
  background-color: #fafbfc;   /* = tailwind.config.cjs:21-23 的 page.bg */
  color: #1e293b;
}

/* 3. 滚动条:global.css:27-38 的 6px 细滚动条,管理端表格/卡片密集,不补会退回系统默认粗滚动条 */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
```

**注意**：`body { overflow: auto }` **不需要写**——admin 入口根本不加载 `global.css`，Tailwind preflight 也不设 `overflow`，没有任何东西会把它设成 `hidden`（写了是空操作，还会误导后续维护者以为两个入口的 CSS 会互相影响）。

### 6.3 页面切换与登录态（`AdminApp.vue`）

**无 vue-router**。用 `ref` + hash 同步：

```js
const PAGES = ['console', 'products', 'orders', 'knowledge', 'tickets'];

// hash → 当前页(支持刷新后停在同一页);非法值回退 console
function pageFromHash() {
  const h = window.location.hash.replace(/^#\/?/, '');
  return PAGES.includes(h) ? h : 'console';
}
const currentPage = ref(pageFromHash());

function go(page) {
  currentPage.value = page;
  window.location.hash = `#/${page}`;
}

onMounted(() => window.addEventListener('hashchange', () => { currentPage.value = pageFromHash(); }));
```

**登录态判定**（三段式，缺一不可）：

```js
const authState = ref('checking');   // checking | anonymous | denied | ok
const me = ref(null);

async function checkAuth() {
  if (!getToken()) { authState.value = 'anonymous'; return; }
  try {
    const user = await getMe();               // 复用 ../api/auth.js
    // ⚠️ me 必须在 role 判断【之前】赋值:denied 提示卡要显示 {email}
    me.value = user;
    authState.value = user.role === 'admin' ? 'ok' : 'denied';
  } catch {
    clearToken();
    me.value = null;
    authState.value = 'anonymous';
  }
}

onMounted(() => {
  checkAuth();
  // ⚠️ 必须监听:api/auth.js:23-30 的 handleUnauthorized 会 clearToken() 并派发此事件,
  // 全仓唯一监听者是客户端 App.vue:319。管理端不接的话 —— token 过期时点任意页面 →
  // 请求 401 → token 已被清、authState 仍是 'ok' → 用户卡在管理端界面看
  // 「数据加载失败，请刷新重试」,而不是回登录页,必须手动刷新才能恢复。
  // /api/upload 的 XHR 路径(src/api/upload.js:46)同理派发该事件。
  window.addEventListener('auth:unauthorized', () => {
    me.value = null;
    authState.value = 'anonymous';
  });
});
```

`AdminLogin` 的 `@logged-in` 直接绑 `checkAuth`（不是 `recheck`）——登录成功后重新走一遍 `getMe` + role 判断，非管理员会正确落到 `denied` 而不是误进控制台。

| 状态 | 渲染 |
|---|---|
| `checking` | 居中的加载指示 |
| `anonymous` | `<AdminLogin @logged-in="checkAuth" />` |
| `denied` | 提示卡：「当前账号（`{{ me?.email }}`）没有管理员权限」+「返回客服端」链接（`href="/"`）+「切换账号」按钮（`clearToken()` + 清 `me` + 回 `anonymous`） |
| `ok` | 顶部导航 + 当前页视图 |

**登出**：`clearToken()` → `authState = 'anonymous'`（与客户端行为一致，不用 confirm）。

#### 6.3.1 管理端登录页 `AdminLogin.vue`

客户端 `LoginView.vue` 是**深色**盒子（`global.css:213-244`：`#1e1e1e` 底 + `#2d2d2d` 盒 + 绿色按钮），而管理端五个页面是**浅色**。两张皮都不算错，**此处取浅色**——理由：管理端是独立站点，登录页是它的第一屏，与后面的浅色页面保持一致比与客户端的登录页保持一致更重要；且用户要求"管理端风格和客户端一致"指的是那 5 个功能页（参考图也全是浅色）。

| 项 | 规格 |
|---|---|
| 容器 | 居中卡片，`bg-white rounded-2xl shadow-sm border border-gray-100`，宽 400px，页面底 `bg-page`(`#fafbfc`) |
| 标题 | 「SmartCS-Agent 管理端」+ 副标题「请使用管理员账号登录」(小字灰色) |
| 字段 | 邮箱 `<input type="email">`、密码 `<input type="password">`，样式复用客户端的表单观感（圆角 8px、聚焦主色边） |
| 按钮 | 「登 录」主色实心，`loading` 时禁用 + 转圈图标 |
| 错误提示 | 红色小字（登录失败 / 网络错误）；**不区分**"密码错"和"非管理员"——两者都只在登录后由 role 判断给出，避免泄露账号是否存在 |
| 底部链接 | 「返回客服端」→ `href="/"`（**必须有**，否则误入管理端的用户没有退路） |
| 不做 | 「记住账号密码」不做（客户端那个功能把密码明文写进了 localStorage，`docs/项目问题.md` #15 附带发现②已记录该问题，管理端不复制这个做法）；「注册」入口不做（管理员账号由种子脚本创建） |
| 提交 | 调 `../api/auth.js` 的 `login(email, password)`（它内部 `setToken`），成功后 `emit('logged-in')` |

### 6.4 五个页面的逐页设计

#### 6.4.1 顶部导航条（AdminApp 内，所有页面共享）

参考图布局，从左到右：

| 位置 | 内容 |
|---|---|
| 左 | 圆形渐变 logo（`bg-gradient-to-br from-primary to-emerald-600` + `fas fa-headset` 图标）+ 文字「SmartCS-Agent」+ 灰色小字「管理端」 |
| 中 | 5 个导航项，各含图标 + 文字：控制台(`fa-gauge-high`) / 商品管理(`fa-box`) / 订单管理(`fa-clipboard-list`) / 知识库(`fa-book`) / 工单(`fa-ticket`) |
| 右 | 「退出登录」文字按钮 + 圆形头像（`me.username` 首字）+ 用户名 |

导航项样式：默认灰色（`text-gray-500`），当前项 `text-primary` + **下方 2px 主色下划线**（参考图特征），hover `text-gray-700`。

#### 6.4.2 控制台 `ConsoleView.vue`

**页头**：标题「管理控制台」(text-2xl font-bold) + 副标题「商品、订单、知识库与工单数据概览，供智能客服实时检索与升级处理。」+ 右侧「体验客服」按钮（`bg-primary text-white rounded-lg px-4 py-2`，点击 `window.open('/', '_blank')`）。

**6 张统计卡片**（`grid grid-cols-2 lg:grid-cols-6 gap-4`）：

| 卡片 | 图标 | 图标底色 | 主数字 | 标签 | 副标签 |
|---|---|---|---|---|---|
| 商品 | `fa-box` | `bg-blue-100` / `text-blue-600` | `products.total` | 商品 | 商品·上架 {in_stock} |
| 订单 | `fa-clipboard-list` | `bg-sky-100` / `text-sky-600` | `orders.total` | 订单总数 | — |
| 知识库文档 | `fa-book` | `bg-emerald-100` / `text-emerald-600` | `knowledge.total` | 知识库文档 | — |
| 工单 | `fa-ticket` | `bg-amber-100` / `text-amber-600` | `tickets.total` | 工单 | 工单·待办 {pending} |
| 用户 | `fa-user` | `bg-purple-100` / `text-purple-600` | `users.total` | 活跃用户 | — |
| 客服会话 | `fa-comment-dots` | `bg-red-100` / `text-red-600` | `conversations.total` | 客服会话 | 客服会话·消息 {messages} |

卡片容器：`bg-white rounded-xl border border-gray-100 p-4 shadow-sm`。

**图表区**（`grid grid-cols-1 lg:grid-cols-2 gap-4`，四张图各占一格）：

| 图 | 组件 | 数据源 | 备注 |
|---|---|---|---|
| 近 7 日趋势 | `LineChart` | `trend` | 两条折线：订单、客服会话；标题右侧小图例 |
| 订单状态分布 | `DonutChart` | `order_status` | 环形 |
| 商品品类分布 | `BarChart` | `product_category` | 柱状 |
| 工单状态 | `DonutChart` | `ticket_status` | 环形 |

**加载与错误**：进页面时并行 `GET /console/stats` + `GET /console/charts`（`Promise.all`）；失败显示一行灰色提示「数据加载失败，请刷新重试」，不弹窗。

#### 6.4.3 商品管理 `ProductView.vue`（**卡片网格**，按用户指定的参考图版式）

**工具条**：搜索框（placeholder「搜索商品名/SKU」，宽 `w-72`）+ 品类下拉（默认「全部品类」）+ 「查询」绿按钮 + 「新增商品」绿按钮。

**卡片网格**：`grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4`，每张卡片：

```
┌──────────────────────────────────────┐
│ ┌────────┐  8H 智能电动床 6电机护腰…  │  ← 商品名 2 行截断 (line-clamp-2)
│ │  缩略图 │  [上架]                    │  ← 状态徽章
│ │ 96×96  │  ¥9957.50  2026-09-19      │  ← 价格(红色加粗) + 更新日期
│ └────────┘  智能电动床 · 库存 50       │  ← 品类 · 库存
│                          编辑  删除   │  ← 右下角文字按钮
└──────────────────────────────────────┘
```

- 缩略图：`ProductThumb` 组件，`<img :src="item.image" @error="fallback = true">`；`fallback` 为 true 时渲染 `bg-gray-100` 方块 + 品类首字（新增商品没有 SVG 时走这条）
- 状态徽章：`stock_quantity > 0` → 「上架」绿；`=== 0` → 「无货」红
- 编辑/删除按钮：文字按钮，编辑主色、删除红色（参考图配色）

**新增/编辑弹窗**（`AdminModal`）：

| 字段 | 控件 | 新增 | 编辑 |
|---|---|---|---|
| 商品编码 | input + placeholder「JD-XXX-000」 | 可填，必填 | **只读置灰**（不可改，§5.5） |
| 商品名称 | input | 必填 | 必填 |
| 品类 | input + `<datalist>` 提示既有品类 | 必填 | 必填 |
| 当前价格 | number input，`step="0.01"` | 必填，> 0 | 必填，> 0 |
| 库存数量 | number input，`step="1"` | 必填，≥ 0 | 必填，≥ 0 |

**编辑商品名或删除商品的二次确认文案**（对应 §12-1 的风险，必须明确提示）：

- 改名称时：`修改商品名称会影响智能客服对该商品的检索命中（知识库中的旧名称对不上），确认修改？`
- 删除时：`确定删除商品 {sku}？删除后智能客服将查不到该商品的价格与库存（已入库的静态知识仍会返回）。`

**分页条**：`共 {total} 条` + 每页条数下拉（12/24/48）+ 上一页/页码/下一页。与参考图一致放在右下角。边界规则（0 条、删后越界、切换每页条数）见 §6.4.7。

**加载/空态/删除确认/保存禁用/前端校验**：全部走 §6.4.8 的通用约定，不在本页另定。

#### 6.4.4 订单管理 `OrderView.vue`（**表格，与工单页同风格**）

> **为什么是表格而不是参考图的卡片**：用户明确指定「订单页面使用相同风格的表格展示」，对应最初需求里的「对于订单管理界面你可以参考工单管理界面」。参考图里订单虽是卡片，但工单页是表格——**采用用户指定，不照搬参考图**。
>
> **"同风格"的判据**（与 §6.4.6 逐项对齐，实现时两页应当肉眼看上去是一套）：同一套表头样式（`bg-gray-50 text-gray-500 text-xs`）、同样的行高与 `border-b hover:bg-gray-50`、同样的 `StatusBadge` 徽章、同样的文字按钮操作列、同样的分页条。**订单表不加商品缩略图**——工单表是纯文本，加了图行高就不一致了，会破坏"相同风格"。

**工具条**：搜索框（placeholder「搜索订单号/商品名/买家」）+ 状态下拉（全部/处理中/已发货/已送达）+ 「查询」绿按钮 + 「新增订单」绿按钮。

**表格**：

| # | 列 | 宽度 | 内容 | 说明 |
|---|---|---|---|---|
| 1 | 订单号 | `w-32` | `order_no` | |
| 2 | 商品 | `w-[26%]` | `product_name`，单行截断 + `title` 属性存全名 | 列宽最大，商品名最长 |
| 3 | 品类 | `w-28` | `category` | 对应工单表的「类别」列 |
| 4 | 买家 | `w-32` | `buyer_name` + 空格 + `buyer_code`（如「沈七 P001」）；`buyer_code` 为空只显示姓名 | 对应工单表的「用户原话」列位置 |
| 5 | 金额 | `w-24` | `¥{Number(amount).toFixed(2)}`，**红色加粗**（对齐参考图卡片的价格样式） | |
| 6 | 状态 | `w-24` | `StatusBadge`：处理中(amber) / 已发货(blue) / 已送达(green) | 对应工单表的「状态」列 |
| 7 | 下单日期 | `w-28` | `order_date`（`Date` 列，`isoformat()` 直接就是 `YYYY-MM-DD`） | 对应工单表的「创建时间」列 |
| 8 | 操作 | `w-28` | 「编辑」「删除」文字按钮（编辑主色、删除红色） | 对应工单表的「处理」列 |

**行样式与表头**：见 §6.4.8 的「表格统一规格」，与 §6.4.6 工单表共用同一组 class，不另写一套。

**新增/编辑弹窗**：

| 字段 | 控件 | 新增 | 编辑 |
|---|---|---|---|
| 商品 | select（选项 = `GET /products?page_size=100`，label 商品名） | 必选 | **只读展示**（不可换商品，§5.6） |
| 买家姓名 | input | 必填 | 必填 |
| 买家编码 | input + placeholder「P001」 | 选填 | 选填 |
| 金额 | number input | 留空则取商品当前价 | 必填 |
| 状态 | select 处理中/已发货/已送达 | 默认处理中 | 必填 |
| 下单日期 | `<input type="date">` | 默认今天 | 必填 |

**分页条**：`共 {total} 条` + 每页条数下拉（12/24/48）+ 上一页/页码/下一页，右下角。边界规则见 §6.4.7。

**编辑弹窗里的商品字段**：只读展示（`product_name` + `category`），**不渲染成 select**——不给"换商品"的入口（§5.6 已规定 sku/商品名/品类不可改，换商品应删除后重建）。

**删除确认**：`确定删除订单「{order_no}」？删除后不可恢复。`（§6.4.8 的模板）

**加载/空态/保存禁用/前端校验**：全部走 §6.4.8 的通用约定，不在本页另定。

#### 6.4.5 知识库管理 `KnowledgeView.vue`（表格 + 暂存式新增表单）

**工具条**：搜索框（placeholder「搜索文档编号/文件名」）+ 「查询」+ 「新增文档」绿按钮。

**表格**：

| # | 列 | 宽度 | 内容 | 对应要求 |
|---|---|---|---|---|
| 1 | 文档编号 | `w-24` | `item.id` | 参考图「文档编号」 |
| 2 | **文件名** | `w-[24%]` | `item.original_filename`（单行截断，`title` 属性存全名供 hover 查看） | 「把 title 去掉使用原来的 filename」 |
| 3 | **文件描述** | 自适应 | `item.description \|\| '—'` | 「把内容字段改为文件描述字段」 |
| 4 | **片段数** | `w-20` | `item.chunk_count` | 「添加片段数和创建时间字段」 |
| 5 | 状态 | `w-20` | 徽章：启用(绿) / 停用(灰) | 参考图「状态」列 |
| 6 | **创建时间** | `w-40` | `item.created_at.slice(0,10) + ' ' + slice(11,16)` | 「添加片段数和创建时间字段」 |
| 7 | 操作 | `w-28` | 编辑 / 删除（文字按钮） | 参考图「编辑 删除」 |

行样式与表头：见 §6.4.8 的「表格统一规格」，**不在本页另写**。

**新增文档弹窗 —— 暂存式表单（两阶段）**

与其它弹窗（填好字段、点保存才落库）**根本不同**：这个表单分两步——**先暂存文件（不索引），补完描述再提交索引**。

弹窗有**四个状态**，UI 按状态切换：

| 状态 | 触发 | 界面 |
|---|---|---|
| `idle` | 刚打开 / 暂存失败后 | 只有「上传文件」按钮 + 说明「支持 PDF / Word / TXT / Markdown，单个不超过 30MB」。**下半区的只读信息与描述框都不渲染**；「保存」置灰禁用 |
| `staging` | 点了上传、请求进行中 | 按钮文案变「上传中…」并禁用；「保存」仍禁用。**这一步很快**——只存文件 + 读大小 + 算 MD5，不解析内容 |
| `staged` | 暂存成功 | 「上传文件」按钮文案变「重新上传」；只读区出现**文件基本信息三项**；「文件描述」框启用；「保存」启用。命中重复时额外显示黄色提示条 |
| `committed` | 提交索引成功 | 只读区**补齐「片段数」「创建时间」**；顶部绿色成功条；底部按钮只剩「完成」。**这是「创建时间」第一次出现的地方**（D18：它是索引链路的产物） |

**`staged` 态的完整表单**：

```
┌─ 新增文档 ───────────────────────────────────── × ┐
│                                                  │
│   ┌─────────────────────────────────────────┐    │
│   │           [ 上传文件 ]                  │    │  ← 按钮触发隐藏的 <input type="file">
│   │   支持 PDF / Word / TXT / Markdown       │    │
│   └─────────────────────────────────────────┘    │
│                                                  │
│   ── 以下为上传后自动解析的基本信息，不可编辑 ──   │
│                                                  │
│   文件名     京东智能家具产品知识文档.docx        │  ← 一行截断 + title 全名
│   文件类型   docx                                │
│   文件大小   27.8 KB                             │
│   片段数     —                                   │  ← 保存后生成
│   创建时间   —                                   │  ← 保存后生成
│                                                  │
│   文件描述   ┌─────────────────────────────┐     │  ← 唯一可编辑项
│              │                             │     │
│              └─────────────────────────────┘     │
│                                                  │
│                            [ 取消 ]  [ 保存 ]    │
└──────────────────────────────────────────────────┘
```

**`committed` 态**：同样的框，`片段数` 变成 `38`、`创建时间` 变成 `2026-09-22 10:30`，顶部一条绿底「已保存，智能客服现在可以检索到它」，按钮变「完成」。

**只读信息区的实现约定**（不要用 `disabled` 的 input）：

- 两列布局（`grid grid-cols-[80px_1fr] gap-y-3`），左列标签灰色小字，右列值
- 值是**纯文本节点**。理由：`disabled` 输入框视觉上仍像"能填但被禁用了"，用户会反复点击试图编辑；纯文本 + 浅灰底（`bg-gray-50 rounded px-3 py-1.5`）才是"这就是个信息展示"的语义
- `片段数` / `创建时间` 在 `staged` 态显示 `—`，**不显示"加载中"**——它们不是"正在取"，而是"还没产生"，用破折号表达更准
- **没有任何一项进提交体**（除了描述），它们全是展示

**流程（四个动作，对应 §5.7 的四个端点）**：

**① 点「上传文件」→ `stage`**

1. 隐藏的 `<input type="file" accept=".pdf,.doc,.docx,.txt,.md">`
2. **本地预检**（避免必然失败的请求）：扩展名对照 `settings.allowed_extensions`（`txt,md,pdf,docx`）；大小 ≤ 30MB。不符 → 弹窗内红字提示，**不发请求**，停在 `idle`
3. `POST /api/admin/knowledge/stage`（multipart，`file` + `user_id: me.id`）
4. 成功 → 存下 `{md5, original_filename, file_type, file_size, duplicate, existing}`，进 `staged`
5. 失败（`400`）→ 显示后端返回的 `detail`（管理端端点用标准 4xx 语义，**能拿到具体原因**，不像 `/api/upload` 的 400 会把响应体丢掉），退回 `idle`

**② 命中重复（`duplicate: true`）时**

表单顶部显示黄色提示条：

> 该文件已存在于知识库（{existing.chunk_count} 个片段，创建于 {existing.created_at 截取}）。继续保存只会**更新它的文件描述**，不会新增一条记录。

**不阻止继续**——用户可能就是想给已有文档补描述。这与初稿"检测到重复就终止"不同，因为现在能拿到 `existing` 的信息，提示可以说得很具体。

**③ 点「保存」→ `commit`**

`POST /api/admin/knowledge/commit`，body `{md5, original_filename, description}`（`description` 为空则省略该键，配合 §6.7 的 `cleanBody`）。

| 结果 | 行为 |
|---|---|
| `200` | 存下返回的完整行（含 `chunk_count` / `created_at`）→ **切到 `committed` 态**（不立即关窗） |
| `4xx` | 弹窗**不关闭**，顶部红色错误条 + 保留已填描述。**暂存文件保留**，用户可直接再点「保存」重试（不必重传）；也可以点「取消」走 `unstage` 清掉 |

**为什么要 `committed` 态而不是保存完就关窗**：索引是这条链路里**唯一耗时且可能失败**的一步（PDF 走 MinerU 云端，超时上限 300s）。保存成功后停一下、把「片段数 / 创建时间」亮出来 + 给一句"智能客服现在可以检索到它"，是对这个耗时动作的交代。顺带也满足用户「创建时间在上传成功后显示供展示」的要求（D18：它只能在这时出现）。

**④ 点「取消」→ `unstage`**

这是两阶段设计的核心收益：

| 当前状态 | 点「取消」的行为 |
|---|---|
| `idle` | 直接关闭，**不发任何请求**（什么都没发生） |
| `staging` | 按钮禁用，等请求结束（避免删一个正在写的文件） |
| `staged` | 调 `DELETE /api/admin/knowledge/stage/{md5}` → 删掉暂存文件 → 关闭。**数据库里从头到尾没有过痕迹，不需要确认弹窗**——没什么可丢的（除了用户敲的描述，那段长度有限，可不确认） |
| `committed` | 直接关闭（文档已入库，此时"取消"没有意义，故该状态下按钮已变成「完成」） |

> **与初稿的关键差异**：初稿是"上传即入库，取消不撤销"，需要在取消时弹确认说"文件已入库不会撤销"。**现在不需要了**——取消真的撤销。这是这个改动最直接的收益。

**编辑弹窗**（表格里点「编辑」）：与新增弹窗**共用同一个组件**，通过 `mode` 区分：

| 项 | 新增（`create`） | 编辑（`edit`） |
|---|---|---|
| 「上传文件」按钮 | 显示 | **隐藏**（不提供"替换文件"——换文件等于换一条记录，语义上是删了重建） |
| 文件名 / 类型 / 大小 / 片段数 / 创建时间 | 暂存后填入 | 直接由列表行 `record` 填入 |
| 文件描述 | textarea，`staged` 后可编辑 | textarea，可编辑 |
| 状态 | **不显示**（新建一律 `enabled`） | select（启用 / 停用），可编辑 |
| 「保存」 | 调 `commit` | 调 `PATCH` |

**删除**：`confirm('确定删除文档「{original_filename}」？将同时删除其 {chunk_count} 个知识片段，智能客服不再检索到它。')` → `DELETE /api/admin/knowledge/{md5}`。

#### 6.4.5.1 暂存残留的清理方案（机会式清理）

**残留怎么来的**：三种情况——关掉弹窗没点取消、直接关浏览器、网络断了。共同特征是**前端没机会调 `unstage`**，所以文件留在 `_staging/` 里。

**方案：机会式清理（opportunistic cleanup）**——不引入定时任务，而是在**每次 `stage` 调用开头**扫一遍 `_staging/`，删掉 mtime 超过 TTL 的文件。

**为什么触发时机是完备的**（这是选它的核心理由，不是省事的借口）：

> 残留**只在 `stage` 时产生**。所以只要还有人在用这个功能，清理就会被触发；一旦没人用了，也就不再产生新残留。**泄漏量的上界 = "最后一次使用该功能之前的未提交文件数"**，不随时间无限增长。相比之下，"定时任务"解决的正是"没人用了还要清"这个不存在的问题。

**配置**（`llm_backend/app/core/config.py`，循既有 `*_TTL` 命名惯例，与 `MEMORY_CACHE_TTL` 同类）：

```python
    # 知识库暂存(管理端两阶段上传)设置
    KNOWLEDGE_STAGE_TTL_HOURS: int = 24   # 暂存文件存活上限(小时);超过则被下一次 stage 机会式清理
```

**实现**（放 `llm_backend/app/api/admin/knowledge.py` 模块级）：

```python
import os
import time
from pathlib import Path

from app.core.config import settings

# 解析:knowledge.py → api/admin → api → app → llm_backend,再拼 uploads/_staging
# 与 main.py:34 的 UPLOAD_DIR(= CWD 下的 "uploads")等价 —— run.py:23 会 chdir 到 llm_backend
STAGING_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "_staging"


def _cleanup_stale_staging() -> int:
    """机会式清理过期暂存文件,返回删除个数。

    在 stage 端点开头调用(见下)。删除失败静默跳过 —— 文件可能正被并发的
    unstage / commit 处理,那种情况下"没删成"不是错误。
    """
    if not STAGING_DIR.exists():
        return 0
    cutoff = time.time() - settings.KNOWLEDGE_STAGE_TTL_HOURS * 3600
    removed = 0
    for p in STAGING_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("清理过期暂存文件 {} 个(TTL {}h)", removed, settings.KNOWLEDGE_STAGE_TTL_HOURS)
    return removed
```

**唯一调用点**：`POST /knowledge/stage` 的**第一行**（在写新文件之前——清理放前面语义更清楚；放后面虽也不会误删刚写的文件，但"先打扫再进门"更好读）。

**防"清理误删正在提交的文件"**（唯一的竞态，一行解决）：`commit` 开头对暂存文件 touch 一次，刷新 mtime：

```python
    # commit 开头:刷新 mtime。
    # 否则"暂存后隔了 24h 才点保存"+"恰好有另一次 stage 并发"时,
    # 正在被 process_file 读的文件可能被机会式清理删掉(PDF 走 MinerU 要几十秒,窗口不小)
    os.utime(staged_path)
```

**清理掉了用户还在等的情况**：用户暂存后去开会，隔天回来点「保存」→ `commit` 找不到文件 → 返回 `404 {"detail": "暂存文件不存在或已被清理: <md5>"}`（§5.7 的文案就是为这条路径写的）→ 前端提示"暂存已过期，请重新上传" → 用户重传即可，不会卡住。

**为什么不选其它方案**：

| 方案 | 否决理由 |
|---|---|
| asyncio 定时任务（lifespan 里起 task） | 要管生命周期（启动/关闭/异常重启），多 worker 会重复跑；项目现无任何调度设施，为一个低频清理引入是过度设计 |
| 独立 cron / 系统计划任务 | 部署多一个依赖，与本项目"单服务进程 + 手工脚本"的形态不符 |
| 放在 `commit` 成功后顺带清 | 触发面比 `stage` 窄——残留也可能产生于"从没成功提交过"的用户，覆盖不全 |
| 塞进 `init_db.py` 等运维脚本 | 用户得记得跑，等于没有 |
| 什么都不做（初稿的取舍） | 残留无限累积，虽然是慢性的、也不是本次新引入的问题（`/api/upload` 上传成功的文件同样从不删除），但既然有 10 行就能兜住的方案，没有理由留着 |

**兜底的手工清理**：`rm -rf llm_backend/uploads/_staging/*`——该目录没有任何 DB 依赖，随时可删，删了只影响"尚未提交的暂存"，不影响已入库文档与检索。

#### 6.4.6 工单管理 `TicketView.vue`（表格 + 处理弹窗，对齐参考图）

**工具条**：搜索框（placeholder「搜索工单号/摘要/用户原话」）+ 状态下拉（全部/待处理/已解决）+ 「查询」绿按钮。

**表格**：

| 列 | 宽度 | 内容 |
|---|---|---|
| 工单号 | `w-44` | `ticket_no` |
| 摘要 | `w-52` | `summary`，单行截断 |
| 类别 | `w-28` | `category` |
| 紧急度 | `w-20` | 徽章：低(绿) / 中(琥珀) / 高(红) |
| 用户原话 | 自适应 | `user_query`，单行截断 |
| 状态 | `w-24` | 徽章：待处理(红) / 已解决(绿) |
| 创建时间 | `w-40` | `created_at.slice(0, 10)` + `slice(11, 16)` |
| 操作 | `w-20` | 「处理」主色文字按钮 |

**处理弹窗**（字段与只读性对齐参考图）：

| 字段 | 控件 | 只读 | 备注 |
|---|---|---|---|
| 工单号 | input | ✅ 只读置灰 | 参考图如此 |
| 用户原话 | textarea rows=2 | ✅ 只读置灰 | 参考图如此 |
| 摘要 | input | ❌ | 右下角实时 `{length} / 200` 计数 |
| 问题类别 | input | ❌ | — |
| 紧急度 | select 低/中/高 | ❌ | — |
| 状态 | select 待处理/已解决 | ❌ | — |
| 问题详情 | textarea rows=4 | ❌ | — |
| 处理建议 | textarea rows=4 | ❌ | — |

底部右侧：`取消` + `保存`（绿）。保存 → `PUT /api/admin/tickets/{id}` → 关闭弹窗 + 刷新列表当前页（§6.4.8）。

**分页条**：与其它三个列表页一致（§6.4.7）。种子 8 条一页放得下，但接口返回的是分页对象、UI 也照常渲染分页条——不做特例，否则工单涨到 50 条时要改三处。

#### 6.4.7 通用分页条 `Pagination.vue`

Props：`total` / `page` / `pageSize`；Emits：`update:page` / `update:pageSize`。

布局（右下角，对齐参考图）：`共 {total} 条` + 每页条数 `<select>`（12/24/48）+ `‹` 上一页 + 页码按钮（当前页高亮 `bg-primary text-white`）+ `›` 下一页。禁用态：首页时 `‹` 置灰、末页时 `›` 置灰。

**边界规则（不定义就会出"空白页"）**：

| 情况 | 行为 |
|---|---|
| `total === 0` | 整条分页栏**隐藏**（由各页面渲染「暂无数据」空态代替），不显示"共 0 条"和 0 个页码按钮 |
| 删除后当前页越界（在第 3 页删光，只剩 2 页） | 列表刷新后若 `page > 总页数`，**自动回退到最后一页**并重新拉取。判据：`page > Math.max(1, Math.ceil(total/pageSize))` |
| 切换每页条数 | **重置到第 1 页**（保持当前页会导致越界或跳过数据） |
| 执行搜索 / 切换筛选条件 | **重置到第 1 页**（同上） |

### 6.4.8 通用交互约定（4 个列表页 + 5 个弹窗共用）

spec 到这一节为止只定义了每页的字段与布局，**没定义"数据没回来时、点保存时、删之前"长什么样**。5 个页面各写各的必然不一致，统一在此规定：

| 场景 | 统一行为 |
|---|---|
| **列表加载中** | 列表区域显示 3 行骨架条（`animate-pulse` 的灰条）；**不清空已有数据**（翻页时的体验比闪白好），仅首次加载显示 |
| **列表为空** | 居中空态：`fas fa-inbox` 灰图标 + 「暂无数据」+ 若有搜索词则追加「没有匹配「{keyword}」的记录」 |
| **加载失败** | 居中一行灰字「数据加载失败」+ 「重试」文字按钮（重跑当前请求），**不弹窗**（弹窗会打断后续操作） |
| **删除** | 一律 `confirm()` 二次确认，文案模板 `确定删除{对象}「{名称}」？{后果说明}`。三个删除各有一句后果说明：商品见 §6.4.3、文档见 §6.4.5、**订单**用「确定删除订单「{order_no}」？删除后不可恢复。」 |
| **保存中** | 保存按钮置 `disabled` + 转圈图标，**禁止重复提交**（否则连点两次会因唯一约束弹「商品名称已存在」这类误导性错误） |
| **保存失败** | 弹窗**不关闭**，在弹窗顶部显示红色错误条（保留用户已填内容，不清空） |
| **保存成功** | 关闭弹窗 + 刷新当前页列表（保持 `page` / `page_size` / 筛选条件不变） |
| **前端预校验** | 必填项为空 → 按钮置灰或不提交 + 字段下方红字提示；商品 sku 按 `^JD-[A-Z]{3}-\d{3}$` 本地校验（不合法不发请求）；价格 > 0、库存 ≥ 0。**目的**：把 422 挡在网络往返之前 |
| **金额展示** | 一律 `Number(v).toFixed(2)` 带 `¥` 前缀（见 §5.9：`Decimal("9957.50")` 序列化后是 `9957.5`，不格式化会少一位） |
| **日期展示** | 一律字符串切片（`.slice(0,10)` / `.slice(11,16)`），**不经过 `new Date()`**（§12-8） |
| **表格样式** | 见下方「表格统一规格」，三个表格页（订单 §6.4.4 / 知识库 §6.4.5 / 工单 §6.4.6）共用，**不各写一套** |

**表格统一规格**（订单与工单要求"同风格"，把 class 定在一处才有依据）：

```html
<!-- 外层:横向可滚动,防止列多时撑破布局 -->
<div class="overflow-x-auto bg-white rounded-xl border border-gray-100">
  <table class="w-full text-sm">
    <thead class="bg-gray-50 text-gray-500 text-xs">
      <tr>
        <th class="text-left font-medium px-4 py-3 whitespace-nowrap">订单号</th>
        <!-- 其余列同 -->
      </tr>
    </thead>
    <tbody>
      <tr class="border-t border-gray-100 hover:bg-gray-50 transition-colors">
        <td class="px-4 py-3 whitespace-nowrap">ORD-018</td>
        <!-- 需要截断的列(商品名/摘要/文件名/用户原话):-->
        <td class="px-4 py-3 truncate max-w-0 w-[26%]" :title="row.product_name">{{ row.product_name }}</td>
      </tr>
    </tbody>
  </table>
</div>
```

**单行截断的正确写法**：`truncate max-w-0` + 显式宽度。只写 `truncate` 在 `<td>` 上**不生效**（表格单元格宽度由内容撑开），必须配 `max-w-0` 让宽度约束接管，同时用 `:title` 存全名供 hover 查看。这是实现时最容易返工的一处。

**空态与加载态渲染在 `<table>` 之外**（`v-if="!items.length"` 的空态块与表格平级），不要塞一个占满列数的 `<td>`——列数会随需求变，塞进去每次改列都要同步改空态的 `colspan`。

### 6.5 商品缩略图 `ProductThumb.vue` 与 SVG 生成

**使用范围：只有商品管理页（卡片网格）用它**。订单管理页改成表格后不插图（§6.4.4 的"同风格"判据），所以这个组件在 5 个页面里只被 `ProductView.vue` 引用一处。**后端的 `image` 字段仍照常返回**（订单列表也有），只是表格不渲染——将来若想给表格加缩略图，前端加一个 `<img>` 即可，不用动接口。

**组件契约**：

```vue
<!-- props: src(String), alt(String), size(Number, 默认 96) -->
<img v-if="!failed" :src="src" :alt="alt" @error="failed = true"
     :style="{ width: size + 'px', height: size + 'px' }"
     class="rounded-xl object-cover bg-gray-100 shrink-0" />
<div v-else class="rounded-xl bg-gray-100 text-gray-400 flex items-center justify-center shrink-0"
     :style="{ width: size + 'px', height: size + 'px' }">
  <i class="fas fa-box text-2xl"></i>
</div>
```

**为什么用 `onerror` 而不是后端查文件存在**：后端不引入文件系统依赖，`image` 字段永远返回 `/products/{sku}.svg`；文件不在就由前端兜底。新增商品天然走兜底分支，无需任何额外代码。

**SVG 生成脚本**：`scripts/build_product_placeholders.py`（项目根的 `scripts/`，与 `build_smart_furniture_docx.py` 同级）

| 项 | 设计 |
|---|---|
| 输入 | 直连 DB 读 `SELECT sku, product_name, category FROM product_price_stock` |
| 输出 | `frontend/public/products/{sku}.svg`（目录不存在则创建） |
| 尺寸 | `viewBox="0 0 400 400"` |
| 内容 | 圆角矩形（`rx="32"`）品类渐变底 + 商品名首字符大字（白色，居中偏上）+ 品类小字（白色 70% 透明，居中偏下） |
| 幂等 | 直接覆盖写，可重复执行 |
| 品类→渐变色映射 | 见下表（9 个品类全覆盖，未命中回退灰） |
| **git 追踪** | **不入库**——在 `frontend/.gitignore` 追加一行 `public/products/`，只提交生成脚本 |

**为什么 SVG 不入库**（对齐项目既有约定）：`CLAUDE.md` 商品知识文档规范 §4 明确"docx 文件在 `.gitignore` 内，变更只提交生成脚本与 TSV"，根 `.gitignore:54-55` 同样把生成物 `llm_backend/knowledge_data/` 整体忽略。这 47 个 SVG 与 docx 性质相同——**由 DB 数据派生的生成物**，提交它们等于把"可由脚本重建的东西"塞进仓库，且商品改名/增删后会与脚本产物不一致。实测当前 `git check-ignore -v frontend/public/products/JD-BED-001.svg` **无命中**（不被忽略），**不加规则会被 §10 步 16 的 `git add .` 静默入库**。

**不入库的代价（明说）**：新克隆的仓库只有源码，`frontend/public/` 是空的 → 跑 `npm run build` 后页面**图片全走 `onerror` 兜底**，显示灰色方块 + 箱子图标。页面"看起来正常但没图"，容易被误判成 bug。因此：

- `README.md` 的启动步骤里，`build_product_placeholders.py` 必须列在 `npm run build` **之前**
- §9 验证步骤 5 已把它列为独立步骤，跑完再构建

| 品类 | 渐变起 | 渐变止 |
|---|---|---|
| 智能门锁 | `#2563eb` | `#1d4ed8` |
| 电动智能沙发 | `#b45309` | `#92400e` |
| 智能窗帘 | `#7c3aed` | `#5b21b6` |
| 电动升降桌 | `#0891b2` | `#0e7490` |
| 智能晾衣架 | `#059669` | `#047857` |
| 智能电动床 | `#4f46e5` | `#4338ca` |
| 智能床垫 | `#db2777` | `#be185d` |
| 智能床头柜 | `#ea580c` | `#c2410c` |
| 按摩椅 | `#dc2626` | `#b91c1c` |
| （未命中） | `#64748b` | `#475569` |

**首字符取法**：取 `product_name` 第一个非空字符（中英文均可）。若首字符是数字/字母（如 `8H 智能电动床`），取其前 2 个字符（数字单字视觉太单薄）。

### 6.6 ECharts 封装（三个图表组件）

**依赖**：`npm --prefix frontend install echarts`（写进 `frontend/package.json` 的 `dependencies`）。

**按需引入**（减小 admin chunk 体积）：

```js
import * as echarts from 'echarts/core';
import { LineChart, BarChart, PieChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
echarts.use([LineChart, BarChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);
```

**组件统一契约**（`LineChart.vue` / `BarChart.vue` / `DonutChart.vue` 一致）：

| Props | 类型 | 说明 |
|---|---|---|
| `data` | Object/Array | 各图自己的数据结构（见 §5.4 响应体） |
| `title` | String | 图标题，渲染为组件内的 `<h3>`，**不用 ECharts 的 title**（样式统一交给 Tailwind） |

**共同实现要点**：

- **`echarts.use([...])` 必须写在模块顶层**（每个图表组件文件顶部各写一份，幂等；或抽到一个 `charts/echarts-setup.js` 由三个组件 import）。**绝不能写在 `onMounted` 里**——那晚于 `echarts.init`，会报 `Component series.line not exists. Load it first.`。这是最容易犯的一个错，因为"初始化前先注册"的顺序直觉上很像该放在挂载钩子里。
- `onMounted` 里 `echarts.init(el)`；`onBeforeUnmount` 里 `chart.dispose()`（**必须 dispose**，否则切换页面会泄漏 canvas 与监听器）
- `watch(() => props.data, ...)` 里 `chart.setOption(option, true)`（`true` = 不合并，避免图例残留）
- `window.addEventListener('resize', chart.resize)`，`onBeforeUnmount` 一并 `removeEventListener`
- 容器高度固定 `320px`
- **空数据/全零态**：`props.data` 为空数组、或所有 `value` 都为 0 时，**不调 `setOption`**，容器内渲染居中灰字「暂无数据」。理由：ECharts 在数据全 0 时**一个扇区都不画**（环图只剩图例、柱状图只剩坐标轴），看起来像加载失败。触发场景真实存在——工单种子若全是「待处理」，`已解决` 就是 0；商品表被清空时 `product_category` 是 `[]`。

**图例实现方式统一**：三张图**图例一律交给 ECharts 的 `LegendComponent`**（已注册），不用 HTML 自绘图例。§6.4.2 说的"标题右侧小图例"指 §5.4 返回的 `trend` 数据对应折线的两条线名（订单 / 客服会话）——同样走 ECharts legend，位置 `right: 0, top: 0`。不混用两种图例实现，否则四个图并排时会明显不齐。

**颜色规范（固定，不随数据变）**：

| 用途 | 颜色 |
|---|---|
| 订单线 / 会话线 | 主色 `#16a34a`（订单）+ `#3b82f6`（会话） |
| 订单状态（处理中/已发货/已送达） | `#f59e0b` / `#3b82f6` / `#10b981` |
| 工单状态（待处理/已解决） | `#ef4444` / `#10b981` |
| 商品品类柱 | 主色 `#16a34a`（单色，参考图也是单色柱） |

颜色数组按 §5.4 里后端保证的**固定顺序**取用——这是后端 `ORDER BY` 顺序不能随意改的原因。

### 6.7 `src/admin/api.js` 契约

统一封装：所有请求带 `authHeaders()`，`handleUnauthorized(res)` 处理 401，非 2xx 抛 `Error(detail)`。

```js
import { authHeaders, handleUnauthorized } from '../api/auth.js';

async function request(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}),
               ...authHeaders(), ...options.headers },
  });
  if (handleUnauthorized(res)) throw new Error('登录已失效');
  if (res.status === 403) throw new Error('需要管理员权限');
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `请求失败: ${res.status}`);
  }
  return res.json();
}

export const getStats = ()          => request('/api/admin/console/stats');
export const getCharts = (days=7)   => request(`/api/admin/console/charts?days=${days}`);
export const listProducts = (p)     => request(`/api/admin/products?${qs(p)}`);
export const createProduct = (b)    => request('/api/admin/products', { method: 'POST', body: JSON.stringify(b) });
export const updateProduct = (sku,b)=> request(`/api/admin/products/${encodeURIComponent(sku)}`, { method: 'PUT', body: JSON.stringify(b) });
export const deleteProduct = (sku)  => request(`/api/admin/products/${encodeURIComponent(sku)}`, { method: 'DELETE' });
export const listCategories = ()    => request('/api/admin/products/categories');
export const listOrders = (p)       => request(`/api/admin/orders?${qs(p)}`);
export const createOrder = (b)      => request('/api/admin/orders', { method: 'POST', body: JSON.stringify(b) });
export const updateOrder = (id, b)  => request(`/api/admin/orders/${id}`, { method: 'PUT', body: JSON.stringify(b) });
export const deleteOrder = (id)     => request(`/api/admin/orders/${id}`, { method: 'DELETE' });
export const listKnowledge = (p)    => request(`/api/admin/knowledge?${qs(p)}`);
export const commitKnowledge = (b)  => request('/api/admin/knowledge/commit', { method: 'POST', body: JSON.stringify(cleanBody(b)) });
export const unstageKnowledge = (md5)=> request(`/api/admin/knowledge/stage/${encodeURIComponent(md5)}`, { method: 'DELETE' });
export const updateKnowledge = (md5,b) => request(`/api/admin/knowledge/${encodeURIComponent(md5)}`, { method: 'PATCH', body: JSON.stringify(cleanBody(b)) });
export const deleteKnowledge = (md5)=> request(`/api/admin/knowledge/${encodeURIComponent(md5)}`, { method: 'DELETE' });
export const listTickets = (p)      => request(`/api/admin/tickets?${qs(p)}`);
export const updateTicket = (id, b) => request(`/api/admin/tickets/${id}`, { method: 'PUT', body: JSON.stringify(b) });
```

`qs(obj)`：过滤掉 `undefined` / `null` / `''` 的键后用 `URLSearchParams` 拼串。

**知识库暂存不走 `request()`，也不复用 `src/api/upload.js`**——后者把 URL 硬编码成了 `/api/upload`（`upload.js:31`），管理端要打的是 `/api/admin/knowledge/stage`。所以在 `admin/api.js` 里自带一个改名版：

```js
// 管理端专用的带进度上传。与 src/api/upload.js:25 的 uploadFileWithProgress 结构相同,
// 两点差异:(1) URL 指向管理端 stage 端点;(2) 【非 2xx 时解析响应体】把 detail 带出来 ——
// 后者是管理端端点用标准 4xx 语义换来的好处,客户端那个版本的 400 是把响应体丢掉的
export function stageFile({ file, userId, onProgress }) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('user_id', userId);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/admin/knowledge/stage');
    const token = localStorage.getItem('token');
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* 非 JSON,下面按状态码兜底 */ }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      if (xhr.status === 401) {
        window.dispatchEvent(new Event('auth:unauthorized'));
        return reject(new Error('登录已失效'));
      }
      reject(new Error(body?.detail || `上传失败: ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    xhr.send(fd);
  });
}
```

**同一文件里注意**：`/api/upload` 与本端点对 4xx 的处理不同（前者把失败包进 200 的 `status=failed`），所以**不要**把这个函数改成通用上传器去服务客户端——两者契约不同，各留各的。

**`request()` 的附加职责：请求体空串归一化**（实测坑，不加必炸）：

```js
// 浏览器 <input type="number"> / <input type="date"> 留空时给的是 ''(不是 undefined),
// JSON.stringify 后就是 ""。而实测 pydantic 2.12 对 Optional[Decimal]/Optional[int]/Optional[date]
// 收到 '' 一律 ValidationError → 422,只有 Optional[str] 接受 ''(存成空字符串)。
// 所以发请求前必须把 "" 从 body 里摘掉,等价于"未传"= 不更新。
function cleanBody(body) {
  return Object.fromEntries(
    Object.entries(body).filter(([, v]) => v !== '' && v !== undefined)
  );
}
// 用法:updateProduct(sku, cleanBody(form)) 等
```

**实测结果**（本机 pydantic 2.12.5）：

| 字段类型 | 收到 `''` | 处理 |
|---|---|---|
| `Optional[Decimal]`（`current_price`/`amount`） | **422** | 必须摘掉 |
| `Optional[int]`（`stock_quantity`/`user_id`） | **422** | 必须摘掉 |
| `Optional[date]`（`order_date`） | **422** | 必须摘掉 |
| `Optional[str]`（`buyer_code`/`description`） | 通过，存成 `''` | 无需摘，但若要"清空为 NULL"得传 `null` 且后端用 `exclude_unset`（§5.7） |

---

## 7. 种子数据规范

四个脚本，均为**幂等**（重复执行不产生额外行）。**分两处放置**，按"是否连 DB"划：

| 脚本 | 位置 | 为什么 |
|---|---|---|
| `seed_admin_account.py` | `llm_backend/scripts/` | 连 DB（`users` 表），与 `import_product_price_stock.py` / `ingest_knowledge.py` 同族 |
| `seed_orders.py` | `llm_backend/scripts/` | 连 DB（读 `product_price_stock`、写 `orders`） |
| `seed_tickets.py` | `llm_backend/scripts/` | 连 DB（写 `tickets`） |
| `build_product_placeholders.py` | **根 `scripts/`** | 产物是**前端静态资源**，与 `build_smart_furniture_docx.py` / `build_jd_aftersales_docx.py` 同族。但它**要连 DB 读商品**——这在本项目里没有先例，见 §7.4 的 `sys.path` 引导写法 |

**⚠️ 根 `scripts/` 下没有任何连 DB 的先例**（实测 `grep -rn "AsyncSessionLocal\|psycopg\|get_logger" scripts/*.py` → 0 命中；那里只有纯文件读写的 build 脚本）。所以 §7.4 必须写明引导代码，照抄根脚本的 `PROJECT_ROOT = Path(__file__).parent.parent` 是**导入不到 `app` 包的**（那指向项目根，而 `app` 在 `llm_backend/` 下一层）。

### 7.1 `seed_admin_account.py` — 管理员账号

| 项 | 规范 |
|---|---|
| 目标账号 | 优先取 `email = 'admin_test@test.com'`（现存 id=6，持有全部 2 份知识文档） |
| 存在时 | `UPDATE users SET role='admin' WHERE email='admin_test@test.com'` |
| 不存在时 | 创建 `username='admin_test'`, `email='admin_test@test.com'`, `role='admin'`，密码见下 |
| 密码 | **`get_password_hash(明文)`，明文 `admin`**（对齐 `docs/项目问题.md` #15e 里已改成 `admin` 的现有账号） |
| 已有账号 | **只 `UPDATE role='admin'`，不碰 `password_hash`**——避免把人已经用顺手的密码改掉 |
| 新建账号 | 才用 `get_password_hash("admin")` 设密码 |
| 打印 | 结束时打印 `email / role / id`；账号是**新建的**才额外打印密码，已存在的打印 `(密码未改动，沿用原密码)` |

**关键约束**：`get_password_hash` 接受的是**明文**（`hashing.py` 的 docstring 说"前端已做过 SHA256"是过期的，前端实际传明文——见 §2.3B）。脚本里**绝不能**写 `get_password_hash(hashlib.sha256(b"admin").hexdigest())`，那样这个账号永远登不上。

**不重置现有账号密码**：若账号已存在，只改 `role`，不碰 `password_hash`——避免把人已经用顺手的密码改掉。

### 7.2 `seed_orders.py` — 订单种子（18 条）

| 项 | 规范 |
|---|---|
| 数据源 | `SELECT sku, product_name, category, current_price FROM product_price_stock ORDER BY sku` |
| 条数 | 18 |
| 商品选取 | 取前 18 行（sku 升序），保证每条对应真实商品 |
| 金额 | 直接取该商品 `current_price` |
| 买家名 | 从固定名单轮转：`沈七, 蒋六, 卫五, 楚四, 陈三, 冯二, 郑一, 吴十, 周九, 孙八, 钱七, 赵六, 李四, 王五, 张伟, 刘敏, 陈静, 杨帆` |
| 买家编码 | `P{序号:03d}`，与买家名一一对应（`沈七→P001`） |
| `user_id` | **现查**轮转：`SELECT id FROM users ORDER BY id LIMIT 4`，第 n 条填 `ids[n % len(ids)]`；查不到任何用户时填 `NULL`。**不写死 `[3,4,5,6]`**——那 4 个 id 今天存在，但库一旦重建/清过，写死会让 `orders.user_id` 外键直接报错、整个种子失败 |
| 状态 | 均匀分布：`处理中` / `已发货` / `已送达` 各 6 条（保证控制台环形图三色都有） |
| 下单日期 | 从 **`datetime.now(timezone.utc).date()`** 往前推 `(index % 14)` 天——基准必须与 §5.4(a) 的图表日期轴完全一致（都用 UTC），否则折线错位一天。**必须落在近 14 天内**，否则控制台"近 7 日趋势"折线全是 0 |
| 订单号 | `ORD-{index+1:03d}` → `ORD-001` … `ORD-018` |
| 幂等策略 | **upsert 覆盖，不是"已存在则跳过"**：`INSERT ... ON CONFLICT (order_no) DO UPDATE SET order_date=EXCLUDED.order_date, status=EXCLUDED.status` |

**为什么必须 upsert 而不是跳过**（原设计的一个真实缺陷）：`order_date` 是"运行日往前推 `index%14` 天"。若已存在就跳过，已有 18 行的日期**永不刷新**——今天是 2026-09-22，跑完种子后趋势图覆盖 09-09~09-22；一周后（09-29）图表窗口变成 09-23~09-29，**订单折线全 0**，而统计卡片仍写着"订单 18"。演示前重跑种子是再自然不过的操作，upsert 让它同时起到"刷新演示数据"的作用。

**同类窗口效应（不处理，但要知道）**：`conversations` 是真实数据（实测最后一天 2026-09-19），一周后"近 7 日趋势"的会话折线同样会归零。这是真实数据自然衰减，不是 bug，**本 spec 不造假会话**。

**日期分布要明确**：18 条订单里，让**最近 7 天每天至少 1 条**（`index % 14` 的前 7 个索引落在近 7 天），保证折线图有起伏不是一条平线。

### 7.3 `seed_tickets.py` — 工单种子（8 条）

| 字段 | 规范 |
|---|---|
| `ticket_no` | `TK-{YYYYMMDD}{序号:06d}`，**日期部分写成模块级常量 `BASE_DATE = "20260914"`（对齐参考图的 `TK-20260914...`），不取运行日** |
| `created_at` | **`datetime.now(timezone.utc)`**（UTC，与 §5.4a/§7.2 同基准）往前推 `(index * 6)` 小时，保证时间戳不同 |
| `status` | 5 条 `待处理` + 3 条 `已解决`（参考图 4 条是 3 待处理 1 已解决的比例） |
| `urgency` | 低 2 / 中 5 / 高 1（参考图 4 条全是「中」，但控制台图需要区分度） |
| `category` | 售后服务 / 退货咨询 / 投诉 / 其他 |
| 已解决的 3 条 | 填 `resolved_at`（= `created_at` + 2 小时）+ `handler = 'admin_test'` |
| 待处理的 5 条 | `resolved_at` / `handler` 均为 `NULL` |
| 幂等键 | `ticket_no`（**因为日期已固定为常量，跨天重跑才会稳定命中**） |

**为什么 `ticket_no` 的日期不能取运行日**（原设计的一个真实缺陷）：原写法是"日期取 `created_at` 的日期"、`created_at` 又由 `datetime.now()` 推导 → **每天重跑，8 条的 `ticket_no` 全部是新号 → 全部 INSERT 成功 → 表变 16 条**。§9 步骤 4 的判据"重复执行仍为 8"就只在**同一天内**成立，而"演示前重跑一次种子"是最自然的操作。把日期钉成常量后，跨天重跑也稳定命中幂等键。

**为什么 `created_at` 用 UTC 而不是本地 `datetime.now()`**：全链路约定库时区为 UTC（§5.4a，实测 `TimeZone=Etc/UTC`）。若种子写本地时间（UTC+8）的 naive datetime，存进 `timestamp without time zone` 后被全系统当 UTC 读 → 工单列表显示的时间比实际**早 8 小时**（与 `docs/项目问题.md` #15a 同源的坑，只是这次源头在种子数据）。工单的图表按 status 聚合，不受影响，但列表时间列会错。

**8 条的字面内容**（`user_query` / `summary` / `detail` / `suggestion` 全部写死，直接采用参考图的 4 条原文 + 补 4 条，保证与参考图观感一致）：

| # | user_query | summary | category | urgency | status |
|---|---|---|---|---|---|
| 1 | 如果我买不合适，怎么去退货呢 | 智能手表S3退货流程咨询 | 售后服务 | 中 | 待处理 |
| 2 | ORD-001这个 | 用户问题需人工处理 | 其他 | 中 | 已解决 |
| 3 | 我要投诉 | 用户投诉，需人工处理 | 投诉 | 中 | 待处理 |
| 4 | 怎么退货？ | 用户咨询退货流程 | 退货咨询 | 低 | 待处理 |
| 5 | 订单一直没发货，都一周了 | 订单发货延迟咨询 | 售后服务 | 高 | 待处理 |
| 6 | 买贵了能退差价吗 | 价格保护咨询 | 售后服务 | 低 | 已解决 |
| 7 | 收到货是坏的 | 商品破损投诉 | 投诉 | 中 | 待处理 |
| 8 | 发票怎么开 | 发票开具咨询 | 其他 | 低 | 已解决 |

第 1 条的 `detail` / `suggestion` 直接采用参考图原文：

- `detail`: `用户已选定智能手表S3（¥699），关注购买后不合适时的退货政策与操作流程。`
- `suggestion`: `转接人工客服，提供清晰退货指引：7天无理由退换（商品未拆封/配件齐全）、寄回方式（平台预付快递单）、退款时效（签收后1-3工作日原路退回），并主动协助生成退货单。`

其余 7 条的 `detail` / `suggestion` 按同样文风自拟（各 1~2 句），内容与 `category` 对应。

### 7.4 `scripts/build_product_placeholders.py` — 商品占位图

见 §6.5 的完整规范。脚本开头**必须**按下面的写法引导（这是本项目**第一个**放在根 `scripts/` 却要连 DB 的脚本，没有先例可抄）：

```python
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent          # = 项目根
sys.path.insert(0, str(PROJECT_ROOT / "llm_backend"))          # ⚠️ app 包在 llm_backend 下,不是项目根
import app.core.database  # noqa: F401  —— 触发 Windows SelectorEventLoop 补丁(database.py:5-8)

from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.models.product_price_stock import ProductPriceStock
```

对照根 `scripts/build_smart_furniture_docx.py` 的 `PROJECT_ROOT = Path(__file__).resolve().parent.parent`——那一行在那边够用（它只做文件读写），在这里**不够**：`app` 在 `llm_backend/` 下，必须再拼一层。而 `llm_backend/scripts/import_product_price_stock.py:13-17` 是另一套写法（它的 `ROOT_DIR` 就是 `llm_backend`）。

好消息：`app/core/config.py:6-7` 的 `ENV_FILE` 是**绝对路径**，所以从任何 CWD 跑都能读到 `.env`，只需要处理模块路径这一个坑。

结束时打印生成数量与输出目录。

### 7.5 执行顺序

```bash
# ── 第一步必做,且必须先于启动服务(见 §9 顶部告警) ──
cd llm_backend
python scripts/init_db.py                 # 1. 建 orders/tickets 表 + 加 4 个增量列
python scripts/seed_admin_account.py      # 2. 管理员账号
python scripts/seed_orders.py             # 3. 18 条订单
python scripts/seed_tickets.py            # 4. 8 条工单

# ── 占位图脚本在项目根的 scripts/(它产出前端静态资源),所以要在根目录跑 ──
cd ..
python scripts/build_product_placeholders.py   # 5. 47 个 SVG → frontend/public/products/
```

**顺序不能换**：`init_db.py` 必须在最前——种子脚本 INSERT 的列（`users.role`）与表（`orders`/`tickets`）都依赖它。占位图脚本读 `product_price_stock`（既有表），不依赖种子，但放在最后让"数据 → 产物"的因果链清晰。

**重跑语义**（四个脚本都可重复执行，但效果不同）：

| 脚本 | 重跑效果 |
|---|---|
| `init_db.py` | 幂等，无变化 |
| `seed_admin_account.py` | 只确保 `role='admin'`，不重置密码 |
| `seed_orders.py` | **upsert 覆盖**——顺带把订单日期刷新到"近 14 天"，解决趋势图随时间归零（见 §7.2） |
| `seed_tickets.py` | 幂等（`ticket_no` 日期是常量），无变化 |
| `build_product_placeholders.py` | 覆盖写，无变化 |

---

## 8. 测试方案

沿用项目既有写法：`httpx.ASGITransport(app=app)` + `AsyncClient` + **真实数据库**（`tests/test_documents_api.py` 即此模式）。

### 8.1 `tests/conftest.py` 新增 fixture

```python
TEST_PASSWORD = "TestAdmin123"


@asynccontextmanager
async def _temp_user(role: str):
    """建一个临时账号,退出时删除。明文密码 TEST_PASSWORD(前端传明文,见 spec §2.3B)。"""
    import uuid
    from sqlalchemy import delete
    from app.core.database import AsyncSessionLocal
    from app.core.hashing import get_password_hash
    from app.models.user import User

    email = f"{role}_{uuid.uuid4().hex[:8]}@test.com"
    async with AsyncSessionLocal() as s:
        u = User(username=email.split("@")[0], email=email,
                 password_hash=get_password_hash(TEST_PASSWORD), role=role)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        uid = u.id
    try:
        yield {"id": uid, "email": email, "password": TEST_PASSWORD}
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(User).where(User.id == uid))
            await s.commit()


@pytest.fixture
async def admin_user():
    """临时管理员(role='admin')。"""
    async with _temp_user("admin") as u:
        yield u


@pytest.fixture
async def normal_user():
    """临时普通用户(role='user'),用于越权测试。"""
    async with _temp_user("user") as u:
        yield u


async def _login(email: str, password: str) -> str:
    # ⚠️ main 必须【函数内】导入,不能提到 conftest 顶层 ——
    # main.py:543 的 StaticFiles(directory=frontend/dist) 在目录不存在时【构造即抛】
    # RuntimeError。frontend/dist 被 gitignore,没构建过的环境里,conftest 顶层 import
    # 会让【整套测试】(含与本次无关的 test_cleaner/test_rrf)在 collection 阶段全灭。
    # 现有 tests/test_documents_api.py:5 也是按需在模块内 import 的,保持一致。
    from main import app                 # noqa: PLC0415

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/token", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]


@pytest.fixture
async def admin_token(admin_user):
    return await _login(admin_user["email"], admin_user["password"])


@pytest.fixture
async def normal_token(normal_user):
    return await _login(normal_user["email"], normal_user["password"])
```

用 `@asynccontextmanager` 抽公共建号逻辑，避免 `admin_user` / `normal_user` 两份重复代码——两者只差 `role` 一个字面值。

**清理要彻底**：临时用户删除时，若有测试给它建了 `conversations`，`users` 上的 `ON DELETE CASCADE` 会连带清理；本模块的测试不建会话，故只需删用户。

### 8.2 `tests/test_admin_auth.py`（**核心文件，越权是本模块的主要风险**）

| 用例 | 断言 |
|---|---|
| 无令牌访问 `/api/admin/console/stats` | `401` |
| 伪造令牌（`Bearer invalid.token.here`） | `401` |
| **普通用户令牌**访问 `/api/admin/console/stats` | **`403`** |
| **普通用户令牌**访问 `/api/admin/products` | **`403`** |
| **普通用户令牌**访问 `/api/admin/orders` / `knowledge` / `tickets` | 均 **`403`** |
| 普通用户令牌访问 `GET /api/users/me` | `200`（既有端点不受影响） |
| 管理员令牌访问 `/api/admin/console/stats` | `200` |
| `POST /api/token` 响应含 `role` | 管理员返回 `"admin"`，普通用户返回 `"user"` |
| `GET /api/users/me` 响应含 `role` | 同上 |

**参数化**：5 个模块各挑 1 个端点用 `@pytest.mark.parametrize` 覆盖，避免 5 份重复代码。

### 8.3 `tests/test_admin_products.py`

**断言一律与现查 DB 比对，不写死 47 / 12 / 9**（与 §8.6 同原则）——写死会被本模块自己的功能推翻：§9 步骤 12 要求人工新增一条测试商品，只要那一步的删除没做干净，整套商品测试立刻变红。

| 用例 | 断言 |
|---|---|
| 列表默认分页 | `total == await db_count(ProductPriceStock)`；`len(items) <= 12`；`page == 1`；`total > len(items)` 且 `page_size=12`（即**分页确实在分页**，`total` 不是 `len(items)`） |
| `keyword` 匹配商品名 | 搜「门锁」→ `total > 0` 且所有 item 的 name/sku 含关键词 |
| `keyword` 匹配 sku | 搜 `JD-BED-001` → `total == 1`（sku 唯一，可写死） |
| `category` 筛选 | 筛「智能门锁」→ `total == await db_count(ProductPriceStock, ProductPriceStock.category == "智能门锁")` |
| 新增成功 | `POST` 带合法 sku → list 中能查到，`stock_quantity` 一致 |
| 新增 sku 格式非法 | `POST` sku=`ABC-1` → `422` |
| 新增 sku 重复 | 用 `JD-BED-001` → `400` |
| 新增 product_name 重复 | 用已存在名称 + 新 sku → `400` |
| 编辑改价格与库存 | `PUT` → 重新 GET 值已变 |
| 编辑不存在的 sku | `PUT /products/JD-XXX-999` → `404` |
| 删除成功 | `DELETE` → list 中消失 |
| 删除不存在 | `404` |
| 品类列表 | 长度 == `SELECT COUNT(DISTINCT category) FROM product_price_stock`；含「智能门锁」；**去重且有序** |
| `current_price` 是 json number 不是字符串 | `isinstance(item["current_price"], float)` |

清理：所有新增用 `JD-TST-9xx` 段的 sku，`cleanup` fixture 里 `DELETE WHERE sku LIKE 'JD-TST-%'`。

### 8.4 `tests/test_admin_orders.py`

| 用例 | 断言 |
|---|---|
| 列表 | `total >= 18`（种子）或 `>= 0`（空库）；字段齐全 |
| 新增（不传 amount） | `amount == 该商品 current_price`；`product_name` 回填正确 |
| 新增（传 amount） | 用传入值 |
| 新增不存在的 sku | `400` |
| 新增后 `order_no` 唯一 | 连续新增 2 条，两个 `order_no` 不同 |
| **删除中间一条后再新增** | `order_no` 不与现存重复（验证 §5.6 的 max+1 而非 count+1） |
| 改状态 | `PUT` → GET 值已变 |
| 改 sku 字段被忽略 | body 里塞 `sku` → 值不变（schema 无该字段，pydantic 默认忽略） |
| 删除 | 成功；再删 `404` |

清理：新增订单的 `buyer_name` 统一用 `测试买家`，`cleanup` 里 `DELETE WHERE buyer_name = '测试买家'`。

### 8.5 `tests/test_admin_knowledge.py`

| 用例 | 断言 |
|---|---|
| 列表 | **不传 user_id 也能拿到**（这是 D6 的核心行为）；`total >= 2` 且必含 `original_filename` 为「京东自营售后政策.docx」「京东智能家具产品知识文档.docx」两行（不写死 `== 2`，避免与其他用例的执行顺序耦合） |
| 列表字段 | 含 `original_filename` / `description` / `status` / `chunk_count` / `created_at` / `owner_id`；**不含 `title`**（断言 `"title" not in item`，防止后面有人又加回来） |
| 两行种子的 `chunk_count` | 分别为 `4` 与 `38`（实测值，§2.1） |
| PATCH 描述 | 成功后重查值已变 |
| **PATCH 能把 description 还原为 NULL** | 发 `json={"description": None}` → 重查 `description IS NULL`。**这条是 §5.7「用 `exclude_unset` 而非 `is not None`」的直接验收**——写成后者这条必挂 |
| **`stage` 不写库**（**两阶段设计的核心断言**） | 暂存一个临时 md 文件 → 响应含 `md5`/`file_type`/`file_size`/`duplicate: false`；**同时断言 `SELECT COUNT(*) FROM documents` 与暂存前一致、`document_chunks` 也一致**——这是 D17"暂存不索引"的直接验收。再断言磁盘上出现了 `uploads/_staging/{md5}.md` |
| **`unstage` 真的撤销** | 暂存后调 `DELETE /knowledge/stage/{md5}` → `deleted: true`；**暂存文件消失**；`documents` 计数仍与最初一致（从头到尾没写过库） |
| **`unstage` 幂等** | 连调两次 → 第二次 `deleted: false` 且 **HTTP 200**（不是 404） |
| **`stage` 校验** | 传 `.exe` → `400` 且 detail 含「不支持」；传一个 0 字节文件 → `400`。**两种情况都不该在 `_staging/` 留下文件** |
| **`commit` 走完整链路** | 暂存临时 md → `commit` → 返回完整行且含 `chunk_count > 0`、`created_at` 非空、`description` 为提交时传的值；列表能查到；`document_chunks` 有对应行 |
| **`commit` 成功后删暂存文件** | `commit` 后 `uploads/_staging/{md5}.md` **不存在** |
| **`commit` 失败保留暂存文件** | 暂存一个**内容为空**的 md（能过 stage 的空文件校验吗？——0 字节会被 stage 挡掉，改用只含空白字符的 md 让它过 stage、倒在 commit 的 `empty_file`）→ `commit` 返回 `4xx` → **暂存文件仍在**（可重试） |
| **`commit` 暂存文件不存在** | 用一个格式合法但没暂存过的 md5 → `404` |
| `commit` 的 `md5` 格式非法 | 传 `"xyz"` → `422` |
| **`commit` 命中重复** | 同一个临时 md 暂存两次、`commit` 两次 → 第二次 `duplicate: true` 且 `documents` **不新增行** |
| 测试后还原 | 用 `json={"description": None}`（**不是 `""`**）把两行种子文档还原为 NULL 原值；`""` 会留下空串而非 NULL，属静默污染演示数据（前端 `description \|\| '—'` 恰好能兜住，所以**看不出问题**） |
| PATCH 不存在 md5 | `404` |
| PATCH status 非法值 | `422` |
| DELETE 不存在 md5 | `404` |
| **stage→commit→PATCH→DELETE 全链路** | 用 `test_user_id` 风格的新 user_id：暂存临时 md → `commit`（带描述）→ 列表能查到且描述正确 → `PATCH` 改状态为 `disabled` → `DELETE` → `documents` 与 `document_chunks` 都清空 |

全链路用例的清理复用既有 `cleanup_test_data` fixture（它已按 `user_id` 删 documents + chunks）。**但暂存文件不在它的清理范围内**——测试要在 `finally` 里补一句 `unstage`，或断言失败时手工 `rm -rf llm_backend/uploads/_staging/`。测试用的暂存文件名是 md5，与生产文件**不会重名**（内容不同），但仍应清掉避免堆积。

**重要**：`PATCH` 用例必须还原种子文档的 `description` 原值（初始为 `NULL`），否则跑完测试会污染演示数据。

### 8.6 `tests/test_admin_console.py`

**断言方式：全部与"现查 DB"比对，不写死数字。** 测试跑在真实库上，写死数字会随种子/演示数据变化而碎（§2.1 的 47/2/13/64 是写 spec 时的实测值，仅供人工核对，不作为断言常量）。统一用这个模式：

```python
async def db_count(model, *conds):
    from sqlalchemy import func, select
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        stmt = select(func.count()).select_from(model)
        if conds:
            stmt = stmt.where(*conds)
        return (await s.execute(stmt)).scalar()

# 用例内
assert stats["products"]["total"] == await db_count(ProductPriceStock)
assert stats["products"]["in_stock"] == await db_count(ProductPriceStock, ProductPriceStock.stock_quantity > 0)
assert stats["conversations"]["messages"] == await db_count(Message)
assert stats["tickets"]["pending"] == await db_count(Ticket, Ticket.status == "待处理")
```

| 用例 | 断言 |
|---|---|
| 6 个卡片字段全部齐全 | 响应含 `products/orders/knowledge/users/conversations/tickets` 六个键，且 `products` 有 `total`+`in_stock`，`tickets` 有 `total`+`pending`，`conversations` 有 `total`+`messages` |
| 各字段口径 | 逐条与 `db_count(...)` 现查比对（见上方代码）。**重点覆盖 `in_stock` 与 `pending` 这两个带 where 的口径**——只比 total 覆盖不到条件过滤写错 |
| `knowledge.total` | `== db_count(Document)`，**不按 user_id 过滤**（D6 的核心行为，比对时确认该值 ≥ 2） |
| `charts.trend.days` 长度 | `== 7`；`chart?days=14` → `== 14` |
| `trend.orders` / `trend.conversations` 长度 | `== len(days)`（**补零逻辑**） |
| `trend.days[-1]` | `== datetime.now(timezone.utc).strftime("%m-%d")` —— **必须写 UTC**。若测试侧用本地 `datetime.now()` 构造"今天"，在 UTC+8 的本地 00:00~08:00 窗口内两者差一天 → 该用例每天随机变红（后端按 §5.4a 用 UTC 生成日期轴） |
| `order_status` 含全部 3 个状态 | 即使某状态 0 条也返回该项 |
| `ticket_status` 含全部 2 个状态 | 同上 |
| `product_category` 按数量降序 | 首项是数量最多的品类 |
| `days=0` / `days=31` | `422` |

### 8.7 回归：既有测试全绿

`python -m pytest llm_backend/tests/ -q`，除 `test_bm25_retriever.py::test_bm25_recalls_docs_with_partial_terms`（**既有失败**，`docs/项目问题.md` #8，与本次改动无关）外全过。

**回归重点**：`test_documents_api.py` 必须仍全过——本次给 `documents` 加了 3 列、给 `users` 加了 1 列，若模型定义有误（如 `nullable=False` 缺 `server_default`），这个文件会先炸。

---

## 9. 验证方案（按序执行）

> **⚠️ 步骤 1 是不可跳过的前置**：`run.py` 不跑任何迁移（`run.py:28-35` 只有 uvicorn 启动）。若先起服务再跑迁移，SQLAlchemy 的 `select(User)` 会**显式列出 `role` 列**（不是 `SELECT *`）→ PG 抛 `UndefinedColumn` → 所有查 `users` 的路径全 500：`/api/token`（登录）、`/api/register`、`/api/users/me` 以及全部 16 个 admin 端点。**而客户端聊天 `/api/langgraph/query` 不查 users 表，照常能用**——故障现象是"聊天正常但登录全挂"，极难第一时间定位。**永远先迁移，再启动。**

| # | 步骤 | 判据 |
|---|---|---|
| 1 | `cd llm_backend && python scripts/init_db.py` | 日志 `Database initialization completed successfully!`；随后查表见 `orders` / `tickets` 存在，`users` 有 `role` 列，`documents` 有 `description` / `status` 列且**没有 `title` 列**。**再查两次数据**：① `SELECT role FROM users` 应为 4 行且都非 NULL（`ALTER ... DEFAULT 'user'` 给存量行填了值）；② `SELECT description, status FROM documents` 应为 2 行，`description` 为 NULL、`status` 为 `enabled` |
| 2 | `python scripts/seed_admin_account.py` | 打印管理员 email/role/id；`SELECT role FROM users WHERE email='admin_test@test.com'` → `admin` |
| 3 | `python scripts/seed_orders.py` | `SELECT COUNT(*) FROM orders` → 18；重复执行仍为 18（幂等） |
| 4 | `python scripts/seed_tickets.py` | `SELECT COUNT(*) FROM tickets` → 8；重复执行仍为 8（幂等） |
| 5 | `python ../scripts/build_product_placeholders.py` | `frontend/public/products/` 下 47 个 `.svg`；浏览器打开其一可见色块+文字 |
| 6 | `cd frontend && npm install && npm run build` | 构建成功；产物含 `dist/admin.html` + `dist/products/*.svg`。**ECharts 分包核对（可执行的判据）**：① `dist/index.html` 与 `dist/admin.html` 引用的 assets **文件名互不相同**（多页构建按入口分 chunk，实测形如 `main--EHfa8l.css` / `admin-BxLDXbEe.js` / `shared-*.js`，都带内容 hash——**不要用"同名 chunk 体积对比"那种做不到的方式**）；② `grep -l "echarts" dist/assets/*.js` **只命中 admin 的 chunk**，不命中 index 或 shared 的。注：`shared-*.js`（vue 所在）两边共用属正常 |
| 7 | 启动后端 `cd llm_backend && python run.py`，浏览器开 `http://127.0.0.1:8000/admin.html` | 未登录 → 显示管理端登录页 |
| 8 | 用**普通用户**登录管理端 | 显示「当前账号没有管理员权限」提示卡（不是能进控制台） |
| 9 | 用 **admin_test / admin** 登录 | 进入控制台；6 张卡片数字与 §2.1 实测一致：商品 47（副标"上架"数按现库实查，不写死）、订单 18、知识库 2、工单 8（待办 5）、用户 4、会话 13（消息 64）。**若密码不对登不上**：`docs/项目问题.md` #15e 记载该账号密码曾被改为 `admin`，种子脚本不重置已存在账号的密码；此时在管理端登录页用「切换账号」，或手动 `UPDATE users SET password_hash=<bcrypt(明文)> WHERE email='admin_test@test.com'` |
| 10 | 控制台图表 | 四张图正常渲染，无空白；折线图 X 轴是连续 7 天 |
| 11 | 「体验客服」按钮 | 新标签页打开客户端页面 |
| 12 | 商品管理 | 47 条商品卡片、图片显示为 SVG 占位图；搜索「门锁」→ 12 条；新增一条测试商品 → 出现在列表且**图片走 fallback 图标**；改价格 → 重新查询值已变；删除 → 消失 |
| 13 | 订单管理 | 18 行**表格**（8 列：订单号/商品/品类/买家/金额/状态/下单日期/操作）；**与工单页并排对比，表头样式、行高、徽章、操作按钮、分页条肉眼一致**；金额显示两位小数（`¥9957.50` 不是 `¥9957.5`）；新增一条（选商品后金额自动带出）→ 出现在列表；改状态 → 徽章颜色变；删除 → 消失 |
| 14 | 知识库管理 | 2 行，文件名显示 `.docx` 全名、片段数 4 / 38、创建时间 `2026-09-06`。点「新增文档」→ 弹窗**只有「上传文件」按钮**（保存置灰）→ 上传一个 md → **只读区出现文件名/类型/大小三项**，片段数与创建时间为 `—`、「文件描述」框启用 → 填描述 → 保存 → 弹窗切到**完成态**（片段数变具体数字、创建时间出现、绿条「已保存，智能客服现在可以检索到它」）→ 点「完成」→ 列表变 3 行且描述为所填。再点该行「编辑」→ **没有「上传文件」按钮**、只读区五项直接有值，改描述与状态 → 生效；删除 → 回到 2 行 |
| 14b | 知识库新增的**重复文件**路径 | 用第 14 步同一个 md 再点一次「新增文档」上传 → 出现黄色提示「该文件已存在于知识库（{N} 个片段，创建于 {日期}）」→ 保存只更新描述，列表**仍为 3 行**（不新增第 4 行） |
| 14c | **「取消」真的撤销**（两阶段设计的核心验收） | 上传一个**新的** md → 暂存成功后点「取消」→ 弹窗直接关闭（**不弹确认**）→ 列表**行数不变**；查库 `SELECT COUNT(*) FROM documents` 与暂存前一致；`llm_backend/uploads/_staging/` 下该 md5 文件**已消失**。**对比**：若此时直接关浏览器（不点取消），该暂存文件仍在磁盘上——这是 §6.4.5.1 记录的已知取舍 |
| 15 | **知识库上传后确实可检索** | 在客户端聊天里问一个只有新上传文档才有的问题，确认能召回（这是知识库管理最重要的验收点——证明**索引链路真的跑通了**。注意：管理端已不复用 `/api/upload`，走的是 `stage`→`commit`→`process_file`，所以这条验收要重跑一遍才算数） |
| 16 | 工单管理 | 8 行；点「处理」→ 弹窗字段与参考图一致（工单号/用户原话只读）；改状态为「已解决」+ 保存 → 列表徽章变绿、`resolved_at` 与 `handler` 有值（查库确认） |
| 17 | 客户端回归 | `http://127.0.0.1:8000/` 客户端页面一切照旧：登录、发消息、知识库面板、上传文档均正常 |
| 18 | 测试全绿 | `python -m pytest llm_backend/tests/ -q`，仅既有失败项（`test_bm25_retriever`） |
| 18b | **机会式清理生效** | 手工造残留：`touch -d '2 days ago' llm_backend/uploads/_staging/<某个暂存文件>`（或先暂存一个文件再把系统时间往前拨，或用 Python `os.utime` 把 mtime 改老）→ 再调一次 `stage` 上传任意文件 → 日志出现「清理过期暂存文件 1 个(TTL 24h)」且该文件消失。**再反向验一次**：新建一个暂存文件（mtime 是现在）→ 调 `stage` → 它**不该**被删 |
| 19 | **手工清一次暂存目录**（可选但建议） | `rm -rf llm_backend/uploads/_staging/*`——验证过程会留下若干测试用的暂存文件，清掉让环境干净。该目录无 DB 依赖，随时可删 |
| 19b | **关闭后端进程** | 验证结束后必须 kill 掉 dev server，否则占用 127.0.0.1:8000 会静默抢走用户 `run.py` 的请求 |

---

## 10. 实施步骤

| 步 | 内容 | 产出文件 | 验证 |
|---|---|---|---|
| 1 | 数据表：2 个模型 + `__init__` 导出 + `init_db.py` 增量 DDL | `models/order.py`、`models/ticket.py`、`models/__init__.py`、`models/user.py`(改)、`models/document.py`(改)、`scripts/init_db.py`(改) | 跑 `init_db.py`，查表结构（§9-1） |
| 2 | 鉴权：`require_admin` + schema 加 role + `auth.py` 登录返回 role | `core/security.py`(改)、`schemas/user.py`(改)、`api/auth.py`(改) | `curl /api/users/me` 带 token → 响应含 role |
| 3 | 管理端路由骨架 + 控制台 2 端点 | `api/admin/__init__.py`、`api/admin/console.py`、`api/__init__.py`(改) | `curl /api/admin/console/stats`（管理员 token）→ 200；无 token → 401 |
| 4 | 商品 5 端点 + `schemas/admin.py` | `api/admin/products.py`、`schemas/admin.py` | curl 冒烟（列表/新增/编辑/删除） |
| 5 | 订单 4 端点 | `api/admin/orders.py` | 同上 |
| 6 | 知识库 6 端点（列表 / stage / commit / unstage / PATCH / DELETE）+ 暂存清理 + 配置项 | `api/admin/knowledge.py`、`app/core/config.py`(改，加 `KNOWLEDGE_STAGE_TTL_HOURS`) | 同上 + 与既有 `/api/documents`、`/api/upload` 并存不冲突；**`stage` 只写磁盘不写库**（§8.5 有断言）；机会式清理生效（§9-18b） |
| 7 | 工单 2 端点 | `api/admin/tickets.py` | 同上 |
| 8 | 种子脚本 3 个 + 占位图脚本 1 个，跑通 | `llm_backend/scripts/seed_admin_account.py`、`llm_backend/scripts/seed_orders.py`、`llm_backend/scripts/seed_tickets.py`、**根** `scripts/build_product_placeholders.py` | §9-2~5 全部判据；**每个脚本连跑两次**核对幂等（订单为 upsert、其余无变化） |
| 9 | 后端测试 5 个文件 + conftest fixtures | `tests/test_admin_auth.py` 等 5 个、`tests/conftest.py`(改) | 在**项目根**执行 `python -m pytest -q`（`pyproject.toml:60` 的 `testpaths` 指向 `llm_backend/tests`，从 `llm_backend/` 里跑也能因上溯到根 pyproject 而生效）全绿（§9-18） |
| 10 | 前端骨架：入口 + 构建配置 + `api.js` + `admin.css` + 登录页 + 导航 | `admin.html`、`vite.config.js`(改)、`tailwind.config.cjs`(改)、`src/admin/`（main/App/css/api/AdminLogin） | `npm run build` 成功；浏览器见登录页，管理员登录进控制台 |
| 11 | 控制台页 + 3 个图表组件 | `ConsoleView.vue`、`charts/*.vue` | §9-9~11 |
| 12 | 商品管理页 + 通用组件（Modal/分页/缩略图/徽章） | `ProductView.vue`、`components/*.vue` | §9-12 |
| 13 | 订单管理页（表格，复用 §6.4.6 的表头/行样式） | `OrderView.vue` | §9-13 |
| 14 | 知识库管理页 + 暂存式表单组件 | `KnowledgeView.vue`、`components/KnowledgeFormModal.vue` | §9-14 / 14b / 14c / 15 |
| 15 | 工单管理页 | `TicketView.vue` | §9-16 |
| 16 | 客户端回归 + 全量测试 + 文档同步 + Git 提交 | — | §9-17~19 |

**文档同步（步 16 内含）**：
- 本文档标题下补「归档状态」行，`git mv` 到 `docs/spec_plan/已完成/`
- 修 `docs/PROJECT_ANALYSIS.md` / `docs/SHOP_SAGE_ANALYSIS.md` 中对本 spec 的导航引用（如有）
- `docs/项目问题.md` 补一条：管理端引入了 role 与 require_admin，但 `/api/upload`、`/api/documents` 仍不校验令牌（既有缺陷，本次未修）
- `README.md` 补三件事：① **管理端入口 `http://127.0.0.1:8000/admin.html`**（D15 选定的唯一发现路径）；② **启动前必须先跑 `python scripts/init_db.py`**（§12-20，顺序错了登录会全挂）；③ 种子脚本执行顺序，其中 `build_product_placeholders.py` 必须排在 `npm run build` **之前**（§6.5）
- `frontend/.gitignore` 追加 `public/products/`（§6.5，与 docx 同策略）

**Git 提交注意事项**：
- `frontend/package.json` + `package-lock.json` 因装 ECharts 会变更，**要一起提交**
- **不要 `git add .` 盲提交**——先 `git status` 核对 47 个 `frontend/public/products/*.svg` 确实被忽略（§12-27）。若 gitignore 规则没生效，会静默入库生成物

**分支与提交**：当前分支 `main`（`git branch --show-current` 复查后再提交——多窗口并发场景下分支可能被外部切换）。按项目惯例 `[feat] 管理端：<模块>` 分步提交，或按用户要求一次提交。

---

## 11. 评审确认事项（5 项已全部落定）

> 2026-09-22 用户评审确认。**下表是最终结论，实施时以此为准**；正文各处已按结论同步（"落定位置"列出对应章节）。

| # | 事项 | **确认结果** | 备选（未采用） | 落定位置 |
|---|---|---|---|---|
| 1 | 订单管理页版式 | ✅ **表格，与工单页同风格** | 参考图的卡片网格 | §3 D19、§6.4.4（已重写）、§9-13 |
| 2 | 商品「编辑」是否允许改名称 | ✅ **允许，但弹二次确认** | 直接禁掉名称字段 | §6.4.3（已有确认文案） |
| 3 | 控制台「活跃用户」口径 | ✅ **`COUNT(*) FROM users`**（全部注册用户） | 近 7 日有会话的用户数 | §5.4 `stats.users.total` |
| 4 | 订单种子选品范围 | ✅ **`ORDER BY sku LIMIT 18`**（只覆盖 4 个品类） | 跨品类抽样 | §7.2 |
| 5 | 订单弹窗的商品下拉 | ✅ **`GET /products?page_size=100` 一次拉取**，不加新接口 | 精简的 `/products/options` | §6.4.4 |
| 6 | 知识库新增的**上传语义** | ✅ **改两阶段**：`stage` 只存文件+基本信息（不索引），「取消」调 `unstage` 真撤销，点「保存」才 `commit` 走完整索引 | 初稿的"上传即入库、取消不撤销"（**已否决**） | §3 D7/D17/D18/D19、§5.7、§6.4.5 |

**第 6 项的连带影响（重要）**：这一项**推翻了初稿"复用 `/api/upload`"的做法**——那个接口的语义就是"上传即索引"，没有"只存不索引"的模式，要做到"取消能撤销"必须在索引前拦一道。因此管理端新增 3 个知识库端点（`stage` / `commit` / `unstage`），并删掉了初稿计划的"单条详情"端点（`commit` 的响应直接返回完整行即可，见 D19）。**`/api/upload` 本身不改**，客户端照常用。

**第 4 项的影响已减弱**：订单页从卡片网格改为表格后，"卡片看起来单调"这个原先的顾虑不再成立——表格里商品名是文本列，品类集中不造成视觉问题。所以**维持原抽样规则**（可复现、确定性好），不引入跨品类抽样的额外逻辑。

**第 5 项的边界（要知道）**：商品数超过 100 时下拉会静默截断（`page_size` 上限 `le=100`）。当前 47 条，余量约一半。真到那天再改成可搜索下拉，届时改一处前端即可，接口不用动。

---

## 12. 风险与避坑清单

| # | 风险 | 说明 | 应对 |
|---|---|---|---|
| 1 | **改商品名/删商品会打坏 RAG** | `product_name` 与 `sku` 是检索对齐键。改名后 docx 里的旧名对不上新名；删商品后静态知识仍在库里但价格库存查不到 | 接口接受改动（用户选了完整 CRUD）；前端加二次确认文案；**不做级联删除**（保住静态知识）；§12-9 记录为已知限制 |
| 2 | **新增商品没有静态知识** | `CLAUDE.md` 知识分层原则禁止把动态信息写 docx；且新增商品没有 docx 文档 | 已知限制，写进 README 与归档说明。管理端不提供"为新商品生成 docx"的功能 |
| 3 | **`hashing.py` 注释误导密码流程** | docstring 称"前端已做过 SHA256"，实际前端传明文。若种子脚本按注释先算 SHA256，管理员账号永远登不上 | §7.1 已明确写死"传明文"；实施时用 `get_password_hash("admin")` |
| 4 | **`/api/upload` 返回 `duplicate` 时的语义要说清** | md5 已存在说明该文档行已存在，此时 PATCH **会改到旧记录**。**这不一定是错的**——如果用户就是想给已有文档补描述，那正合适；但如果用户以为自己在上传一份新文档，就会静默覆盖别人写好的描述 | §6.4.5 已明确：`duplicate` **继续走回填与保存**，但表单顶部显示黄色提示「该文件已存在于知识库，继续保存只会更新它的文件描述」。**且列表不新增行**（§9-14b 有验收） |
| 5 | **`documents.status='disabled'` 不影响检索** | 停用只是展示态，检索侧仍会召回其 chunks（加谓词要改两条检索 SQL，超范围） | 前端「状态」列做 tooltip 注明；写入 §12-9 已知限制；将来做检索侧过滤时一并修 |
| 6 | **`/api/upload` 与 `/api/documents` 无鉴权** | 既有缺陷（`docs/项目问题.md` #14）。管理端页面照常带管理员令牌，但端点本身不校验 | **本次不修**（改它会连带屏蔽客户端上传）。`docs/项目问题.md` 补记一条 |
| 7 | **`Decimal` 序列化后尾零被吃掉，页面会显示 `¥9957.5`** | 实测 FastAPI 手搓 dict 返回 `Decimal("9957.50")` → JSON 数字 `9957.5`（不报错，但精度位丢了） | 前端所有金额/价格展示一律 `Number(v).toFixed(2)`（§6.4.8）。**不需要**后端转 `float()`——那是无效动作 |
| 8 | **日期时区坑（既有）** | `isoformat()` 不带时区，前端 `new Date()` 按本地时间解析 → 差 8 小时（`docs/项目问题.md` #15a） | 管理端所有日期展示走**字符串切片**（`slice(0,10)` / `slice(11,16)`），不经过 `new Date()` |
| 9 | **工单种子日期分布** | 若订单日期全部堆在一天，控制台折线图会是一条平线，看不出"趋势" | §7.2 已规范：18 条订单的日期按 `index % 14` 铺开到近 14 天，且近 7 天每天至少 1 条 |
| 10 | **ECharts 实例泄漏** | 不 `dispose()` 时切页面会累积 canvas 与 resize 监听器，久了页面卡顿 | §6.6 已明确 `onBeforeUnmount` 里 `dispose()` + `removeEventListener` |
| 11 | **`order_no` 生成撞唯一键** | 用 `COUNT(*)+1` 时，删掉中间一条再新增会撞已存在的号 | §5.6 已明确：解析现有 `order_no` 取 `max(序号)+1` |
| 12 | **管理端登录态与客户端共用 `token` 键** | 这是有意的（点"体验客服"不用重新登录），但意味着**管理员在管理端登出会连带把客户端登出** | 属预期行为；写进 README |
| 13 | **`create_all` 建新表依赖模型已导入** | 只在 `models/` 建文件而漏改 `models/__init__.py`，`create_all` 不会建 `orders`/`tickets`，且**不报错**（静默不建） | §10 步骤 1 的验证判据明确要求查表存在 |
| 14 | **测试污染演示数据** | `PATCH /knowledge/{md5}` 用例会改到真实种子的 2 份文档；用 `""` 还原会留空串而非 NULL（**前端能兜住，所以看不出问题**） | §8.5 已明确要求用 `json={"title": None}` 还原，并加了"能还原为 NULL"这条验收断言 |
| 15 | **`uvicorn` Windows 启动坑** | `python -m uvicorn main:app` 在 py3.13 下因 Proactor 启动失败，必须走 `run.py`（已内置 Selector 补丁） | §9-7 用 `python run.py`；验证完 §9-19 必须 kill 进程 |
| 16 | **`reload=True` 实测不生效** | `docs/项目问题.md` #15 附带发现③：改后端代码后 `run.py` 不自动重启 | 改后端后手动重启，别以为热重载生效了（否则会对着旧代码排查） |
| 17 | **`vite.config.js` 是 ESM，`__dirname` 不存在** | `package.json` 有 `"type": "module"`，配置被当 ESM 加载。写 `resolve(__dirname, 'index.html')` 会在启动构建时直接报 `__dirname is not defined in ES module scope` | §6.1 已给出 `fileURLToPath(new URL('./index.html', import.meta.url))` 的写法 |
| 18 | **图表日期基准不一致会错位一天** | 图表日期轴若用本地 `date.today()`、而 `conversations.created_at` 是 UTC（库时区 `Etc/UTC`），近 7 日趋势的会话数会整体偏移一格 | §5.4(a) 与 §7.2 已统一为 UTC 基准，两处必须一起改；改一处会静默错位（图还是能画出来，只是数对不上） |
| 19 | **`total` 用 `len(items)` 会退化成单页** | 前端分页条依赖 `total` 算页数；若返回当前页条数，则永远只有 1 页，翻页功能形同虚设，且**不会报错** | §5.3 已明确要求同一组 where 另跑 count |
| 20 | **🔴 模型改了但库没迁移 → 登录全挂、聊天正常** | SQLAlchemy 的 `select(User)` **显式列出全部列**（不是 `SELECT *`），库缺 `role` 列 → `UndefinedColumn` → `/api/token`、`/api/register`、`/api/users/me` + 16 个 admin 端点全 500。**而 `/api/langgraph/query` 不查 users 表，照常能用** → 现象是"聊天好好的，就是登不上"，极难定位。`run.py:28-35` 不跑任何迁移 | §9 顶部已加显式告警，步骤 1 永远是先跑 `init_db.py`（此条比 §12-13 严重点在于：13 是"少建表"，这条是"服务能起但登录坏"，后者更难发现） |
| 21 | **🔴 `require_admin` 缺 `User` 导入 → 整个服务起不来** | `security.py` 现有 44 行没有 `from app.models.user import User`，而函数注解在 Python 3.13 定义时求值 → `NameError` → 导入链全断 → uvicorn 起不来 + 所有测试 collection 全灭 | §5.1 已给出带导入的完整代码块，并点名 `tickets.py` 有同样的坑 |
| 22 | **工单种子跨天重跑会让数据翻倍** | `ticket_no` 若含运行日，每天重跑全部是新号 → 8 条变 16 条，"演示前重跑种子"这个最自然的操作就会坏掉 | §7.3 已把日期钉成常量 `BASE_DATE = "20260914"` |
| 23 | **订单种子的趋势图会随时间归零** | `order_date` 是"运行日往前推 `index%14` 天"，若幂等策略是"已存在则跳过"，日期永不刷新 → 一周后近 7 日折线全 0，而卡片仍写"订单 18" | §7.2 已改为 upsert 覆盖。**同类**：`conversations` 是真实数据，一周后会话折线也会归零——那是真实衰减，不造假 |
| 24 | **工单种子用本地时间 → 列表时间早 8 小时** | `datetime.now()` 是本地（UTC+8），写进 `timestamp without time zone` 后被当 UTC 读（与 `docs/项目问题.md` #15a 同源，只是源头在种子） | §7.3 已改 `datetime.now(timezone.utc)` |
| 25 | **`/api/upload` 的 400 响应体前端拿不到** | `src/api/upload.js:42-51` 只在 2xx 时解析响应体，其余状态码直接 `reject('上传失败: ' + status)` → `unsupported`/`too_large`/`empty_file` 的具体原因前端**看不见** | §6.4.5 已给出二选一（本 spec 取"接受限制 + 前端本地预检"），并修正了 §6.7 原写"无需改动"的含糊表述 |
| 26 | **表单空串 → 422** | 浏览器数字/日期输入框留空给的是 `''`，而实测 pydantic 对 `Optional[Decimal]/int/date` 收到 `''` 一律 ValidationError | §6.7 已给 `cleanBody()` 归一化函数 + 实测对照表 |
| 27 | **SVG 生成物会被静默提交入库** | 实测 `git check-ignore frontend/public/products/x.svg` 无命中，而 §10 步 16 走 `git add .` → 47 个生成物入库，违反项目"docx 不入库、只提交生成脚本"的既有约定 | §6.5 已要求 `frontend/.gitignore` 加 `public/products/`，并说明代价（新克隆仓库需先跑脚本，否则图片走兜底） |
| 28 | **根 `scripts/` 脚本连不上 DB** | 根 `scripts/` 下没有任何连 DB 的先例（实测 `grep AsyncSessionLocal\|psycopg` → 0 命中），照抄 `PROJECT_ROOT = Path(__file__).parent.parent` 导入不到 `app`（它指向项目根，而 `app` 在 `llm_backend/` 下） | §7.4 已给出完整的 `sys.path.insert(0, PROJECT_ROOT / "llm_backend")` 引导写法 |
| 29 | **conftest 顶层 `import main` 会连累全部测试** | `main.py:543` 的 `StaticFiles(frontend/dist)` 在目录不存在时构造即抛 `RuntimeError`；`dist` 被 gitignore，没构建过的环境里会让含 `test_cleaner`/`test_rrf` 在内的**整套测试**在 collection 阶段全灭 | §8.1 已改为在 `_login()` 函数内局部导入（与 `tests/test_documents_api.py:5` 的既有做法一致） |
| 30 | **暂存文件无人清理会留残留** | 「暂存了但既没提交也没取消」会在 `uploads/_staging/` 留文件（关弹窗、关浏览器、断网三种来源）。**且这是唯一一种前端没机会调 `unstage` 的路径**，光靠前端保证不了 | §6.4.5.1 已给方案：**机会式清理**——`stage` 开头扫一遍 `_staging/`，删 mtime 超 `KNOWLEDGE_STAGE_TTL_HOURS`(24h) 的文件。选它的依据是**残留只在 stage 时产生**，所以"有上传就有清理"在触发时机上完备，泄漏量有上界，不需要定时任务 |
| 30b | **机会式清理可能误删正在提交的文件** | `commit` 要读暂存文件（PDF 走 MinerU 可能几十秒），若此时另一次 `stage` 触发清理、且该文件 mtime 已超 24h，就会被删掉，`process_file` 读到一半文件没了 | §6.4.5.1 已用一行 `os.utime(staged_path)` 消掉：**`commit` 开头先 touch 刷新 mtime**，使提交中的文件不可能落入清理窗口 |
| 31 | **`commit` 失败时暂存文件要保留，不能一律删** | 若失败也删，用户重试就得重新上传（PDF 可能几 MB，MinerU 还可能瞬时失败）；若成功也留，则残留累积 | §5.7 已规定差异化清理：`success`/`duplicate` → 删；`failed` → **保留**（弹窗停在暂存态可重试，或点取消走 `unstage` 兜底）。§8.5 有正反两条断言 |
| 31b | **管理端端点的 4xx 语义与 `/api/upload` 不同** | 后者把处理类失败包进 `200 + status=failed`（客户端依赖该契约），管理端端点用标准 4xx。**若有人图省事把两者合并成一个通用上传器，就会打破客户端契约** | §6.7 已明确警示"两者契约不同，各留各的"；`admin/api.js` 里的 `stageFile` 是独立实现，不复用 `src/api/upload.js`（后者 URL 还硬编码成了 `/api/upload`） |
| 32 | **表单只读区不要用 `disabled` 的 input** | `disabled` 输入框视觉上仍像"能填但被禁用"，用户会反复点击试图编辑；且 `disabled` 字段虽不进提交体，但容易被后来者接上 `v-model` 而变成可写 | §6.4.5 已规定用**纯文本节点 + 浅灰底**渲染只读信息区；「创建时间」尤其强调是展示项、不进提交体 |

---

## 附：端点清单速查

| # | 方法 | 路径 | 鉴权 |
|---|---|---|---|
| 1 | GET | `/api/admin/console/stats` | require_admin |
| 2 | GET | `/api/admin/console/charts` | require_admin |
| 3 | GET | `/api/admin/products` | require_admin |
| 4 | POST | `/api/admin/products` | require_admin |
| 5 | PUT | `/api/admin/products/{sku}` | require_admin |
| 6 | DELETE | `/api/admin/products/{sku}` | require_admin |
| 7 | GET | `/api/admin/products/categories` | require_admin |
| 8 | GET | `/api/admin/orders` | require_admin |
| 9 | POST | `/api/admin/orders` | require_admin |
| 10 | PUT | `/api/admin/orders/{id}` | require_admin |
| 11 | DELETE | `/api/admin/orders/{id}` | require_admin |
| 12 | GET | `/api/admin/knowledge` | require_admin |
| 13 | POST | `/api/admin/knowledge/stage` | require_admin |
| 14 | POST | `/api/admin/knowledge/commit` | require_admin |
| 15 | DELETE | `/api/admin/knowledge/stage/{md5}` | require_admin |
| 16 | PATCH | `/api/admin/knowledge/{md5}` | require_admin |
| 17 | DELETE | `/api/admin/knowledge/{md5}` | require_admin |
| 18 | GET | `/api/admin/tickets` | require_admin |
| 19 | PUT | `/api/admin/tickets/{id}` | require_admin |
| — | — | — | — |
| — | POST | `/api/upload` | **原样保留；管理端已不再调用** |
| — | POST | `/api/token` | **复用，加返回 role** |
| — | GET | `/api/users/me` | **复用，加返回 role** |

**总计 19 个新端点**：控制台 2 + 商品 5 + 订单 4 + 知识库 6 + 工单 2。

**路由注册顺序提醒**（两处，实施时按此检查）：

| 情况 | 是否有冲突 | 说明 |
|---|---|---|
| `GET /products/categories` vs `GET /products/{sku}` | **当前无，将来会有** | 现在没有 `GET /products/{sku}`（只有 PUT/DELETE），所以不冲突。**将来若加详情端点，必须把 `categories` 注册在前面**——否则 `/products/categories` 会被 `{sku}="categories"` 抢先命中 |
| `POST /knowledge/stage` · `POST /knowledge/commit` vs 变量路径 | **无** | knowledge 组里唯一带变量的路由是 `PATCH /{md5}` 与 `DELETE /{md5}`，**没有 `POST /{md5}`**，所以 `POST /stage` / `POST /commit` 不与任何东西竞争 |
| `DELETE /knowledge/stage/{md5}` vs `DELETE /knowledge/{md5}` | **无** | 路径段数不同（`stage` 段多一层）。附带一个无害的边角：调 `DELETE /knowledge/stage`（少一段）会命中 `DELETE /{md5}` 且 `md5="stage"` → 查不到 → `404`。前端不会这么调 |
