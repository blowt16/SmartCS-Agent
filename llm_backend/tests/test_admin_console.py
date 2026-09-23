"""管理端控制台接口测试(2 端点)。

断言方式:全部与"现查 DB"比对,不写死数字 —— 测试跑在真实库上,写死数字会随
种子/演示数据变化而碎(spec §2.1 的 47/2/13/64 是写 spec 时的实测值,仅供人工
核对,不作为断言常量)。
"""
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import sys

LLM_BACKEND = Path(__file__).resolve().parent.parent
if str(LLM_BACKEND) not in sys.path:
    sys.path.insert(0, str(LLM_BACKEND))

from sqlalchemy import func, select  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.product_price_stock import ProductPriceStock  # noqa: E402
from app.models.ticket import Ticket  # noqa: E402
from app.models.user import User  # noqa: E402

STATS_URL = "/api/admin/console/stats"
CHARTS_URL = "/api/admin/console/charts"


async def db_count(model, *conds):
    """现查 DB 的 COUNT,与接口返回的数字比对。"""
    async with AsyncSessionLocal() as s:
        stmt = select(func.count()).select_from(model)
        if conds:
            stmt = stmt.where(*conds)
        return (await s.execute(stmt)).scalar()


def _client():
    # main 必须在函数内 import:main.py 末尾的 StaticFiles(frontend/dist) 在目录不存在时
    # 构造即抛 RuntimeError,顶层 import 会让整套测试 collection 阶段全灭(spec §12-29)
    from main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ==================== stats ====================


async def test_stats_has_all_six_cards_with_subfields(admin_token):
    """6 个卡片键齐全,且带 where 的口径字段都在。"""
    async with _client() as c:
        r = await c.get(STATS_URL, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    stats = r.json()

    assert set(stats) == {"products", "orders", "knowledge", "tickets", "users", "conversations"}
    assert {"total", "in_stock"} <= set(stats["products"])
    assert {"total", "pending"} <= set(stats["tickets"])
    assert {"total", "messages"} <= set(stats["conversations"])
    assert {"total"} <= set(stats["orders"])
    assert {"total"} <= set(stats["knowledge"])
    assert {"total"} <= set(stats["users"])


async def test_stats_counts_match_live_db(admin_token):
    """逐条与现查 DB 比对,重点覆盖 in_stock 与 pending 这两个带 where 的口径
    —— 只比 total 覆盖不到条件过滤写错。"""
    async with _client() as c:
        r = await c.get(STATS_URL, headers={"Authorization": f"Bearer {admin_token}"})
    stats = r.json()

    assert stats["products"]["total"] == await db_count(ProductPriceStock)
    assert stats["products"]["in_stock"] == await db_count(
        ProductPriceStock, ProductPriceStock.stock_quantity > 0
    )
    assert stats["orders"]["total"] == await db_count(Order)
    assert stats["tickets"]["total"] == await db_count(Ticket)
    assert stats["tickets"]["pending"] == await db_count(Ticket, Ticket.status == "待处理")
    assert stats["users"]["total"] == await db_count(User)
    assert stats["conversations"]["total"] == await db_count(Conversation)
    assert stats["conversations"]["messages"] == await db_count(Message)


async def test_knowledge_total_is_not_filtered_by_user(admin_token):
    """knowledge.total 不按 user_id 过滤(D6 的核心行为)。"""
    async with _client() as c:
        r = await c.get(STATS_URL, headers={"Authorization": f"Bearer {admin_token}"})
    total = r.json()["knowledge"]["total"]

    assert total == await db_count(Document)
    # 全平台口径:至少要能看到种子那 2 份挂在 user_id='6' 的文档
    assert total >= 2
    async with AsyncSessionLocal() as s:
        owner_ids = set((await s.execute(select(Document.user_id).distinct())).scalars().all())
    assert len(owner_ids) >= 1  # 文档确实归属于某个/某些用户,而非无归属


# ==================== charts ====================


async def test_trend_axis_length_follows_days_param(admin_token):
    async with _client() as c:
        d7 = (await c.get(f"{CHARTS_URL}?days=7", headers={"Authorization": f"Bearer {admin_token}"})).json()
        d14 = (await c.get(f"{CHARTS_URL}?days=14", headers={"Authorization": f"Bearer {admin_token}"})).json()
    assert len(d7["trend"]["days"]) == 7
    assert len(d14["trend"]["days"]) == 14


async def test_trend_series_lengths_equal_days(admin_token):
    """补零逻辑:分组结果左连接到日期轴,缺失日补 0 —— 长度对不上就是折线会跳日。"""
    async with _client() as c:
        d = (await c.get(f"{CHARTS_URL}?days=7", headers={"Authorization": f"Bearer {admin_token}"})).json()
    n = len(d["trend"]["days"])
    assert len(d["trend"]["orders"]) == n
    assert len(d["trend"]["conversations"]) == n


async def test_trend_last_day_is_utc_today(admin_token):
    """必须写 UTC:后端按 sql 的 UTC 基准生成日期轴。测试侧若用本地 datetime.now()
    构造"今天",在 UTC+8 的本地 00:00~08:00 窗口内两者差一天 → 该用例每天随机变红。"""
    async with _client() as c:
        d = (await c.get(f"{CHARTS_URL}?days=7", headers={"Authorization": f"Bearer {admin_token}"})).json()
    assert d["trend"]["days"][-1] == datetime.now(timezone.utc).strftime("%m-%d")


async def test_order_status_returns_all_four_values(admin_token):
    """即使某状态 0 条也返回该项,否则图例会随数据消失。"""
    async with _client() as c:
        d = (await c.get(CHARTS_URL, headers={"Authorization": f"Bearer {admin_token}"})).json()
    assert [x["name"] for x in d["order_status"]] == ["处理中", "已发货", "已送达", "已签收"]


async def test_ticket_status_returns_all_two_values(admin_token):
    async with _client() as c:
        d = (await c.get(CHARTS_URL, headers={"Authorization": f"Bearer {admin_token}"})).json()
    assert [x["name"] for x in d["ticket_status"]] == ["待处理", "已解决"]


async def test_product_category_sorted_by_count_desc(admin_token):
    async with _client() as c:
        d = (await c.get(CHARTS_URL, headers={"Authorization": f"Bearer {admin_token}"})).json()
    values = [x["value"] for x in d["product_category"]]
    assert values == sorted(values, reverse=True), values

    # 首项须与现查 DB 里数量最多的品类一致
    async with AsyncSessionLocal() as s:
        top = (await s.execute(
            select(ProductPriceStock.category, func.count().label("n"))
            .group_by(ProductPriceStock.category)
            .order_by(func.count().desc())
        )).first()
    if top is not None:
        assert d["product_category"][0]["name"] == top[0]


@pytest.mark.parametrize("days", [0, 31])
async def test_charts_days_out_of_range_422(admin_token, days):
    async with _client() as c:
        r = await c.get(f"{CHARTS_URL}?days={days}", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 422


# ==================== 越权(控制台侧的三态) ====================


async def test_console_requires_token():
    async with _client() as c:
        assert (await c.get(STATS_URL)).status_code == 401
        assert (await c.get(CHARTS_URL)).status_code == 401
