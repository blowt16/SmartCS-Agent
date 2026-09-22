"""管理端商品接口测试(SPEC_ADMIN_CONSOLE §8.3)。

断言一律与"现查 DB"比对,不写死 47/12/9 —— 人工验收步骤 12 会往表里加测试商品,
写死数字会被本模块自己的功能推翻(与 §8.6 同原则)。
"""
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, or_, select

from app.core.database import AsyncSessionLocal
from app.models.product_price_stock import ProductPriceStock

# ⚠️ main 必须在【模块顶层】导入,与 test_documents_api.py / test_admin_auth.py 一致:
# 跑 pytest 时 CWD(仓库根)会被插到 sys.path[0],而仓库根有个同名 stub main.py,
# 于是"用例运行期"才首次 import main 会拿到那个 stub(没有 app) →
# conftest._login 里的 `from main import app` ImportError。
# collection 阶段 sys.path[0] 还是 llm_backend,顶层导入可把它固化进 sys.modules。
# 代价:frontend/dist 不存在时 StaticFiles 构造即抛 —— 与既有测试同等前提。
from main import app

API = "/api/admin/products"


# ==================== 基础设施 ====================


async def db_count(model, *conds):
    """spec §8.6:与现查 DB 比对,不写死种子数字。"""
    async with AsyncSessionLocal() as s:
        stmt = select(func.count()).select_from(model)
        if conds:
            stmt = stmt.where(*conds)
        return (await s.execute(stmt)).scalar()


async def db_scalar(stmt):
    async with AsyncSessionLocal() as s:
        return (await s.execute(stmt)).scalar()


async def db_all(stmt):
    async with AsyncSessionLocal() as s:
        return list((await s.execute(stmt)).scalars().all())


@asynccontextmanager
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def purge_tst_products():
    async with AsyncSessionLocal() as s:
        await s.execute(delete(ProductPriceStock).where(ProductPriceStock.sku.like("JD-TST-%")))
        await s.commit()


@pytest.fixture(autouse=True)
async def clean_tst_products():
    """用例前后各清一次 JD-TST-* 残留。

    前置也要清:上一次跑挂留下的脏数据会让 total / len(items) 类断言误判。
    """
    await purge_tst_products()
    yield
    await purge_tst_products()


async def _post_product(c, token, sku, name, price=199.99, stock=7, category="智能门锁"):
    return await c.post(API, headers=auth(token), json={
        "sku": sku,
        "product_name": name,
        "category": category,
        "current_price": price,
        "stock_quantity": stock,
    })


async def _find(c, token, keyword):
    """按 sku 精确召回(走 keyword 通道),返回 (total, items)。"""
    r = await c.get(API, headers=auth(token), params={"keyword": keyword})
    assert r.status_code == 200, r.text
    return r.json()["total"], r.json()["items"]


# ==================== 列表与筛选 ====================


async def test_list_default_pagination(admin_token):
    async with client() as c:
        r = await c.get(API, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total"] == await db_count(ProductPriceStock)
    assert body["page"] == 1
    assert body["page_size"] == 12
    assert len(body["items"]) <= 12
    # total 必须是过滤后的全量条数,不能是 len(items) —— 否则分页形同虚设
    assert body["total"] > len(body["items"])


async def test_list_keyword_matches_product_name(admin_token):
    async with client() as c:
        r = await c.get(API, headers=auth(admin_token), params={"keyword": "门锁"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total"] > 0
    for item in body["items"]:
        assert "门锁" in item["product_name"] or "门锁" in item["sku"]
    # 与同一组 where 的现查 DB 计数比对
    assert body["total"] == await db_count(ProductPriceStock, or_(
        ProductPriceStock.product_name.ilike("%门锁%"),
        ProductPriceStock.sku.ilike("%门锁%"),
    ))


async def test_list_keyword_matches_sku(admin_token):
    async with client() as c:
        total, items = await _find(c, admin_token, "JD-BED-001")
    # sku 唯一,可写死
    assert total == 1
    assert items[0]["sku"] == "JD-BED-001"


async def test_list_filter_by_category(admin_token):
    async with client() as c:
        r = await c.get(API, headers=auth(admin_token), params={"category": "智能门锁"})
    assert r.status_code == 200, r.text
    body = r.json()

    db_total = await db_count(ProductPriceStock, ProductPriceStock.category == "智能门锁")
    assert db_total > 0
    assert body["total"] == db_total
    for item in body["items"]:
        assert item["category"] == "智能门锁"


async def test_current_price_is_json_number(admin_token):
    """Numeric 列不转 float 会以字符串下发,前端 toFixed 之类直接炸。"""
    async with client() as c:
        r = await c.get(API, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items
    for item in items:
        assert isinstance(item["current_price"], float), item


async def test_categories(admin_token):
    async with client() as c:
        r = await c.get(f"{API}/categories", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    cats = r.json()

    assert len(cats) == await db_scalar(
        select(func.count(func.distinct(ProductPriceStock.category)))
    )
    assert "智能门锁" in cats
    # 去重且有序:与 DB 的 SELECT DISTINCT ... ORDER BY 逐项比对(顺序由 DB 排序规则决定)
    assert cats == await db_all(
        select(ProductPriceStock.category).distinct().order_by(ProductPriceStock.category)
    )


# ==================== 新增 ====================


async def test_create_product_ok(admin_token):
    async with client() as c:
        r = await _post_product(c, admin_token, "JD-TST-901", "测试商品 TST-901", price=199.99, stock=7)
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["sku"] == "JD-TST-901"
        assert created["stock_quantity"] == 7

        total, items = await _find(c, admin_token, "JD-TST-901")
    assert total == 1
    assert items[0]["stock_quantity"] == 7
    assert items[0]["current_price"] == 199.99


async def test_create_invalid_sku_422(admin_token):
    async with client() as c:
        r = await _post_product(c, admin_token, "ABC-1", "测试商品 非法编码")
    assert r.status_code == 422, r.text


async def test_create_duplicate_sku_400(admin_token):
    async with client() as c:
        r = await _post_product(c, admin_token, "JD-BED-001", "测试商品 编码重复")
    # 先查重返回可读 400;不先查会 IntegrityError → 500
    assert r.status_code == 400, r.text


async def test_create_duplicate_product_name_400(admin_token):
    existing_name = await db_scalar(
        select(ProductPriceStock.product_name).where(ProductPriceStock.sku == "JD-BED-001")
    )
    async with client() as c:
        r = await _post_product(c, admin_token, "JD-TST-902", existing_name)
    assert r.status_code == 400, r.text


# ==================== 编辑 ====================


async def test_update_product_price_and_stock(admin_token):
    async with client() as c:
        assert (await _post_product(c, admin_token, "JD-TST-903", "测试商品 TST-903")).status_code == 200

        r = await c.put(f"{API}/JD-TST-903", headers=auth(admin_token),
                        json={"current_price": 288.5, "stock_quantity": 3})
        assert r.status_code == 200, r.text
        assert r.json()["current_price"] == 288.5
        assert r.json()["stock_quantity"] == 3

        # 重新 GET 确认真的落库了,不是只在响应体里改
        _, items = await _find(c, admin_token, "JD-TST-903")
    assert items[0]["current_price"] == 288.5
    assert items[0]["stock_quantity"] == 3
    assert float(await db_scalar(
        select(ProductPriceStock.current_price).where(ProductPriceStock.sku == "JD-TST-903")
    )) == 288.5


async def test_update_missing_sku_404(admin_token):
    async with client() as c:
        r = await c.put(f"{API}/JD-XXX-999", headers=auth(admin_token), json={"stock_quantity": 1})
    assert r.status_code == 404, r.text


async def test_update_to_duplicate_product_name_400(admin_token):
    """改名撞既有商品名与新增撞名同理:应返回 400,不是 IntegrityError 的 500。"""
    existing_name = await db_scalar(
        select(ProductPriceStock.product_name).where(ProductPriceStock.sku == "JD-BED-001")
    )
    async with client() as c:
        assert (await _post_product(c, admin_token, "JD-TST-905", "测试商品 TST-905")).status_code == 200

        r = await c.put(f"{API}/JD-TST-905", headers=auth(admin_token),
                        json={"product_name": existing_name})
    assert r.status_code == 400, r.text


# ==================== 删除 ====================


async def test_delete_product(admin_token):
    async with client() as c:
        assert (await _post_product(c, admin_token, "JD-TST-904", "测试商品 TST-904")).status_code == 200

        r = await c.delete(f"{API}/JD-TST-904", headers=auth(admin_token))
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True

        total, _ = await _find(c, admin_token, "JD-TST-904")
    assert total == 0


async def test_delete_missing_sku_404(admin_token):
    async with client() as c:
        r = await c.delete(f"{API}/JD-XXX-998", headers=auth(admin_token))
    assert r.status_code == 404, r.text
