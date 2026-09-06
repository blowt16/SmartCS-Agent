"""product_stock_lookup tool 测试（mock AsyncSessionLocal，不连库）——sku-only 契约（2026-09-06 方案 A）

职责定位：rag_retrieval 的辅助工具，sku 必填精确查询；名称/品类模糊通道已移除。
覆盖：
- 查询拼接（sku 等值 + 大写归一）
- 三态返回（ok/empty/error）+ 入参校验（sku 空 → invalid_argument）+ 错误分类
- 参数 schema 完整性（sku 必填唯一参数）
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


def _row(name=DOC_NAME, category="智能门锁", price="899.00", stock=156, updated=None, sku="JD-LCK-001"):
    return ProductPriceStock(
        sku=sku,
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


# ---------- 查询拼接（sku 精确 + 大写归一） ----------


async def test_sku_exact_match_sql(db_mock):
    db_mock["set_rows"]([_row()])
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "JD-LCK-001"}))
    sql = _sql_of(db_mock)
    assert "product_price_stock.sku = 'JD-LCK-001'" in sql
    assert out["status"] == "ok" and out["count"] == 1


async def test_sku_uppercase_normalized(db_mock):
    """大小写归一：jd-lck-001 → JD-LCK-001（防大小写差异稳定 empty）。"""
    db_mock["set_rows"]([_row()])
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "  jd-lck-001  "}))
    assert "product_price_stock.sku = 'JD-LCK-001'" in _sql_of(db_mock)
    assert out["status"] == "ok"


async def test_sku_miss_returns_empty(db_mock):
    db_mock["set_rows"]([])
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "JD-LCK-999"}))
    assert out["status"] == "empty" and out["count"] == 0
    assert "已下线" in out["message"] or "未收录" in out["message"]  # 编码不存在语义
    assert "编造" in out["message"]  # 防编造指引


# ---------- 三态返回 ----------


async def test_ok_records_fields(db_mock):
    db_mock["set_rows"]([_row()])
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "JD-LCK-001"}))
    assert out["status"] == "ok"
    rec = out["data"][0]
    assert rec["sku"] == "JD-LCK-001"
    assert rec["product_name"] == DOC_NAME  # 展示/身份核对字段保留（非检索键）
    assert rec["category"] == "智能门锁"
    assert rec["current_price"] == 899.0  # Decimal → float
    assert rec["stock_quantity"] == 156
    assert rec["updated_at"] == "2026-08-30T14:23:11"


async def test_stock_zero_kept(db_mock):
    db_mock["set_rows"]([_row(stock=0)])
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "JD-LCK-001"}))
    assert out["data"][0]["stock_quantity"] == 0  # 0=无货，保留原值


async def test_invalid_argument_empty_sku(db_mock):
    for bad in ("", "   "):
        out = json.loads(await product_stock_lookup.ainvoke({"sku": bad}))
        assert out["status"] == "error"
        assert out["error_type"] == "invalid_argument"
        assert out["retryable"] is True
        assert "rag_retrieval" in out["message"]  # 引导先 rag 拿编码


# ---------- 错误分类与重试 ----------


async def test_error_db_connection_retried_then_error(db_mock):
    db_mock["session"].execute.side_effect = OperationalError("s", {}, Exception("conn"))
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "JD-LCK-001"}))
    assert db_mock["session"].execute.call_count == 2  # 瞬时错误自动重试 1 次
    assert out["status"] == "error" and out["error_type"] == "db_connection"
    assert out["retryable"] is False
    assert "转人工" in out["message"]  # 不抛异常，错误信息给 LLM


async def test_error_permanent_no_retry(db_mock):
    db_mock["session"].execute.side_effect = ProgrammingError("s", {}, Exception("no table"))
    out = json.loads(await product_stock_lookup.ainvoke({"sku": "JD-LCK-001"}))
    assert db_mock["session"].execute.call_count == 1  # 永久错误不重试
    assert out["error_type"] == "db_config"


# ---------- 参数 schema 完整性 ----------


def test_args_schema_sku_only_required():
    schema = product_stock_lookup.args_schema.model_json_schema()
    props = schema["properties"]
    assert schema["required"] == ["sku"]                      # sku 必填唯一参数
    assert set(props.keys()) == {"sku"}                        # 无 product_name/category/limit
    assert "rag_retrieval" in props["sku"]["description"]      # 来源引导（【商品编码:】前缀）
    assert "猜测检索" in props["sku"]["description"]            # 禁止按名称猜测检索语义


def test_tool_description_complete():
    desc = product_stock_lookup.description
    assert "辅助" in desc  # 职责定位：rag 的辅助补全工具
    assert "何时不要使用本工具" in desc
    assert "未检索到" in desc  # rag 未命中不调用的负向指引
    assert "不得" not in desc or "猜" in desc  # 禁止按名称猜测语义在 docstring 或 schema 描述
