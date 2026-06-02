"""企业微信机器人消息长度限制（按 UTF-8 字节，非字符数）。"""
from __future__ import annotations

# 企微 markdown 上限 4096 bytes；留余量避免边界拒收
WECHAT_MARKDOWN_MAX_BYTES = 4000
WECHAT_TEXT_MAX_BYTES = 2000


def truncate_wechat_utf8(text: str, max_bytes: int) -> str:
    raw = text or ""
    enc = raw.encode("utf-8")
    if len(enc) <= max_bytes:
        return raw
    cut = enc[:max_bytes]
    # 避免截断多字节字符中间
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""
