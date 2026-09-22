from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func

from app.core.database import Base


class Ticket(Base):
    """工单表:本轮数据由 seed_tickets.py 生成;conversation_id 为将来接驳对话流预留"""

    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    ticket_no = Column(String(32), nullable=False, unique=True)  # TK-20260914161654
    user_query = Column(Text, nullable=False)                    # 用户原话(处理弹窗只读)
    summary = Column(String(200), nullable=True)                 # 摘要(弹窗 200 字计数)
    category = Column(String(50), nullable=True)                 # 售后服务/退货咨询/投诉/其他
    urgency = Column(String(10), nullable=True)                  # 低/中/高
    status = Column(String(20), nullable=False, default="待处理")  # 待处理/已解决
    detail = Column(Text, nullable=True)                         # 问题详情
    suggestion = Column(Text, nullable=True)                     # 处理建议
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="SET NULL"),
                             nullable=True)
    handler = Column(String(50), nullable=True)                  # 处理人
    resolved_at = Column(DateTime, nullable=True)                # 结单时间
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
