"""AkShare / 东财 HTTP：东财域名直连，其它走系统代理。"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_EASTMONEY_SUFFIXES = ("eastmoney.com",)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

_proxied_session: requests.Session | None = None
_direct_session: requests.Session | None = None
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
    explicit = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if explicit:
        return explicit, "env"
    if os.getenv("AKSHARE_USE_SYSTEM_PROXY", "true").lower() == "true":
        system = resolve_system_http_proxy_url()
        if system:
            return system, "system"
    return None, "direct"


def _make_session(*, use_proxy: bool) -> requests.Session:
    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.trust_env = False
    if use_proxy:
        proxy_url, source = resolve_proxy_url()
        if proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
            if _proxied_session is None:
                logger.info("[HTTP] 非东财请求走代理 (%s): %s", source, proxy_url)
    return session


def _eastmoney_direct_enabled() -> bool:
    return os.getenv("AKSHARE_EASTMONEY_DIRECT", "true").lower() == "true"


def _is_eastmoney_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in _EASTMONEY_SUFFIXES)


def _pick_session(url: str) -> requests.Session:
    global _proxied_session, _direct_session
    if _eastmoney_direct_enabled() and _is_eastmoney_url(url):
        if _direct_session is None:
            _direct_session = _make_session(use_proxy=False)
            logger.info("[HTTP] 东财域名 (*.eastmoney.com) 直连，不走 Clash 代理")
        return _direct_session
    if _proxied_session is None:
        _proxied_session = _make_session(use_proxy=True)
    return _proxied_session


def _eastmoney_proxy_fallback_enabled() -> bool:
    return os.getenv("AKSHARE_EASTMONEY_PROXY_FALLBACK", "true").lower() == "true"


def patch_requests_for_akshare() -> None:
    global _patched, _proxied_session
    if _patched:
        return
    original_get = requests.get

    def _get(url: str, **kwargs: Any) -> requests.Response:
        global _proxied_session, _direct_session
        timeout = kwargs.pop("timeout", None) or 30
        headers = {**_DEFAULT_HEADERS, **(kwargs.pop("headers", None) or {})}
        session = _pick_session(url)
        try:
            return session.get(url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException:
            if session is _proxied_session:
                if _direct_session is None:
                    _direct_session = _make_session(use_proxy=False)
                logger.warning("[HTTP] 代理不可用，直连重试: %s", url[:80])
                return _direct_session.get(
                    url, headers=headers, timeout=timeout, **kwargs
                )
            if (
                _eastmoney_proxy_fallback_enabled()
                and _eastmoney_direct_enabled()
                and _is_eastmoney_url(url)
                and session is _direct_session
            ):
                if _proxied_session is None:
                    _proxied_session = _make_session(use_proxy=True)
                logger.warning("[HTTP] 东财直连失败，改用代理重试: %s", url[:80])
                return _proxied_session.get(
                    url, headers=headers, timeout=timeout, **kwargs
                )
            raise

    requests.get = _get  # type: ignore[assignment]
    _patched = True
    _patch_akshare_request_with_retry()


def _patch_akshare_request_with_retry() -> None:
    """AkShare 新版用 session.get，不走 requests.get；统一走东财直连逻辑。"""
    try:
        import akshare.utils.request as ak_req
    except ImportError:
        return
    if getattr(ak_req, "_zplan_patched", False):
        return

    def _request_with_retry(
        url: str,
        params: dict | None = None,
        timeout: int = 15,
        max_retries: int = 3,
        base_delay: float = 1.0,
        random_delay_range: tuple[float, float] = (0.5, 1.5),
    ) -> requests.Response:
        import random

        last_exception: Exception | None = None
        for attempt in range(max_retries):
            session = _pick_session(url)
            try:
                resp = session.get(
                    url,
                    params=params,
                    timeout=timeout or 30,
                    headers=_DEFAULT_HEADERS,
                )
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exception = exc
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt) + random.uniform(*random_delay_range)
                    time.sleep(delay)
        assert last_exception is not None
        raise last_exception

    ak_req.request_with_retry = _request_with_retry  # type: ignore[assignment]
    ak_req._zplan_patched = True


def configure_akshare_http() -> None:
    patch_requests_for_akshare()


def throttle(seconds: float | None = None) -> None:
    time.sleep(seconds if seconds is not None else float(os.getenv("AKSHARE_RATE_LIMIT_SECONDS", "3")))
