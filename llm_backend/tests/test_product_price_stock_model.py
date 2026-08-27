"""模型定义测试(不连库,校验表名/约束/字段声明)"""
from app.models.product_price_stock import ProductPriceStock
from sqlalchemy import Integer, Numeric, String, UniqueConstraint


def test_table_name():
    assert ProductPriceStock.__tablename__ == "product_price_stock"


def test_unique_constraint():
    names = [c.name for c in ProductPriceStock.__table__.constraints
             if isinstance(c, UniqueConstraint)]
    assert "uq_product_price_stock_name" in names


def test_price_type():
    col = ProductPriceStock.__table__.c.current_price
    assert isinstance(col.type, Numeric)
    assert not col.nullable
