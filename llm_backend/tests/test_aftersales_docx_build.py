"""售后政策文档构建测试:章节 ≥450 字符约束(防切分器混合块,spec §3.3)"""
import sys
from pathlib import Path

# 项目根 scripts/ 非 Python 包,按目录加入 path 后顶层导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from build_jd_aftersales_docx import POLICY_SECTIONS  # noqa: E402


def test_each_section_at_least_450_chars():
    """每章节(标题+条款+分隔符)≥450 字符,强制章节边界成为切块边界"""
    for title, bullets in POLICY_SECTIONS:
        n = len(title) + sum(len(b) + 2 for b in bullets)
        assert n >= 450, f"章节 [{title}] 仅 {n} 字符,不足 450,会与相邻章节合并成混合块"


def test_doc_structure_contains_required_sections():
    titles = [t for t, _ in POLICY_SECTIONS]
    for required in ["七天无理由退货", "价格保护", "售后流程", "运费", "类目差异"]:
        assert any(required in t for t in titles), f"缺少章节: {required}"
