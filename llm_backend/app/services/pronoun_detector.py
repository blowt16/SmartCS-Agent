"""
指代检测器：语义缓存分级指代消解的规则引擎（第一道闸门）

设计原则（SPEC_SEMANTIC_CACHE_RESOLVE.md §4）：
    - 三层检测：显性指代 → 省略主语 → 纯语气词，O(n) 字符串匹配，毫秒级完成
    - 宁可多检测（假阳性只多一次 LLM 消解调用），不能漏检测（假阴性导致缓存永久失效）
    - 只有规则引擎判定"含指代/省略"时，才触发 LLM 消解（15% 消息）

用法:
    from app.services.pronoun_detector import detect_pronoun, DetectionDecision
    decision = detect_pronoun("那个有货吗")      # NEED_RESOLVE
    decision = detect_pronoun("iPhone 15多少钱")  # PASS_THROUGH
    decision = detect_pronoun("好的")             # SKIP_CACHE
"""

from enum import Enum
from typing import List

from app.core.logger import get_logger

logger = get_logger(service="pronoun_detector")


class DetectionDecision(str, Enum):
    """检测决策枚举"""
    PASS_THROUGH = "pass_through"   # 不含指代，直接透传（零额外开销）
    NEED_RESOLVE = "need_resolve"   # 含指代/省略，需要 LLM 消解
    SKIP_CACHE = "skip_cache"       # 纯语气词，跳过缓存（不查不写）


# ==================== 第一层：显性指代词词典 ====================

# 近指代词：指代上文刚提到的内容
NEAR_PRONOUNS = ["这个", "这件", "这款", "这种", "这些"]
# 远指代词：指代上文提到的内容
FAR_PRONOUNS = ["那个", "那件", "那款", "那种", "那些"]
# 单数人称代词
SINGULAR_PRONOUNS = ["它", "他", "她"]
# 复数人称代词
PLURAL_PRONOUNS = ["它们", "他们", "她们"]
# 书面指代：电商客服场景常见书面语
WRITTEN_PRONOUNS = ["该产品", "该商品", "其", "上述"]

PRONOUNS: List[str] = (
    NEAR_PRONOUNS + FAR_PRONOUNS + SINGULAR_PRONOUNS + PLURAL_PRONOUNS + WRITTEN_PRONOUNS
)

# 那/哪 混淆归一（第一层前置）：疑问词 "哪些" 的常见输入错误为 "那些"。
# 仅当 "那些" 紧跟存在动词 "有" 时归一（如 "你们有那些比较好的沙发"
# = "你们有哪些比较好的沙发"，非远指）；句首位置（"那些沙发有货吗"）与
# "是那些"（"是那些吗" 真实远指，归一后不成句）均保持原样，只消解不漏检。
INTERROGATIVE_CONFUSION: List[tuple] = [("有那些", "有哪些")]

# ==================== 第二层：省略主语触发词 ====================

# 短句以这些词开头时，判定为省略主语（如 "有货吗" "能退吗"）
# 注意 "可以"：单独成句是语气词（走第三层），作为问句开头是省略触发（"可以退吗"）
ELLIPSIS_TRIGGERS: List[str] = [
    "有货", "有卖", "多少钱", "价格", "能退", "能换", "支持", "兼容",
    "怎么", "为什么", "还有", "再", "可以", "能不能", "包邮", "保修",
    "多久", "什么时候", "哪里", "怎么样",
]

# 省略主语消息的最大长度（超过即认为包含明确主语）
ELLIPSIS_MAX_LEN = 15

# ==================== 第三层：纯语气词词典 ====================

# 整条消息去掉标点后完全命中这些词 → 跳过缓存
FILLER_PHRASES: List[str] = [
    "好的", "行", "可以", "OK", "ok", "对", "是的", "好",
    "嗯", "哦", "知道了", "明白了",
    "谢谢", "再见", "拜拜",
]

# 语气词消息尾部可忽略的标点
FILLER_STRIP_CHARS = "？?。！!～~.，, "


def _is_filler(text: str) -> bool:
    """第三层：判断是否为纯语气词（整句匹配，忽略尾部标点）"""
    return text.rstrip(FILLER_STRIP_CHARS) in FILLER_PHRASES


def detect_pronoun(text: str, skip_filler: bool = True) -> DetectionDecision:
    """
    指代检测总入口：三层检测，返回决策结果。

    Args:
        text: 最后一条用户消息
        skip_filler: 是否跳过纯语气词（RESOLVE_SKIP_FILLER 开关，false 时语气词按普通消息处理）

    Returns:
        PASS_THROUGH / NEED_RESOLVE / SKIP_CACHE
    """
    text = text.strip()
    if not text:
        return DetectionDecision.PASS_THROUGH

    # 第三层：纯语气词（判定优先于指代，语气词既不消解也不缓存）
    if skip_filler and _is_filler(text):
        logger.debug("检测到纯语气词: '{}' → SKIP_CACHE", text)
        return DetectionDecision.SKIP_CACHE

    # 那/哪 混淆归一（仅检测用）：疑问词误写 "有那些"→"有哪些"，避免误判为远指代词
    for src, dst in INTERROGATIVE_CONFUSION:
        text = text.replace(src, dst)

    # 第一层：显性指代词（子串匹配，宁可假阳性）
    for pronoun in PRONOUNS:
        if pronoun in text:
            logger.debug("检测到指代词 '{}' in '{}' → NEED_RESOLVE", pronoun, text)
            return DetectionDecision.NEED_RESOLVE

    # 第二层：省略主语（短句 + 以触发词开头）
    if len(text) <= ELLIPSIS_MAX_LEN and text.startswith(tuple(ELLIPSIS_TRIGGERS)):
        logger.debug("检测到省略主语 '{}' → NEED_RESOLVE", text)
        return DetectionDecision.NEED_RESOLVE

    return DetectionDecision.PASS_THROUGH
