from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, String, Text, func

from app.core.database import Base


class Shipment(Base):
    """运单表:一个订单一个运单(1:1),轨迹以文本存本表。数据由管理端手工录入或 seed_shipments.py 生成"""

    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True, index=True)
    tracking_no = Column(String(50), nullable=False, unique=True)  # 运单号,来自承运商,手工录入
    # 关联订单用业务单号而非 order_id:orders.order_no 已是 UNIQUE 约束,可直接当外键目标
    # unique=True 不能省 —— 这是"一单一个运单"的强制点,且它自带唯一索引
    # (PostgreSQL 不给外键的引用侧自动建索引,漏了按订单号查物流就全表扫)
    order_no = Column(
        String(32),
        ForeignKey("orders.order_no", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    carrier = Column(String(50), nullable=False)                  # 8 选 1
    status = Column(String(20), nullable=False, default="待揽收")   # 8 选 1,见 app/services/order_status.py
    shipped_at = Column(Date, nullable=True)                      # 发货时间
    # 签收时间:仅 status='已签收' 时有值;其余状态由接口强制置 NULL
    # (orders.signed_date 是本值投影过去的,不独立计算)
    signed_at = Column(Date, nullable=True)
    # 轨迹:一行一个节点,格式强制为「YYYY-MM-DD HH:MM | 地点 | 描述」(表内不做约束,由接口校验)
    trace = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
