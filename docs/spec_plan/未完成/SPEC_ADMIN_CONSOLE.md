# 管理端（Admin Console）实施规格

> **用途**: 为 SmartCS-Agent 增加一个独立的管理员端，含控制台、商品管理、订单管理、知识库管理、工单管理五个模块。管理员登录后进入，风格与客户端一致（同套 Tailwind + 品牌绿 `#16a34a`）。
> **依赖前置**: 无阻塞依赖。可复用的既有件：`users` 表 + JWT 登录链路（`app/api/auth.py`）、`product_price_stock` 表（47 行真实数据）、`POST /api/upload` 索引链路（`app/services/indexing_service.py`）、`documents`/`document_chunks` 表。
> **技术栈**: FastAPI + SQLAlchemy async + psycopg + PostgreSQL/pgvector（后端）；Vue3 + Vite + Tailwind + ECharts（前端）。
> **状态**: ⏳ 待实施（设计已评审，2026-09-22）
> **关联文档**: `CLAUDE.md` §6（spec 生命周期）§商品知识文档编写规范（知识分层原则）、[[SPEC_SKU_ALIGNMENT.md]]（商品 sku 对齐键，本 spec 风险 §11-1 的依据）、[[SPEC_FRONTEND_VUE3_REFACTOR.md]]（客户端前端结构，本 spec 的隔离对象）、`docs/项目问题.md` #14（知识库归属）/#15（浏览器实测）、`docs/PROJECT_ANALYSIS.md` §10.1（业务端点鉴权现状）

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
11. [待确认事项](#11-待确认事项)
12. [风险与避坑清单](#12-风险与避坑清单)

---

## 1. 范围与明确不做

### 1.1 本次做

| # | 模块 | 交付 |
|---|---|---|
| 1 | 数据层 | 新建 2 表（`orders` / `tickets`）+ 改 2 表（`users.role` / `documents` 三字段） |
| 2 | 鉴权 | `users.role` 列 + `require_admin` 依赖 + 管理端接口组级鉴权 |
| 3 | 后端接口 | 16 个新端点（`/api/admin/*`）+ 1 个复用端点（`POST /api/upload`）+ 2 个既有响应扩展（`Token`/`UserResponse` 加 `role`） |
| 4 | 前端 | 新入口 `admin.html` + `src/admin/` 目录，5 个页面 + 登录页 + 通用弹窗/分页/图表组件 |
| 5 | 数据 | 4 个幂等脚本：管理员账号、订单种子、工单种子、商品占位图生成 |
| 6 | 测试 | 6 个测试文件，重点覆盖越权（401/403）与 CRUD 契约 |

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
| **D2** | 登录入口 | **复用** `POST /api/token`，不分接口 | 一套登录逻辑。前端登录后读 `role` 决定跳客户端还是管理端（管理员也能用客户端，对应"体验客服"） |
| **D3** | 工单数据来源 | **只做种子数据**（用户指定） | 备选"从对话流真实生成"：需改 `lg_builder.py` 三个节点 + 流式事件协议，工程量翻倍且本轮不需要 |
| **D4** | 订单数据来源 | 新建 `orders` 表 + 种子脚本（用户指定） | 备选"把商品当订单展示"：订单号/买家/状态全是编的，语义对不上，否决 |
| **D5** | 商品写权限 | **完整 CRUD**（用户指定） | 备选"只改价格库存"：参考图的新增按钮就没了 |
| **D6** | 管理端知识库列表 | 新建 `/api/admin/knowledge`，**不做 user_id 过滤** | 备选"复用 `/api/documents`"：该接口强制 `user_id` 过滤（`main.py:191`），管理员看不到别人的文档，也看不到 `docs/项目问题.md` #14 修复后挂在 id=6 的那 2 份 |
| **D7** | 知识库上传 | **直接复用 `POST /api/upload`**（用户指定） | 该接口本身无鉴权（既有缺陷），管理端页面会带管理员令牌；上传归属用管理员自己的 `user_id` |
| **D8** | 前端组织 | 独立 `admin.html` 多页入口（用户指定） | 备选"装 vue-router 统一 SPA"：要重构 `App.vue`（419 行，状态全堆在一个文件），客户端有回归风险 |
| **D9** | 商品图片 | 脚本生成本地 SVG 占位图（用户指定） | 备选"外部占位图服务"：国内网络可能加载不出，演示不可靠 |
| **D10** | 图表 | 引入 ECharts（用户指定） | 只打管理端的包，客户端 bundle 不受影响（Vite 按入口分包） |
| **D11** | 导航范围 | 5 项 + 「体验客服」按钮 | 不做用户管理（§1.2） |
| **D12** | 后端代码组织 | 路由按模块拆 `app/api/admin/` 5 个文件，**不建 service 层** | 遵循 `main.py` 既有做法（`/api/documents` 等端点直接 `select`），CRUD 逻辑薄，建 service 层是纯样板 |
| **D13** | 工单状态取值 | `待处理` / `已解决`（两值） | 对齐参考图（列表徽章 + 环形图图例均只此两值），不擅自加"处理中" |
| **D14** | 订单状态取值 | `处理中` / `已发货` / `已送达`（三值） | 对齐参考图（卡片徽章 + 环形图图例） |

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

`server_default="user"` 必须写：`init_db.py` 的增量 ALTER 给存量 4 行填默认值，没有 server_default 则存量行为 NULL，`role != 'admin'` 判断仍能工作，但列语义不干净。

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

### 4.4 改 `documents`：加 `title` / `description` / `status`

文件：`llm_backend/app/models/document.py`，在 `chunk_count` 后追加：

```python
    title = Column(String(255), nullable=True)       # 展示标题(参考图"标题"列)
    description = Column(Text, nullable=True)        # 文件描述(参考图"文件描述"列)
    status = Column(String(20), nullable=False, default="enabled", server_default="enabled")  # enabled/disabled
```

**为什么这三列可空**：存量 2 行没有标题/描述；且 `POST /api/upload` 是复用接口，**上传时还不知道标题**——标题由随后的 `PATCH /api/admin/knowledge/{md5}` 补写（§5.7）。所以上传路径零改动，`title` 允许为空。

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
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS title VARCHAR(255)"
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
async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """管理端依赖:复用既有 JWT 校验,再查 role。非管理员 403。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user
```

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
    "orders":        [0, 0, 0, 0, 0, 0, 18],
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
2. `product_name` / `category` 从商品表**回填快照**
3. `amount` 未传则取 `current_price`，传了则用传入值
4. `order_date` 未传则取 **`datetime.now(timezone.utc).date()`**（UTC，与 §5.4(a) 的图表日期轴同基准；**不用** `date.today()`——那是本地日期，与 UTC 轴会在早 8 小时窗口内错位一天）
5. `order_no` 由后端生成：`ORD-{max(现有序号)+1:03d}`。序号从现有 `order_no` 里解析（`ORD-(\d+)` 取最大值），**不是 `COUNT(*)+1`**——删除过订单后 `COUNT+1` 会撞唯一键
6. 返回完整订单对象（同列表元素结构），状态码 `200`

#### `PUT /api/admin/orders/{id}` — 编辑

可改 `buyer_name` / `buyer_code` / `status` / `amount` / `order_date` / `user_id`。

**`sku` / `product_name` / `category` 不可改**：换商品等于换订单，语义上应删了重建。若前端需要换商品，走"删除 + 新增"。

`id` 不存在 → `404`

#### `DELETE /api/admin/orders/{id}` — 删除

`404` 若不存在；成功 `{"id": 18, "deleted": true}`。

### 5.7 知识库管理（3 个新端点 + 1 个复用）

文件：`llm_backend/app/api/admin/knowledge.py`

#### `GET /api/admin/knowledge` — 全平台文档列表

**不接收 `user_id` 参数**（§3 D6）。`keyword` 匹配 `original_filename` / `title` / `md5`。按 `created_at DESC`。

`items` 元素结构：

```json
{
  "id": 1,
  "md5": "a1b2c3d4e5f6...",
  "original_filename": "京东智能家具产品知识文档.docx",
  "title": "京东智能家具产品知识文档",
  "description": "京东智能家具 50 款商品的静态知识：品类、品牌、功能特点、规格参数、售后服务",
  "file_type": "docx",
  "file_size": 28416,
  "chunk_count": 38,
  "status": "enabled",
  "owner_id": "6",
  "created_at": "2026-09-06T08:54:50"
}
```

`title` 为 NULL 时前端回退显示 `original_filename`（存量 2 行就是这种情况，除非跑了种子脚本补标题）。

`owner_id` 一并返回（管理端要知道这份文档属于谁），前端在本轮不做展示，留作信息完整性。

#### `POST /api/upload` — **直接复用，不改一行**

调用方（管理端前端）传 `file` + `user_id`（管理员自己的 id，从 `/api/users/me` 拿）。

**为什么不加鉴权**：该端点属既有共享端点，客户端也在用。给它加 `require_admin` 会连带屏蔽客户端上传（客户端登录的是普通用户），属破坏性变更。已在 §12-6 记录该既有缺陷，本次不修。

返回值（既有契约，`indexing_service.process_file` 的真实返回）：

```json
{ "index_result": { "status": "success", "md5": "a1b2...", "chunks": 38,
                    "original_filename": "xxx.docx", "user_id": "6" } }
```

失败态：

| `status` | `error` | HTTP |
|---|---|---|
| `failed` | `unsupported` / `too_large` / `empty_file` | `400` |
| `failed` | `parse_error` / `embedding_failed` | `200`（契约：处理类错误 200+status） |
| `duplicate` | — | `200`（同 `(user_id, md5)` 已存在） |

管理端前端必须处理 `duplicate`：提示"该文件已存在"，并**跳过**后续 PATCH（md5 已存在，此时 PATCH 反而会改到旧记录的标题）。

#### `PATCH /api/admin/knowledge/{md5}` — 写标题/描述/状态

```python
class KnowledgeUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern=r"^(enabled|disabled)$")
```

按 `md5` 全表查找（**不加 user_id 过滤**，与列表口径一致）。`404` 若不存在。返回更新后的完整对象（同列表元素结构）。

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


class KnowledgeUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
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

**刻意不用 `response_model`**：本项目现有端点（`main.py`）全部手搓 dict 返回，未见 `response_model` 用法。为保持一致、且避免 `Decimal`/`datetime` 的序列化细节被 `response_model` 接管后行为分叉，管理端接口同样返回手搓 dict，序列化规则由 §5.3 明确约定。

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

**按入口分包**：ECharts 只被 `src/admin/**` 引用，Vite 会把它打进 admin 的 chunk，`dist/index.html` 那一侧不会加载 ECharts。构建后可用文件大小核对（§9 步骤 6）。

### 6.2 目录结构

```
frontend/
  admin.html                          ← 新建
  public/products/{sku}.svg           ← 新建(脚本生成,47 个)
  src/admin/
    main.js                           ← createApp(AdminApp).mount('#admin-app')
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
      charts/
        LineChart.vue
        DonutChart.vue
        BarChart.vue
```

**复用客户端既有件**（相对路径 `../api/auth.js`）：`getToken` / `setToken` / `clearToken` / `authHeaders` / `handleUnauthorized`。token 存 `localStorage` 的 `token` 键——与客户端**同一个键**，故管理端登录后客户端也是登录态（对应"体验客服"点过去不用再登）。这是有意为之。

**不复用** `global.css`：它含 `body { overflow: hidden }`（为聊天界面定制），会让管理端列表页无法滚动。`admin.css` 只写：

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

/* 管理端:列表页需要正常滚动,覆盖 global.css 的 overflow:hidden(两份 CSS 不同入口,互不影响) */
body { overflow: auto; }
```

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

onMounted(async () => {
  if (!getToken()) { authState.value = 'anonymous'; return; }
  try {
    const user = await getMe();               // 复用 ../api/auth.js
    if (user.role !== 'admin') { authState.value = 'denied'; return; }
    me.value = user;
    authState.value = 'ok';
  } catch {
    clearToken();
    authState.value = 'anonymous';
  }
});
```

| 状态 | 渲染 |
|---|---|
| `checking` | 居中的加载指示 |
| `anonymous` | `<AdminLogin @logged-in="recheck" />` |
| `denied` | 提示卡：「当前账号（`{email}`）没有管理员权限」+「返回客服端」链接（`href="/"`）+「切换账号」按钮（清 token 回 anonymous） |
| `ok` | 顶部导航 + 当前页视图 |

**登出**：`clearToken()` → `authState = 'anonymous'`（与客户端行为一致，不用 confirm）。

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

**分页条**：`共 {total} 条` + 每页条数下拉（12/24/48）+ 上一页/页码/下一页。与参考图一致放在右下角。

#### 6.4.4 订单管理 `OrderView.vue`（卡片网格，对齐参考图）

**工具条**：搜索框（placeholder「搜索订单号/商品名/买家」）+ 状态下拉（全部/处理中/已发货/已送达）+ 「查询」+ 「新增订单」。

**卡片网格**（同 4 列）：

```
┌──────────────────────────────────────┐
│ ┌────────┐  ORD-018          [已发货] │  ← 订单号 + 状态徽章
│ │  缩略图 │  8H 智能电动床 6电机…      │  ← 商品名 2 行截断
│ │ 96×96  │  ¥9957.50   2026-09-14     │  ← 金额(红色加粗) + 下单日期
│ └────────┘  买家·沈七 P001             │  ← 买家
│                          编辑  删除   │
└──────────────────────────────────────┘
```

**新增/编辑弹窗**：

| 字段 | 控件 | 新增 | 编辑 |
|---|---|---|---|
| 商品 | select（选项 = `GET /products?page_size=100`，label 商品名） | 必选 | **只读展示**（不可换商品，§5.6） |
| 买家姓名 | input | 必填 | 必填 |
| 买家编码 | input + placeholder「P001」 | 选填 | 选填 |
| 金额 | number input | 留空则取商品当前价 | 必填 |
| 状态 | select 处理中/已发货/已送达 | 默认处理中 | 必填 |
| 下单日期 | `<input type="date">` | 默认今天 | 必填 |

#### 6.4.5 知识库管理 `KnowledgeView.vue`（表格，含用户要求的字段改动）

**工具条**：搜索框（placeholder「搜索文档编号/文件名/标题」）+ 「查询」+ 「新增文档」绿按钮。

**表格**（列序对齐参考图 + 用户补的两列）：

| # | 列 | 宽度 | 内容 | 用户要求出处 |
|---|---|---|---|---|
| 1 | 文档编号 | `w-24` | `item.id` |  |
| 2 | 标题 | `w-[22%]` | `item.title \|\| item.original_filename` |  |
| 3 | **文件描述** | 自适应 | `item.description \|\| '—'` | 「把内容字段改为文件描述字段」 |
| 4 | **片段数** | `w-20` | `item.chunk_count` | 「添加片段数和创建时间字段」 |
| 5 | 状态 | `w-20` | 徽章：启用(绿) / 停用(灰) | 参考图「状态」列 |
| 6 | **创建时间** | `w-40` | `item.created_at.slice(0, 10)` + `' '` + `slice(11, 16)` | 「添加片段数和创建时间字段」 |
| 7 | 操作 | `w-28` | 编辑 / 删除（文字按钮） | 参考图「编辑 删除」 |

行样式：`border-b hover:bg-gray-50`，表头 `bg-gray-50 text-gray-500 text-xs`。

**新增文档弹窗**（用户要求「点击添加文档出现表单可以填写相关字段」）：

| 字段 | 控件 | 必填 | 说明 |
|---|---|---|---|
| 选择文件 | `<input type="file" accept=".pdf,.doc,.docx,.txt,.md">` | **是** | 白名单与后端 `ALLOWED_FILE_TYPES` 一致 |
| 标题 | input | 否 | 留空则用文件名（去掉扩展名） |
| 文件描述 | textarea rows=3 | 否 | 写入 `description` |

**保存流程（三步，必须按序）**：

1. `POST /api/upload`（**复用接口**，`FormData{file, user_id: me.id}`，XHR 带进度条）
2. 判断 `index_result.status`：
   - `failed` → 展示 `index_result.detail`，**终止**（不调 PATCH）
   - `duplicate` → 提示「该文件已存在（md5 重复），未新增」+ 展示已有记录的 md5，**终止**
   - `success` → 继续第 3 步
3. `PATCH /api/admin/knowledge/{index_result.md5}`，body `{title, description}`（都为空则跳过这一步）

第 3 步失败时：不静默吞掉，弹提示「文件已上传成功，但标题/描述保存失败，请在列表中编辑补填」。**理由**：此时 `documents` 行已落库、chunks 已入库、检索已可用，"上传"这个主目的已达成，不该报"新增失败"误导用户；标题可事后编辑。

**编辑弹窗**：标题 / 文件描述（textarea）/ 状态（select 启用、停用）→ `PATCH`。

**删除**：`confirm('确定删除文档「{title}」？将同时删除其 {chunk_count} 个知识片段，智能客服不再检索到它。')` → `DELETE /api/admin/knowledge/{md5}`。

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

底部右侧：`取消` + `保存`（绿）。保存 → `PUT /api/admin/tickets/{id}` → 关闭弹窗 + 刷新列表当前页。

#### 6.4.7 通用分页条 `Pagination.vue`

Props：`total` / `page` / `pageSize`；Emits：`update:page` / `update:pageSize`。

布局（右下角，对齐参考图）：`共 {total} 条` + 每页条数 `<select>`（12/24/48）+ `‹` 上一页 + 页码按钮（当前页高亮 `bg-primary text-white`）+ `›` 下一页。禁用态：首页时 `‹` 置灰、末页时 `›` 置灰。

### 6.5 商品缩略图 `ProductThumb.vue` 与 SVG 生成

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

- `onMounted` 里 `echarts.init(el)`；`onBeforeUnmount` 里 `chart.dispose()`（**必须 dispose**，否则切换页面会泄漏 canvas 与监听器）
- `watch(() => props.data, ...)` 里 `chart.setOption(option, true)`（`true` = 不合并，避免图例残留）
- `window.addEventListener('resize', chart.resize)`，`onBeforeUnmount` 一并 `removeEventListener`
- 容器高度固定 `320px`

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
export const updateKnowledge = (md5,b) => request(`/api/admin/knowledge/${md5}`, { method: 'PATCH', body: JSON.stringify(b) });
export const deleteKnowledge = (md5)=> request(`/api/admin/knowledge/${encodeURIComponent(md5)}`, { method: 'DELETE' });
export const listTickets = (p)      => request(`/api/admin/tickets?${qs(p)}`);
export const updateTicket = (id, b) => request(`/api/admin/tickets/${id}`, { method: 'PUT', body: JSON.stringify(b) });
```

`qs(obj)`：过滤掉 `undefined` / `null` / `''` 的键后用 `URLSearchParams` 拼串。

**知识库上传不走 `request()`**——它需要 XHR 上传进度，复用 `src/api/upload.js` 的 `uploadFileWithProgress({ file, userId, onProgress })`（已在客户端使用，直接 import 即可，无需改动）。

---

## 7. 种子数据规范

四个脚本，均为**幂等**（重复执行不产生重复行），全部放 `llm_backend/scripts/`。

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
| `user_id` | 轮转填现有真实用户 id `[3, 4, 5, 6]`（第 n 条填 `ids[n % 4]`） |
| 状态 | 均匀分布：`处理中` / `已发货` / `已送达` 各 6 条（保证控制台环形图三色都有） |
| 下单日期 | 从 **`datetime.now(timezone.utc).date()`** 往前推 `(index % 14)` 天——基准必须与 §5.4(a) 的图表日期轴完全一致（都用 UTC），否则折线错位一天。**必须落在近 14 天内**，否则控制台"近 7 日趋势"折线全是 0 |
| 订单号 | `ORD-{index+1:03d}` → `ORD-001` … `ORD-018` |
| 幂等键 | `order_no`：先 `SELECT order_no FROM orders`，已存在则跳过（`ON CONFLICT DO NOTHING` 语义） |

**日期分布要明确**：18 条订单里，让**最近 7 天每天至少 1 条**（`index % 14` 的前 7 个索引落在近 7 天），保证折线图有起伏不是一条平线。

### 7.3 `seed_tickets.py` — 工单种子（8 条）

| 字段 | 规范 |
|---|---|
| `ticket_no` | `TK-{YYYYMMDD}{序号:06d}`，日期取 `created_at` 的日期 |
| `created_at` | 从 `datetime.now()` 往前推 `(index * 6)` 小时，保证时间戳不同（参考图 4 条的创建时间是 05:12~08:16 的递增序列） |
| `status` | 5 条 `待处理` + 3 条 `已解决`（参考图 4 条是 3 待处理 1 已解决的比例） |
| `urgency` | 低 2 / 中 5 / 高 1（参考图 4 条全是「中」，但控制台图需要区分度） |
| `category` | 售后服务 / 退货咨询 / 投诉 / 其他 |
| 已解决的 3 条 | 填 `resolved_at`（= `created_at` + 2 小时）+ `handler = 'admin_test'` |
| 待处理的 5 条 | `resolved_at` / `handler` 均为 `NULL` |
| 幂等键 | `ticket_no` |

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

见 §6.5 的完整规范。补充：脚本开头按项目既有脚本惯例加 `sys.path.append(ROOT_DIR)` 与 `get_logger(service="build_placeholders")`，结束时打印生成数量与输出目录。

### 7.5 执行顺序

```
cd llm_backend
python scripts/init_db.py                 # 1. 建 orders/tickets 表 + 加 4 个增量列
python scripts/seed_admin_account.py      # 2. 管理员账号(必须先于其它,后续脚本无依赖但便于先登录验证)
python scripts/seed_orders.py             # 3. 18 条订单
python scripts/seed_tickets.py            # 4. 8 条工单
python ../scripts/build_product_placeholders.py   # 5. 47 个 SVG(从项目根目录)
```

注意：`init_db.py` 必须在最前——种子脚本 INSERT 的列（`documents.title` 等）与表（`orders`/`tickets`）都依赖它。

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

| 用例 | 断言 |
|---|---|
| 列表默认分页 | `total == 47`；`len(items) <= 12`；`page == 1` |
| `keyword` 匹配商品名 | 搜「门锁」→ `total > 0` 且所有 item 的 name/sku 含关键词 |
| `keyword` 匹配 sku | 搜 `JD-BED-001` → `total == 1` |
| `category` 筛选 | 筛「智能门锁」→ `total == 12`（实测值，§2.1） |
| 新增成功 | `POST` 带合法 sku → list 中能查到，`stock_quantity` 一致 |
| 新增 sku 格式非法 | `POST` sku=`ABC-1` → `422` |
| 新增 sku 重复 | 用 `JD-BED-001` → `400` |
| 新增 product_name 重复 | 用已存在名称 + 新 sku → `400` |
| 编辑改价格与库存 | `PUT` → 重新 GET 值已变 |
| 编辑不存在的 sku | `PUT /products/JD-XXX-999` → `404` |
| 删除成功 | `DELETE` → list 中消失 |
| 删除不存在 | `404` |
| 品类列表 | 返回 9 项（实测值，§2.1），含「智能门锁」 |
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
| 列表字段 | 含 `title` / `description` / `status` / `chunk_count` / `created_at` / `owner_id` |
| 两行种子的 `chunk_count` | 分别为 `4` 与 `38`（实测值，§2.1） |
| PATCH 标题与描述 | 成功后重查值已变；**测试后还原原值**（这两行是生产种子数据） |
| PATCH 不存在 md5 | `404` |
| PATCH status 非法值 | `422` |
| DELETE 不存在 md5 | `404` |
| **上传→PATCH→删除 全链路** | 用 `test_user_id` 风格的新 md5，上传一个临时 md 文件 → PATCH 标题 → 列表能查到 → DELETE → `document_chunks` 也清空 |

全链路用例的清理复用既有 `cleanup_test_data` fixture（它已按 `user_id` 删 documents + chunks）。

**重要**：`PATCH` 用例必须还原种子文档的 `title`/`description` 原值（它们初始为 `NULL`），否则跑完测试会污染演示数据。

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
| `trend.days[-1]` | `== 今天（MM-DD）` |
| `order_status` 含全部 3 个状态 | 即使某状态 0 条也返回该项 |
| `ticket_status` 含全部 2 个状态 | 同上 |
| `product_category` 按数量降序 | 首项是数量最多的品类 |
| `days=0` / `days=31` | `422` |

### 8.7 回归：既有测试全绿

`python -m pytest llm_backend/tests/ -q`，除 `test_bm25_retriever.py::test_bm25_recalls_docs_with_partial_terms`（**既有失败**，`docs/项目问题.md` #8，与本次改动无关）外全过。

**回归重点**：`test_documents_api.py` 必须仍全过——本次给 `documents` 加了 3 列、给 `users` 加了 1 列，若模型定义有误（如 `nullable=False` 缺 `server_default`），这个文件会先炸。

---

## 9. 验证方案（按序执行）

| # | 步骤 | 判据 |
|---|---|---|
| 1 | `cd llm_backend && python scripts/init_db.py` | 日志 `Database initialization completed successfully!`；随后查表见 `orders` / `tickets` 存在，`users` 有 `role` 列，`documents` 有 `title`/`description`/`status` 列 |
| 2 | `python scripts/seed_admin_account.py` | 打印管理员 email/role/id；`SELECT role FROM users WHERE email='admin_test@test.com'` → `admin` |
| 3 | `python scripts/seed_orders.py` | `SELECT COUNT(*) FROM orders` → 18；重复执行仍为 18（幂等） |
| 4 | `python scripts/seed_tickets.py` | `SELECT COUNT(*) FROM tickets` → 8；重复执行仍为 8（幂等） |
| 5 | `python ../scripts/build_product_placeholders.py` | `frontend/public/products/` 下 47 个 `.svg`；浏览器打开其一可见色块+文字 |
| 6 | `cd frontend && npm install && npm run build` | 构建成功；`dist/admin.html` 存在；**核对 ECharts 只在 admin 的 chunk 里**：`ls -la dist/assets/` 中体积最大的 js 属 admin 入口，`dist/assets/` 下与 index 入口同名的 chunk 体积无明显增长（对比构建前后 `dist/index.html` 引用的 js 体积） |
| 7 | 启动后端 `cd llm_backend && python run.py`，浏览器开 `http://127.0.0.1:8000/admin.html` | 未登录 → 显示管理端登录页 |
| 8 | 用**普通用户**登录管理端 | 显示「当前账号没有管理员权限」提示卡（不是能进控制台） |
| 9 | 用 **admin_test / admin** 登录 | 进入控制台；6 张卡片数字与 §2.1 实测一致：商品 47（副标"上架"数按现库实查，不写死）、订单 18、知识库 2、工单 8（待办 5）、用户 4、会话 13（消息 64）。**若密码不对登不上**：`docs/项目问题.md` #15e 记载该账号密码曾被改为 `admin`，种子脚本不重置已存在账号的密码；此时在管理端登录页用「切换账号」，或手动 `UPDATE users SET password_hash=<bcrypt(明文)> WHERE email='admin_test@test.com'` |
| 10 | 控制台图表 | 四张图正常渲染，无空白；折线图 X 轴是连续 7 天 |
| 11 | 「体验客服」按钮 | 新标签页打开客户端页面 |
| 12 | 商品管理 | 47 条商品卡片、图片显示为 SVG 占位图；搜索「门锁」→ 12 条；新增一条测试商品 → 出现在列表且**图片走 fallback 图标**；改价格 → 重新查询值已变；删除 → 消失 |
| 13 | 订单管理 | 18 条卡片；新增一条（选商品后金额自动带出）→ 出现；改状态徽章颜色变化；删除 → 消失 |
| 14 | 知识库管理 | 2 行，片段数 4 / 38，创建时间 `2026-09-06`；点「新增文档」上传一个 md → 列表变 3 行、标题与描述为表单所填；编辑改描述 → 生效；删除 → 回到 2 行 |
| 15 | **知识库上传后确实可检索** | 在客户端聊天里问一个只有新上传文档才有的问题，确认能召回（这是知识库管理最重要的验收点——证明"复用上传接口"真的打通了检索） |
| 16 | 工单管理 | 8 行；点「处理」→ 弹窗字段与参考图一致（工单号/用户原话只读）；改状态为「已解决」+ 保存 → 列表徽章变绿、`resolved_at` 与 `handler` 有值（查库确认） |
| 17 | 客户端回归 | `http://127.0.0.1:8000/` 客户端页面一切照旧：登录、发消息、知识库面板、上传文档均正常 |
| 18 | 测试全绿 | `python -m pytest llm_backend/tests/ -q`，仅既有失败项（`test_bm25_retriever`） |
| 19 | **关闭后端进程** | 验证结束后必须 kill 掉 dev server，否则占用 127.0.0.1:8000 会静默抢走用户 `run.py` 的请求 |

---

## 10. 实施步骤

| 步 | 内容 | 产出文件 | 验证 |
|---|---|---|---|
| 1 | 数据表：2 个模型 + `__init__` 导出 + `init_db.py` 增量 DDL | `models/order.py`、`models/ticket.py`、`models/__init__.py`、`models/user.py`(改)、`models/document.py`(改)、`scripts/init_db.py`(改) | 跑 `init_db.py`，查表结构（§9-1） |
| 2 | 鉴权：`require_admin` + schema 加 role + `auth.py` 登录返回 role | `core/security.py`(改)、`schemas/user.py`(改)、`api/auth.py`(改) | `curl /api/users/me` 带 token → 响应含 role |
| 3 | 管理端路由骨架 + 控制台 2 端点 | `api/admin/__init__.py`、`api/admin/console.py`、`api/__init__.py`(改) | `curl /api/admin/console/stats`（管理员 token）→ 200；无 token → 401 |
| 4 | 商品 5 端点 + `schemas/admin.py` | `api/admin/products.py`、`schemas/admin.py` | curl 冒烟（列表/新增/编辑/删除） |
| 5 | 订单 4 端点 | `api/admin/orders.py` | 同上 |
| 6 | 知识库 3 端点 | `api/admin/knowledge.py` | 同上 + 与既有 `/api/documents` 并存不冲突 |
| 7 | 工单 2 端点 | `api/admin/tickets.py` | 同上 |
| 8 | 种子脚本 3 个 + 占位图脚本 1 个，跑通 | `scripts/seed_admin_account.py`、`scripts/seed_orders.py`、`scripts/seed_tickets.py`、`scripts/build_product_placeholders.py` | §9-2~5 全部判据 |
| 9 | 后端测试 6 个文件 + conftest fixtures | `tests/test_admin_*.py`、`tests/conftest.py`(改) | `pytest -q` 全绿（§9-18） |
| 10 | 前端骨架：入口 + 构建配置 + `api.js` + `admin.css` + 登录页 + 导航 | `admin.html`、`vite.config.js`(改)、`tailwind.config.cjs`(改)、`src/admin/`（main/App/css/api/AdminLogin） | `npm run build` 成功；浏览器见登录页，管理员登录进控制台 |
| 11 | 控制台页 + 3 个图表组件 | `ConsoleView.vue`、`charts/*.vue` | §9-9~11 |
| 12 | 商品管理页 + 通用组件（Modal/分页/缩略图/徽章） | `ProductView.vue`、`components/*.vue` | §9-12 |
| 13 | 订单管理页 | `OrderView.vue` | §9-13 |
| 14 | 知识库管理页 | `KnowledgeView.vue` | §9-14~15 |
| 15 | 工单管理页 | `TicketView.vue` | §9-16 |
| 16 | 客户端回归 + 全量测试 + 文档同步 + Git 提交 | — | §9-17~19 |

**文档同步（步 16 内含）**：
- 本文档标题下补「归档状态」行，`git mv` 到 `docs/spec_plan/已完成/`
- 修 `docs/PROJECT_ANALYSIS.md` / `docs/SHOP_SAGE_ANALYSIS.md` 中对本 spec 的导航引用（如有）
- `docs/项目问题.md` 补一条：管理端引入了 role 与 require_admin，但 `/api/upload`、`/api/documents` 仍不校验令牌（既有缺陷，本次未修）
- `README.md` 补管理端入口与种子脚本的执行说明

**分支与提交**：当前分支 `main`（`git branch --show-current` 复查后再提交——多窗口并发场景下分支可能被外部切换）。按项目惯例 `[feat] 管理端：<模块>` 分步提交，或按用户要求一次提交。

---

## 11. 待确认事项

以下 3 项在设计评审中提出、尚未得到明确答复，本 spec 采用**加粗的默认选择**实现，实施前可改：

| # | 事项 | 默认选择 | 备选 |
|---|---|---|---|
| 1 | **订单管理页用卡片还是表格** | **卡片网格**（用户要求"订单管理参考工单界面"，但参考图里订单是卡片、工单是表格；本 spec 按参考图取卡片） | 改成表格（列：订单号/商品/买家/金额/状态/下单日期/操作），与工单页样式统一 |
| 2 | **商品「编辑」是否允许改名称** | **允许，但弹二次确认**（文案见 §6.4.3） | 直接禁掉名称字段（理由：`product_name` 是 RAG 检索对齐键，改名会让 docx 块里的旧名对不上） |
| 3 | **控制台「活跃用户」口径** | **`COUNT(*) FROM users`**（全部注册用户） | 近 7 日有会话的用户数（`COUNT(DISTINCT conversations.user_id)` 近 7 天），更贴合"活跃"字面义 |

---

## 12. 风险与避坑清单

| # | 风险 | 说明 | 应对 |
|---|---|---|---|
| 1 | **改商品名/删商品会打坏 RAG** | `product_name` 与 `sku` 是检索对齐键。改名后 docx 里的旧名对不上新名；删商品后静态知识仍在库里但价格库存查不到 | 接口接受改动（用户选了完整 CRUD）；前端加二次确认文案；**不做级联删除**（保住静态知识）；§12-9 记录为已知限制 |
| 2 | **新增商品没有静态知识** | `CLAUDE.md` 知识分层原则禁止把动态信息写 docx；且新增商品没有 docx 文档 | 已知限制，写进 README 与归档说明。管理端不提供"为新商品生成 docx"的功能 |
| 3 | **`hashing.py` 注释误导密码流程** | docstring 称"前端已做过 SHA256"，实际前端传明文。若种子脚本按注释先算 SHA256，管理员账号永远登不上 | §7.1 已明确写死"传明文"；实施时用 `get_password_hash("admin")` |
| 4 | **`/api/upload` 返回 `duplicate` 时不能 PATCH** | md5 已存在说明该文档行已存在，此时 PATCH 会改到**旧记录**的标题，覆盖人工维护的元数据 | §6.4.5 已明确：`duplicate` 时终止，不做 PATCH |
| 5 | **`documents.status='disabled'` 不影响检索** | 停用只是展示态，检索侧仍会召回其 chunks（加谓词要改两条检索 SQL，超范围） | 前端「状态」列做 tooltip 注明；写入 §12-9 已知限制；将来做检索侧过滤时一并修 |
| 6 | **`/api/upload` 与 `/api/documents` 无鉴权** | 既有缺陷（`docs/项目问题.md` #14）。管理端页面照常带管理员令牌，但端点本身不校验 | **本次不修**（改它会连带屏蔽客户端上传）。`docs/项目问题.md` 补记一条 |
| 7 | **`Numeric` 的 `Decimal` 无法直接 JSON 序列化** | fastapi 的 `jsonable_encoder` 能处理 `Decimal`→float，但**手搓 dict 返回时不经过 encoder 的完整路径**，容易报 `Object of type Decimal is not JSON serializable` | §5.3 已明确约定：所有价格/金额字段显式 `float(...)` |
| 8 | **日期时区坑（既有）** | `isoformat()` 不带时区，前端 `new Date()` 按本地时间解析 → 差 8 小时（`docs/项目问题.md` #15a） | 管理端所有日期展示走**字符串切片**（`slice(0,10)` / `slice(11,16)`），不经过 `new Date()` |
| 9 | **工单种子日期分布** | 若订单日期全部堆在一天，控制台折线图会是一条平线，看不出"趋势" | §7.2 已规范：18 条订单的日期按 `index % 14` 铺开到近 14 天，且近 7 天每天至少 1 条 |
| 10 | **ECharts 实例泄漏** | 不 `dispose()` 时切页面会累积 canvas 与 resize 监听器，久了页面卡顿 | §6.6 已明确 `onBeforeUnmount` 里 `dispose()` + `removeEventListener` |
| 11 | **`order_no` 生成撞唯一键** | 用 `COUNT(*)+1` 时，删掉中间一条再新增会撞已存在的号 | §5.6 已明确：解析现有 `order_no` 取 `max(序号)+1` |
| 12 | **管理端登录态与客户端共用 `token` 键** | 这是有意的（点"体验客服"不用重新登录），但意味着**管理员在管理端登出会连带把客户端登出** | 属预期行为；写进 README |
| 13 | **`create_all` 建新表依赖模型已导入** | 只在 `models/` 建文件而漏改 `models/__init__.py`，`create_all` 不会建 `orders`/`tickets`，且**不报错**（静默不建） | §10 步骤 1 的验证判据明确要求查表存在 |
| 14 | **测试污染演示数据** | `PATCH /knowledge/{md5}` 用例会改到真实种子的 2 份文档 | §8.5 已明确要求用例内还原原值 |
| 15 | **`uvicorn` Windows 启动坑** | `python -m uvicorn main:app` 在 py3.13 下因 Proactor 启动失败，必须走 `run.py`（已内置 Selector 补丁） | §9-7 用 `python run.py`；验证完 §9-19 必须 kill 进程 |
| 16 | **`reload=True` 实测不生效** | `docs/项目问题.md` #15 附带发现③：改后端代码后 `run.py` 不自动重启 | 改后端后手动重启，别以为热重载生效了（否则会对着旧代码排查） |
| 17 | **`vite.config.js` 是 ESM，`__dirname` 不存在** | `package.json` 有 `"type": "module"`，配置被当 ESM 加载。写 `resolve(__dirname, 'index.html')` 会在启动构建时直接报 `__dirname is not defined in ES module scope` | §6.1 已给出 `fileURLToPath(new URL('./index.html', import.meta.url))` 的写法 |
| 18 | **图表日期基准不一致会错位一天** | 图表日期轴若用本地 `date.today()`、而 `conversations.created_at` 是 UTC（库时区 `Etc/UTC`），近 7 日趋势的会话数会整体偏移一格 | §5.4(a) 与 §7.2 已统一为 UTC 基准，两处必须一起改；改一处会静默错位（图还是能画出来，只是数对不上） |
| 19 | **`total` 用 `len(items)` 会退化成单页** | 前端分页条依赖 `total` 算页数；若返回当前页条数，则永远只有 1 页，翻页功能形同虚设，且**不会报错** | §5.3 已明确要求同一组 where 另跑 count |

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
| 13 | PATCH | `/api/admin/knowledge/{md5}` | require_admin |
| 14 | DELETE | `/api/admin/knowledge/{md5}` | require_admin |
| 15 | GET | `/api/admin/tickets` | require_admin |
| 16 | PUT | `/api/admin/tickets/{id}` | require_admin |
| 17 | POST | `/api/upload` | **复用，无鉴权（既有）** |
| 18 | POST | `/api/token` | **复用，加返回 role** |
| 19 | GET | `/api/users/me` | **复用，加返回 role** |
