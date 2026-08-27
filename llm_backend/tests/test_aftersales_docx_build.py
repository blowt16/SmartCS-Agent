"""售后政策文档构建测试:结构完整性(章节齐全,内容自然长度,不约束字符量)"""
import sys
from pathlib import Path

# 项目根 scripts/ 非 Python 包,按目录加入 path 后顶层导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from build_jd_aftersales_docx import POLICY_SECTIONS  # noqa: E402


def test_doc_structure_contains_required_sections():
    titles = [t for t, _ in POLICY_SECTIONS]
    for required in ["七天无理由退货", "价格保护", "售后流程", "运费", "类目差异"]:
        assert any(required in t for t in titles), f"缺少章节: {required}"


def test_each_section_has_bullets():
    """每章节至少含一条条款(空章节无意义)。"""
    for title, bullets in POLICY_SECTIONS:
        assert bullets, f"章节 [{title}] 无条款内容"
        assert all(b.strip() for b in bullets), f"章节 [{title}] 存在空条款"
