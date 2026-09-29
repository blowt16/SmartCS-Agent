"""订单状态 ⇄ 运单状态映射的真值表与完备性守卫。

本文件【不连库、不起 app】—— 纯函数测试,毫秒级。
目的是把 spec §2.1 那张映射表钉死,并防止将来加了状态却忘了补映射。
"""
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from app.services.order_status import (  # noqa: E402
    NO_SHIPMENT_ORDER_STATUS,
    ORDER_STATUSES,
    SHIPMENT_STATUSES,
    SHIPMENT_TO_ORDER,
    derive_order_fields,
    derive_order_status,
    sync_order_from_shipment,
)


@pytest.mark.parametrize("shipment_status,expected", [
    ("待揽收", "处理中"),
    ("已揽收", "已发货"),
    ("运输中", "已发货"),
    ("派送中", "已发货"),
    ("已签收", "已签收"),
    ("异常", "售后处理中"),
    ("退货中", "售后处理中"),
    ("已退货", "已退款·交易关闭"),
])
def test_derive_order_status_truth_table(shipment_status, expected):
    assert derive_order_status(shipment_status) == expected


def test_every_shipment_status_has_mapping():
    """加了运单状态却忘了补映射 -> 这里红。"""
    assert set(SHIPMENT_TO_ORDER) == set(SHIPMENT_STATUSES)


def test_every_order_status_is_reachable():
    """纯派生下,没有运单状态能触发的订单状态就是死值 —— 不许存在。"""
    reachable = set(SHIPMENT_TO_ORDER.values()) | {NO_SHIPMENT_ORDER_STATUS}
    assert reachable == set(ORDER_STATUSES)


def test_no_shipment_falls_back_to_processing():
    assert NO_SHIPMENT_ORDER_STATUS == "处理中"


def test_signed_date_only_follows_signed_status():
    """signed_at 只在订单落到「已签收」时透传,其余一律 None。"""
    d = date(2026, 9, 20)
    assert derive_order_fields("已签收", d) == ("已签收", d)
    assert derive_order_fields("运输中", d) == ("已发货", None)
    assert derive_order_fields("已退货", d) == ("已退款·交易关闭", None)
    # ⚠️ 已签收 + 日期不详 -> 订单也跟着 NULL,【不补今天】。
    #    旧 orders.py 的 `data.get(...) or today` 会在这里补当天,别改回去。
    assert derive_order_fields("已签收", None) == ("已签收", None)


def test_no_shipment_clears_status_and_date():
    d = date(2026, 9, 20)
    assert derive_order_fields(None, d) == ("处理中", None)


def test_unknown_shipment_status_raises():
    """映射表缺项时必须立刻炸,不能静默返回 None 把订单状态写成空值。"""
    with pytest.raises(KeyError, match="不存在的状态"):
        derive_order_status("不存在的状态")


def test_sync_order_from_shipment_writes_both_fields():
    """钉住包装本身:属性名写错在 SQLAlchemy 上是静默 no-op,不会有别的测试发现。"""
    order = SimpleNamespace(status=None, signed_date=None)
    d = date(2026, 9, 20)

    sync_order_from_shipment(order, "已签收", d)
    assert (order.status, order.signed_date) == ("已签收", d)

    sync_order_from_shipment(order, None, d)
    assert (order.status, order.signed_date) == ("处理中", None)
