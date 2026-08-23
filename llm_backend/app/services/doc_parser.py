"""文档解析器:txt/md/docx → Segment 列表(文本 + 章节上下文)。

Segment 是"段落/表格粒度"的 (text, chapter) 元组;分块阶段将全文
(段间 \n\n 连接)统一递归切分,跨章节块归属块内首个非空字符所在段,见
spec_plan/SPEC_CHUNK_MERGE_STRATEGY.md §3。
"""
import re
from dataclasses import dataclass
from pathlib import Path

_ENCODINGS = ["utf-8", "gbk", "gb2312", "latin-1"]  # gbk 覆盖 gb2312,保留对齐外仓库
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Segment:
    text: str
    chapter: str = ""


def parse_text_file(path: Path, ext: str) -> list[Segment]:
    """txt/md:编码降级链读取;txt/md 均按 # 标题切段维护章节栈(无标题退化为单段)。"""
    content = None
    for enc in _ENCODINGS:
        try:
            content = Path(path).read_text(encoding=enc)
            if content.strip():
                break
        except (UnicodeDecodeError, OSError):
            continue
    if content is None or not content.strip():
        raise ValueError("文本文件所有编码均解码失败或为空")

    return _split_md_by_headings(content)


def _split_md_by_headings(content: str) -> list[Segment]:
    segments: list[Segment] = []
    chapter_stack: list[tuple[int, str]] = []  # (级别, 标题);同级标题互相替换
    cur: list[str] = []

    def flush():
        text = "\n".join(cur).strip()
        if text:
            segments.append(
                Segment(text=text, chapter=" > ".join(t for _, t in chapter_stack))
            )
        cur.clear()

    def push_heading(level: int, title: str):
        # 仅保留级别更低的祖先,同级/更高级标题直接替换(避免 `stack[:level-1]`
        # 把旧的同级标题误当祖先,见 test_md_cross_chapter_chunk_ownership)
        chapter_stack[:] = [(lv, t) for lv, t in chapter_stack if lv < level]
        chapter_stack.append((level, title))

    for line in content.split("\n"):
        m = _HEADING.match(line)
        if m:
            flush()
            push_heading(len(m.group(1)), m.group(2).strip())
            cur.append(line)  # 标题行保留进正文(商品文档标题含产品名,需可检索)
        else:
            cur.append(line)
    flush()
    if not segments:
        segments.append(Segment(text=content))
    return segments


parse_md_string = _split_md_by_headings  # 公开别名:供 MinerU 输出 markdown 复用章节逻辑


def parse_docx(path: Path) -> list[Segment]:
    """docx:遍历 element.body 按原序取段落+表格(合并单元格按 id 去重)。"""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    segments: list[Segment] = []
    chapter_stack: list[tuple[int, str]] = []  # (级别, 标题);同级标题互相替换
    cur: list[str] = []

    def flush():
        text = "\n".join(cur).strip()
        if text:
            segments.append(
                Segment(text=text, chapter=" > ".join(t for _, t in chapter_stack))
            )
        cur.clear()

    def push_heading(level: int, title: str):
        # 与 md 章节栈同规则:仅保留级别更低的祖先(切片赋值避免闭包重绑定)
        chapter_stack[:] = [(lv, t) for lv, t in chapter_stack if lv < level]
        chapter_stack.append((level, title))

    def handle_para(p: Paragraph):
        style = (p.style.name or "") if p.style else ""
        if style.startswith("Heading"):
            flush()
            try:
                level = int(style.split()[-1])
            except (ValueError, IndexError):
                level = 1
            title = p.text.strip()
            if title:
                push_heading(level, title)
            cur.append(title)  # 标题保留进正文
        elif p.text.strip():
            cur.append(p.text.strip())

    def handle_table(t: Table):
        rows = []
        for row in t.rows:
            seen: set[int] = set()
            cells = []
            for cell in row.cells:
                if id(cell) in seen:
                    continue  # 合并单元格在 python-docx 中为重复对象引用,按 id 去重
                seen.add(id(cell))
                cells.append(cell.text.strip())
            rows.append(" | ".join(cells))
        cur.append("\n".join(rows))

    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            handle_para(Paragraph(child, doc))
        elif tag == "tbl":
            handle_table(Table(child, doc))

    flush()
    if not segments:
        raise ValueError("docx 解析结果为空")
    return segments
