"""从 Google News RSS 等聚合标题中解析真实媒体名。"""
from __future__ import annotations

import re

# 标题常见尾部：「…… - 东方财富」「…… | 新浪财经」
_TAIL_SEPARATORS = (" - ", " – ", " | ", " — ", "－")


def split_aggregator_title(title: str) -> tuple[str, str]:
    """
    返回 (纯正文标题, 媒体名)。
    媒体名为空表示未能从标题解析。
    """
    t = (title or "").strip()
    if not t:
        return "", ""
    for sep in _TAIL_SEPARATORS:
        if sep not in t:
            continue
        head, tail = t.rsplit(sep, 1)
        pub = tail.strip()
        head = head.strip()
        if head and pub and 2 <= len(pub) <= 48 and not pub.startswith("http"):
            return head, pub
    return t, ""


def display_source_name(title: str, fallback: str = "") -> str:
    """展示用媒体名：优先标题尾部，其次 RSS 字段，不用 Google News RSS。"""
    _, pub = split_aggregator_title(title)
    if pub:
        return pub
    fb = (fallback or "").strip()
    if fb and fb.lower() not in ("google news rss", "google news"):
        return fb
    return "资讯"
