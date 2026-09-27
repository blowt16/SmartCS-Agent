"""管理端物流管理(4 个端点):列表 / 建单 / 编辑 / 删除。

对齐 SPEC_ADMIN_CONSOLE §5.3(分页与序列化约定),业务规则见
docs/superpowers/specs/2026-09-27-管理端物流模块-design.md §4。
"""
from datetime import date, datetime, timezone
from itertools import pairwise
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.order import Order
from app.models.shipment import Shipment
from app.schemas.admin import (
    SHIPPABLE_ORDER_STATUSES,
    TRACE_MAX,
    UNRECORDED,
    ShipmentCreate,
    ShipmentUpdate,
)

router = APIRouter()


class TraceFormatError(Exception):
    """轨迹文本不合格式。line_no 供接口拼错误文案。"""

    def __init__(self, line_no: int, reason: str):
        self.line_no, self.reason = line_no, reason
        super().__init__(f"第 {line_no} 行：{reason}")


class TraceNode(NamedTuple):
    """一个已解析的轨迹节点。

    用具名字段而不是裸元组:下面 `nodes[-1].dt.date()` 与 `nodes[-1].line_no`
    在裸元组里只差一个下标数字(`[1]` vs `[0]`),手滑写反不会报语义错 ——
    `int > date` 会抛 TypeError,把一个本该 400 的路径变成 500。
    """

    line_no: int
    dt: datetime
    loc: str
    desc: str


def parse_trace(raw: str | None, shipped_at: date | None,
                signed_at: date | None) -> str | None:
    """校验并规范化轨迹文本。空文本返回 None;不合格式抛 TraceFormatError。

    格式(§4.5 规则 C):一行一个节点,每行三段用 | 分隔 ——
        YYYY-MM-DD HH:MM | 地点 | 描述

    规范化:逐行 trim、丢弃空行、分隔符两侧统一为 " | "、行间统一 "\\n"。
    空文本 / 纯空白 -> None(不返回空串,避免库里两种"空"并存)。

    另两条硬约束:节点时间必须严格递增;节点日期必须落在 [shipped_at, signed_at]
    区间内(两端各自有值时才检查,边界相等算通过)。

    ⚠️ 两条容易写错的细节:

    1. **行号必须按管理员在 textarea 里看到的行来数**。先带原行号落表、再丢空行 ——
       如果反过来(丢完空行才 enumerate),"多敲回车"这个本格式明确允许的写法会让
       空行之后的所有行号前移,报错就指到空行上,而行号是这条错误唯一的定位手段。
    2. **按 `\\r\\n|\\r|\\n` 显式拆行**,与前端 LogisticsView 的拆法一致。
       `str.splitlines()` 还会按 U+2028 / U+2029 等 Unicode 分隔符拆(从 Word /
       微信粘贴时会出现),前端不认这些,会造成前后端对"第几行"的判定不一致。

    本函数的输出是【幂等】的(parse_trace(parse_trace(x)) == parse_trace(x)),
    这是 §4.8 那条长度不变式成立的前提:存下来的值必须能原样再提交。
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    numbered = [(i, line.strip()) for i, line in enumerate(text.split("\n"), 1)]

    nodes: list[TraceNode] = []
    for line_no, line in numbered:
        if not line:                              # 空行直接忽略,不算错
            continue
        parts = line.split("|")
        if len(parts) != 3:
            raise TraceFormatError(
                line_no, f"应为「YYYY-MM-DD HH:MM | 地点 | 描述」三段，实际 {len(parts)} 段")
        ts, loc, desc = (p.strip() for p in parts)
        if not loc:
            raise TraceFormatError(line_no, "地点不能为空")
        if not desc:
            raise TraceFormatError(line_no, "描述不能为空")
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        except ValueError:
            raise TraceFormatError(line_no, f"时间「{ts}」不是 YYYY-MM-DD HH:MM")
        nodes.append(TraceNode(line_no, dt, loc, desc))

    if not nodes:
        return None

    # 时间必须严格递增(相同也不行:同一时刻的两个节点排不出先后)
    for prev, cur in pairwise(nodes):
        if cur.dt <= prev.dt:
            raise TraceFormatError(
                cur.line_no, f"时间必须比上一行晚（上一行 {prev.dt:%Y-%m-%d %H:%M}）")

    # 节点必须落在 [shipped_at, signed_at] 内(两端各自有值时才检查)
    if shipped_at and nodes[0].dt.date() < shipped_at:
        raise TraceFormatError(nodes[0].line_no, f"首节点日期不能早于发货时间 {shipped_at}")
    if signed_at and nodes[-1].dt.date() > signed_at:
        raise TraceFormatError(nodes[-1].line_no, f"末节点日期不能晚于签收时间 {signed_at}")

    return "\n".join(f"{n.dt:%Y-%m-%d %H:%M} | {n.loc} | {n.desc}" for n in nodes)


def _serialize(order: Order, shipment: Shipment | None) -> dict:
    """一行 = 一个订单 + 它的运单(可为 None = 未录入)。

    入参顺序与 select(Order, Shipment) 一致。shipment 为 None 就是"未录入",
    前端据此决定渲染「未录入」徽章还是「补录」按钮 —— 不另加布尔字段。

    ⚠️ 判空一律用 `is not None`,**不要写 `if shipment`** —— 那是对象真值判断,
    今天恰好等价(模型没有 __bool__/__len__),但一旦不等价,故障是【静默且全量】的:
    12 个字段里运单那半边一起变 None、每条已录入的行都渲染成「未录入」,
    而且不抛任何异常,只能靠肉眼发现。
    """
    return {
        "id": shipment.id if shipment is not None else None,
        "order_no": order.order_no,
        "order_status": order.status,
        "product_name": order.product_name,
        "category": order.category,
        "buyer_name": order.buyer_name,
        "tracking_no": shipment.tracking_no if shipment is not None else None,
        "carrier": shipment.carrier if shipment is not None else None,
        "status": shipment.status if shipment is not None else None,
        # Date 列无时间部分,isoformat() 直接是 YYYY-MM-DD,不经 new Date() 无时区风险
        "shipped_at": (shipment.shipped_at.isoformat()
                       if shipment is not None and shipment.shipped_at else None),
        "signed_at": (shipment.signed_at.isoformat()
                      if shipment is not None and shipment.signed_at else None),
        "trace": shipment.trace if shipment is not None else None,
    }


@router.get("")
async def list_logistics(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: str = Query(""),
    status: str | None = Query(None),
    order_status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """物流列表:列出【所有已发货及之后的订单】∪【任何已有运单的订单】。

    驱动表是 orders 不是 shipments —— 未录物流的订单也要出现在列表里
    (标「未录入」)。第二个 OR 分支保证订单状态被改回「处理中」时,
    它的运单不会从页面上隐身。
    """
    # 第一个条件保证"没隐身":已发货及之后的订单(含未录入) ∪ 已有运单的订单
    conds = [or_(
        Order.status.in_(SHIPPABLE_ORDER_STATUSES),
        Shipment.id.isnot(None),
    )]

    kw = (keyword or "").strip()
    if kw:
        conds.append(or_(
            Shipment.tracking_no.ilike(f"%{kw}%"),
            Order.order_no.ilike(f"%{kw}%"),
            Order.product_name.ilike(f"%{kw}%"),
            Order.buyer_name.ilike(f"%{kw}%"),
            Shipment.carrier.ilike(f"%{kw}%"),
        ))
    if status == UNRECORDED:
        conds.append(Shipment.id.is_(None))
    elif status:
        conds.append(Shipment.status == status)
    if order_status:
        conds.append(Order.status == order_status)

    # total 的 count 必须带同一个 outer join:漏 join 时 SQLAlchemy 把两张表
    # 补成笛卡尔积 —— 不报错,只发一条 SAWarning,total 会算成"运单数 × 订单数"。
    # 也不能取 len(items)(分页下恒等于 page_size 且不报错)。
    total = (await db.execute(
        select(func.count()).select_from(Order)
        .outerjoin(Shipment, Shipment.order_no == Order.order_no)
        .where(*conds)
    )).scalar_one()

    rows = (await db.execute(
        select(Order, Shipment)
        .outerjoin(Shipment, Shipment.order_no == Order.order_no)
        .where(*conds)
        .order_by(Shipment.shipped_at.desc().nullslast(), Order.order_no.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(order, shipment) for order, shipment in rows],
    }


def _resolve_signed_at(status: str, given: date | None, existing: date | None,
                       *, explicit_clear: bool = False,
                       fill_today: bool = False) -> date | None:
    """规则 A:signed_at 与 status 强绑定。

    非「已签收」一律置 NULL;「已签收」显式传了用传的 -> 没传沿用原值 ->
    原值也没有【且 fill_today】才补当天(UTC,不用 date.today() ——
    本地日期在早 8 小时窗口内错位一天)。

    ⚠️ `explicit_clear` 区分「管理员显式清空」与「没传」—— 少了它,`given or existing`
    会把显式传的 None 当成"没传",回填库里的旧值,前端那个可清空的日期框就成了摆设。

    ⚠️ `fill_today` 区分「刚变成已签收」与「本来就是已签收」—— **编辑路径必须传
    `became_signed`**。不传的话,「已签收、日期不详」(signed_at 为 NULL,本模块有意
    支持的状态)会在**任何一次无关编辑**后被填上今天的日期:管理员只改了个承运商,
    签收日期自己冒出来了 —— 凭空造了一条没人录入的数据。

    ⚠️ 默认值是 **False**,不是 True —— 安全的那个方向。两个调用点都已显式传参,
    所以行为与默认值无关;但万一将来有人漏传:不填只留下一个可见的「日期不详」,
    填今天则是凭空造一条**看起来合理**的假数据。默认值应该站在能被发现的那一侧。
    """
    if status != "已签收":
        return None
    if explicit_clear:
        return None
    if given is not None:
        return given
    if existing is not None:
        return existing
    return datetime.now(timezone.utc).date() if fill_today else None


def _check_time_order(shipped: date | None, signed: date | None, status: str) -> None:
    """规则 B:时间先后 + 已签收必须有发货时间。"""
    if status == "已签收" and not shipped:
        raise HTTPException(status_code=400, detail="已签收的运单必须有发货时间")
    if shipped and signed and signed < shipped:
        raise HTTPException(status_code=400, detail="签收时间不能早于发货时间")


def _parse_trace_or_400(raw: str | None, shipped: date | None, signed: date | None) -> str | None:
    """规则 C:校验并规范化轨迹,不合格式转 400。

    ⚠️ 长度检查必须在【规范化之后】做,不能只靠 schema 的 max_length。
    规范化每行会加 4 个字符(`14:32|广州|已揽收` → `14:32 | 广州 | 已揽收`),
    所以 max_length 只能管住"录入长度",管不住"存下来的长度"。
    实测:1974 字符的紧凑写法能过 max_length=2000,规范化后是 2290 ——
    存下来之后,管理员在编辑弹窗里原样保存就会被 422 打回。
    parse_trace 的输出是幂等的,所以在这一层卡住即可保证
    "任何被接受过的值都能被原样再提交"。
    """
    try:
        normalized = parse_trace(raw, shipped, signed)
    except TraceFormatError as e:
        raise HTTPException(status_code=400, detail=f"轨迹格式不对：{e}")
    if normalized and len(normalized) > TRACE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"轨迹规范化后 {len(normalized)} 字，超过上限 {TRACE_MAX} 字")
    return normalized


def _conflict_409(e: IntegrityError, order_no: str, tracking_no: str) -> HTTPException:
    """按约束名区分两条唯一键 —— 都是 UniqueViolation,只能靠 diag.constraint_name 分辨。

    ⚠️ 用【精确名】而不是子串匹配。子串 `"order_no_key" in name` 在本库今天就已经
    同时命中 `orders_order_no_key` 与 `shipments_order_no_key`（连库查过）——
    当前不可达（本端点只往 shipments 插），但那是命名巧合，不是被强制的不变量。
    精确名在 spec §4.4 的表格里本来就有。
    """
    name = getattr(getattr(e.orig, "diag", None), "constraint_name", "") or ""
    if name == "shipments_order_no_key":
        return HTTPException(status_code=409, detail=f"订单 {order_no} 已有运单，请勿重复创建")
    if name == "shipments_tracking_no_key":
        return HTTPException(status_code=409, detail=f"运单号已存在: {tracking_no}")
    return HTTPException(status_code=409, detail="运单写入冲突，请重试")


@router.post("")
async def create_shipment(payload: ShipmentCreate, db: AsyncSession = Depends(get_db)):
    """建运单(order_no 由前端从被点「补录」的那一行带入,不经过下拉)。"""
    order = (await db.execute(
        select(Order).where(Order.order_no == payload.order_no)
    )).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=400, detail=f"订单不存在: {payload.order_no}")

    # 先查「已有运单」再查状态:订单被改回处理中但已有运单时,真实原因是前者,
    # 报「状态不对」会误导管理员
    exists_id = (await db.execute(
        select(Shipment.id).where(Shipment.order_no == payload.order_no)
    )).scalar_one_or_none()
    if exists_id is not None:
        raise HTTPException(status_code=409, detail=f"订单 {payload.order_no} 已有运单，请勿重复创建")

    if order.status not in SHIPPABLE_ORDER_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"订单 {payload.order_no} 当前状态为「{order.status}」，只有已发货及之后的订单才能建运单",
        )

    # explicit_clear 也要接上,别让建单路径与编辑路径行为不一致:
    # 补录时管理员选了「已签收」又把日期框清空 -> 前端按 §4.9 送 null ->
    # 若这里不传 explicit_clear,那句 `given or existing or today` 会把 null 当成
    # "没传"、补成今天,管理员看到的日期与提交的不一致。而 spec §4.5 明说
    # 「已签收、日期不详」是有意的可达状态。
    # pydantic v2 用 model_fields_set 分辨:显式传 null 时含 'signed_at',不传时不含。
    #
    # fill_today=True 必须显式写出来(默认值是 False,漏了就变成"已签收但日期不详"):
    # 建单路径上"刚变成已签收"恒成立 —— 没有旧状态可言,管理员选了已签收又没给日期,
    # 补当天是唯一合理的解释。编辑路径不一样,那里的 fill_today 要传 became_signed,
    # 见 Task 7。
    signed_at = _resolve_signed_at(
        payload.status, payload.signed_at, None,
        explicit_clear=("signed_at" in payload.model_fields_set and payload.signed_at is None),
        fill_today=True,
    )
    _check_time_order(payload.shipped_at, signed_at, payload.status)
    trace = _parse_trace_or_400(payload.trace, payload.shipped_at, signed_at)

    shipment = Shipment(
        order_no=payload.order_no,
        tracking_no=payload.tracking_no,
        carrier=payload.carrier,
        status=payload.status,
        shipped_at=payload.shipped_at,
        signed_at=signed_at,
        trace=trace,
    )
    try:
        db.add(shipment)
        # flush 必须显式写:add() 本身不抛异常,冲突要到 flush/autoflush/commit 才暴露。
        # 漏了它,唯一键冲突会从下面那次重取 SELECT 的 autoflush 冒出(在 try 之外 -> 500),
        # 或从 get_db yield 之后的隐式 commit 冒出(响应已生成 -> 500)。
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise _conflict_409(e, payload.order_no, payload.tracking_no)

    # 重取一次拿 join 出来的订单字段,不要在 ORM 对象上手工拼
    row = (await db.execute(
        select(Order, Shipment)
        .join(Shipment, Shipment.order_no == Order.order_no)
        .where(Shipment.id == shipment.id)
    )).one()
    return _serialize(*row)


@router.put("/{shipment_id}")
async def update_shipment(
    shipment_id: int,
    payload: ShipmentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """编辑运单。order_no 不可改(schema 里就没有,换订单应删除后重建)。"""
    shipment = (await db.execute(
        select(Shipment).where(Shipment.id == shipment_id)
    )).scalar_one_or_none()
    if shipment is None:
        raise HTTPException(status_code=404, detail=f"运单不存在: {shipment_id}")

    # exclude_unset 区分"没传"与"传了 null";trace 需要能清空,所以必须用它
    data = payload.model_dump(exclude_unset=True)

    # ⚠️ 三个 NOT NULL 列不接受显式 null:放行的话 setattr(None) 会在 flush 时抛
    # NotNullViolation,落进下面那个为 UniqueViolation 写的 except IntegrityError 兜底,
    # 拿到空的 constraint_name -> 返回「运单写入冲突，请重试」。
    # 那是【请求错误】不是冲突 —— 管理员照着文案重试一万次也不会成功。
    # schema 层拦不住(三个字段都是 Optional[...] = Field(None, ...),None 是合法值),
    # 所以在这里显式挡掉,给一个能指导修改的 400。
    # 不要改成"静默丢弃 null":那会变成「保存了但没生效」,比 409 更难查。
    for _f in ("tracking_no", "carrier", "status"):
        if _f in data and data[_f] is None:
            raise HTTPException(status_code=400, detail=f"{_f} 不能为 null")

    # 三条规则都用 effective 值:管理员最常见的操作是「只把状态改成已签收」,
    # 此时 shipped_at 根本没传。直接读 data 会让规则 B / C 的窗口校验形同失效。
    # ⚠️ 必须【先】把旧状态取出来:下面那个 for 循环会就地改写 shipment 的属性,
    # 之后再读 shipment.status 拿到的就是新值,became_signed 会恒为 False。
    prev_status = shipment.status
    effective_status = data.get("status", shipment.status)
    effective_shipped = data.get("shipped_at", shipment.shipped_at)
    # ⚠️ fill_today 必须传 became_signed:
    # 规则 A 的"补当天"只适用于【刚变成已签收】。本来就已经签收、这次只是改了个
    # 无关字段(比如承运商)、又没传 signed_at 的,要【保持原样】—— 包括保持 NULL。
    # 否则「已签收、日期不详」这个 spec §4.5 明说有意的状态,会被任何一次无关编辑
    # 悄悄填上今天:管理员只改了承运商,签收日期自己冒出来,凭空造了一条没人录入的数据。
    became_signed = effective_status == "已签收" and prev_status != "已签收"
    effective_signed = _resolve_signed_at(
        effective_status,
        data.get("signed_at", shipment.signed_at),
        shipment.signed_at,
        explicit_clear=("signed_at" in data and data["signed_at"] is None),
        fill_today=became_signed,
    )

    _check_time_order(effective_shipped, effective_signed, effective_status)
    data["signed_at"] = effective_signed
    if "trace" in data:
        data["trace"] = _parse_trace_or_400(data["trace"], effective_shipped, effective_signed)

    # 🔴 rollback 会让 session 里所有 ORM 对象过期,之后再读它们的属性会触发惰性刷新,
    # 异步下直接抛 MissingGreenlet。所以冲突分支要用的值必须在 try 之前取成局部变量 ——
    # data.get("tracking_no", shipment.tracking_no) 的默认参数也是在这里就求值了。
    # 这是项目踩过的坑,orders.py:115 有同款注释。
    order_no = shipment.order_no
    tracking_no = data.get("tracking_no", shipment.tracking_no)

    for field, value in data.items():
        setattr(shipment, field, value)

    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise _conflict_409(e, order_no, tracking_no)

    row = (await db.execute(
        select(Order, Shipment)
        .join(Shipment, Shipment.order_no == Order.order_no)
        .where(Shipment.id == shipment.id)
    )).one()
    return _serialize(*row)


@router.delete("/{shipment_id}")
async def delete_shipment(shipment_id: int, db: AsyncSession = Depends(get_db)):
    """删除运单。订单不会被删,删除后它回到列表里的「未录入」行。"""
    shipment = (await db.execute(
        select(Shipment).where(Shipment.id == shipment_id)
    )).scalar_one_or_none()
    if shipment is None:
        raise HTTPException(status_code=404, detail=f"运单不存在: {shipment_id}")

    await db.delete(shipment)
    return {"id": shipment_id, "deleted": True}
