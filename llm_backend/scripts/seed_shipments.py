"""运单种子数据(先删后建,可重复执行)。

用法:
  python scripts/seed_shipments.py

本脚本是订单状态的【驱动方】:seed_orders.py 把 ORDER_COUNT 条订单全建成「处理中」,
本脚本按 ALLOC 给它们建运单(或无运单),再用 order_status.derive_order_fields
把派生出来的订单状态写回 orders —— 控制台环形图的五色、物流页的八状态都由这里决定。

覆盖范围(订单数 = seed_orders.ORDER_COUNT,分三个桶):
  · 无运单 3 条 —— 页面上演示「未录入 + 补录按钮」
  · 待揽收 4 条 —— 有运单但未发货(shipped_at 为 NULL)
  · 其余 11 条铺满另外 7 个运单状态(已揽收/运输中/派送中/已签收 各 2 条,
    异常/退货中/已退货 各 1 条)—— 派生出的订单状态正好覆盖控制台环形图五色

幂等策略是【先删后建】(delete 种子范围内的运单 -> 重新 insert),不是 upsert。
upsert 表达不了"这条不该有运单",而库里可能已有管理员手工建的运单,不清就会让
「无运单」桶落不了地,并留下「订单处理中 + 运单派送中」这类不一致。三个后果:
  1. 种子范围内管理员手工建的运单会被整行删除重建 —— 与旧版不同,
     tracking_no 与 carrier 也【会】被种子值覆盖(不再"保留手改的号")
  2. 重跑不累积重复行;种子范围之外的运单不受影响
  3. tracking_no 撞号仍【可能】让整个脚本中止:若非种子订单占用了 JDV 号段,
     整条多行 INSERT 抛 UniqueViolation,单语句单事务 -> 一行都不写。
     概率极低(JDV 前缀是种子专用),但记着这条排查方向

订单侧的状态/签收日期一律由 derive_order_fields 算出(与接口层共用同一份判断),
不在本脚本里另写映射。
"""
import asyncio
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent          # llm_backend
sys.path.insert(0, str(ROOT_DIR))
import app.core.database  # noqa: E402 —— Windows Selector 事件循环补丁

from sqlalchemy import delete, select, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.api.admin.logistics import parse_trace  # noqa: E402
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.logger import get_logger  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.shipment import Shipment  # noqa: E402
from app.schemas.admin import CARRIERS  # noqa: E402
from app.services.order_status import derive_order_fields  # noqa: E402

# 订单数【只此一处】:直接取 seed_orders.py 的常量,不在这里再写一份 18。
# ⚠️ 复制一份的代价不是"多余",而是静默错误:ORDER_COUNT 上调到 20 时,
#    本脚本仍只查 ORD-001..018 -> len(orders) == 18 照样通过 ->
#    新增的两条订单永远没有运单、停在「处理中」,没有任何东西会报错。
#    (只 import 常量,不 import 它的 main;seed_orders 是纯定义 + __main__ 守卫,
#     已在真实调用上下文实测:不建库连接、无副作用)
from seed_orders import ORDER_COUNT  # noqa: E402

logger = get_logger(service="seed_shipments")

# ── 运单状态分配 ─────────────────────────────────────────────────────────
# 键是【0-based 下标】,键 0 = ORD-001(订单号在下面按 f"ORD-{i + 1:03d}" 生成)。
# ⚠️ 必须按 index 显式指定,不能按"数量顺序填充"。
# 原因:seed_orders.py 的 order_date = today - (index % 14),所以
# index ∈ {0,1,14,15} 的 4 条 order_date 恒为 today 或 today-1,其 shipped_at
# 必被 min(..., today) 钳到今天,正撞下面"末节点不得落到未来"的护栏。
# 按数量顺序填充时被跳掉的恰好是「异常」和第二条「已签收」-> 环形图缺色,
# 且每天重跑都复现。
#
# 这 4 条因此全部放进【不需要 shipped_at】的桶(无运单 / 待揽收)。
#
# ✅ 已实测验算(today=2026-09-28):18 条全部通过,0 条被跳过,
#    所有轨迹末节点均早于当前时刻。
ALLOC = {
    0: None, 1: None, 14: None,                          # 无运单 -> 演示「补录」
    2: "待揽收", 3: "待揽收", 4: "待揽收", 15: "待揽收",
    5: "已揽收", 17: "已揽收",
    6: "运输中", 7: "运输中",
    8: "派送中", 9: "派送中",
    10: "已签收", 11: "已签收",
    16: "异常",
    12: "退货中",
    13: "已退货",
}
# ORDER_COUNT 来自 seed_orders.py:一旦它上调而 ALLOC 没补齐,这条断言会【当场炸】,
# 不会退化成"多出来的订单静默没有运单"
assert len(ALLOC) == ORDER_COUNT, "ALLOC 必须覆盖全部种子订单"

# 加权抽样池:京东自营为主,所以京东物流占两席。
# ⚠️ 这是【抽样分布】不是【合法值枚举】—— 与 schemas.admin.CARRIERS(8 个合法承运商)
# 语义不同,不能合并;但成员必须都在枚举里(下面有断言,防止两处漂移)
CARRIER_POOL = ["京东物流", "京东物流", "顺丰速运", "中通快递"]
assert all(c in CARRIERS for c in CARRIER_POOL), "CARRIER_POOL 里有不在 CARRIERS 枚举里的值"

# ── 轨迹模板:运单状态 -> [(小时偏移, 地点, 描述), ...] ──────────────────
# 偏移从 shipped_at 当天 09:00 起算。
NODES = {
    "已揽收": [
        (0, "广州市", "已揽收"),
    ],
    "运输中": [
        (0, "广州市", "已揽收"),
        (6, "广州转运中心", "到达转运中心"),
    ],
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
    "异常": [
        (0, "广州市", "已揽收"),
        (6, "广州转运中心", "到达转运中心"),
        (14, "广州转运中心", "包裹破损，已联系寄件人"),
    ],
    "退货中": [
        (0, "广州市", "已揽收"),
        (6, "广州转运中心", "到达转运中心"),
        (30, "深圳市", "收件人拒收，退回中"),
    ],
    "已退货": [
        (0, "广州市", "已揽收"),
        (6, "广州转运中心", "到达转运中心"),
        (18, "深圳市", "派送中"),
        (24, "深圳市", "收件人拒收，退回中"),
        (48, "广州市", "已退回寄件人"),
    ],
}

# 各状态轨迹的最大偏移(小时),用于"末节点不得落到未来"的护栏
MAX_OFFSET = {s: max(h for h, _, _ in nodes) for s, nodes in NODES.items()}


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

        # 按 order_no 升序取种子范围的订单 —— 与 seed_orders.py 的 ORD-%03d 对应,
        # 下标即 ALLOC 的 index。
        # ⚠️ 不能按 Order.status 过滤:seed_orders 已把 18 条全刷成「处理中」,
        #    按旧状态过滤会返回 0 行,脚本直接 error 退出、一条运单都写不出去。
        orders = (await s.execute(
            select(Order.order_no, Order.order_date)
            .where(Order.order_no.in_([f"ORD-{i + 1:03d}" for i in range(ORDER_COUNT)]))
            .order_by(Order.order_no)
        )).all()

        if len(orders) != ORDER_COUNT:
            # 用 error + return 1 而不是 warning + return 0 —— 与 seed_orders.py 一致。
            # 串在初始化脚本里时,「跳过」和「成功」必须能被调用方区分出来。
            logger.error("种子订单不齐({}/{}),请先跑 seed_orders.py",
                         len(orders), ORDER_COUNT)
            return 1

        seed_order_nos = [o[0] for o in orders]

        # 先删后建(为什么 upsert 不够:见模块 docstring)
        await s.execute(delete(Shipment).where(Shipment.order_no.in_(seed_order_nos)))

        rows = []
        order_updates = []
        skipped = []

        for index, (order_no, order_date) in enumerate(orders):
            shipment_status = ALLOC[index]

            # 无运单桶:跳过,该订单留在「处理中」
            if shipment_status is None:
                order_updates.append((order_no, *derive_order_fields(None, None)))
                continue

            # 「待揽收」尚未发货 -> shipped_at 为 NULL。
            # ⚠️ 不能沿用下面的 min(order_date + 1, today):那会算出非空值。
            if shipment_status == "待揽收":
                shipped_at = None
                signed_at = None
                trace = None
            else:
                shipped_at = min(order_date + timedelta(days=1), today)
                signed_at = (min(order_date + timedelta(days=2), today)
                             if shipment_status == "已签收" else None)

                # 护栏一:窗口不足一天就跳过,不硬种出自相矛盾的轨迹
                if signed_at and (signed_at - shipped_at).days < 1:
                    skipped.append((order_no, "发货/签收窗口不足一天"))
                    order_updates.append((order_no, *derive_order_fields(None, None)))
                    continue

                # 护栏二:轨迹节点【不得落到未来】。
                # ⚠️ 判据必须按"末节点 datetime ≤ now",不能按旧的
                #    "shipped_at == today" —— 那个对"shipped_at=昨天、模板跨多天"
                #    完全无效,而「已退货」的 signed_at 是 NULL,
                #    parse_trace 不会卡它的末节点上界,只能靠这条兜住。
                last_node = (datetime.combine(shipped_at, time(hour=9))
                             + timedelta(hours=MAX_OFFSET[shipment_status]))
                if last_node > datetime.now(timezone.utc).replace(tzinfo=None):
                    skipped.append((order_no, f"末节点 {last_node:%m-%d %H:%M} 落到未来"))
                    order_updates.append((order_no, *derive_order_fields(None, None)))
                    continue

                trace = build_trace(shipped_at, shipment_status)
                # 自检:模板是静态的,但手改坏时没有任何东西会拦住它 ——
                # 种子绕过接口直接写库,坏数据会静默进库。
                # 这条覆盖"节点严格递增"与"落在 [shipped_at, signed_at] 内",
                # 是上面护栏二(只兜"不落到未来")覆盖不到的两个方向。
                if trace:
                    parse_trace(trace, shipped_at, signed_at)

            digits = "".join(ch for ch in order_no if ch.isdigit()) or f"{index + 1:03d}"
            rows.append({
                "order_no": order_no,
                "tracking_no": f"JDV{int(digits):010d}",
                "carrier": CARRIER_POOL[index % len(CARRIER_POOL)],
                "status": shipment_status,
                "shipped_at": shipped_at,
                "signed_at": signed_at,
                "trace": trace,
            })
            order_updates.append(
                (order_no, *derive_order_fields(shipment_status, signed_at))
            )

        if rows:
            stmt = pg_insert(Shipment).values(rows)
            # tracking_no / carrier 不进 set_:它们是种子确定性生成的值,而且上面那条
            # delete 已经把种子范围内的行(含管理员手改过的号)整行删掉了,
            # 重跑时不存在"需要保留旧值"的行。
            # 但仍有一条会中止全脚本的路:tracking_no 上有唯一索引,若【非种子订单】
            # 占用了 JDV 号段,整条多行 INSERT 抛 UniqueViolation —— 单语句单事务,
            # 一行都不写。概率极低(JDV 前缀是种子专用),但它是排查时的第一方向。
            # 下面的 DO UPDATE 在"先删后建"下是走不到的死分支(order_no 上的冲突行
            # 刚被上面的 delete 删掉),留着只是防御性写法。
            stmt = stmt.on_conflict_do_update(
                index_elements=[Shipment.order_no],      # 1:1 之后的天然幂等键
                set_={
                    "status": stmt.excluded.status,
                    "shipped_at": stmt.excluded.shipped_at,
                    "signed_at": stmt.excluded.signed_at,
                    "trace": stmt.excluded.trace,
                },
            )
            await s.execute(stmt)

        # 订单侧:调【同一个】纯函数 derive_order_fields(见 order_status.py 的注释),
        # 不在这里另写一份映射 —— 那是第四份同义集合。
        for order_no, status, signed_date in order_updates:
            await s.execute(
                update(Order).where(Order.order_no == order_no)
                .values(status=status, signed_date=signed_date)
            )

        await s.commit()

        for order_no, reason in skipped:
            logger.warning("跳过 {}:{}", order_no, reason)
        logger.info("运单种子完成: 写入 {} 条,跳过 {} 条,无运单 {} 条",
                    len(rows), len(skipped),
                    sum(1 for v in ALLOC.values() if v is None) + len(skipped))
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
