"""
Outbound HTTP for X API: env proxy > macOS system proxy > PAC (pypac) > plain requests.

谢公屐等「仅 PAC、无本地端口」场景：由 pypac 下载并执行 PAC JS，按 URL 选择远端 HTTPS 代理。
macOS 系统设置里的 HTTP/HTTPS 代理（scutil）会被 Python 自动读取，无需手动写 .env。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCUTIL_PROXY_CACHE: str | None = None

# pypac 默认只认两种 MIME；不少 PAC 托管（如 duckpac）误标为 text/html，需一并接受，无效正文会在 PAC 解析阶段失败。
_PAC_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/x-ns-proxy-autoconfig",
        "application/x-javascript-config",
        "text/html",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/plain",
    }
)

_session: Any | None = None
_session_kind: str | None = None


def _read_scutil_proxy() -> str:
    global _SCUTIL_PROXY_CACHE
    if _SCUTIL_PROXY_CACHE is not None:
        return _SCUTIL_PROXY_CACHE
    try:
        _SCUTIL_PROXY_CACHE = subprocess.check_output(
            ["scutil", "--proxy"], text=True, timeout=6
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("scutil --proxy failed: %s", exc)
        _SCUTIL_PROXY_CACHE = ""
    return _SCUTIL_PROXY_CACHE


def _scutil_flag(text: str, key: str) -> bool:
    match = re.search(rf"{re.escape(key)}\s*:\s*(\d+)", text)
    return bool(match and match.group(1) == "1")


def _scutil_host_port(text: str, host_key: str, port_key: str) -> Optional[tuple[str, int]]:
    host_match = re.search(rf"{re.escape(host_key)}\s*:\s*(\S+)", text)
    port_match = re.search(rf"{re.escape(port_key)}\s*:\s*(\d+)", text)
    if not host_match or not port_match:
        return None
    return host_match.group(1), int(port_match.group(1))


def resolve_system_http_proxy_url() -> Optional[str]:
    """Read macOS Wi‑Fi/Ethernet proxy from scutil --proxy (HTTP CONNECT style)."""
    text = _read_scutil_proxy()
    if not text:
        return None
    if _scutil_flag(text, "HTTPSEnable"):
        pair = _scutil_host_port(text, "HTTPSProxy", "HTTPSPort")
        if pair:
            host, port = pair
            return f"http://{host}:{port}"
    if _scutil_flag(text, "HTTPEnable"):
        pair = _scutil_host_port(text, "HTTPProxy", "HTTPPort")
        if pair:
            host, port = pair
            return f"http://{host}:{port}"
    return None


def _system_pac_url() -> Optional[str]:
    text = _read_scutil_proxy()
    if not text:
        return None
    match = re.search(r"ProxyAutoConfigURLString\s*:\s*(\S+)", text)
    return match.group(1) if match else None


def resolve_pac_url() -> Optional[str]:
    explicit = os.getenv("PAC_URL", "").strip()
    if explicit:
        return explicit
    if os.getenv("PAC_FROM_SYSTEM", "true").lower() != "true":
        return None
    return _system_pac_url()


def _has_env_proxy() -> bool:
    return bool(os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY"))


def resolve_effective_proxy_url() -> tuple[Optional[str], str]:
    """Return (proxy_url, source) where source is env|system|none."""
    env_url = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if env_url:
        return env_url, "env"
    if os.getenv("USE_SYSTEM_PROXY", "true").lower() == "true":
        system_url = resolve_system_http_proxy_url()
        if system_url:
            return system_url, "system"
    return None, "none"


def _apply_proxy_to_session(session: Any, proxy_url: str) -> None:
    session.proxies.update({"http": proxy_url, "https": proxy_url})


def get_x_api_session() -> Any:
    """Return a requests-compatible Session (plain Session or pypac.PACSession)."""
    global _session, _session_kind
    if _session is not None:
        return _session

    import requests

    proxy_url, proxy_source = resolve_effective_proxy_url()
    if proxy_url:
        _session = requests.Session()
        _apply_proxy_to_session(_session, proxy_url)
        _session_kind = "env_proxy" if proxy_source == "env" else "system_proxy"
        logger.info("[INFO] X 出站: 使用 %s 代理 %s", proxy_source, proxy_url)
        return _session

    if os.getenv("USE_PYPAC", "true").lower() != "true":
        _session = requests.Session()
        _session_kind = "direct"
        logger.info("[INFO] X 出站: USE_PYPAC=false，直连")
        return _session

    pac_url = resolve_pac_url()
    if not pac_url:
        _session = requests.Session()
        _session_kind = "direct_no_pac"
        logger.info("[INFO] X 出站: 未配置 PAC_URL 且系统无 PAC，直连")
        return _session

    try:
        from pypac import PACSession, get_pac
    except ImportError as exc:
        _session = requests.Session()
        _session_kind = "direct_no_pypac"
        logger.warning(
            "[WARN] 已配置 PAC 但未安装 pypac，无法执行 PAC JS。请执行: uv pip install pypac dukpy  或  pip install pypac dukpy  (%s)",
            exc,
        )
        return _session

    try:
        pac = get_pac(url=pac_url, allowed_content_types=_PAC_ALLOWED_CONTENT_TYPES)
        _session = PACSession(pac)
        _session_kind = "pac"
        logger.info("[INFO] X 出站: PACSession, pac_url=%s", pac_url)
        return _session
    except Exception as exc:
        _session = requests.Session()
        _session_kind = "pac_failed"
        logger.warning("[WARN] PACSession 初始化失败，降级直连: %s", exc)
        return _session


def get_x_api_session_mode() -> str:
    get_x_api_session()
    return _session_kind or "unknown"


def reset_x_api_session() -> None:
    """测试或切换 PAC 后可调用以重建会话。"""
    global _session, _session_kind
    _session = None
    _session_kind = None


_proxied_session: Any | None = None


def get_proxied_requests_session() -> Any:
    """Gemini/Google 等出站：HTTP(S)_PROXY > macOS 系统代理 > 直连（不走 PAC）。"""
    global _proxied_session
    if _proxied_session is not None:
        return _proxied_session
    import requests

    _proxied_session = requests.Session()
    proxy_url, _ = resolve_effective_proxy_url()
    if proxy_url:
        _apply_proxy_to_session(_proxied_session, proxy_url)
    return _proxied_session


def reset_proxied_requests_session() -> None:
    global _proxied_session
    _proxied_session = None
