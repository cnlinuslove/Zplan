from __future__ import annotations

import argparse
import os
import re
import subprocess

from agents.news_agent import (
    can_reach_x_api,
    get_history_payload,
    map_exception_to_user_error,
    payload_to_json,
    run_news_cycle,
    send_wechat_test_message,
    unwrap_retry_exception,
)
from llm.gemini_client import check_gemini_connectivity, gemini_available
import config as app_config
from outbound_http import (
    get_x_api_session_mode,
    resolve_effective_proxy_url,
    resolve_pac_url,
    resolve_system_http_proxy_url,
)
from proxy_probe import list_localhost_listen_ports, run_probe
from topic_admin import add_topic, delete_topic, list_topics, update_topic
from wechat_interact import handle_inbound_text


def _read_scutil_proxy() -> str:
    try:
        return subprocess.check_output(["scutil", "--proxy"], text=True, timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"(scutil failed: {exc})"


def _parse_pac_url(scutil_text: str) -> str | None:
    match = re.search(r"ProxyAutoConfigURLString\s*:\s*(\S+)", scutil_text)
    return match.group(1) if match else None


def _fetch_pac_snippet(url: str, max_len: int = 400) -> str:
    try:
        out = subprocess.check_output(
            ["curl", "-sS", "--max-time", "12", url],
            timeout=15,
        )
        text = (out or b"").decode("utf-8", errors="replace").strip().replace("\r\n", "\n")
        return text[:max_len]
    except Exception as exc:
        return f"(pac fetch failed: {exc})"


def build_diag_payload() -> dict:
    scutil_text = _read_scutil_proxy()
    pac_url_scutil = _parse_pac_url(scutil_text)
    pac_url_effective = resolve_pac_url()
    pac_snippet = _fetch_pac_snippet(pac_url_effective) if pac_url_effective else None
    effective_proxy, proxy_source = resolve_effective_proxy_url()
    return {
        "http_proxy": os.getenv("HTTP_PROXY"),
        "https_proxy": os.getenv("HTTPS_PROXY"),
        "system_http_proxy": resolve_system_http_proxy_url(),
        "effective_proxy": effective_proxy,
        "effective_proxy_source": proxy_source,
        "localhost_listen_ports": list_localhost_listen_ports(),
        "x_api_base_url": app_config.X_API_BASE_URL,
        "x_bearer_configured": bool(app_config.X_BEARER_TOKEN),
        "x_outbound_mode": get_x_api_session_mode(),
        "pac_url_scutil": pac_url_scutil,
        "pac_url_effective": pac_url_effective,
        "can_reach_x_api": can_reach_x_api(),
        "x_fetch_usernames": app_config.X_FETCH_USERNAMES,
        "news_schedule_hours": app_config.NEWS_SCHEDULE_HOURS,
        "news_fetch_limit_per_topic": app_config.NEWS_FETCH_LIMIT_PER_TOPIC,
        "x_max_pages_per_topic": app_config.X_MAX_PAGES_PER_TOPIC,
        "gemini_configured": gemini_available(),
        "llm_summary_enabled": app_config.LLM_SUMMARY_ENABLED,
        "gemini_min_seconds_between_topics": app_config.GEMINI_MIN_SECONDS_BETWEEN_TOPICS,
        "gemini_min_seconds_between_calls": app_config.GEMINI_MIN_SECONDS_BETWEEN_CALLS,
        "gemini_max_output_tokens": app_config.GEMINI_MAX_OUTPUT_TOKENS,
        "wechat_webhook_configured": bool(app_config.WECHAT_PUSH_WEBHOOK),
        "wechat_http_token_configured": bool(app_config.WECHAT_HTTP_TOKEN),
        "wework_app_configured": bool(
            app_config.WECHAT_CORP_ID and app_config.WECHAT_CORP_SECRET and app_config.WECHAT_AGENT_ID
        ),
        "wework_callback_configured": bool(
            app_config.WECHAT_CORP_ID
            and app_config.WECHAT_CORP_SECRET
            and app_config.WECHAT_AGENT_ID
            and app_config.WECHAT_CALLBACK_TOKEN
            and app_config.WECHAT_CALLBACK_AES_KEY
        ),
        "wechat_push_mode": app_config.WECHAT_PUSH_MODE,
        "wechat_push_digest": app_config.WECHAT_PUSH_DIGEST,
        "scutil_proxy": scutil_text.strip(),
        "pac_snippet": pac_snippet,
        "hint": (
            "真实 X 数据：需 can_reach_x_api=true。Gemini 摘要需 GEMINI_API_KEY。"
            "微信推送需 WECHAT_PUSH_WEBHOOK（企业微信群机器人地址）。"
            "双向交互：配置企业微信应用回调 `/v1/wework/callback`（推荐），"
            "或编排层 POST `/v1/wechat/reply`、CLI `wechat-reply --text …`。"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Z-Plan bridge for OpenClaw")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run-once")

    sub.add_parser("diag")

    sub.add_parser("gemini-check", help="探测 Gemini API 是否可达（不写响应中的密钥）")

    sub.add_parser("wechat-test")

    p_wx_reply = sub.add_parser("wechat-reply", help="解析用户微信文本，返回 reply_markdown（供 OpenClaw 回发）")
    p_wx_reply.add_argument("--text", required=True, help="用户发来的纯文本")
    p_wx_reply.add_argument(
        "--push",
        action="store_true",
        help="若已配置 WECHAT_PUSH_WEBHOOK，将回复再推送到同一群机器人",
    )

    p_ask = sub.add_parser("ask", help="根据问题检索本地多源资讯（东财/Google RSS/NewsAPI 等）")
    p_ask.add_argument("--text", required=True, help="用户问题")
    p_ask.add_argument("--no-gemini", action="store_true", help="仅用检索列表，不调用 Gemini")
    p_ask.add_argument("--no-live", action="store_true", help="仅查本地库，不现场拉取各源")

    p_wx_serve = sub.add_parser(
        "wechat-serve",
        help="启动 HTTP 桥：POST /v1/wechat/reply JSON {\"text\":\"…\",\"push\":false}",
    )
    p_wx_serve.add_argument("--host", default="127.0.0.1", help="监听地址（公网暴露请配合反向代理与 WECHAT_HTTP_TOKEN）")
    p_wx_serve.add_argument("--port", type=int, default=8765, help="监听端口")

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--write-env", action="store_true")
    p_probe.add_argument("--no-lsof", action="store_true")

    p_hist = sub.add_parser("history")
    p_hist.add_argument("--mode", choices=["latest", "7d"], default="latest")
    p_hist.add_argument("--topic")

    p_topic = sub.add_parser("topic")
    p_topic.add_argument("--action", choices=["list", "add", "update", "delete"], required=True)
    p_topic.add_argument("--topic-key")
    p_topic.add_argument("--display-name")
    p_topic.add_argument("--query")
    p_topic.add_argument("--enabled", choices=["true", "false"])
    return parser.parse_args()


def handle_topic(args: argparse.Namespace) -> dict:
    def _need(value: str | None, field: str) -> str:
        if not value:
            raise ValueError(f"missing required field: {field}")
        return value

    if args.action == "list":
        topics = list_topics(echo=False)
        return {"ok": True, "action": "list", "topics": topics, "count": len(topics)}
    if args.action == "add":
        topic = add_topic(
            topic_key=_need(args.topic_key, "topic_key"),
            display_name=_need(args.display_name, "display_name"),
            query=_need(args.query, "query"),
            enabled=(args.enabled or "true") == "true",
            echo=False,
        )
        return {"ok": True, "action": "add", "topic": topic}
    if args.action == "update":
        enabled = None if args.enabled is None else args.enabled == "true"
        topic = update_topic(
            topic_key=_need(args.topic_key, "topic_key"),
            display_name=args.display_name,
            query=args.query,
            enabled=enabled,
            echo=False,
        )
        return {"ok": True, "action": "update", "topic": topic}
    result = delete_topic(topic_key=_need(args.topic_key, "topic_key"), echo=False)
    return {"ok": True, "action": "delete", **result}


def main() -> None:
    args = parse_args()
    try:
        if args.cmd == "diag":
            print(payload_to_json({"ok": True, **build_diag_payload()}))
            return
        if args.cmd == "gemini-check":
            result = check_gemini_connectivity()
            print(payload_to_json(result))
            raise SystemExit(0 if result.get("ok") else 1)
        if args.cmd == "wechat-test":
            print(payload_to_json({"ok": True, **send_wechat_test_message()}))
            return
        if args.cmd == "wechat-serve":
            from wechat_http_bridge import run_wechat_http_server

            run_wechat_http_server(host=args.host, port=args.port)
            return
        if args.cmd == "wechat-reply":
            from wechat_push import push_wechat_text

            payload = handle_inbound_text(args.text)
            out: dict = {"ok": True, **payload}
            if getattr(args, "push", False):
                text = payload.get("reply_text") or payload.get("reply_markdown")
                if text:
                    out["pushed"] = push_wechat_text(str(text))
            print(payload_to_json(out))
            return
        if args.cmd == "ask":
            from agents.info_query import answer_info_question

            result = answer_info_question(
                args.text,
                use_gemini=not getattr(args, "no_gemini", False),
                live=not getattr(args, "no_live", False),
            )
            print(payload_to_json({"ok": True, **result}))
            return
        if args.cmd == "probe":
            print(
                payload_to_json(
                    run_probe(
                        write_env=getattr(args, "write_env", False),
                        no_lsof=getattr(args, "no_lsof", False),
                    )
                )
            )
            return
        if args.cmd == "run-once":
            print(payload_to_json({"ok": True, "stats": run_news_cycle()}))
            return
        if args.cmd == "history":
            print(payload_to_json({"ok": True, **get_history_payload(args.mode, args.topic)}))
            return
        if args.cmd == "topic":
            print(payload_to_json(handle_topic(args)))
    except Exception as exc:
        root_exc = unwrap_retry_exception(exc)
        print(
            payload_to_json(
                {
                    "ok": False,
                    "error": map_exception_to_user_error(exc),
                    "debug": {"type": root_exc.__class__.__name__, "detail": str(root_exc)},
                }
            )
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
