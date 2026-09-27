"""管理端请求 schema(5 个模块的入参校验)。"""
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    sku: str = Field(..., pattern=r"^JD-[A-Z]{3}-\d{3}$")
    product_name: str = Field(..., min_length=1, max_length=255)
    category: str = Field(..., min_length=1, max_length=50)
    current_price: Decimal = Field(..., gt=0, le=99999999.99)
    stock_quantity: int = Field(..., ge=0)


class ProductUpdate(BaseModel):
    product_name: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, min_length=1, max_length=50)
    current_price: Optional[Decimal] = Field(None, gt=0, le=99999999.99)
    stock_quantity: Optional[int] = Field(None, ge=0)


class OrderCreate(BaseModel):
    product_sku: str
    buyer_name: str = Field(..., min_length=1, max_length=50)
    buyer_code: Optional[str] = Field(None, max_length=20)
    user_id: Optional[int] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    status: str = Field("处理中", pattern=r"^(处理中|已发货|已送达|已签收)$")
    order_date: Optional[date] = None
    # 留空且 status='已签收' 时由接口补当天;status 不是已签收则一律存 NULL
    signed_date: Optional[date] = None


class OrderUpdate(BaseModel):
    buyer_name: Optional[str] = Field(None, min_length=1, max_length=50)
    buyer_code: Optional[str] = Field(None, max_length=20)
    user_id: Optional[int] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    status: Optional[str] = Field(None, pattern=r"^(处理中|已发货|已送达|已签收)$")
    order_date: Optional[date] = None
    signed_date: Optional[date] = None


class KnowledgeCommit(BaseModel):
    md5: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    original_filename: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class KnowledgeUpdate(BaseModel):
    description: Optional[str] = None
    status: Optional[str] = Field(None, pattern=r"^(enabled|disabled)$")


class TicketUpdate(BaseModel):
    summary: Optional[str] = Field(None, max_length=200)
    category: Optional[str] = Field(None, max_length=50)
    urgency: Optional[str] = Field(None, pattern=r"^(低|中|高)$")
    status: Optional[str] = Field(None, pattern=r"^(待处理|已解决)$")
    detail: Optional[str] = None
    suggestion: Optional[str] = None


# ==================== 物流:枚举常量与入参 ====================
# 常量只在这里定义一次,logistics.py 反向 import(方向:api → schemas,无循环导入)

SHIPPABLE_ORDER_STATUSES = ["已发货", "已送达", "已签收"]
ORDER_STATUSES = ["处理中", "已发货", "已送达", "已签收"]
SHIPMENT_STATUSES = ["待揽收", "已揽收", "运输中", "派送中", "已签收", "异常"]
CARRIERS = ["京东物流", "顺丰速运", "中通快递", "圆通速递",
            "申通快递", "韵达快递", "邮政EMS", "德邦快递"]
UNRECORDED = "未录入"          # 列表筛选用展示态,不是 shipments.status 的取值
TRACE_MAX = 2000              # 轨迹文本上限,与前端 TEXTAREA 的 maxlength 对齐

SHIPMENT_STATUS_PATTERN = "^(" + "|".join(SHIPMENT_STATUSES) + ")$"
CARRIER_PATTERN = "^(" + "|".join(CARRIERS) + ")$"


class ShipmentCreate(BaseModel):
    order_no: str = Field(..., min_length=1, max_length=32)
    tracking_no: str = Field(..., min_length=1, max_length=50)
    carrier: str = Field(..., pattern=CARRIER_PATTERN)
    status: str = Field("待揽收", pattern=SHIPMENT_STATUS_PATTERN)
    shipped_at: Optional[date] = None
    signed_at: Optional[date] = None
    trace: Optional[str] = Field(None, max_length=TRACE_MAX)


class ShipmentUpdate(BaseModel):
    tracking_no: Optional[str] = Field(None, min_length=1, max_length=50)
    carrier: Optional[str] = Field(None, pattern=CARRIER_PATTERN)
    status: Optional[str] = Field(None, pattern=SHIPMENT_STATUS_PATTERN)
    shipped_at: Optional[date] = None
    signed_at: Optional[date] = None
    trace: Optional[str] = Field(None, max_length=TRACE_MAX)
    # 注意:没有 order_no —— 订单号不可改
