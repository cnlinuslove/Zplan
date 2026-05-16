"""AkShare / 东财 HTTP：系统代理、重试、浏览器头（避免裸请求被断连）。"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_EASTMONEY_HOSTS = (
    "eastmoney.com",
    "push2his.eastmoney.com",
    "push2.eastmoney.com",
    "quote.eastmoney.com",
)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

_session: requests.Session | None = None
_patched = False


def _read_scutil_proxy() -> str:
    try:
        return subprocess.check_output(["scutil", "--proxy"], text=True, timeout=6)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def resolve_system_http_proxy_url() -> Optional[str]:
    text = _read_scutil_proxy()
    if not text:
        return None
    if re.search(r"HTTPSEnable\s*:\s*1", text):
        host = re.search(r"HTTPSProxy\s*:\s*(\S+)", text)
        port = re.search(r"HTTPSPort\s*:\s*(\d+)", text)
        if host and port:
            return f"http://{host.group(1)}:{port.group(1)}"
    if re.search(r"HTTPEnable\s*:\s*1", text):
        host = re.search(r"HTTPProxy\s*:\s*(\S+)", text)
        port = re.search(r"HTTPPort\s*:\s*(\d+)", text)
        if host and port:
            return f"http://{host.group(1)}:{port.group(1)}"
    return None


def resolve_proxy_url() -> tuple[Optional[str], str]:
    """返回 (proxy_url, source)，source 为 env|system|direct。"""
    explicit = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if explicit:
        return explicit, "env"
    if os.getenv("AKSHARE_USE_SYSTEM_PROXY", "true").lower() == "true":
        system = resolve_system_http_proxy_url()
        if system:
            return system, "system"
    if os.getenv("AKSHARE_DIRECT", "false").lower() == "true":
        return None, "direct"
    return None, "direct"


def build_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    proxy_url, source = resolve_proxy_url()
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
        session.trust_env = False
        logger.info("[HTTP] 使用代理 (%s): %s", source, proxy_url)
    else:
        session.trust_env = False
        logger.info("[HTTP] 东财直连（无代理）")
    return session


def get_http_session() -> requests.Session:
    global _session
    if _session is None:
        _session = build_requests_session()
    return _session


def patch_requests_for_akshare() -> None:
    """让 akshare 内 ``requests.get`` 走统一 Session（代理 + 重试 + Referer）。"""
    global _patched
    if _patched:
        return
    session = get_http_session()
    original_get = requests.get

    def _get(url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", kwargs.pop("timeout", None) or 30)
        headers = {**_DEFAULT_HEADERS, **(kwargs.pop("headers", None) or {})}
        return session.get(url, headers=headers, **kwargs)

    requests.get = _get  # type: ignore[assignment]
    _patched = True


def configure_akshare_http() -> None:
    """在调用 akshare 前执行一次。"""
    patch_requests_for_akshare()
    # 探测代理是否可用（避免 Clash 未启动时连续 ProxyError）
    proxy_url, source = resolve_proxy_url()
    if not proxy_url:
        return
    try:
        r = get_http_session().get(
            "https://push2his.eastmoney.com",
            timeout=8,
        )
        logger.debug("东财探测 status=%s via %s", r.status_code, source)
    except requests.RequestException as exc:
        logger.warning(
            "[HTTP] 代理 %s 不可用 (%s)。请启动 Clash/V2Ray，或设置 AKSHARE_DIRECT=true 直连。",
            proxy_url,
            exc,
        )


def throttle(seconds: float | None = None) -> None:
    time.sleep(seconds if seconds is not None else float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "3")))
