"""向后兼容模块（已废弃）。

本模块原名对应 Gemini，现已全面切换至 DeepSeek（OpenAI 兼容 API）。
所有函数内部转发到 ``zplan_shared.llm.deepseek``。

推荐用法::

    from zplan_shared.llm.deepseek import (
        llm_available,        # 替代 gemini_available
        generate_json,        # 替代 generate_json_with_gemini
        generate_text,        # 替代 generate_text_with_deepseek
        LLMError,             # 替代 GeminiError
    )

切换模型：修改 .env 中 ``DEEPSEEK_MODEL`` 和 ``DEEPSEEK_API_BASE_URL`` 即可，
例如切到 Gemini：

    DEEPSEEK_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta
    DEEPSEEK_MODEL=gemini-2.5-pro
    DEEPSEEK_API_KEY=你的Gemini Key
"""

from zplan_shared.llm.deepseek import (
    DeepSeekError,
    chat_json_with_deepseek,
    check_deepseek_connectivity,
    deepseek_available,
    generate_json_with_deepseek,
    generate_text_with_deepseek,
    # 新别名
    LLMError,
    chat_json,
    check_llm_connectivity,
    generate_json,
    generate_text,
    llm_available,
    pop_usage,
)

# ── 废弃别名（保留以兼容旧代码）───────────────────────────────────

GeminiError = DeepSeekError
gemini_available = deepseek_available
generate_json_with_gemini = generate_json_with_deepseek
check_gemini_connectivity = check_deepseek_connectivity

__all__ = [
    # 新名（推荐）
    "LLMError",
    "chat_json",
    "check_llm_connectivity",
    "generate_json",
    "generate_text",
    "llm_available",
    "pop_usage",
    # 精确名
    "DeepSeekError",
    "chat_json_with_deepseek",
    "check_deepseek_connectivity",
    "deepseek_available",
    "generate_json_with_deepseek",
    "generate_text_with_deepseek",
    # 废弃别名（向下兼容）
    "GeminiError",
    "check_gemini_connectivity",
    "gemini_available",
    "generate_json_with_gemini",
]
