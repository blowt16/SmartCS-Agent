"""管理端订单接口测试(SPEC_ADMIN_CONSOLE §8.4,端点行为见 §5.6)。

覆盖 4 个端点(列表 / 新增 / 编辑 / 删除),重点钉两处实现细节:
- 新增不传 amount → 回填商品表 current_price + product_name/category 快照
- order_no 用 max(现有序号)+1 而非 COUNT(*)+1 —— 删掉中间一条(留下空档)后
  再新增,count+1 会撞现存唯一键,故必须断言新号不在保留集合里

清理策略:本条文件造的订单 buyer_name 一律 '测试买家',由 autouse 的
cleanup_test_orders 在**用例前后各清一次**(前一次兜住上次跑挂留下的残留)。
被删除的种子订单(为了造空档)在 finally 里按整行快照原样还原,保证演示数据不减。
"""
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# ⚠️ main 必须在【collection 阶段】导入,与 test_admin_auth.py / test_admin_products.py 一致:
# 从仓库根跑 pytest 时,根目录另有一个无关的 D:\SmartCS-Agent\main.py,用例执行阶段
# 它排在 sys.path[0],此时才首次 `from main import app` 会命中那个 stub(没有 app),
# 连 conftest 的 _login 也会栽在同一处(实测:
# ImportError: cannot import name 'app' from 'main' (D:\SmartCS-Agent\main.py))。
# 先把 llm_backend 提到最前并导入一次,正确的 main 进 sys.modules 后,
# 后续 conftest 与本文件的 import 全部命中缓存。
# 代价:main.py 末尾的 StaticFiles(frontend/dist) 在 dist 缺失时构造即抛 RuntimeError,
# 那样本文件 collection 报错——但同环境下 conftest 的令牌 fixture 本来也不可用。
_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from main import app  # noqa: E402

BUYER = "测试买家"
SKU_A = "JD-BED-001"          # 种子 current_price 9957.50
SKU_B = "JD-BED-002"          # 种子 current_price 5635.00
UNKNOWN_SKU = "JD-NO-SUCH-SKU"
UNKNOWN_USER_ID = 999999      # users 表无此 id(种子为 3/4/5/6)

# 列表元素字段集(§5.6)。少一个前端就渲染不出来,故逐字段钉住。
ITEM_FIELDS = {
    "id", "order_no", "sku", "product_name", "category",
    "buyer_name", "buyer_code", "amount", "status", "order_date",
    "signed_date", "image",
}

# 单页上限(端点 page_size 约束 le=100),取全量时用
MAX_PAGE_SIZE = 100


@asynccontextmanager
async def _client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ==================== 清理与数据访问 ====================


async def _purge_test_orders() -> None:
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM orders WHERE buyer_name = :n"), {"n": BUYER})
        await s.commit()


@pytest.fixture(autouse=True)
async def cleanup_test_orders():
    """用例前后都清一次:前置清理防上次跑挂留下的 '测试买家' 脏数据。"""
    await _purge_test_orders()
    yield
    await _purge_test_orders()


async def _product_row(sku: str):
    """读商品表原值,避免在断言里写死中文商品名/价格。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.product_price_stock import ProductPriceStock

    async with AsyncSessionLocal() as s:
        p = (await s.execute(
            select(ProductPriceStock).where(ProductPriceStock.sku == sku)
        )).scalar_one()
        return {"name": p.product_name, "category": p.category, "price": float(p.current_price)}


async def _snapshot_order(order_id: int) -> dict:
    """取整行(含列表 API 不返回的 user_id),用于删除后原样还原。"""
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        o = (await s.execute(select(Order).where(Order.id == order_id))).scalar_one()
        return {c.name: getattr(o, c.name) for c in Order.__table__.columns}


async def _restore_order(snap: dict) -> None:
    """按原 id 插回。主键 id 由 PostgreSQL 序列分配,此处显式给值不影响序列状态。"""
    from app.core.database import AsyncSessionLocal
    from app.models.order import Order

    async with AsyncSessionLocal() as s:
        s.add(Order(**snap))
        await s.commit()


# ==================== 请求小工具 ====================


async def _create(c, token, sku=SKU_A, **extra):
    return await c.post(
        "/api/admin/orders",
        json={"product_sku": sku, "buyer_name": BUYER, **extra},
        headers=_bearer(token),
    )


async def _list_items(c, token, page_size=MAX_PAGE_SIZE, **params):
    r = await c.get(
        "/api/admin/orders",
        params={"page_size": page_size, **params},
        headers=_bearer(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] <= body["page_size"], "订单数超过单页上限,全量断言会不完整"
    return body


async def _get_by_order_no(c, token, order_no):
    """按 order_no 重查(列表 keyword 匹配 order_no),用于验证落库而非仅响应体。"""
    body = await _list_items(c, token, keyword=order_no)
    for item in body["items"]:
        if item["order_no"] == order_no:
            return item
    raise AssertionError(f"列表中查不到订单 {order_no}")


# ==================== 列表 ====================


async def test_list_orders_total_and_fields(admin_token):
    """§8.4:total >= 18(种子)或 >= 0(空库);字段齐全。"""
    async with _client() as c:
        body = await _list_items(c, admin_token)

    assert body["total"] >= 18, f"种子订单应 >= 18 条,实际 {body['total']}"
    assert body["page"] == 1
    assert body["items"], "空前列表也要返回 items 数组"

    for item in body["items"]:
        assert set(item) == ITEM_FIELDS, f"字段集不符: 多 {set(item) - ITEM_FIELDS} 少 {ITEM_FIELDS - set(item)}"
        assert isinstance(item["id"], int)
        assert isinstance(item["amount"], float)          # Numeric 需转 float,否则前端拿到字符串
        assert len(item["order_date"]) == 10, item["order_date"]   # YYYY-MM-DD,无时区后缀
        assert item["image"] == f"/products/{item['sku']}.svg"


# ==================== 新增 ====================


async def test_create_without_amount_backfills_product_price(admin_token):
    """不传 amount → 取商品表 current_price;product_name/category 回填快照。"""
    async with _client() as c:
        product = await _product_row(SKU_A)
        r = await _create(c, admin_token, SKU_A)
        assert r.status_code == 200, r.text
        body = r.json()

    assert body["amount"] == pytest.approx(product["price"]), "未传 amount 应回填商品 current_price"
    assert body["product_name"] == product["name"], "product_name 应回填商品表快照"
    assert body["category"] == product["category"]
    assert body["sku"] == SKU_A
    assert body["buyer_name"] == BUYER
    assert body["status"] == "处理中"                     # schema 默认值
    assert body["order_date"], "未传 order_date 应取 UTC 当天"
    assert body["order_no"].startswith("ORD-")
    assert set(body) == ITEM_FIELDS                       # 新增响应与列表元素同结构


async def test_create_with_amount_uses_given_value(admin_token):
    """传了 amount → 用传入值,不覆盖成商品价。"""
    async with _client() as c:
        product = await _product_row(SKU_A)
        r = await _create(c, admin_token, SKU_A, amount=1234.56, status="已发货")
        assert r.status_code == 200, r.text
        body = r.json()

    assert body["amount"] == pytest.approx(1234.56)
    assert body["amount"] != pytest.approx(product["price"]), "传入的 amount 不应被商品价覆盖"
    assert body["status"] == "已发货"


async def test_create_unknown_sku_400(admin_token):
    async with _client() as c:
        r = await _create(c, admin_token, UNKNOWN_SKU)
    assert r.status_code == 400, r.text
    assert UNKNOWN_SKU in r.json()["detail"]


async def test_create_unknown_user_id_400(admin_token):
    """外键校验:不存在的 user_id → 400(不是 IntegrityError 崩成 500)。"""
    async with _client() as c:
        r = await _create(c, admin_token, SKU_A, user_id=UNKNOWN_USER_ID)
    assert r.status_code == 400, r.text
    assert str(UNKNOWN_USER_ID) in r.json()["detail"]


async def test_create_twice_order_no_distinct(admin_token):
    """连续新增两条 → order_no 互不相同。"""
    async with _client() as c:
        first = await _create(c, admin_token, SKU_A)
        second = await _create(c, admin_token, SKU_B)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

    nos = [first.json()["order_no"], second.json()["order_no"]]
    assert nos[0] != nos[1], f"连续新增撞号: {nos}"


async def test_create_after_deleting_middle_order_no_collision(admin_token):
    """**核心用例**:验证 §5.6 的 max+1 而非 COUNT(*)+1。

    删掉中间一条(留下空档)后 count 少 1 而 max 不变:
    - max+1  → 生成 max+1,不与现存重复 ✓
    - count+1 → 生成一个已存在的号 → 撞唯一键 → 409/500 ✗
    故断言必须落在"新号不在保留集合里",不能只断言"两次新增不同"。
    """
    async with _client() as c:
        before = (await _list_items(c, admin_token))["items"]
        assert len(before) >= 2, "至少两条订单才能造出中间空档"

        # 按 order_no 排序取正中一条 —— 必须是中间那条。删 max 那条的话
        # count+1 与 max+1 结果相同,区分不出两种实现。
        ordered = sorted(before, key=lambda i: i["order_no"])
        victim = ordered[len(ordered) // 2]
        assert victim["order_no"] != ordered[-1]["order_no"], "选中的是最大号,无法区分 max/count"

        kept = {i["order_no"] for i in before if i["order_no"] != victim["order_no"]}
        snap = await _snapshot_order(victim["id"])
        try:
            d = await c.delete(f"/api/admin/orders/{victim['id']}", headers=_bearer(admin_token))
            assert d.status_code == 200, d.text

            r = await _create(c, admin_token, SKU_A)
            assert r.status_code == 200, f"删除中间订单后新增失败({r.status_code}): {r.text}"
            new_no = r.json()["order_no"]

            assert new_no not in kept, f"新订单号 {new_no} 与现存订单重复,疑似 count+1 实现"
            # 再钉死生成规则:现存最大号 + 1
            assert new_no == f"ORD-{int(max(kept)[len('ORD-'):]) + 1:03d}"
        finally:
            # 还原被删的种子订单,别让测试把演示数据越跑越少
            await _restore_order(snap)

        after = (await _list_items(c, admin_token))["total"]
        assert after == len(before) + 1, "被删的种子订单未还原到位(应为 原条数 + 本次新增的 1 条)"


# ==================== 编辑 ====================


async def test_update_status_persists(admin_token):
    """PUT 改状态 → 重查(GET)值已变。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A, status="处理中")
        assert created.status_code == 200, created.text
        order = created.json()

        r = await c.put(
            f"/api/admin/orders/{order['id']}",
            json={"status": "已发货"},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "已发货"

        fetched = await _get_by_order_no(c, admin_token, order["order_no"])
        assert fetched["status"] == "已发货", "PUT 响应变了但库里没落"


async def test_update_ignores_sku_field(admin_token):
    """body 里塞 sku → 被忽略(OrderUpdate schema 无该字段,pydantic 默认忽略)。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A)
        assert created.status_code == 200, created.text
        order = created.json()

        # 同一请求里带一个合法字段,证明请求确实被处理了(只是 sku 那部分没生效)
        r = await c.put(
            f"/api/admin/orders/{order['id']}",
            json={"sku": SKU_B, "buyer_code": "P999"},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["sku"] == SKU_A, "sku 不应被修改(换商品应删除后重建)"
        assert body["buyer_code"] == "P999", "合法字段应已生效"

        fetched = await _get_by_order_no(c, admin_token, order["order_no"])
        assert fetched["sku"] == SKU_A


async def test_update_unknown_user_id_400(admin_token):
    """编辑时的外键校验:不存在的 user_id → 400(不是 500)。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A)
        assert created.status_code == 200, created.text
        r = await c.put(
            f"/api/admin/orders/{created.json()['id']}",
            json={"user_id": UNKNOWN_USER_ID},
            headers=_bearer(admin_token),
        )
    assert r.status_code == 400, r.text
    assert str(UNKNOWN_USER_ID) in r.json()["detail"]


async def test_update_unknown_id_404(admin_token):
    async with _client() as c:
        r = await c.put(
            "/api/admin/orders/999999",
            json={"status": "已发货"},
            headers=_bearer(admin_token),
        )
    assert r.status_code == 404, r.text


# ==================== 签收日期 ====================


def _today_utc() -> str:
    """与接口同一基准(UTC)——不用本地日期,否则在 UTC+8 的 00:00~08:00 窗口内会差一天。"""
    return datetime.now(timezone.utc).date().isoformat()


async def _put(c, token, order_id, **body):
    return await c.put(f"/api/admin/orders/{order_id}", json=body, headers=_bearer(token))


async def test_create_signed_order_defaults_signed_date_to_today(admin_token):
    """新增即「已签收」且不传 signed_date → 后端补 UTC 当天。"""
    async with _client() as c:
        r = await _create(c, admin_token, SKU_A, status="已签收")
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["status"] == "已签收"
    assert body["signed_date"] == _today_utc()


async def test_create_non_signed_order_ignores_signed_date(admin_token):
    """非「已签收」时即使显式传了日期也必须落 NULL——不许造出「未签收却带签收日期」的数据。"""
    async with _client() as c:
        r = await _create(c, admin_token, SKU_A, status="已发货", signed_date="2026-01-01")
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["status"] == "已发货"
    assert body["signed_date"] is None


async def test_update_to_signed_sets_signed_date(admin_token):
    """状态改成「已签收」但没传日期 → 补当天。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A, status="已发货")
        assert created.json()["signed_date"] is None
        r = await _put(c, admin_token, created.json()["id"], status="已签收")
    assert r.status_code == 200, r.text
    assert r.json()["signed_date"] == _today_utc()


async def test_update_away_from_signed_clears_signed_date(admin_token):
    """从「已签收」改回其它状态 → 签收日期清空(状态与日期强绑定的核心断言)。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A, status="已签收", signed_date="2026-03-05")
        assert created.json()["signed_date"] == "2026-03-05"
        r = await _put(c, admin_token, created.json()["id"], status="已发货")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "已发货"
    assert r.json()["signed_date"] is None


async def test_update_keeps_explicit_signed_date(admin_token):
    """已签收时显式传的日期要保留(用于补录历史订单),不被「补当天」覆盖。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A, status="已签收")
        r = await _put(c, admin_token, created.json()["id"], signed_date="2026-03-05")
    assert r.status_code == 200, r.text
    assert r.json()["signed_date"] == "2026-03-05"


async def test_update_other_field_keeps_existing_signed_date(admin_token):
    """已签收订单只改无关字段(请求里没有 status / signed_date)→ 原签收日期不被重置成今天。

    这条盯的是 exclude_unset 与"补当天"的交互:实现里取 data.get('signed_date', order.signed_date),
    若误写成 data.get('signed_date') or today,改个无关字段就会把签收日期冲成今天。

    ⚠️ 改的是 buyer_code 而非 buyer_name:本文件约定"造的订单 buyer_name 一律 BUYER",
    清理 fixture 按 buyer_name 精确匹配删除;改买家名会让订单逃出清理、污染演示数据(实测踩过)。
    """
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A, status="已签收", signed_date="2026-03-05")
        r = await _put(c, admin_token, created.json()["id"], buyer_code="P999")
    assert r.status_code == 200, r.text
    assert r.json()["signed_date"] == "2026-03-05", "改无关字段不应重置签收日期"


# ==================== 删除 ====================


async def test_delete_then_delete_again_404(admin_token):
    """删除成功 → 再删同一条 404。"""
    async with _client() as c:
        created = await _create(c, admin_token, SKU_A)
        assert created.status_code == 200, created.text
        order = created.json()

        r = await c.delete(f"/api/admin/orders/{order['id']}", headers=_bearer(admin_token))
        assert r.status_code == 200, r.text
        assert r.json() == {"id": order["id"], "deleted": True}

        again = await c.delete(f"/api/admin/orders/{order['id']}", headers=_bearer(admin_token))
        assert again.status_code == 404, again.text

        # 确认真的没了,而不是只返回了个成功
        body = await _list_items(c, admin_token, keyword=order["order_no"])
        assert all(i["order_no"] != order["order_no"] for i in body["items"])
