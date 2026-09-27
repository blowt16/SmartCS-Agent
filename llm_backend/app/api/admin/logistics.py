"""管理端物流管理(4 个端点):列表 / 建单 / 编辑 / 删除。

对齐 SPEC_ADMIN_CONSOLE §5.3(分页与序列化约定),业务规则见
docs/superpowers/specs/2026-09-27-管理端物流模块-design.md §4。
"""
from datetime import date, datetime, timezone
from itertools import pairwise
from typing import NamedTuple

from fastapi import APIRouter

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
