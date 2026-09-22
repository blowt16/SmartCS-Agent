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
    status: str = Field("处理中", pattern=r"^(处理中|已发货|已送达)$")
    order_date: Optional[date] = None


class OrderUpdate(BaseModel):
    buyer_name: Optional[str] = Field(None, min_length=1, max_length=50)
    buyer_code: Optional[str] = Field(None, max_length=20)
    user_id: Optional[int] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    status: Optional[str] = Field(None, pattern=r"^(处理中|已发货|已送达)$")
    order_date: Optional[date] = None


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
