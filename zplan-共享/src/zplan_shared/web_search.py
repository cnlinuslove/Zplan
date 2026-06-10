"""Web Search 模块：为概念产品摘要提供 grounding 搜索结果。

默认使用 DuckDuckGo（免费，无需 API Key）。
设置 ``TAVILY_API_KEY`` 环境变量后自动切换到 Tavily（结构化更好，付费）。

所有搜索函数内部自带简单缓存（同进程同 query 不重复搜），
避免批量查询时重复请求。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time as time_module
from typing import Any

from zplan_shared.config import (
    TAVILY_API_KEY,
    WEB_SEARCH_BACKEND,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# ── 进程内简单缓存 ──────────────────────────────────────────────────
# 避免同一进程中对同一 query 反复搜索（但不跨进程，不影响 DB 缓存）。
_cache: dict[str, list[dict[str, str]]] = {}
_cache_lock = threading.Lock()
_max_cache_size = 512


def _cache_key(query: str, max_results: int) -> str:
    raw = f"{query}|{max_results}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(query: str, max_results: int) -> list[dict[str, str]] | None:
    key = _cache_key(query, max_results)
    with _cache_lock:
        return _cache.get(key)


def _cache_set(query: str, max_results: int, results: list[dict[str, str]]) -> None:
    key = _cache_key(query, max_results)
    with _cache_lock:
        if len(_cache) > _max_cache_size:
            # 简单淘汰：清一半
            remove = list(_cache.keys())[: _max_cache_size // 2]
            for k in remove:
                del _cache[k]
        _cache[key] = results


# ── DuckDuckGo 后端 ─────────────────────────────────────────────────


def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    """DuckDuckGo 文字搜索（免费，无需 API Key）。

    优先使用新版 ``ddgs`` 包，回退到旧版 ``duckduckgo_search``。
    """
    DDGS = None
    for module_name in ("ddgs", "duckduckgo_search"):
        try:
            mod = __import__(module_name, fromlist=["DDGS"])
            DDGS = mod.DDGS
            break
        except ImportError:
            continue

    if DDGS is None:
        logger.warning("ddgs / duckduckgo_search 均未安装，回退到空结果")
        return []

    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results, timelimit=WEB_SEARCH_TIMEOUT_SECONDS))
    except Exception as exc:
        logger.warning("DuckDuckGo 搜索失败 (%s): %s", type(exc).__name__, exc)
        return []

    results: list[dict[str, str]] = []
    for r in raw:
        results.append(
            {
                "title": str(r.get("title", "")).strip(),
                "url": str(r.get("href", "")).strip(),
                "snippet": str(r.get("body", "")).strip(),
            }
        )
    return results


# ── Tavily 后端（可选，需 API Key）──────────────────────────────────


def _search_tavily(query: str, max_results: int) -> list[dict[str, str]]:
    """Tavily Search API（付费，结构化更好）。"""
    if not TAVILY_API_KEY:
        logger.warning("TAVILY_API_KEY 未配置，回退到 DuckDuckGo")
        return _search_duckduckgo(query, max_results)

    try:
        from tavily import TavilyClient
    except ImportError:
        logger.warning("tavily 未安装，回退到 DuckDuckGo")
        return _search_duckduckgo(query, max_results)

    try:
        client = TavilyClient(api_key=TAVILY_API_KEY)
        resp = client.search(
            query,
            max_results=max_results,
            search_depth="basic",
            include_answer=False,
        )
    except Exception as exc:
        logger.warning("Tavily 搜索失败: %s，回退到 DuckDuckGo", exc)
        return _search_duckduckgo(query, max_results)

    results: list[dict[str, str]] = []
    for r in resp.get("results", []) or []:
        results.append(
            {
                "title": str(r.get("title", "")).strip(),
                "url": str(r.get("url", "")).strip(),
                "snippet": str(r.get("content", "")).strip(),
            }
        )
    return results


# ── 统一入口 ────────────────────────────────────────────────────────


def search_web(
    query: str,
    *,
    max_results: int = 0,
    backend: str | None = None,
    skip_cache: bool = False,
) -> list[dict[str, str]]:
    """通用网页搜索。

    Args:
        query: 搜索关键词
        max_results: 返回数量上限（0=使用 WEB_SEARCH_MAX_RESULTS 环境变量）
        backend: 后端选择（None=自动按优先级 Tavily > DuckDuckGo）
        skip_cache: 跳过进程内缓存，强制重新搜索

    Returns:
        [{title, url, snippet}] 列表
    """
    limit = max_results if max_results > 0 else WEB_SEARCH_MAX_RESULTS
    use_backend = backend or WEB_SEARCH_BACKEND

    # 读缓存
    if not skip_cache:
        cached = _cache_get(query, limit)
        if cached is not None:
            return cached

    # 选择后端
    if use_backend == "tavily" or (use_backend == "auto" and TAVILY_API_KEY):
        results = _search_tavily(query, limit)
        actual_backend = "tavily"
    else:
        results = _search_duckduckgo(query, limit)
        actual_backend = "duckduckgo"

    # 标记后端来源
    for r in results:
        r.setdefault("source", actual_backend)

    # 写缓存
    _cache_set(query, limit, results)

    # 礼貌限速（不同 query 之间至少间隔 0.3s）
    time_module.sleep(0.3)
    return results


def search_company_website(company_name: str) -> str | None:
    """搜索 A 股上市公司官网。

    搜索 "{公司名} 官网"，从搜索结果中提取最可能的官网 URL。
    使用多 query + 评分机制，优先匹配公司拼音/英文域名。
    """
    # 已知映射（常用大公司的官网，避免搜索误判）
    _KNOWN_WEBSITES: dict[str, str] = {
        "平安银行": "https://bank.pingan.com",
        "万科A": "https://www.vanke.com",
        "比亚迪": "https://www.byd.com",
        "宁德时代": "https://www.catl.com",
        "中国平安": "https://www.pingan.com",
        "贵州茅台": "https://www.moutaichina.com",
        "招商银行": "https://www.cmbchina.com",
    }
    if company_name in _KNOWN_WEBSITES:
        return _KNOWN_WEBSITES[company_name]

    # 排除的非官网域名/模式
    _skip_domains = (
        "baidu.com", "zhihu.com", "weibo.com", "douyin.com", "wikipedia.org",
        "eastmoney.com", "10jqka.com", "sina.com.cn", "sohu.com", "163.com",
        "news.qq.com", "cninfo.com.cn", "badfl.com", "chinanews.com.cn",
        "doc88.com", "docin.com", "taodocs.com", "max.book118.com",
        "renrendoc.com", "pinble.com",
    )

    queries = [
        f"{company_name} 官网",
        f"{company_name} 官方网站",
    ]
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    for q in queries:
        for r in search_web(q, max_results=5):
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                candidates.append(r)

    if not candidates:
        return None

    # 评分：域名含公司名拼音/英文 > .com.cn > .cn > 短路径 > 其他
    def _score(result: dict[str, str]) -> int:
        url = result.get("url", "")
        title = result.get("title", "")

        # 硬排除：已知非官网域
        if any(d in url for d in _skip_domains):
            return -100
        # 排除非 http/https
        if not url.startswith("http"):
            return -100

        score = 0
        # 域名含 .com.cn 或 .cn（中国公司官网特征）
        if ".com.cn" in url:
            score += 10
        elif ".cn" in url:
            score += 5
        # 标题含"官网"/"官方网站"
        if "官网" in title or "官方网站" in title or " official" in title.lower():
            score += 8
        # 短路径偏好（首页 vs 内页）
        path_count = url.count("/")
        if path_count <= 3:
            score += 3
        # URL 含公司名部分（前 2 字）
        prefix = company_name[:2]
        if prefix in url:
            score += 4

        return score

    scored = [(c, _score(c)) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)

    best, best_score = scored[0]
    if best_score >= 5:
        return best.get("url", "")

    logger.info("公司官网未找到高置信结果: %s (best_score=%d)", company_name, best_score)
    return None


def search_company_concept(
    company_name: str,
    concept: str,
    *,
    max_results: int = 0,
) -> list[dict[str, str]]:
    """搜索公司在特定概念下的产品与市场信息。

    构造多条搜索词以获取不同角度的信息：
    1. "{公司名} {概念} 产品 主营"
    2. "{公司名} {概念} 市场份额 龙头"

    Returns:
        [{title, url, snippet}] 去重合并后的结果列表
    """
    limit = max_results if max_results > 0 else max(3, WEB_SEARCH_MAX_RESULTS)

    queries = [
        f"{company_name} {concept} 产品 主营业务",
        f"{company_name} {concept} 市场份额",
    ]

    seen_urls: set[str] = set()
    merged: list[dict[str, str]] = []

    for q in queries:
        results = search_web(q, max_results=limit)
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                merged.append(r)

    return merged[:limit]


# ── 辅助函数 ────────────────────────────────────────────────────────


def format_search_results(results: list[dict[str, str]]) -> str:
    """将搜索结果格式化为可嵌入 LLM prompt 的文本。"""
    if not results:
        return "（无搜索结果）"

    lines: list[str] = []
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "")[:80]
        snippet = r.get("snippet", "")[:200]
        url = r.get("url", "")
        lines.append(f"[{i}] {title}")
        lines.append(f"    {snippet}")
        if url:
            lines.append(f"    来源: {url}")
    return "\n".join(lines)


def flush_search_cache() -> None:
    """清空进程内搜索缓存（调试用）。"""
    with _cache_lock:
        _cache.clear()
        logger.info("搜索缓存已清空")
