"""工单种子数据(8 条,幂等,可重复执行)。

用法:
  python scripts/seed_tickets.py

ticket_no 的日期部分写成模块常量 BASE_DATE(不取运行日):若取运行日,每天重跑 8 条
全部是新号 → 全部 INSERT 成功 → 表变 16 条,而"演示前重跑一次种子"是最自然的操作。
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent          # llm_backend
sys.path.insert(0, str(ROOT_DIR))
import app.core.database  # noqa: E402 —— Windows Selector 事件循环补丁

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.ticket import Ticket  # noqa: E402

logger = get_logger(service="seed_tickets")

# 对齐参考图的 TK-20260914... 形式;钉成常量才能跨天稳定命中幂等键
BASE_DATE = "20260914"

HANDLER = "admin_test"

# (user_query, summary, category, urgency, status, detail, suggestion)
TICKETS = [
    (
        "如果我买不合适，怎么去退货呢", "智能手表S3退货流程咨询", "售后服务", "中", "待处理",
        "用户已选定智能手表S3（¥699），关注购买后不合适时的退货政策与操作流程。",
        "转接人工客服，提供清晰退货指引：7天无理由退换（商品未拆封/配件齐全）、"
        "寄回方式（平台预付快递单）、退款时效（签收后1-3工作日原路退回），并主动协助生成退货单。",
    ),
    (
        "ORD-001这个", "用户问题需人工处理", "其他", "中", "已解决",
        "用户就订单 ORD-001 的售后事宜发起咨询，未明确具体诉求，需人工核实订单详情后答复。",
        "已核对订单信息并回访用户，确认其诉求已完成处理。",
    ),
    (
        "我要投诉", "用户投诉，需人工处理", "投诉", "中", "待处理",
        "用户表达投诉意愿但未说明具体事由，需人工介入了解情况并安抚。",
        "建议人工客服主动外呼，先致歉并倾听诉求，确认赔偿或补救方案后登记跟进。",
    ),
    (
        "怎么退货？", "用户咨询退货流程", "退货咨询", "低", "待处理",
        "用户咨询通用退货流程，未涉及具体订单，关注申请入口与时效。",
        "提供退货入口指引：订单详情页→申请售后→选择退货原因→提交；"
        "说明七天无理由时效与运费承担规则。",
    ),
    (
        "订单一直没发货，都一周了", "订单发货延迟咨询", "售后服务", "高", "待处理",
        "用户下单已逾一周仍未发货，情绪较为急迫，需核实库存与仓储状态。",
        "优先人工跟进：核查订单实际状态与仓库出库记录，给出发货时间承诺；"
        "若确无库存，主动提供换货或退款方案。",
    ),
    (
        "买贵了能退差价吗", "价格保护咨询", "售后服务", "低", "已解决",
        "用户咨询下单后商品降价是否能退差价，关注价保申请条件与期限。",
        "已告知价格保护规则：签收后 7 日内可申请，需提供新价格截图，审核通过后原路退回差价。",
    ),
    (
        "收到货是坏的", "商品破损投诉", "投诉", "中", "待处理",
        "用户反馈签收商品存在破损，需核实是运输损坏还是来货瑕疵。",
        "请用户提供开箱照片与破损细节，走质量问题退换通道，运费由平台承担，"
        "并同步反馈仓储与物流方排查。",
    ),
    (
        "发票怎么开", "发票开具咨询", "其他", "低", "已解决",
        "用户咨询电子发票的开具方式与获取时间。",
        "已指引：订单详情页→申请开票→选择发票类型与抬头→提交；"
        "电子发票一般 24 小时内开具，可在订单页下载。",
    ),
]


async def main() -> int:
    async with AsyncSessionLocal() as s:
        # 朴素 UTC:列是 timestamp without time zone,全库按 UTC 读。
        # 写本地 datetime.now() 会被当 UTC 读 → 列表时间早 8 小时。
        base = datetime.now(timezone.utc).replace(tzinfo=None)

        rows = []
        for index, (query, summary, category, urgency, status, detail, suggestion) in enumerate(TICKETS):
            # 每条往前推 6 小时,保证时间戳互不相同(同事务内 now() 是事务开始时间,会完全一样)
            created_at = base - timedelta(hours=index * 6)
            rows.append({
                "ticket_no": f"TK-{BASE_DATE}{index + 1:06d}",
                "user_query": query,
                "summary": summary,
                "category": category,
                "urgency": urgency,
                "status": status,
                "detail": detail,
                "suggestion": suggestion,
                "conversation_id": None,          # 本轮全 NULL,字段为将来接驳对话流预留
                "handler": HANDLER if status == "已解决" else None,
                "resolved_at": created_at + timedelta(hours=2) if status == "已解决" else None,
                "created_at": created_at,
                "updated_at": created_at,
            })

        stmt = pg_insert(Ticket).values(rows)
        # 幂等键 ticket_no:日期是常量,跨天重跑也稳定命中
        stmt = stmt.on_conflict_do_nothing(index_elements=[Ticket.ticket_no])
        await s.execute(stmt)
        await s.commit()

        total = (await s.execute(select(func.count()).select_from(Ticket))).scalar()
        pending = (await s.execute(
            select(func.count()).select_from(Ticket).where(Ticket.status == "待处理")
        )).scalar()
        logger.info("工单种子完成: 定义 {} 条,表内共 {} 条(待处理 {})", len(rows), total, pending)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
