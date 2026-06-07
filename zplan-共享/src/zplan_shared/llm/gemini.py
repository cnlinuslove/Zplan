"""向后兼容包装器：所有调用已切换至 DeepSeek。

原 Gemini 函数名保留，内部转发到 ``zplan_shared.llm.deepseek``。
新代码请直接 import deepseek 模块。
"""

from zplan_shared.llm.deepseek import (
    # 核心函数
    DeepSeekError,
    chat_json_with_deepseek,
    check_deepseek_connectivity,
    deepseek_available,
    generate_json_with_deepseek,
    generate_text_with_deepseek,
    pop_usage,
)

# ── 向后兼容别名 ────────────────────────────────────────────────────

GeminiError = DeepSeekError
gemini_available = deepseek_available
generate_json_with_gemini = generate_json_with_deepseek
check_gemini_connectivity = check_deepseek_connectivity

__all__ = [
    "DeepSeekError",
    "GeminiError",
    "chat_json_with_deepseek",
    "check_deepseek_connectivity",
    "check_gemini_connectivity",
    "deepseek_available",
    "gemini_available",
    "generate_json_with_deepseek",
    "generate_json_with_gemini",
    "generate_text_with_deepseek",
    "pop_usage",
]
