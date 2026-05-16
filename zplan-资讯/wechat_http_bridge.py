"""
企业微信 / 编排层 与 Z-Plan 的 HTTP 桥：收 JSON 文本 → 返回与 CLI 一致的 JSON。

用法：在项目根目录执行 `python openclaw_bridge.py wechat-serve`，将 `http://<host>:<port>`
暴露给内网穿透或网关，由对方 POST `{"text":"最新","push":false}`。
可选：在 .env 设置 `WECHAT_HTTP_TOKEN`，请求需带 `Authorization: Bearer <token>`。
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import config as app_config
from agents.news_agent import push_to_wechat
from wechat_interact import handle_inbound_text
from wechat_push import push_wechat_text
from wework_callback import handle_wework_callback_post, verify_wework_callback_url, wework_status

logger = logging.getLogger(__name__)


def _auth_ok(handler: BaseHTTPRequestHandler) -> bool:
    token = (app_config.WECHAT_HTTP_TOKEN or "").strip()
    if not token:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {token}":
        return True
    return handler.headers.get("X-Zplan-Token", "") == token


def process_wechat_reply_request(body: dict[str, Any]) -> dict[str, Any]:
    text = str(body.get("text", "")).strip()
    push = bool(body.get("push", False))
    payload = handle_inbound_text(text)
    out: dict[str, Any] = {"ok": True, **payload}
    if push:
        text = payload.get("reply_text") or payload.get("reply_markdown")
        if text:
            if payload.get("intent") in ("info_query", "help", "history_latest", "history_7d", "history_topic", "topic_list"):
                out["pushed"] = push_wechat_text(str(text))
            else:
                out["pushed"] = push_to_wechat(str(text))
    return out


def _read_json_body(handler: BaseHTTPRequestHandler, max_bytes: int = 256_000) -> dict[str, Any] | None:
    length = handler.headers.get("Content-Length")
    if not length:
        return None
    try:
        n = int(length)
    except ValueError:
        return None
    if n < 0 or n > max_bytes:
        return None
    raw = handler.rfile.read(n)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path == "/v1/wework/callback":
                qs = parse_qs(parsed_url.query)
                try:
                    plain = verify_wework_callback_url(
                        msg_signature=(qs.get("msg_signature") or [""])[0],
                        timestamp=(qs.get("timestamp") or [""])[0],
                        nonce=(qs.get("nonce") or [""])[0],
                        echostr=(qs.get("echostr") or [""])[0],
                    )
                except Exception as exc:
                    logger.warning("wework url verify failed: %s", exc)
                    self.send_response(403)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(plain.encode("utf-8"))
                return
            if path in ("/", "/health"):
                if not _auth_ok(self):
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps(
                    {"ok": True, "service": "zplan-wechat-bridge", "wework": wework_status()},
                    ensure_ascii=False,
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path == "/v1/wework/callback":
                qs = parse_qs(parsed_url.query)
                length = self.headers.get("Content-Length")
                raw = b""
                if length:
                    try:
                        n = int(length)
                        if 0 <= n <= 256_000:
                            raw = self.rfile.read(n)
                    except ValueError:
                        pass
                try:
                    body = handle_wework_callback_post(
                        raw,
                        msg_signature=(qs.get("msg_signature") or [""])[0],
                        timestamp=(qs.get("timestamp") or [""])[0],
                        nonce=(qs.get("nonce") or [""])[0],
                    )
                except Exception as exc:
                    logger.exception("wework callback failed: %s", exc)
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            if path != "/v1/wechat/reply":
                self.send_response(404)
                self.end_headers()
                return
            if not _auth_ok(self):
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False).encode("utf-8")
                )
                return
            body = _read_json_body(self)
            if body is None:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": False, "error": "invalid_json_or_body"}, ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
                return
            try:
                out = process_wechat_reply_request(body)
            except Exception as exc:
                logger.exception("wechat reply failed: %s", exc)
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"ok": False, "error": {"type": exc.__class__.__name__, "detail": str(exc)}},
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
                return
            raw = json.dumps(out, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(raw.encode("utf-8"))

    return Handler


def run_wechat_http_server(host: str, port: int) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    server = ThreadingHTTPServer((host, port), make_handler())
    logger.info(
        "wechat HTTP bridge on http://%s:%s — POST /v1/wechat/reply | GET/POST /v1/wework/callback",
        host,
        port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown")
    finally:
        server.server_close()
