"""管理端物流管理(4 个端点):列表 / 建单 / 编辑 / 删除。

对齐 SPEC_ADMIN_CONSOLE §5.3(分页与序列化约定),业务规则见
docs/superpowers/specs/2026-09-27-管理端物流模块-design.md §4。
"""
from datetime import date, datetime, timezone

from fastapi import APIRouter

router = APIRouter()


class TraceFormatError(Exception):
    """轨迹文本不合格式。line_no 供接口拼错误文案。"""

    def __init__(self, line_no: int, reason: str):
        self.line_no, self.reason = line_no, reason
        super().__init__(f"第 {line_no} 行：{reason}")


def parse_trace(raw: str | None, shipped_at: date | None,
                signed_at: date | None) -> str | None:
    """校验并规范化轨迹文本。

    格式(§4.5 规则 C):一行一个节点,每行三段用 | 分隔 ——
        YYYY-MM-DD HH:MM | 地点 | 描述

    规范化:逐行 trim、丢弃空行、分隔符两侧统一为 " | "、行间统一 "\\n"。
    空文本 / 纯空白 -> None(不返回空串,避免库里两种"空"并存)。

    另两条硬约束:节点时间必须严格递增;节点日期必须落在 [shipped_at, signed_at]
    区间内(两端各自有值时才检查,边界相等算通过)。

    不合格式抛 TraceFormatError,由调用方转 400。
    """
    lines = [l.strip() for l in (raw or "").splitlines()]
    lines = [l for l in lines if l]              # 空行直接忽略,不算错
    if not lines:
        return None

    parsed = []
    for i, line in enumerate(lines, 1):
        parts = line.split("|")
        if len(parts) != 3:
            raise TraceFormatError(
                i, f"应为「YYYY-MM-DD HH:MM | 地点 | 描述」三段，实际 {len(parts)} 段")
        ts, loc, desc = (p.strip() for p in parts)
        if not loc or not desc:
            raise TraceFormatError(i, "地点和描述不能为空")
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        except ValueError:
            raise TraceFormatError(i, f"时间「{ts}」不是 YYYY-MM-DD HH:MM")
        parsed.append((i, dt, loc, desc))

    # 时间必须严格递增(相同也不行:同一时刻的两个节点排不出先后)
    for (_, d1, _, _), (i2, d2, _, _) in zip(parsed, parsed[1:]):
        if d2 <= d1:
            raise TraceFormatError(
                i2, f"时间必须比上一行晚（上一行 {d1:%Y-%m-%d %H:%M}）")

    # 节点必须落在 [shipped_at, signed_at] 内(两端各自有值时才检查)
    if shipped_at and parsed[0][1].date() < shipped_at:
        raise TraceFormatError(parsed[0][0], f"首节点日期不能早于发货时间 {shipped_at}")
    if signed_at and parsed[-1][1].date() > signed_at:
        raise TraceFormatError(parsed[-1][0], f"末节点日期不能晚于签收时间 {signed_at}")

    return "\n".join(f"{d:%Y-%m-%d %H:%M} | {loc} | {desc}" for _, d, loc, desc in parsed)
