"""运单种子数据(幂等 upsert,可重复执行)。

用法:
  python scripts/seed_shipments.py

覆盖范围:只给「已送达」和「已签收」的订单建运单,「已发货」的【故意不建】——
页面上要能同时看到「已录物流」与「未录入 + 补录按钮」两种状态,补录流程才演示得了。

幂等策略是【upsert 覆盖】而不是"已存在则跳过"(与 seed_orders.py 同款)。三个后果:
  1. 覆盖对象是【订单号落在种子范围内的所有运单】,不只种子自己插的那些 ——
     管理员手工给这些订单建的运单,status/shipped_at/signed_at/trace 同样会被覆盖
     (tracking_no / carrier 不在 set_ 里,保留)
  2. tracking_no 不在 set_ 里 -> 上面的"确定性生成"只在首次插入成立,
     重跑不会把漂移的运单号改回来(有意:管理员手改的号应被尊重)
  3. 跨订单撞号【可能】让整个脚本中止 —— 但要看目标订单有没有已存在的行(实测确认):
     · 目标订单【没有】运单行 -> 走插入分支 -> 整条多行 INSERT 抛 UniqueViolation,
       单语句单事务 -> 全脚本失败,一行都不写
     · 目标订单【已有】运单行 -> 走 ON CONFLICT (order_no) 的 DO UPDATE 分支,
       【不报错也不中止】,只是种子想用的那个号静默没生效(库里保留原来的号)
     重跑场景多数是后者(种子上次已经写过),所以这条不是"一定会炸"而是"看情况"。
     概率都极低(JDV 前缀是种子专用),但记着这条排查方向
"""
import asyncio
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent          # llm_backend
sys.path.insert(0, str(ROOT_DIR))
import app.core.database  # noqa: E402 —— Windows Selector 事件循环补丁

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.api.admin.logistics import parse_trace  # noqa: E402
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.shipment import Shipment  # noqa: E402
from app.schemas.admin import CARRIERS  # noqa: E402

logger = get_logger(service="seed_shipments")

# 只覆盖这两个状态的订单;「已发货」留给页面演示「未录入」
SEED_ORDER_STATUSES = ["已送达", "已签收"]
# 加权抽样池:京东自营为主,所以京东物流占两席。
# ⚠️ 这是【抽样分布】不是【合法值枚举】—— 与 schemas.admin.CARRIERS(8 个合法承运商)
# 语义不同,不能合并;但成员必须都在枚举里(下面有断言,防止两处漂移)
CARRIER_POOL = ["京东物流", "京东物流", "顺丰速运", "中通快递"]
assert all(c in CARRIERS for c in CARRIER_POOL), "CARRIER_POOL 里有不在 CARRIERS 枚举里的值"

# 轨迹节点模板:状态 -> (小时偏移, 地点, 描述)
# 偏移必须 <= signed_at,且行数与节点数一一对应
NODES = {
    "派送中": [
        (0, "广州市", "已揽收"),
        (6, "广州转运中心", "到达转运中心"),
        (12, "广州转运中心", "运输中"),
        (18, "深圳市", "派送中"),
    ],
    "已签收": [
        (0, "广州市", "已揽收"),
        (6, "广州转运中心", "到达转运中心"),
        (12, "广州转运中心", "运输中"),
        (18, "深圳市", "派送中"),
        (22, "深圳市", "已签收"),
    ],
}

# 订单状态 -> 运单状态
SHIPMENT_STATUS = {"已送达": "派送中", "已签收": "已签收"}


def build_trace(shipped_at, shipment_status: str) -> str | None:
    """按运单状态生成三段式轨迹文本。

    ⚠️ 不能直接对 shipped_at(Date) 加 timedelta(hours=N) —— date + timedelta(hours=N)
    返回的还是 date,小时部分被直接截断,4 个偏移会塌成 2 个不同值。
    必须先把 Date 升成 datetime 再加偏移。
    """
    nodes = NODES.get(shipment_status)
    if not nodes:
        return None
    base = datetime.combine(shipped_at, time(hour=9))      # 9 点起揽收,比 0 点自然
    return "\n".join(
        f"{(base + timedelta(hours=h)):%Y-%m-%d %H:%M} | {loc} | {desc}"
        for h, loc, desc in nodes
    )


async def main() -> int:
    async with AsyncSessionLocal() as s:
        today = datetime.now(timezone.utc).date()
        orders = (await s.execute(
            select(Order.order_no, Order.status, Order.order_date)
            .where(Order.status.in_(SEED_ORDER_STATUSES))
            .order_by(Order.order_no)
        )).all()

        if not orders:
            # 用 error + return 1 而不是 warning + return 0 —— 与 seed_orders.py 一致。
            # 串在初始化脚本里时,「跳过」和「成功」必须能被调用方区分出来。
            logger.error("没有「已送达/已签收」的订单,请先跑 seed_orders.py")
            return 1

        rows = []
        for index, (order_no, order_status, order_date) in enumerate(orders):
            # 「订单号数字部分」确定性生成,tracking_no 相同则幂等
            digits = "".join(ch for ch in order_no if ch.isdigit()) or f"{index + 1:03d}"
            shipped_at = min(order_date + timedelta(days=1), today)
            shipment_status = SHIPMENT_STATUS[order_status]
            # 签收日与 seed_orders.py:73 的订单签收日取同一个偏移(+2 天),
            # 写成 +3 天会出现「订单签收日 9-22、运单签收日 9-23」的自相矛盾演示数据
            signed_at = min(order_date + timedelta(days=2), today) if order_status == "已签收" else None

            # ⚠️ 窗口不足一天就跳过,别硬种。
            # 当天重跑 seed_orders.py 刷新演示数据后(那是它自己文档里写明的用途),
            # 「昨天下的单 + 已签收」会被上面两个 min 同时钳到 today,
            # 于是 shipped_at == signed_at,而「已签收」模板的末节点偏移是 22h
            # (09:00 + 22h = 次日 07:00),必然越过签收日 -> 下面那句 parse_trace 抛错 ->
            # 整个脚本中止、一行都不写。
            # 那类单留在页面上演示「未录入 + 补录」,比种出一条自相矛盾的轨迹好。
            if signed_at and (signed_at - shipped_at).days < 1:
                logger.warning("跳过 {}:发货/签收窗口不足一天", order_no)
                continue

            # ⚠️ 同上,另一半:轨迹节点不能落到未来。
            # `shipped_at` 被 min(..., today) 钳到【今天】时(「今天下的单」),
            # 两个状态的模板末节点偏移(派送中 +18h、已签收 +22h,都从 09:00 起算)
            # 必然落到【明天】—— 演示数据不该声称一个还没发生的事件已经发生。
            # 已签收那一半由上面的窗口护栏挡住了;派送中【没有签收日可作上界】,
            # 所以在这里按同样思路单独挡一次。
            if shipped_at == today:
                logger.warning("跳过 {}:发货日被钳到今天,轨迹节点会落到未来", order_no)
                continue

            trace = build_trace(shipped_at, shipment_status)
            if trace:                                        # 种子直写库,绕过接口校验,自己先验一遍
                parse_trace(trace, shipped_at, signed_at)
            rows.append({
                "tracking_no": f"JDV{int(digits):010d}",
                "order_no": order_no,
                "carrier": CARRIER_POOL[index % len(CARRIER_POOL)],
                "status": shipment_status,
                "shipped_at": shipped_at,
                "signed_at": signed_at,
                "trace": trace,
            })

        stmt = pg_insert(Shipment).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Shipment.order_no],          # 1:1 之后的天然幂等键
            set_={
                "status": stmt.excluded.status,
                "shipped_at": stmt.excluded.shipped_at,
                "signed_at": stmt.excluded.signed_at,
                "trace": stmt.excluded.trace,
            },
        )
        await s.execute(stmt)
        await s.commit()

        total = (await s.execute(select(Shipment.id))).scalars().all()
        logger.info("运单种子完成: 写入/更新 {} 条,表内共 {} 条", len(rows), len(total))
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
