"""管理端物流接口测试(SPEC: docs/superpowers/specs/2026-09-27-管理端物流模块-design.md)。

覆盖 §4 的 4 个端点与 §4.5 的三条校验规则。

前置:必须先跑 `python scripts/init_db.py` 建出 shipments 表,否则数据类用例
      全部 ERROR(relation "shipments" does not exist)。

清理策略:本文件造的订单 buyer_name 一律 "测试买家",由 autouse 的 fixture
         在用例前后各清一次(前一次兜住上次跑挂留下的残留,不碰种子数据)。
"""
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime
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
