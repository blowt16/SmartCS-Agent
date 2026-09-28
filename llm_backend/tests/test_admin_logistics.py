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
from sqlalchemy import select, update

# main 必须在【collection 阶段】导入(与 test_admin_orders.py 同因):
# 从仓库根跑 pytest 时,根目录另有一个无关的 D:\SmartCS-Agent\main.py,
# 此时才首次 `from main import app` 会命中那个 stub(没有 app)。
_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from main import app  # noqa: E402
from app.api.admin.logistics import TraceFormatError, parse_trace  # noqa: E402
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models.order import Order  # noqa: E402

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

async def _new_order(c, token, *, order_date="2026-09-20"):
    """建一个测试订单。订单号由后端生成 ORD-xxx —— 不要自己造前缀,否则会跑挂
    test_admin_orders.py::test_create_after_deleting_middle_order_no_collision。

    ⚠️ 订单状态恒为「处理中」(由运单派生,建单不可指定),所以 status 参数被删掉。
       要造特定状态的订单:建单 → 建运单 → 推运单状态(见 _order_with_shipment_status)。

    ⚠️ order_date 默认值【必须保留】。本文件大量轨迹校验用例依赖这个固定日期
       (节点必须落在 [shipped_at, signed_at] 窗口内),删掉会连带炸一片。

    ⚠️ 返回结构与改造前保持一致(dict),不要改成 tuple ——
       本文件 59 处调用点写的是 order["order_no"] / order["id"]。
    """
    r = await c.post("/api/admin/orders", headers=_bearer(token), json={
        "product_sku": SKU, "buyer_name": BUYER, "order_date": order_date,
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


async def _order_with_shipment_status(c, token, shipment_status, **kw):
    """建订单 + 建运单 + 推到指定运单状态,返回 (order, shipment)。

    新模型下要造"订单处于某个状态"只能走这条路 —— 建单接口不再接受 status。
    """
    order = await _new_order(c, token)
    sh = await _new_shipment(c, token, order["order_no"],
                             f"T-{shipment_status}-{order['order_no']}",
                             status=shipment_status, **kw)
    return order, sh


# ==================== 直连库造脏数据 ====================

async def _force_order_status(order_no: str, status: str) -> None:
    """直连库改订单状态,用于构造 API 已无法产生的脏数据。

    ⚠️ 订单状态现在由运单派生、PUT 接口已不接受 status,所以"订单与运单对不上"
       这种脏数据只能这样造。不要试图用 API 构造。
    """
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(Order).where(Order.order_no == order_no).values(status=status)
        )
        await s.commit()


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
    """没录物流的订单必须出现在列表里,运单相关字段全为 null。

    ⚠️ 新模型下"还没建运单"的订单恒为「处理中」(旧模型里这里是「已发货」)——
       没有运单就没有派生来源,订单停在 NO_SHIPMENT_ORDER_STATUS。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        d = await _list(c, admin_token)
        row = next(x for x in d["items"] if x["order_no"] == order["order_no"])
        assert row["id"] is None
        assert row["tracking_no"] is None
        assert row["status"] is None
        assert row["trace"] is None
        assert row["order_status"] == "处理中"


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
    """有运单却被压回「处理中」的订单(脏数据),该行仍要出现 —— 否则运单隐身,编辑删除都做不到。

    ⚠️ 造数必须直连库:新模型下订单状态由运单派生,订单 PUT 也已不接受 status,
       经 API 构造不出"运单说运输中、订单却说处理中"这种对不上的数据。
       运单推「运输中」(本应派生「已发货」)后把订单压回「处理中」。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-0002",
                            status="运输中")
        await _force_order_status(order["order_no"], "处理中")
        d = await _list(c, admin_token)
        row = next(x for x in d["items"] if x["order_no"] == order["order_no"])
        # 自证脏数据真的对不上(否则这条用例会退化成"普通有运单的行")
        assert row["status"] == "运输中"
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
        # 商品名
        assert len((await _list(c, admin_token, keyword=order["product_name"][:4]))["items"]) >= 1
        # 买家名:BUYER 本身就把范围缩到本文件造的数据,所以可以断言恰好 1 条
        assert len((await _list(c, admin_token, keyword=BUYER))["items"]) == 1

        # 承运商:⚠️ 【不能断言"恰好 1 条"】—— `keyword="顺丰"` 会命中【全库】所有顺丰的运单,
        # 包括种子数据(实测 Task 10 跑完种子后有 2 条,于是这条断言红:assert 3 == 1)。
        # 这是本文件里唯一一处把 keyword 从 BUYER 换成裸子串的断言,于是成了唯一一处
        # 依赖"库里恰好有什么"的断言 —— 会表现成"换个库就红"的假 flaky。
        # 改成:命中集合【包含本单】,且命中项【都与关键字一致】。两条都不依赖全局内容。
        hits = (await _list(c, admin_token, keyword="顺丰"))["items"]
        assert order["order_no"] in [x["order_no"] for x in hits]
        assert all("顺丰" in (x["carrier"] or "") for x in hits)


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
    """order_status 精确筛选。

    ⚠️ 旧模型靠建单时传 status 造两种订单状态;新模型下订单状态只能由运单派生,
       所以"订单处于某个状态"要走 建单 → 建运单(status=…)。「已送达」已从
       订单状态集里删掉,这里用 5 值中的「已签收」。
    """
    async with _client() as c:
        a, _ = await _order_with_shipment_status(
            c, admin_token, "已签收", shipped_at="2026-09-20", signed_at="2026-09-22")
        await _order_with_shipment_status(c, admin_token, "待揽收")   # 仍是「处理中」
        d = await _list(c, admin_token, order_status="已签收")
        assert [x["order_no"] for x in d["items"]] == [a["order_no"]]
        assert d["items"][0]["order_status"] == "已签收"


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

    ⚠️ 旧写法用「已发货 + 未录入」的订单撑出这个差异(未录入 ⟹ 非处理中,交集为 0)。
       新模型下订单状态由运单派生:【没有运单就恒为「处理中」】,那个组合再也造不出来
       (造出来只能是脏数据)。改成两个「处理中」的订单 —— 都有运单/只有一单有运单,
       同样能让"与"和"或"给出不同的行数,守卫的语义(收窄)不变。
    """
    async with _client() as c:
        # 两单都是「处理中」,但只有一单有运单 —— 于是两个筛选各自捞到的集合不同,
        # "与"和"或"才会给出不同的行数(旧写法已经构造不出这个差异,见 docstring)。
        a = await _new_order(c, admin_token)                        # 处理中 + 未录入
        b = await _new_order(c, admin_token)                        # 处理中 + 有运单
        await _new_shipment(c, admin_token, b["order_no"], "TEST-NARROW-1")
        assert len((await _list(c, admin_token, order_status="处理中"))["items"]) == 2
        assert len((await _list(c, admin_token, status="未录入"))["items"]) == 1
        # 两个筛选同时给:"与" -> 1 行(只剩 a);若被写成"或" -> 2 行
        d = await _list(c, admin_token, status="未录入", order_status="处理中")
        assert [x["order_no"] for x in d["items"]] == [a["order_no"]]


# ==================== 建单 ====================

async def test_create_ok(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-1",
                                shipped_at="2026-09-20", trace="2026-09-20 14:32 | 广州市 | 已揽收")
        assert d["order_no"] == order["order_no"]
        assert d["tracking_no"] == "TEST-C-1"
        assert d["status"] == "待揽收"
        # 待揽收 → 订单仍是「处理中」(旧的"已发货"是建单时手工指定的,已删)
        assert d["order_status"] == "处理中"
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


async def test_create_rejects_duplicate_order(admin_token):
    """一单只能一个运单 —— 文案必须是「已有运单」,不能错报成运单号冲突。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-C-5")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-6", "carrier": "京东物流"})
        assert r.status_code == 409
        assert "已有运单" in r.json()["detail"]


async def test_create_duplicate_beats_processing_status(admin_token):
    """②「已有运单」必须判在 ③「状态合法」【之前】。

    这条顺序是业务要求(端点里的注释写了理由):订单建过运单、状态却与运单对不上时,
    真实原因是"已有运单",报"状态不对"会误导管理员 —— 他改回状态就好了,
    但改了也没用,运单早就在了。

    ⚠️ 造数必须直连库,且脏数据要【同时】踩中两个校验:
       有运单(触发 ②)+ 订单状态不是「处理中」(触发 ③)。
       新模型下订单状态由运单派生、订单 PUT 也不再接受 status,
       "有运单却不是处理中"经 API 已经构造不出来。

    ⚠️ 少了这条用例,把两个校验块对调后【其余用例全绿】(实测确认),
    一个在单测里完全看不出来的改动就会让这条规则静默失效。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-C-21")
        await _force_order_status(order["order_no"], "已签收")   # 与运单对不上的脏数据
        # ⚠️ 这里的自证不能省(旧版对应的是对那次 PUT 的断言):
        # 若脏数据没生效、订单还是「处理中」,③ 就【不会】触发 —— 只剩 ② 能触发,
        # 下面三条断言照样全绿,这条用例会悄悄退化成一条普通的"重复运单"用例,
        # 顺序规则再次失去保护,而它看起来是绿的。
        row = next(x for x in (await _list(c, admin_token))["items"]
                   if x["order_no"] == order["order_no"])
        assert row["id"] is not None                 # ② 的触发条件:已有运单
        assert row["order_status"] == "已签收"        # ③ 的触发条件:状态不是「处理中」
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-22", "carrier": "京东物流"})
        assert r.status_code == 409
        assert "已有运单" in r.json()["detail"]
        assert "只有「处理中」" not in r.json()["detail"]   # 不能报成状态问题


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
    """status='已签收' 未传 signed_at -> 补当天(UTC)。

    ⚠️ 断言写成「请求前后各取一次今天,落在两者之间」而不是「等于此刻的今天」:
    端点算日期与断言算日期之间隔了一次 DB 往返,跨 UTC 零点时两者差一天。
    (概率极低,但这类 flake 在 CI 上长期跑必然碰到,而且极难复现。)
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        before = datetime.now(timezone.utc).date().isoformat()
        d = await _new_shipment(c, admin_token, order["order_no"], "TEST-C-8",
                                status="已签收", shipped_at="2026-09-20")
        after = datetime.now(timezone.utc).date().isoformat()
        assert d["signed_at"] in (before, after)


async def test_create_signed_with_explicit_null_keeps_null(admin_token):
    """已签收 + 显式传 signed_at:null -> 落库 NULL(不是补今天)。

    与上一条的区别【只在「键在不在」】:上一条不传 signed_at(model_fields_set 里没有),
    这条显式传 null。少了 explicit_clear 的话,`given or existing or today` 会把
    显式 null 当成"没传"、补成今天 —— 而管理员在补录弹窗里清空日期框送的正是 null,
    落库结果与他看到的界面不一致。spec §4.5 明说「已签收、日期不详」是有意的可达状态。

    ⚠️ 这里不能用 `_new_shipment` 助手(它不便于表达"显式传 null"),直接发请求。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
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
        order = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": order["order_no"], "tracking_no": "TEST-C-10", "carrier": "京东物流",
            "status": "已签收", "shipped_at": "2026-09-20", "signed_at": "2026-09-18"})
        assert r.status_code == 400
        assert "签收时间不能早于发货时间" in r.json()["detail"]


async def test_create_rejects_signed_without_shipped(admin_token):
    """已签收但没发货时间 -> 400(规则 B 的第二条)。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
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
    # parse_trace 在文件顶部已导入,这里不要重复导入(会让人怀疑是不是另一个对象)
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


# ==================== 编辑 ====================

async def test_update_not_found(admin_token):
    async with _client() as c:
        r = await c.put("/api/admin/logistics/999999", headers=_bearer(admin_token),
                        json={"status": "运输中"})
        assert r.status_code == 404


async def test_update_to_signed_fills_today(admin_token):
    """状态改成已签收、没传日期 -> 补当天。

    ⚠️ 断言用「请求前后各取一次今天」而不是「等于此刻的今天」—— 跨 UTC 零点时
    端点算的日期与断言算的差一天(中间隔了一次 DB 往返)。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-1",
                                shipped_at="2026-09-20")
        before = datetime.now(timezone.utc).date().isoformat()
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"status": "已签收"})
        after = datetime.now(timezone.utc).date().isoformat()
        assert r.status_code == 200, r.text
        assert r.json()["signed_at"] in (before, after)


async def test_update_from_signed_clears_signed_at(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-2",
                                status="已签收", shipped_at="2026-09-20")
        assert s["signed_at"] is not None
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"status": "运输中"})
        assert r.status_code == 200
        assert r.json()["signed_at"] is None


async def test_update_trace_three_forms(admin_token):
    """trace 传新文本 -> 覆盖;传 null -> 清空;不传 -> 原样不动。"""
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-3",
                                shipped_at="2026-09-20",
                                trace="2026-09-20 14:32 | 广州 | 已揽收")
        url = f"/api/admin/logistics/{s['id']}"
        # 不传 -> 原样不动
        r = await c.put(url, headers=_bearer(admin_token), json={"carrier": "顺丰速运"})
        assert r.status_code == 200, r.text          # 不断状态码的话失败会以 KeyError 出现
        d = r.json()
        assert d["trace"] == "2026-09-20 14:32 | 广州 | 已揽收"
        assert d["carrier"] == "顺丰速运"
        # 传新文本 -> 覆盖
        r = await c.put(url, headers=_bearer(admin_token),
                        json={"trace": "2026-09-21 09:00|深圳|派送中"})
        assert r.status_code == 200, r.text
        assert r.json()["trace"] == "2026-09-21 09:00 | 深圳 | 派送中"
        # 传 null -> 清空
        r = await c.put(url, headers=_bearer(admin_token), json={"trace": None})
        assert r.status_code == 200, r.text
        assert r.json()["trace"] is None


async def test_update_order_no_ignored(admin_token):
    """order_no 不可改(schema 里没有该字段,pydantic 默认忽略额外字段)。"""
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, a["order_no"], "TEST-U-4")
        d = (await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                         json={"order_no": b["order_no"], "status": "运输中"})).json()
        assert d["order_no"] == a["order_no"]
        assert d["status"] == "运输中"


async def test_update_tracking_no_conflict(admin_token):
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        sa = await _new_shipment(c, admin_token, a["order_no"], "TEST-U-5")
        sb = await _new_shipment(c, admin_token, b["order_no"], "TEST-U-6")
        r = await c.put(f"/api/admin/logistics/{sb['id']}", headers=_bearer(admin_token),
                        json={"tracking_no": "TEST-U-5"})
        assert r.status_code == 409
        assert "运单号已存在" in r.json()["detail"]


async def test_update_trace_window_uses_effective_values(admin_token):
    """只改轨迹、不传 shipped_at/signed_at 时,窗口校验仍要用库里的值。

    漏了 effective 取值这条路径上窗口校验会静默失效 —— 这条用例专门钉它。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-7",
                                status="已签收", shipped_at="2026-09-20", signed_at="2026-09-22")
        # 末节点晚于库里的 signed_at(2026-09-22) -> 应 400
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"trace": "2026-09-25 10:00 | 广州 | 已签收"})
        assert r.status_code == 400
        assert "末节点" in r.json()["detail"]


async def test_update_bad_trace_format(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-8")
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"trace": "2026-09-20 14:32 广州 已揽收"})
        assert r.status_code == 400
        assert "三段" in r.json()["detail"]


async def test_update_can_clear_signed_at(admin_token):
    """已签收的运单,签收时间必须能被清空成 NULL。

    前端在「已签收」时渲染一个可空的日期框,管理员清空它 -> 显式传 null。
    如果后端把「显式传 null」当成「没传」而回填旧值,那个框就是个摆设
    (spec §4.5 规则 A 下方的说明:本模块有意支持「已签收、日期不详」)。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-9",
                                status="已签收", shipped_at="2026-09-20",
                                signed_at="2026-09-22")
        assert s["signed_at"] == "2026-09-22"
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"signed_at": None})
        assert r.status_code == 200, r.text
        assert r.json()["signed_at"] is None
        # 状态仍是已签收,只是日期不详
        assert r.json()["status"] == "已签收"


async def test_update_unrelated_field_keeps_null_signed_at(admin_token):
    """改无关字段不能把「已签收、日期不详」填成今天。

    规则 A 的"补当天"只适用于【刚变成已签收】(靠 fill_today=became_signed 区分)。
    本来就已经签收、这次只改了个承运商、又没传 signed_at 的,必须保持原样 ——
    这里原样就是 NULL。

    少了这条,上一行那个"有意支持的状态"会被任何一次无关编辑摧毁:
    管理员只改了承运商,签收日期自己冒出来,凭空造了一条没人录入的数据。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-11",
                                status="已签收", shipped_at="2026-09-20", signed_at=None)
        assert s["signed_at"] is None       # 先自证 fixture 真的造出了"日期不详"
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"carrier": "顺丰速运"})
        assert r.status_code == 200, r.text
        assert r.json()["carrier"] == "顺丰速运"
        assert r.json()["signed_at"] is None        # 没被悄悄填成今天
        assert r.json()["status"] == "已签收"


async def test_update_explicit_dates_are_applied(admin_token):
    """显式传的 signed_at / shipped_at 必须真的落库。

    ⚠️ 这条不能省:编辑弹窗里管理员最常干的就是改日期,而上面 11 条用例
    【一条都没 PUT 过非 null 的日期字段】—— 实测把 `data.get("signed_at", ...)`
    改成无视客户端的 `shipment.signed_at`,11 条全绿(变异存活)。
    行为今天是对的,但没有东西拦得住它哪天坏掉。这正是本模块反复强调的
    「输入框变摆设」缺陷的镜像:那边是清不掉,这边是改不动。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-12",
                                status="已签收", shipped_at="2026-09-20",
                                signed_at="2026-09-22")
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"shipped_at": "2026-09-18", "signed_at": "2026-09-21"})
        assert r.status_code == 200, r.text
        assert r.json()["shipped_at"] == "2026-09-18"
        assert r.json()["signed_at"] == "2026-09-21"


async def test_update_shipped_at_is_validated_against_new_value(admin_token):
    """显式传的 shipped_at 必须参与规则 B 校验,不能用库里的旧值。

    ⚠️ 上一条抓不到这个 —— `effective_shipped` **不写回 data**(只有
    `effective_signed` 被写回 `data["signed_at"]`),它只喂给规则 B/C 校验,
    所以响应体里的 `shipped_at` 无论用哪个值都是客户端传的那个,
    **响应断言在结构上就抓不到**。

    实测:把 `data.get("shipped_at", shipment.shipped_at)` 改成
    `shipment.shipped_at` 后上面全部用例仍绿,而它是真漏洞 ——

        PUT {"shipped_at": "2026-09-25"}  (已签收,库里 signed_at=2026-09-22)
        变异体 -> 200,落库 shipped=09-25 / signed=09-22,规则 B 被击穿
        正确   -> 400 签收时间不能早于发货时间

    即直调接口能造出 `signed < shipped` 的数据,而规则 B 是 spec 说的"服务端兜底"。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-14",
                                status="已签收", shipped_at="2026-09-20",
                                signed_at="2026-09-22")
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"shipped_at": "2026-09-25"})
        assert r.status_code == 400
        assert "签收时间不能早于发货时间" in r.json()["detail"]


@pytest.mark.parametrize("field", ["tracking_no", "carrier", "status"])
async def test_update_rejects_explicit_null_for_not_null_columns(admin_token, field):
    """三个 NOT NULL 列传显式 null -> 400,不是误导性的 409「写入冲突」。

    实测过:改之前 `PUT {"status": null}` 返回 `409 运单写入冲突，请重试` ——
    那是【请求错误】不是冲突,管理员照着文案重试一万次也不会成功。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-13")
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={field: None})
        assert r.status_code == 400
        assert field in r.json()["detail"]


@pytest.mark.parametrize("status,given,existing,explicit_clear,fill_today,expected", [
    ("运输中", None, None, False, False, None),                    # 非已签收 -> 一律 NULL
    ("运输中", date(2026, 9, 22), None, False, False, None),        # 同上,传了也不留
    ("已签收", date(2026, 9, 22), None, False, False, date(2026, 9, 22)),   # 显式传值
    ("已签收", None, date(2026, 9, 20), True, True, None),          # 显式清空
    ("已签收", None, date(2026, 9, 20), False, False, date(2026, 9, 20)),   # 沿用原值
    ("已签收", None, None, False, True, "TODAY"),                   # 没传+无原值+刚变成 -> 补当天
    ("已签收", None, None, False, False, None),                     # 没传+无原值+本来就是 -> 保持 NULL
])
def test_resolve_signed_at_truth_table(status, given, existing,
                                       explicit_clear, fill_today, expected):
    """规则 A 的真值表(spec §4.5 那张表的七行逐行对应)。

    直接测纯函数、不经 HTTP —— **间接覆盖会漏掉整条输入维度**:
    上面 `test_update_explicit_dates_are_applied` 想补的那个缺口就是这么产生的。
    """
    from app.api.admin.logistics import _resolve_signed_at

    out = _resolve_signed_at(status, given, existing,
                             explicit_clear=explicit_clear, fill_today=fill_today)
    if expected == "TODAY":
        assert out == datetime.now(timezone.utc).date()
    else:
        assert out == expected


async def test_stored_trace_can_be_resubmitted_unchanged(admin_token):
    """不变式:任何被接受过的值,都能原样再提交一次。

    这是编辑路径的前提 —— 管理员打开编辑弹窗、什么都不改直接保存,
    不能因为"规范化让文本变长"(每行 +4 字符)而被 422 打回。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-U-10",
                                shipped_at="2026-09-20",
                                trace="2026-09-20 14:32|广州|已揽收\n2026-09-21 08:00|深圳|派送中")
        stored = s["trace"]
        r = await c.put(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token),
                        json={"trace": stored})
        assert r.status_code == 200, r.text
        assert r.json()["trace"] == stored


# ==================== 删除 ====================

async def test_delete_not_found(admin_token):
    async with _client() as c:
        r = await c.delete("/api/admin/logistics/999999", headers=_bearer(admin_token))
        assert r.status_code == 404


async def test_delete_ok(admin_token):
    async with _client() as c:
        order = await _new_order(c, admin_token)
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-D-1")
        r = await c.delete(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token))
        assert r.status_code == 200
        assert r.json() == {"id": s["id"], "deleted": True}
        items = (await _list(c, admin_token))["items"]
        # 按 order_no 定位而不是取 items[0] —— 后者依赖"列表里恰好只有一行",
        # 那只因 autouse fixture 清了数据才成立;将来多造一个订单就会指向错误的行。
        row = next(x for x in items if x["order_no"] == order["order_no"])
        assert row["id"] is None           # 订单还在,只是回到未录入


async def test_delete_returns_order_to_unrecorded(admin_token):
    """删掉运单后该订单回到「未录入」而不是从列表消失 —— 列表由 orders 驱动。

    ⚠️ 同时钉住【删除的作用域】:多造一个订单+运单,删掉第一个之后断言第二个的
    `tracking_no` **仍在**。少了这半句,把 delete_shipment 改成
    `delete(Shipment)`(删全表)也会通过 —— 每个用例只造一个运单时,
    「删一个」和「删全部」无法区分。
    """
    async with _client() as c:
        order = await _new_order(c, admin_token)
        other = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, other["order_no"], "TEST-D-3")   # 不该被牵连
        s = await _new_shipment(c, admin_token, order["order_no"], "TEST-D-2")

        await c.delete(f"/api/admin/logistics/{s['id']}", headers=_bearer(admin_token))

        d = await _list(c, admin_token)
        row = next(x for x in d["items"] if x["order_no"] == order["order_no"])
        assert row["id"] is None
        assert row["status"] is None
        # 另一个订单的运单不受影响(按运单身份断言,不是按订单身份)
        other_row = next(x for x in d["items"] if x["order_no"] == other["order_no"])
        assert other_row["tracking_no"] == "TEST-D-3"


# ==================== 级联(跨模块) ====================

async def test_delete_order_cascades_shipment(admin_token):
    """删订单必须连带删掉它的运单,不留孤儿。

    测试订单号由后端生成 ORD-xxx,不会干扰
    test_admin_orders.py::test_create_after_deleting_middle_order_no_collision。
    """
    from sqlalchemy import func as sa_func, select

    from app.core.database import AsyncSessionLocal
    from app.models.shipment import Shipment

    async with _client() as c:
        order = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, order["order_no"], "TEST-CAS-1")
        r = await c.delete(f"/api/admin/orders/{order['id']}", headers=_bearer(admin_token))
        assert r.status_code == 200

    async with AsyncSessionLocal() as s:
        n = (await s.execute(
            select(sa_func.count()).select_from(Shipment)
            .where(Shipment.order_no == order["order_no"])
        )).scalar_one()
    assert n == 0


async def test_delete_order_cascade_does_not_touch_others(admin_token):
    """删 a 的订单不能牵连 b 的【运单】。

    ⚠️ 关键在最后那句断言的是【运单身份】而不是【订单身份】。
    只断言 `[x["order_no"] for x in d["items"]] == [b["order_no"]]` 是不够的 ——
    实测:把级联做过头(连 b 的运单也一起删了),b 照样出现在列表里(它已发货,
    基础条件的第一分支就够),只是变成「未录入」,**那条断言照样通过**。
    一条叫「does not touch others」的守卫,在 over-broad 级联下放行。
    """
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, a["order_no"], "TEST-CAS-2")
        await _new_shipment(c, admin_token, b["order_no"], "TEST-CAS-3")
        await c.delete(f"/api/admin/orders/{a['id']}", headers=_bearer(admin_token))
        d = await _list(c, admin_token)
        assert [x["order_no"] for x in d["items"]] == [b["order_no"]]
        # b 的运单必须原样还在(这才是本用例的名字所指的东西)
        assert d["items"][0]["tracking_no"] == "TEST-CAS-3"
        assert d["items"][0]["id"] is not None


# ==================== 索引与模型 ====================

async def test_shipments_order_no_unique_index():
    """shipments.order_no 上有唯一索引 —— 这是用户原始需求里的那个索引。

    ⚠️ 必须连库查 pg_indexes,不能照抄 test_product_price_stock_model.py:
    那个文件是 DB-free 的,走 __table__.constraints 读 UniqueConstraint.name,
    而 unique=True 生成的约束在 Python 侧 name 是 None -> 会写成永远断言不到
    任何索引的空测试。
    """
    from sqlalchemy import text

    from app.core.database import engine

    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'shipments' AND indexdef ILIKE '%order_no%'"
        ))
        defs = [row[0] for row in r]
    assert defs, "shipments.order_no 上没有任何索引"
    assert any("UNIQUE" in d for d in defs), defs


async def test_shipments_tracking_no_unique_index():
    from sqlalchemy import text

    from app.core.database import engine

    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'shipments' AND indexdef ILIKE '%tracking_no%'"
        ))
        defs = [row[0] for row in r]
    assert any("UNIQUE" in d for d in defs), defs


# ==================== 订单状态由运单派生(新模型核心) ====================


async def test_create_shipment_for_processing_order_succeeds(admin_token):
    """门槛反转:处理中订单现在【可以】建运单。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": o["order_no"], "tracking_no": "T-DRV-PROC-1",
            "carrier": "京东物流", "status": "待揽收",
        })
        assert r.status_code == 200, r.text
        # 待揽收 → 处理中,订单状态不变
        assert r.json()["order_status"] == "处理中"
        assert r.json()["status"] == "待揽收"


async def test_create_shipment_can_jump_straight_to_signed(admin_token):
    """建单时可直接选终态,订单被真的推过去。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": o["order_no"], "tracking_no": "T-DRV-SIGN-1",
            "carrier": "京东物流", "status": "已签收",
            "shipped_at": "2026-09-01", "signed_at": "2026-09-05",
        })
        assert r.status_code == 200, r.text
        assert r.json()["order_status"] == "已签收"


async def test_create_shipment_on_dirty_signed_order_400(admin_token):
    """脏数据兜底:订单是「已签收」却没有运单 → 400 并给出可读文案。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        await _force_order_status(o["order_no"], "已签收")
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": o["order_no"], "tracking_no": "T-DRV-DIRTY-1",
            "carrier": "京东物流",
        })
        assert r.status_code == 400, r.text
        assert "处理中" in r.json()["detail"]


async def test_edit_shipment_drives_order_status(admin_token):
    """运单状态一变,订单跟着变;签收日期直接跟随运单。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        sid = (await _new_shipment(c, admin_token, o["order_no"],
                                   "T-DRV-EDIT-1", status="待揽收"))["id"]

        r = await c.put(f"/api/admin/logistics/{sid}", headers=_bearer(admin_token),
                        json={"status": "已揽收"})
        assert r.status_code == 200, r.text
        assert r.json()["order_status"] == "已发货"

        r = await c.put(f"/api/admin/logistics/{sid}", headers=_bearer(admin_token),
                        json={"status": "已签收", "shipped_at": "2026-09-01",
                              "signed_at": "2026-09-05"})
        assert r.status_code == 200, r.text
        assert r.json()["order_status"] == "已签收"
        assert r.json()["signed_at"] == "2026-09-05"


async def test_order_signed_date_mirrors_shipment(admin_token):
    """订单的签收日期 = 运单的签收日期,不各算各的。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        sid = (await _new_shipment(c, admin_token, o["order_no"], "T-DRV-MIR-1"))["id"]
        await c.put(f"/api/admin/logistics/{sid}", headers=_bearer(admin_token),
                    json={"status": "已签收", "shipped_at": "2026-09-01",
                          "signed_at": "2026-09-05"})
        async with AsyncSessionLocal() as s:
            d = (await s.execute(
                select(Order.signed_date).where(Order.order_no == o["order_no"])
            )).scalar_one()
        assert d.isoformat() == "2026-09-05"


async def test_tracking_no_conflict_returns_409_not_500(admin_token):
    """钉住 Order 查询的落点:放在 setattr 之后会变成 500。"""
    async with _client() as c:
        a = await _new_order(c, admin_token)
        b = await _new_order(c, admin_token)
        await _new_shipment(c, admin_token, a["order_no"], "T-DRV-DUP-A")
        rb = await _new_shipment(c, admin_token, b["order_no"], "T-DRV-DUP-B")
        # 把 b 的运单号改成 a 的 -> 撞唯一键
        r = await c.put(f"/api/admin/logistics/{rb['id']}",
                        headers=_bearer(admin_token), json={"tracking_no": "T-DRV-DUP-A"})
        assert r.status_code == 409, f"期望 409 实际 {r.status_code}: {r.text}"


async def test_delete_shipment_resets_order(admin_token):
    """删运单 -> 订单回「处理中」,签收日期清空。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": o["order_no"], "tracking_no": "T-DRV-DEL-1",
            "carrier": "京东物流", "status": "已签收",
            "shipped_at": "2026-09-01", "signed_at": "2026-09-05",
        })
        sid = r.json()["id"]
        assert r.json()["order_status"] == "已签收"

        r = await c.delete(f"/api/admin/logistics/{sid}", headers=_bearer(admin_token))
        assert r.status_code == 200, r.text

        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(Order.status, Order.signed_date)
                .where(Order.order_no == o["order_no"])
            )).one()
        assert row[0] == "处理中"
        assert row[1] is None


async def test_shipment_drives_order_through_full_flow(admin_token):
    """建单(处理中) → 建运单(待揽收) → 逐级推进 → 订单一路跟随。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        assert o["status"] == "处理中"

        r = await c.post("/api/admin/logistics", headers=_bearer(admin_token), json={
            "order_no": o["order_no"], "tracking_no": "T-DRV-FLOW-1",
            "carrier": "京东物流", "status": "待揽收",
        })
        sid = r.json()["id"]
        assert r.json()["order_status"] == "处理中"

        for ship_status, want_order in [("已揽收", "已发货"),
                                        ("运输中", "已发货"),
                                        ("派送中", "已发货")]:
            r = await c.put(f"/api/admin/logistics/{sid}", headers=_bearer(admin_token),
                            json={"status": ship_status})
            assert r.status_code == 200, r.text
            assert r.json()["order_status"] == want_order, ship_status

        # ⚠️ shipped_at 必须在这条 PUT 里补上 —— 规则 B 要求「已签收的运单必须有发货时间」,
        #    而这条流程前四步只推了状态、一次都没设过发货日期。少了它这里是 400
        #    (旧写法漏了,实测红)。
        r = await c.put(f"/api/admin/logistics/{sid}", headers=_bearer(admin_token),
                        json={"status": "已签收", "shipped_at": "2026-09-20",
                              "signed_at": "2026-09-26"})
        assert r.status_code == 200, r.text
        assert r.json()["order_status"] == "已签收"
        assert r.json()["signed_at"] == "2026-09-26"


@pytest.mark.parametrize("ship_status,want_order", [
    ("异常", "售后处理中"),
    ("退货中", "售后处理中"),
    ("已退货", "已退款·交易关闭"),
])
async def test_aftersale_shipment_states_drive_order(admin_token, ship_status, want_order):
    """售后三态:异常/退货中 → 售后处理中;已退货 → 已退款·交易关闭。"""
    async with _client() as c:
        o = await _new_order(c, admin_token)
        r = await _new_shipment(c, admin_token, o["order_no"],
                                f"T-DRV-AF-{ship_status}", status=ship_status,
                                shipped_at="2026-09-01")
        assert r["order_status"] == want_order


async def test_signed_without_date_mirrors_to_order(admin_token):
    """运单「已签收、日期不详」时,订单也跟着是 NULL —— 两边一致。

    ⚠️ 要造出「日期不详」必须【显式传 signed_at: null】。建单路径的 fill_today=True
       会把"没传日期"补成当天(规则 A,由 test_create_signed_fills_today 钉住,
       那是有意行为),所以只靠"不传"造不出这个状态 —— 实测会拿到今天的日期。
    """
    async with _client() as c:
        o = await _new_order(c, admin_token)
        r = await _new_shipment(c, admin_token, o["order_no"], "T-DRV-NODATE-1",
                                status="已签收", shipped_at="2026-09-01",
                                signed_at=None)
        assert r["order_status"] == "已签收"
        assert r["signed_at"] is None
        async with AsyncSessionLocal() as s:
            d = (await s.execute(
                select(Order.signed_date).where(Order.order_no == o["order_no"])
            )).scalar_one()
        assert d is None
