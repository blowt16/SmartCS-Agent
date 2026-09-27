"""管理端物流接口测试(SPEC: docs/superpowers/specs/2026-09-27-管理端物流模块-design.md)。

覆盖 §4 的 4 个端点与 §4.5 的三条校验规则。

前置:必须先跑 `python scripts/init_db.py` 建出 shipments 表,否则数据类用例
      全部 ERROR(relation "shipments" does not exist)。

清理策略:本文件造的订单 buyer_name 一律 "测试买家",由 autouse 的 fixture
         在用例前后各清一次(前一次兜住上次跑挂留下的残留,不碰种子数据)。
"""
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# main 必须在【collection 阶段】导入(与 test_admin_orders.py 同因):
# 从仓库根跑 pytest 时,根目录另有一个无关的 D:\SmartCS-Agent\main.py,
# 此时才首次 `from main import app` 会命中那个 stub(没有 app)。
_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from main import app  # noqa: E402
from app.api.admin.logistics import TraceFormatError, parse_trace  # noqa: E402

BUYER = "测试买家"
SKU = "JD-BED-001"          # 种子商品,current_price 9957.50
MAX_PAGE_SIZE = 100

# 列表元素字段集(§4.2)。少一个前端就渲染不出来,故逐字段钉住。
ITEM_FIELDS = {
    "id", "order_no", "order_status", "product_name", "category", "buyer_name",
    "tracking_no", "carrier", "status", "shipped_at", "signed_at", "trace",
}


@asynccontextmanager
async def _client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
async def cleanup_test_logistics():
    """用例前后各清一次本文件造的数据。只删 buyer_name='测试买家' 的订单及其运单。"""
    from sqlalchemy import delete, select

    from app.core.database import AsyncSessionLocal
    from app.models.order import Order
    from app.models.shipment import Shipment

    async def _purge():
        async with AsyncSessionLocal() as s:
            nos = (await s.execute(
                select(Order.order_no).where(Order.buyer_name == BUYER)
            )).scalars().all()
            if nos:
                await s.execute(delete(Shipment).where(Shipment.order_no.in_(nos)))
                await s.execute(delete(Order).where(Order.order_no.in_(nos)))
                await s.commit()

    await _purge()
    yield
    await _purge()


# ==================== 轨迹校验器(§4.5 规则 C) ====================

def test_parse_trace_normalizes_format():
    """合法输入被规范化:分隔符两侧统一为 ' | ',行间统一 \\n。"""
    raw = "2026-09-20 14:32|广州市|已揽收\n2026-09-21 08:05 | 广州转运中心 | 到达转运中心"
    assert parse_trace(raw, None, None) == (
        "2026-09-20 14:32 | 广州市 | 已揽收\n"
        "2026-09-21 08:05 | 广州转运中心 | 到达转运中心"
    )


def test_parse_trace_blank_lines_and_crlf_are_absorbed():
    """空行忽略、CRLF 统一成 LF —— 管理员多敲回车不该被拦。"""
    raw = "2026-09-20 14:32 | 广州市 | 已揽收\r\n\r\n2026-09-21 08:05 | 深圳 | 派送中"
    assert parse_trace(raw, None, None) == (
        "2026-09-20 14:32 | 广州市 | 已揽收\n2026-09-21 08:05 | 深圳 | 派送中"
    )


@pytest.mark.parametrize("raw", ["", "   ", "\n\n  \n"])
def test_parse_trace_empty_becomes_none(raw):
    """空文本 / 纯空白 -> None(不是空串),避免库里两种"空"并存。"""
    assert parse_trace(raw, None, None) is None


def test_parse_trace_accepts_empty_string_and_none():
    assert parse_trace(None, None, None) is None


@pytest.mark.parametrize("raw,needle", [
    ("2026-09-20 14:32 | 广州市", "三段"),               # 2 段
    ("2026-09-20 14:32|广州|已揽收|多出来的", "三段"),      # 4 段
    ("2026/09/20 14:32 | 广州 | 已揽收", "YYYY-MM-DD"),   # 时间格式错
    ("2026-13-45 14:32 | 广州 | 已揽收", "YYYY-MM-DD"),   # 非法日期(strptime 才拦得住)
    ("2026-09-20 14:32 |  | 已揽收", "不能为空"),          # 地点空
    ("2026-09-20 14:32 | 广州 |  ", "不能为空"),           # 描述空
])
def test_parse_trace_rejects_malformed(raw, needle):
    with pytest.raises(TraceFormatError) as e:
        parse_trace(raw, None, None)
    assert needle in str(e.value)
    assert "第 1 行" in str(e.value)


def test_parse_trace_rejects_non_increasing_time():
    """时间必须严格递增,相同也不行 —— 同一时刻的两个节点排不出先后。"""
    raw = "2026-09-21 08:05 | 深圳 | 派送中\n2026-09-20 14:32 | 广州 | 已揽收"
    with pytest.raises(TraceFormatError) as e:
        parse_trace(raw, None, None)
    assert "第 2 行" in str(e.value)

    same = "2026-09-20 14:32 | 广州 | 已揽收\n2026-09-20 14:32 | 深圳 | 发车"
    with pytest.raises(TraceFormatError):
        parse_trace(same, None, None)


def test_parse_trace_window_boundaries():
    """节点必须落在 [shipped_at, signed_at] 内;边界相等算通过;两端为空不校验。"""
    raw = "2026-09-20 14:32 | 广州市 | 已揽收\n2026-09-21 08:05 | 深圳 | 派送中"
    # 边界相等 -> 通过
    assert parse_trace(raw, date(2026, 9, 20), date(2026, 9, 21)) is not None
    # 首节点早于发货
    with pytest.raises(TraceFormatError) as e:
        parse_trace(raw, date(2026, 9, 21), None)
    assert "首节点" in str(e.value)
    # 末节点晚于签收
    with pytest.raises(TraceFormatError) as e:
        parse_trace(raw, None, date(2026, 9, 20))
    assert "末节点" in str(e.value)
    # 两端都为空 -> 不校验
    assert parse_trace(raw, None, None) is not None


def test_parse_trace_line_numbers_survive_blank_lines():
    """夹了空行之后,报错行号必须仍指向管理员看到的真实行号。

    空行是本格式明确允许的写法,而"先丢空行再编号"会让其后的所有行号前移 ——
    报错指到空行上,而行号是这条错误唯一的定位手段。
    """
    raw = "2026-09-20 14:32 | 广州 | 已揽收\n\n2026-09-20 09:00 | 深圳 | 发车"
    with pytest.raises(TraceFormatError) as e:
        parse_trace(raw, None, None)
    assert "第 3 行" in str(e.value)          # 不是第 2 行

    raw2 = "2026-09-20 14:32 | 广州 | 已揽收\n\n\n2026-09-20 09:00 | 深圳 | 发车"
    with pytest.raises(TraceFormatError) as e2:
        parse_trace(raw2, None, None)
    assert "第 4 行" in str(e2.value)


@pytest.mark.parametrize("raw,line_no", [
    ("2026-09-20 14:32 | 广州 | 已揽收\n2026/09/21 08:05 | 深圳 | 派送中", 2),
    ("2026-09-20 14:32 | 广州 | 已揽收\n2026-09-21 08:05 |  | 派送中", 2),
])
def test_parse_trace_reports_non_first_line_number(raw, line_no):
    """非首行的行号也要对 —— 只测第 1 行的话,enumerate 恒返回 1 也能全绿。"""
    with pytest.raises(TraceFormatError) as e:
        parse_trace(raw, None, None)
    assert f"第 {line_no} 行" in str(e.value)


def test_parse_trace_trims_location_and_description():
    """地点/描述两侧空格必须被 trim —— 规范化行为本身要被钉住,不能只靠"空字段被拦"间接要求。"""
    out = parse_trace("2026-09-20 14:32 |  广州  |  已揽收  ", None, None)
    assert out == "2026-09-20 14:32 | 广州 | 已揽收"


def test_parse_trace_is_idempotent():
    """输出必须幂等 —— 这是"存下来的值能原样再提交"的前提(spec §4.8 长度不变式)。"""
    once = parse_trace("2026-9-2 9:05|广州|已揽收\n2026-09-03 08:00|深圳|派送中", None, None)
    assert parse_trace(once, None, None) == once


def test_parse_trace_does_not_split_on_unicode_separators():
    """U+2028 不是行分隔符 —— 后端拆行的定义必须与前端一致。

    这条钉住的是"不用 str.splitlines()"这个选择:splitlines 还会把 U+2028 / U+2029
    等 Unicode 分隔符也当成换行(从 Word / 微信粘贴时会出现),前端不认这些,
    会造成同一次输入前后端数出不同的行数。没这条测试,谁改回 splitlines 都不会红。
    """
    # 用 chr(0x2028) 构造,而不是在源码里贴字面量 —— 它是不可见字符,看不懂也改不动
    raw = "2026-09-20 14:32 | 广州" + chr(0x2028) + "| 已揽收"
    # splitlines 会把它当换行 -> 切成 2 行,每行都凑不出 3 段 -> 报错;
    # 按换行符拆则是 1 行 3 段,段中那个 U+2028 被 str.strip() 去掉(实测 isspace() 为 True)
    assert parse_trace(raw, None, None) == "2026-09-20 14:32 | 广州 | 已揽收"


# ==================== 造数助手 ====================

async def _new_order(c, token, *, status="已发货", order_date="2026-09-20"):
    """建一个测试订单。订单号由后端生成 ORD-xxx —— 不要自己造前缀,否则会跑挂
    test_admin_orders.py::test_create_after_deleting_middle_order_no_collision。"""
    r = await c.post("/api/admin/orders", headers=_bearer(token), json={
        "product_sku": SKU, "buyer_name": BUYER, "status": status, "order_date": order_date,
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _new_shipment(c, token, order_no, tracking_no, **kw):
    body = {"order_no": order_no, "tracking_no": tracking_no, "carrier": "京东物流", **kw}
    r = await c.post("/api/admin/logistics", headers=_bearer(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _list(c, token, **params):
    params.setdefault("keyword", BUYER)      # 把范围缩到本文件造的数据,不受种子影响
    params.setdefault("page_size", MAX_PAGE_SIZE)
    # ⚠️ 用 params= 字典而不是手拼查询串:手拼时值里的 `&` 会静默切出第二个参数、
    # `#` 会截断整个 query,而 `assert r.status_code == 200` 抓不到(服务端照常 200)。
    # 同项目的 test_admin_orders.py:131-134 也是 params= 写法。
    # (httpx 会把 None 渲染成空值而不是丢掉该参数 —— 端点把空串视同未传,行为不变,
    #  所以这里不需要再过滤 None。)
    r = await c.get("/api/admin/logistics", headers=_bearer(token), params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ==================== 列表 ====================

async def test_list_item_fields(admin_token):
    """列表元素字段集逐字段钉死 —— 少一个前端就渲染不出来。"""
    async with _client() as c:
        await _new_order(c, admin_token)
        d = await _list(c, admin_token)
        assert d["items"], "至少应有一行"
        assert set(d["items"][0]) == ITEM_FIELDS


async def test_list_shape(admin_token):
    async with _client() as c:
        await _new_order(c, admin_token)
        d = await _list(c, admin_token)
        assert set(d) == {"total", "page", "page_size", "items"}


async def test_unrecorded_order_appears(admin_token):
    """已发货但没录物流的订单必须出现在列表里,运单相关字段全为 null。"""
    async with _client() as c:
        order = await _new_order(c, admin_token, status="已发货")
        d = await _list(c, admin_token)
        row = next(x for x in d["items"] if x["order_no"] == order["order_no"])
        assert row["id"] is None
        assert row["tracking_no"] is None
        assert row["status"] is None
        assert row["trace"] is None
        assert row["order_status"] == "已发货"


async def test_recorded_order_shows_shipment(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-0001",
                            status="运输中", shipped_at="2026-09-20")
        d = await _list(c, admin_token)
        row = next(x for x in d["items"] if x["order_no"] == order["order_no"])
        assert row["id"] is not None
        assert row["tracking_no"] == "TEST-0001"
        assert row["carrier"] == "京东物流"
        assert row["status"] == "运输中"
        assert row["shipped_at"] == "2026-09-20"


async def test_orphan_shipment_not_hidden(admin_token):
    """订单建过运单后被改回「处理中」,该行仍要出现 —— 否则运单隐身,编辑删除都做不到。"""
    async with _client() as c:
        order = await _new_order(c, admin_token, status="已发货")
        await _new_shipment(c, admin_token, order["order_no"], "TEST-0002")
        await c.put(f"/api/admin/orders/{order['id']}", headers=_bearer(admin_token),
                    json={"status": "处理中"})
        d = await _list(c, admin_token)
        row = next(x for x in d["items"] if x["order_no"] == order["order_no"])
        assert row["order_status"] == "处理中"
        assert row["id"] is not None


async def test_total_is_exact_not_cartesian(admin_token):
    """total 必须是精确的过滤后全量。

    钉住 count 漏 join 的坑:漏 join 时 SQLAlchemy 会算出笛卡尔积
    (运单数 × 订单数),不报错只发一条 SAWarning。所以断言必须用精确值,
    `total > len(items)` 那种写法抓不到。
    """
    async with _client() as c:
        for i in range(3):
            await _new_order(c, admin_token)
        d = await _list(c, admin_token)
        assert d["total"] == 3


async def test_keyword_matches_five_fields(admin_token):
    """keyword 覆盖运单号 / 订单号 / 商品名 / 买家名 / 承运商 五个字段。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-KW-1",
                            carrier="顺丰速运")
        # 运单号
        assert len((await _list(c, admin_token, keyword="TEST-KW-1"))["items"]) == 1
        # 订单号(订单号唯一,顺带验证)
        assert len((await _list(c, admin_token, keyword=order["order_no"]))["items"]) == 1
        # 商品名 / 买家名 / 承运商
        assert len((await _list(c, admin_token, keyword=order["product_name"][:4]))["items"]) >= 1
        assert len((await _list(c, admin_token, keyword=BUYER))["items"]) == 1
        assert len((await _list(c, admin_token, keyword="顺丰"))["items"]) == 1


async def test_filter_by_shipment_status(admin_token):
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, a["order_no"], "TEST-F-1", status="运输中")
        await _new_shipment(c, admin_token, b["order_no"], "TEST-F-2", status="派送中")
        d = await _list(c, admin_token, status="派送中")
        assert [x["order_no"] for x in d["items"]] == [b["order_no"]]


async def test_filter_unrecorded(admin_token):
    """status=未录入 -> 只返回 id 为 null 的行。"""
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, a["order_no"], "TEST-U-1")
        d = await _list(c, admin_token, status="未录入")
        assert [x["order_no"] for x in d["items"]] == [b["order_no"]]
        assert all(x["id"] is None for x in d["items"])


async def test_filter_by_order_status(admin_token):
    async with _client() as c:
        await _new_order(c, admin_token, status="已送达")
        await _new_order(c, admin_token, status="已发货")
        d = await _list(c, admin_token, order_status="已送达")
        assert len(d["items"]) == 1
        assert d["items"][0]["order_status"] == "已送达"


async def test_sort_shipped_at_desc_nulls_last(admin_token):
    """shipped_at 降序,未录入的(shipped_at 为 null)排最后。"""
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        await _new_order(c, admin_token)                       # 未录入
        await _new_shipment(c, admin_token, a["order_no"], "TEST-S-1", shipped_at="2026-09-10")
        await _new_shipment(c, admin_token, b["order_no"], "TEST-S-2", shipped_at="2026-09-15")
        items = (await _list(c, admin_token))["items"]
        assert [x["shipped_at"] for x in items] == ["2026-09-15", "2026-09-10", None]


async def test_signed_at_null_when_not_signed(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-SA-1", status="运输中")
        row = (await _list(c, admin_token))["items"][0]
        assert row["signed_at"] is None


async def test_trace_returned_normalized(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-T-1",
                            shipped_at="2026-09-20",
                            trace="2026-09-20 14:32|广州市|已揽收")
        row = (await _list(c, admin_token))["items"][0]
        assert row["trace"] == "2026-09-20 14:32 | 广州市 | 已揽收"


async def test_pagination_slices_without_overlap(admin_token):
    """分页切片与「total 与 page 无关」。

    ⚠️ 这条不能省:上面 13 条用例一条都不带 page(`_list` 把 page_size 默认压成 100),
    于是 `(page - 1) * page_size` 这个 offset 计算、以及"count 查询与 page 无关"
    这条设计理由,全都没被钉住。

    实测过两个错误变体,才看清这条测试的价值区间:
      · `offset = page * page_size` —— 页 1 就跳过全部行(100),13 条一起红,
        被老用例抓得到,**不需要这条**
      · `offset = page - 1` —— page=1 时与正确写法【等价】,只有 page>=2 出错。
        实测 9 failed = 基线 8 + 【仅此一条】,其余 14 条全绿
      → 真正的盲区是后者:一个在单页场景下完全正确的 offset 写法。
    """
    async with _client() as c:
        for _ in range(3):
            await _new_order(c, admin_token)
        p1 = await _list(c, admin_token, page=1, page_size=2)
        p2 = await _list(c, admin_token, page=2, page_size=2)
        p3 = await _list(c, admin_token, page=3, page_size=2)

        # total 与 page 无关(它由那次另跑的 count 决定,不是 len(items))
        assert [x["total"] for x in (p1, p2, p3)] == [3, 3, 3]
        assert [len(x["items"]) for x in (p1, p2, p3)] == [2, 1, 0]
        # 三页不重叠、不丢行
        assert len({r["order_no"] for r in p1["items"] + p2["items"]}) == 3
        # 回显的 page / page_size 与请求一致(前端 Pagination 依赖它)
        assert (p2["page"], p2["page_size"]) == (2, 2)


async def test_filters_narrow_the_base_set(admin_token):
    """基础条件是不变量:任何筛选组合都是它的【严格收窄】,不存在"加了筛选反而多出行"。

    钉住 status 与 order_status 同时给时是"与"关系 —— 这条例外很容易被写反。
    """
    async with _client() as c:
        # 一个已发货未录入的订单:它能被 status=未录入 捞到,但订单状态不是处理中
        await _new_order(c, admin_token, status="已发货")
        assert len((await _list(c, admin_token, status="未录入"))["items"]) == 1
        assert len((await _list(c, admin_token,
                               status="未录入", order_status="处理中"))["items"]) == 0


# ==================== 建单 ====================

async def test_create_ok(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-1",
                                shipped_at="2026-09-20", trace="2026-09-20 14:32 | 广州市 | 已揽收")
        assert d["order_no"] == order["order_no"]
        assert d["tracking_no"] == "TEST-C-1"
        assert d["status"] == "待揽收"
        assert d["order_status"] == "已发货"
        assert d["product_name"] == order["product_name"]     # join 出来的字段也要有
        assert set(d) == ITEM_FIELDS


async def test_create_normalizes_trace(admin_token):
    """无空格写法也要被规范化成 ' | '。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-2",
                                shipped_at="2026-09-20",
                                trace="2026-09-20 14:32|广州市|已揽收\n\n2026-09-21 08:05|深圳|派送中")
        assert d["trace"] == ("2026-09-20 14:32 | 广州市 | 已揽收\n"
                              "2026-09-21 08:05 | 深圳 | 派送中")


async def test_create_order_not_found(admin_token):
    async with _client() as c:
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": "ORD-999", "tracking_no": "TEST-C-3", "carrier": "京东物流"})
        assert r.status_code == 400
        assert "订单不存在" in r.json()["detail"]


async def test_create_rejects_processing_order(admin_token):
    """处理中且无运单的订单不能建运单。"""
    async with _client() as c:
        order = await _new_order(c, admin_token, status="处理中")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-4", "carrier": "京东物流"})
        assert r.status_code == 400
        assert "只有已发货及之后的订单才能建运单" in r.json()["detail"]


async def test_create_rejects_duplicate_order(admin_token):
    """一单只能一个运单 —— 文案必须是「已有运单」,不能错报成运单号冲突。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-C-5")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-6", "carrier": "京东物流"})
        assert r.status_code == 409
        assert "已有运单" in r.json()["detail"]


async def test_create_rejects_duplicate_tracking_no(admin_token):
    """运单号撞号的 409 文案必须是「运单号已存在」,不能错报成订单冲突。

    两个 409 靠约束名分支区分,只断言状态码抓不到错报。
    """
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, a["order_no"], "TEST-C-7")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": b["order_no"], "tracking_no": "TEST-C-7", "carrier": "京东物流"})
        assert r.status_code == 409
        assert "运单号已存在" in r.json()["detail"]


async def test_create_signed_fills_today(admin_token):
    """status='已签收' 未传 signed_at -> 补当天(UTC)。"""
    async with _client() as c:
        order = await _new_order(c, admin_token, status="已送达")
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-8",
                                status="已签收", shipped_at="2026-09-20")
        assert d["signed_at"] == datetime.now(timezone.utc).date().isoformat()


async def test_create_signed_with_explicit_null_keeps_null(admin_token):
    """已签收 + 显式传 signed_at:null -> 落库 NULL(不是补今天)。

    与上一条的区别【只在「键在不在」】:上一条不传 signed_at(model_fields_set 里没有),
    这条显式传 null。少了 explicit_clear 的话,`given or existing or today` 会把
    显式 null 当成"没传"、补成今天 —— 而管理员在补录弹窗里清空日期框送的正是 null,
    落库结果与他看到的界面不一致。spec §4.5 明说「已签收、日期不详」是有意的可达状态。

    ⚠️ 这里不能用 `_new_shipment` 助手(它不便于表达"显式传 null"),直接发请求。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token, status="已送达")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-19",
            "carrier": "京东物流", "status": "已签收",
            "shipped_at": "2026-09-20", "signed_at": None})
        assert r.status_code == 200, r.text
        assert r.json()["signed_at"] is None
        assert r.json()["status"] == "已签收"


async def test_create_signed_at_cleared_when_not_signed(admin_token):
    """非「已签收」传了 signed_at -> 落库为 null。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-9",
                                status="运输中", signed_at="2026-09-25")
        assert d["signed_at"] is None


async def test_create_rejects_signed_before_shipped(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token, status="已送达")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-10", "carrier": "京东物流",
            "status": "已签收", "shipped_at": "2026-09-20", "signed_at": "2026-09-18"})
        assert r.status_code == 400
        assert "签收时间不能早于发货时间" in r.json()["detail"]


async def test_create_rejects_signed_without_shipped(admin_token):
    """已签收但没发货时间 -> 400(规则 B 的第二条)。"""
    async with _client() as c:
        order = await _new_order(c, admin_token, status="已送达")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-11", "carrier": "京东物流",
            "status": "已签收"})
        assert r.status_code == 400
        assert "必须有发货时间" in r.json()["detail"]


@pytest.mark.parametrize("field,bad", [("status", "已取消"), ("carrier", "顺丰")])
async def test_create_rejects_bad_enum(admin_token, field, bad):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        body = {"order_no": order["order_no"], "tracking_no": "TEST-C-12",
                "carrier": "京东物流", field: bad}
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json=body)
        assert r.status_code == 422


async def test_create_blank_trace_becomes_null(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-13", trace="   \n  ")
        assert d["trace"] is None


async def test_create_trace_over_max(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-14",
            "carrier": "京东物流", "trace": "x" * 2001})
        assert r.status_code == 422


# ==================== 轨迹格式(建单路径) ====================

@pytest.mark.parametrize("trace,needle", [
    ("2026-09-20 14:32 | 广州市", "三段"),
    ("2026-09-20 14:32|广州|已揽收|多余", "三段"),
    ("2026/09/20 14:32 | 广州 | 已揽收", "YYYY-MM-DD"),
    ("2026-13-45 14:32 | 广州 | 已揽收", "YYYY-MM-DD"),
    ("2026-09-20 14:32 |  | 已揽收", "不能为空"),
])
async def test_create_rejects_bad_trace(admin_token, trace, needle):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-15",
            "carrier": "京东物流", "trace": trace})
        assert r.status_code == 400
        assert needle in r.json()["detail"]
        assert "第 1 行" in r.json()["detail"]


async def test_create_rejects_trace_before_shipped(admin_token):
    """首节点早于发货时间 -> 400。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-16", "carrier": "京东物流",
            "shipped_at": "2026-09-20", "trace": "2026-09-19 10:00 | 广州 | 已揽收"})
        assert r.status_code == 400
        assert "首节点" in r.json()["detail"]


async def test_trace_length_is_checked_after_normalization(admin_token):
    """长度上限必须卡在【规范化之后】的文本上,否则存下来的值会提交不回去。

    构造:用紧凑写法(无空格)堆够行数,让原始长度在 TRACE_MAX 以内、
    规范化后超出去。算术 —— 每行 `YYYY-MM-DD HH:MM|广州|已揽收` 原始 23 字符,
    规范化后是 `YYYY-MM-DD HH:MM | 广州 | 已揽收` 27 字符(加 4);
    含换行后 raw = 24N-1、normalized = 28N-1。取 N=78:
    原始 1871 ≤ 2000,规范化后 2183 > 2000。
    """
    from app.api.admin.logistics import parse_trace
    from app.schemas.admin import TRACE_MAX

    base = datetime(2026, 9, 1, 0, 0)
    raw = "\n".join(
        f"{(base + timedelta(minutes=i)):%Y-%m-%d %H:%M}|广州|已揽收" for i in range(78))

    # 先自证这个 fixture 真的落在两条线之间,否则用例是空跑
    assert len(raw) <= TRACE_MAX, f"构造有误:原始 {len(raw)} 字符不该超上限"
    assert len(parse_trace(raw, date(2026, 9, 1), None)) > TRACE_MAX, "构造有误:规范化后没超上限"

    async with _client() as c:
        order = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-17",
            "carrier": "京东物流", "status": "运输中", "shipped_at": "2026-09-01",
            "trace": raw})
        # 原始不超、规范化后超 -> 400(而不是 422,也不是静默存下超长值)
        assert r.status_code == 400
        assert "规范化后" in r.json()["detail"]
