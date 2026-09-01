"""product_stock_lookup tool 测试（mock AsyncSessionLocal，不连库）

覆盖 SPEC_PRODUCT_STOCK_TOOL §8 步骤 3 + §9 验证方案：
- 查询参数拼接（ILIKE 去空格/通配符转义/category/limit 钳制/updated_at 降序）
- 三态返回（ok/empty/error）+ 入参校验 + 错误分类
- 参数 schema 完整性（model_json_schema 含 description 与 limit 范围）
"""
import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.models.product_price_stock import ProductPriceStock
from app.tools.product_stock_tool import product_stock_lookup

DOC_NAME = "京东京造 智能门锁 全自动3D人脸识别"


def _row(name=DOC_NAME, category="智能门锁", price="899.00", stock=156, updated=None):
    return ProductPriceStock(
        product_name=name,
        category=category,
        current_price=Decimal(price),
        stock_quantity=stock,
        updated_at=updated or datetime(2026, 8, 30, 14, 23, 11),
    )


@pytest.fixture
def db_mock(monkeypatch):
    """patch AsyncSessionLocal：捕获 execute 的 stmt，scalars().all() 返回可配置 rows"""
    session = AsyncMock()
    result = Mock()  # 真实 Result.scalars()/all() 是同步方法 → 普通 Mock（AsyncMock 会返回 coroutine）
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    factory = Mock(return_value=ctx)  # 普通 Mock：调用同步返回 ctx（AsyncMock 调用返回 coroutine，不支持 async with）
    monkeypatch.setattr("app.tools.product_stock_tool.AsyncSessionLocal", factory)

    state = {"session": session, "result": result}
    state["set_rows"] = lambda rows: setattr(
        result.scalars.return_value.all, "return_value", rows
    )
    return state


def _sql_of(state) -> str:
    stmt = state["session"].execute.call_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# ---------- 查询参数拼接 ----------


async def test_ilike_whitespace_normalized(db_mock):
    db_mock["set_rows"]([_row()])
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "京东京造 智能门锁"}))
    sql = _sql_of(db_mock)
    assert "replace(product_price_stock.product_name, ' ', '')" in sql
    assert "ILIKE '%%京东京造智能门锁%%'" in sql  # 两侧空格压缩后连续匹配（literal_binds 下 % 渲染为 %%）
    assert "ESCAPE" in sql
    assert out["status"] == "ok" and out["count"] == 1


async def test_wildcard_escaped(db_mock):
    db_mock["set_rows"]([])
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "100%"}))
    sql = _sql_of(db_mock)
    assert "\\\\" in sql  # 通配符被转义为 \%（编译后 \\），防注入式全表匹配
    assert out["status"] == "empty"


async def test_category_filter(db_mock):
    db_mock["set_rows"]([])
    await product_stock_lookup.ainvoke({"product_name": "门锁", "category": "智能门锁"})
    assert "product_price_stock.category = '智能门锁'" in _sql_of(db_mock)


async def test_limit_over_20_rejected_by_schema():
    """超界 limit 被 schema 层（Field le=20）拒绝——模型端修正参数重试"""
    with pytest.raises(Exception):
        await product_stock_lookup.ainvoke({"product_name": "门锁", "limit": 100})


async def test_limit_clamped_in_code(db_mock):
    """绕过 schema 直接调函数：代码层钳制 1~20 兜底（防绕过 schema 的直调）"""
    db_mock["set_rows"]([])
    await product_stock_lookup.coroutine(product_name="门锁", limit=100)  # coroutine=原始函数，绕过 schema
    assert "LIMIT 20" in _sql_of(db_mock)


async def test_limit_str_number_coerced(db_mock):
    db_mock["set_rows"]([])
    await product_stock_lookup.ainvoke({"product_name": "门锁", "limit": "20"})
    assert "LIMIT 20" in _sql_of(db_mock)


async def test_limit_invalid_rejected_by_schema():
    """非数字 limit 被 schema 层（int Field）拦截，不进函数"""
    with pytest.raises(Exception):
        await product_stock_lookup.ainvoke({"product_name": "门锁", "limit": "abc"})


async def test_order_by_updated_at_desc(db_mock):
    db_mock["set_rows"]([])
    await product_stock_lookup.ainvoke({"product_name": "门锁"})
    assert "ORDER BY product_price_stock.updated_at DESC" in _sql_of(db_mock)


# ---------- 三态返回 ----------


async def test_ok_records_fields(db_mock):
    db_mock["set_rows"]([_row()])
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "门锁"}))
    assert out["status"] == "ok"
    rec = out["data"][0]
    assert rec["product_name"] == DOC_NAME
    assert rec["category"] == "智能门锁"
    assert rec["current_price"] == 899.0  # Decimal → float
    assert rec["stock_quantity"] == 156
    assert rec["updated_at"] == "2026-08-30T14:23:11"


async def test_stock_zero_kept(db_mock):
    db_mock["set_rows"]([_row(stock=0)])
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "门锁"}))
    assert out["data"][0]["stock_quantity"] == 0  # 0=无货，保留原值


async def test_empty_result_with_advice(db_mock):
    db_mock["set_rows"]([])
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "冰箱"}))
    assert out["status"] == "empty" and out["count"] == 0
    assert "rag_retrieval" in out["message"]  # 静态信息引导


async def test_invalid_argument(db_mock):
    for bad in ("", "   "):
        out = json.loads(await product_stock_lookup.ainvoke({"product_name": bad}))
        assert out["status"] == "error"
        assert out["error_type"] == "invalid_argument"
        assert out["retryable"] is True


# ---------- 错误分类与重试 ----------


async def test_error_db_connection_retried_then_error(db_mock):
    db_mock["session"].execute.side_effect = OperationalError("s", {}, Exception("conn"))
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "门锁"}))
    assert db_mock["session"].execute.call_count == 2  # 瞬时错误自动重试 1 次
    assert out["status"] == "error" and out["error_type"] == "db_connection"
    assert out["retryable"] is False
    assert "转人工" in out["message"]  # 不抛异常，错误信息给 LLM


async def test_error_permanent_no_retry(db_mock):
    db_mock["session"].execute.side_effect = ProgrammingError("s", {}, Exception("no table"))
    out = json.loads(await product_stock_lookup.ainvoke({"product_name": "门锁"}))
    assert db_mock["session"].execute.call_count == 1  # 永久错误不重试
    assert out["error_type"] == "db_config"


# ---------- 参数 schema 完整性 ----------


def test_args_schema_complete():
    schema = product_stock_lookup.args_schema.model_json_schema()
    props = schema["properties"]
    assert schema["required"] == ["product_name"]
    assert props["limit"]["minimum"] == 1 and props["limit"]["maximum"] == 20
    assert props["limit"]["default"] == 5
    assert "泛查询" in props["limit"]["description"]  # limit 意图语义指引
    assert "智能门锁" in props["category"]["description"]  # 品类示例
    assert "门锁" in props["product_name"]["description"]  # 命中示例


def test_tool_description_complete():
    desc = product_stock_lookup.description
    assert "何时不要使用本工具" in desc  # 负向段
    assert "泛查询" in desc  # 列表意图段
