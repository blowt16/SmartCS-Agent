from sqlalchemy import Column, DateTime, Integer, Numeric, String, UniqueConstraint, func

from app.core.database import Base


class ProductPriceStock(Base):
    """商品动态信息表:价格与库存(动态数据,与 docx 静态知识分层)"""

    __tablename__ = "product_price_stock"
    __table_args__ = (
        UniqueConstraint("product_name", name="uq_product_price_stock_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_name = Column(String(255), nullable=False)   # 与 TSV 商品名称完全一致(查询/join 键)
    category = Column(String(50), nullable=False)
    current_price = Column(Numeric(10, 2), nullable=False)
    stock_quantity = Column(Integer, nullable=False)     # 0 = 无货
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
