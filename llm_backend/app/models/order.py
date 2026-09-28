from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, func

from app.core.database import Base


class Order(Base):
    """订单表:项目无下单链路,数据由 seed_orders.py 从 product_price_stock 生成"""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(32), nullable=False, unique=True)   # ORD-001 形式
    sku = Column(String(32), nullable=False)                     # 关联商品(product_price_stock.sku)
    product_name = Column(String(255), nullable=False)           # 下单时快照,商品改名不影响历史订单
    category = Column(String(50), nullable=False)
    buyer_name = Column(String(50), nullable=False)              # 展示名
    buyer_code = Column(String(20), nullable=True)               # P001 形式
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Numeric(10, 2), nullable=False)              # 下单金额
    # 状态由运单派生,不可直接编辑(见 app/services/order_status.py):
    # 处理中/已发货/已签收/售后处理中/已退款·交易关闭
    status = Column(String(20), nullable=False, default="处理中")
    order_date = Column(Date, nullable=False)
    # 签收日期:仅 status='已签收' 时有值;订单改回其它状态时由接口强制清空
    signed_date = Column(Date, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
