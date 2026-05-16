"""Post-fetch filters to reduce X search noise (saves API $ + improves relevance)."""
from __future__ import annotations

import re
from typing import Protocol

_SPAM_PATTERNS = re.compile(
    r"私讯|私聊|开盒|社工|查户籍|查通话|查婚姻|查全家|手机号定位|"
    r"打屁股|#4i|#男m|#四愛|#sub|giveaway|won\s*\$|randomly\s+selected|"
    r"telegram\.me|t\.me/|whatsapp|免费信号|喊单|98%\s*accuracy|accuracy\s*in\s*market|"
    r"forex\s*signal|daily\s*forex|gold\s*and\s*curru|get\s+a\s+daily|"
    r"signals?\s+available|channel\s+link|👉|🚨\s*telegram",
    re.IGNORECASE,
)

_CN_MARKET_KEYWORDS = re.compile(
    r"股|市|指数|涨|跌|板块|上证|深证|创业板|沪深|北向|涨停|跌停|"
    r"央行|证监会|A股|港股|陆股|恒生|行情|财报|IPO|"
    r"stock|market|CSI|shanghai|shenzhen|hsi|hang\s*seng",
    re.IGNORECASE,
)

_CRYPTO_KEYWORDS = re.compile(
    r"bitcoin|btc|ethereum|eth|加密货币|稳定币|defi|nft|链上|"
    r"现货|合约|etf|sec|binance|coinbase|减半|halving|"
    r"solana|layer2|rollup|质押|挖矿",
    re.IGNORECASE,
)


_TOPIC_FILTERS: dict[str, re.Pattern[str]] = {
    "cn_market_hotspots": _CN_MARKET_KEYWORDS,
    "crypto_sentiment": _CRYPTO_KEYWORDS,
}


class _HasText(Protocol):
    text: str


def is_spam_text(text: str) -> bool:
    return bool(_SPAM_PATTERNS.search(text))


def is_relevant_for_topic(topic_key: str, text: str) -> bool:
    pattern = _TOPIC_FILTERS.get(topic_key)
    if pattern is None:
        return True
    return bool(pattern.search(text))


def filter_news_items(topic_key: str, items: list[_HasText]) -> list[_HasText]:
    kept: list[_HasText] = []
    for item in items:
        text = item.text.strip()
        if len(text) < 12:
            continue
        if is_spam_text(text):
            continue
        if not is_relevant_for_topic(topic_key, text):
            continue
        kept.append(item)
    return kept
