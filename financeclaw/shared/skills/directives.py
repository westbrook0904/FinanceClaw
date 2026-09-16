"""只解析真实新 Turn 顶层的显式技能语法，不识别美元或股票前缀。"""

import re

from financeclaw.kernel.skills import SkillError


def requested_skill(message: str) -> str | None:
    """未知普通文本保持原样；缺 ID 的 /skill 返回确定输入错误。"""
    text = message.lstrip()
    if not re.match(r"^/skill(?:\s|$)", text):
        return None
    match = re.match(r"^/skill\s+([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s|$)", text)
    if match is None or len(match[1]) > 64:
        raise SkillError("SKILL_DIRECTIVE_INVALID")
    return match[1]
