from app.services.text_cleaner import clean_text, remove_toc_lines


def test_control_chars_removed():
    assert "\x00\x1f" not in clean_text("a\x00b\x1fc")


def test_whitespace_normalized():
    assert clean_text("a\r\n\r\n\r\nb") == "a\n\nb"
    assert clean_text("a   b") == "a b"


def test_page_footer_removed():
    assert "第 3 页/共 5 页" not in clean_text("正文\n第 3 页/共 5 页\n继续")
    assert "---Page 2---" not in clean_text("正文\n---Page 2---")


def test_naked_number_line_kept():
    """商品价格/库存数字独立成行不得被删(裸数字行规则已移除)。"""
    assert "5999" in clean_text("价格\n5999\n库存")


def test_toc_lines_removed():
    text = "目录\n1. 智能沙发系列 ........ 3\n正文内容"
    assert "智能沙发系列" not in remove_toc_lines(text)
    assert "正文内容" in remove_toc_lines(text)


def test_normal_product_lines_kept():
    text = "### 产品名称：智家云享智能沙发 SF-2000\n- 价格：5999 元"
    cleaned = clean_text(text)
    assert "SF-2000" in cleaned and "5999" in cleaned
