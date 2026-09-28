"""订单状态 ⇄ 运单状态的唯一映射处。

订单状态不直接编辑,而是【纯派生】自运单状态:改运单 → 订单自动跟随。
本模块是两侧状态集与映射关系的单一事实来源 —— models / schemas / api / 种子脚本
一律从这里取,不得各自再写一份(2026-09-27 的物流模块 spec 已记录过
「两份同义集合必然漂移」这个教训)。

⚠️ 本模块【只 import 标准库】,上层引用它不成环。
   注意不要说成"app/services/ 不依赖上层"——那是假的,
   user_service.py:4 就有 `from app.schemas.user import UserCreate`。
   安全是因为【本文件自己什么都不 import】。
"""

from datetime import date
from typing import Optional

# 订单状态(5 个)。
# ⚠️ 顺序即控制台环形图的图例顺序,与前端 ConsoleView.vue 的 ORDER_COLORS 按下标绑定 ——
#    调整顺序必须同步改那个数组,否则颜色与图例错位(而且没有任何测试会红)。
ORDER_STATUSES = ["处理中", "已发货", "已签收", "售后处理中", "已退款·交易关闭"]

# 运单状态(8 个)。顺序即物流页状态下拉顺序。
SHIPMENT_STATUSES = ["待揽收", "已揽收", "运输中", "派送中",
                     "已签收", "异常", "退货中", "已退货"]

# 派生的唯一依据。8 → 5,多对一。
SHIPMENT_TO_ORDER = {
    "待揽收": "处理中",
    "已揽收": "已发货",
    "运输中": "已发货",
    "派送中": "已发货",
    "已签收": "已签收",
    "异常": "售后处理中",
    "退货中": "售后处理中",
    "已退货": "已退款·交易关闭",
}

# 没有运单时订单的落点(删除运单 / 建单初始值都用它)。
# 提成常量而不是散落的字面量:这个值在三个端点、种子脚本、测试辅助里都要用,
# 写错一个(比如写成「已发货」)会让"没运单却有状态"的洞重新出现。
NO_SHIPMENT_ORDER_STATUS = "处理中"

# 唯一需要 signed_date 的订单状态。写成常量避免各处散落字面量。
SIGNED_ORDER_STATUS = "已签收"


def derive_order_status(shipment_status: str) -> str:
    """运单状态 → 订单状态。

    入参必须来自 SHIPMENT_STATUSES(由接口层 schema 的正则保证)。
    用 [] 而不是 .get() —— 映射表缺少某个运单状态时应当立刻抛 KeyError,
    而不是静默返回 None 把订单状态写成空值(那会绕过 NOT NULL 变成 500)。
    """
    return SHIPMENT_TO_ORDER[shipment_status]


def derive_order_fields(
    shipment_status: Optional[str], signed_at: Optional[date]
) -> tuple[str, Optional[date]]:
    """纯函数:算出一个订单应有的 (status, signed_date)。

    ⚠️ 判断逻辑【只在这里写一次】。接口层走 ORM(见 sync_order_from_shipment),
    种子脚本走 Core UPDATE —— 两条路形态不同,但必须共用这一份判断。
    在种子脚本里另写一遍映射,就是本模块开头要防的「第四份同义集合」。

    shipment_status 为 None 表示该订单当前【没有运单】(刚被删除,或还没建):
    没有派生来源,订单回到 NO_SHIPMENT_ORDER_STATUS,签收日期清空。

    签收日期【直接跟随运单】,不各算各的 —— 两处独立计算必然生出自相矛盾的
    演示数据(库里 ORD-015 曾经就是「订单已送达、运单已签收」这类不一致)。
    运单是「已签收、日期不详」(signed_at 为 NULL,物流模块 §4.5 明说是有意的
    可达状态)时,订单也跟着是 NULL,两边保持一致。
    """
    if shipment_status is None:
        return NO_SHIPMENT_ORDER_STATUS, None
    status = derive_order_status(shipment_status)
    return status, (signed_at if status == SIGNED_ORDER_STATUS else None)


def sync_order_from_shipment(
    order, shipment_status: Optional[str], signed_at: Optional[date]
) -> None:
    """把 derive_order_fields 的结果写到 ORM Order 对象上。

    ⚠️ 只改 ORM 对象属性,【不 flush】—— 调用方负责把它并进自己那次 flush 里,
    这样同步与运单写入在同一个事务,冲突回滚时两边一起回退。

    ⚠️ 入参用【值】而不是 Shipment 对象。调用方 rollback 会让 session 里所有
    ORM 对象过期,之后读属性触发惰性刷新、异步下抛 MissingGreenlet
    (项目踩过的坑,orders.py:115 有同款注释)。传值就没有这个风险。
    """
    order.status, order.signed_date = derive_order_fields(shipment_status, signed_at)
