from sqlalchemy import Column, DateTime, Integer, Numeric, String, UniqueConstraint, func

from app.core.database import Base


class ProductPriceStock(Base):
    """商品动态信息表:价格与库存(动态数据,与 docx 静态知识分层)"""

    __tablename__ = "product_price_stock"
    __table_args__ = (
        # sku 为确定性对齐/join 主键(不可变身份,upsert 冲突键);product_name 为可变展示名
        # (1:1 映射由 TSV 源头保证,改名不产生新身份,见 SPEC_SKU_ALIGNMENT D6/D7)
        UniqueConstraint("sku", name="uq_product_price_stock_sku"),
        UniqueConstraint("product_name", name="uq_product_price_stock_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String(32), nullable=False)          # 商品编码:与 TSV sku 列逐字符一致(查询/join 键)
    product_name = Column(String(255), nullable=False)  # 与 TSV 商品名称完全一致(展示名/名称模糊通道)
    category = Column(String(50), nullable=False)
    current_price = Column(Numeric(10, 2), nullable=False)
    stock_quantity = Column(Integer, nullable=False)     # 0 = 无货
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
