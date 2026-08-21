"""文本清洗流水线(纯函数,独立可测,开关由调用方控制)。

设计边界(见 spec §4):不做裸数字行删除——商品文档价格/库存数字常独立成行,
`^\d{1,4}$` 规则会误删("5999"整行);只删页码/页眉模式与目录特征行。
"""
import re

_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PAGE_CN = re.compile(r"第\s*\d+\s*页\s*/\s*共\s*\d+\s*页")
_PAGE_EN = re.compile(r"---\s*Page\s*\d+\s*---")
_TOC_ELLIPSIS = re.compile(r"[.…]{2,}\s*\d{1,4}\s*$")
_TOC_TAB = re.compile(r"\t{1,3}\d{1,4}\s*$")
_TOC_SPACES = re.compile(r"\s{2,}\d{1,4}\s*$")
_TOC_TITLE = re.compile(r"^目\s*录\s*$")


def clean_text(text: str) -> str:
    """清洗主流水线:控制字符 → 换行/空白规范化 → 页码/页眉清除 → 目录清除。"""
    text = _CTRL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _PAGE_CN.sub("", text)
    text = _PAGE_EN.sub("", text)
    text = remove_toc_lines(text)
    return text.strip()


def remove_toc_lines(text: str) -> str:
    """移除目录行:仅匹配"连接符(省略号/制表符/2+空白) + 尾部页码"特征,保留正文。"""
    lines = []
    in_toc = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if _TOC_TITLE.match(stripped):
            in_toc = True
            continue
        is_toc = bool(
            _TOC_ELLIPSIS.search(stripped)
            or _TOC_TAB.search(stripped)
            or (_TOC_SPACES.search(stripped) and len(stripped) > 5)
        )
        if is_toc:
            continue  # 目录特征行一律跳过(含目录区外,如书末目录)
        in_toc = False
        lines.append(line)
    return "\n".join(lines)
