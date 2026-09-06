"""价格解析与库存填充规则测试(纯函数,不连库)"""
from decimal import Decimal

import pytest

from scripts.import_product_price_stock import assign_stock, parse_price, validate_sku


def test_parse_price_single():
    assert parse_price("约5261") == Decimal("5261.00")


def test_parse_price_range_mean():
    assert parse_price("约4712-5154") == Decimal("4933.00")


def test_parse_price_with_suffix():
    assert parse_price("5274(券后)") == Decimal("5274.00")


def test_parse_price_dash_means_empty():
    assert parse_price("—") is None
    assert parse_price("") is None


def test_parse_price_double_range():
    assert parse_price("约791-872 元") == Decimal("831.50")


def test_assign_stock_zero_first_in_category():
    assert assign_stock("智能晾衣架", True) == 0
    assert assign_stock("智能门锁", True) == 0


def test_assign_stock_low_first_in_category():
    assert assign_stock("电动智能沙发", True) == 2
    assert assign_stock("智能床垫", True) == 3
    assert assign_stock("电动升降桌", True) == 5


def test_assign_stock_default():
    assert assign_stock("电动智能沙发", False) == 50
    assert assign_stock("智能窗帘", True) == 50


# ==================== sku 结构性校验(validate_sku) ====================

def _row(sku="JD-LCK-001", name="小米智能门锁M30 掌静脉版"):
    # 键名与 read_tsv_rows 产物一致(SQLAlchemy 列名 product_name)
    return {"sku": sku, "product_name": name}


def test_validate_sku_pass():
    rows = [_row("JD-LCK-001", "A"), _row("JD-SOF-002", "B")]
    validate_sku(rows)  # 不抛即通过


def test_validate_sku_empty():
    with pytest.raises(ValueError, match="空 sku"):
        validate_sku([_row(sku="")])


def test_validate_sku_bad_format():
    with pytest.raises(ValueError, match="格式非法"):
        validate_sku([_row(sku="12345")])
    with pytest.raises(ValueError, match="格式非法"):
        validate_sku([_row(sku="JD-LCK-ABC")])


def test_validate_sku_duplicate_sku():
    with pytest.raises(ValueError, match="sku 重复"):
        validate_sku([_row("JD-LCK-001", "A"), _row("JD-LCK-001", "B")])


def test_validate_sku_duplicate_name():
    with pytest.raises(ValueError, match="商品名重复"):
        validate_sku([_row("JD-LCK-001", "同名"), _row("JD-SOF-002", "同名")])
