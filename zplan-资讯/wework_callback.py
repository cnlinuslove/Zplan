"""
企业微信应用消息回调：收用户文本 → Z-Plan 问答 → 主动回消息。

管理后台：应用 → 接收消息 → 设置 URL 为
  https://<公网域名>/v1/wework/callback
并填写与 .env 一致的 Token、EncodingAESKey。
"""
from __future__ import annotations

import logging
import threading
import xml.etree.ElementTree as ET
from typing import Any

import config as app_config
from wechat_interact import handle_inbound_text
from wework_client import send_text_message, wework_callback_configured

logger = logging.getLogger(__name__)

_THINKING = "正在检索资讯，请稍候…"


def _xml_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def parse_inbound_xml(xml_text: str) -> dict[str, str] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    msg_type = _xml_text(root, "MsgType")
    if msg_type != "text":
        return None
    content = _xml_text(root, "Content")
    if not content:
        return None
    return {
        "msg_type": msg_type,
        "content": content,
        "from_user": _xml_text(root, "FromUserName"),
        "chat_id": _xml_text(root, "ChatId"),
        "agent_id": _xml_text(root, "AgentID"),
    }


def _reply_target(parsed: dict[str, str]) -> dict[str, str]:
    chat_id = parsed.get("chat_id") or ""
    if chat_id:
        return {"chatid": chat_id}
    return {"touser": parsed.get("from_user") or ""}


def _process_and_send(parsed: dict[str, str]) -> None:
    try:
        result = handle_inbound_text(parsed["content"])
        text = str(result.get("reply_text") or result.get("reply_markdown") or "（无回复内容）")
        send_text_message(text, **_reply_target(parsed))
    except Exception as exc:  # noqa: BLE001
        logger.exception("wework reply failed: %s", exc)
        try:
            send_text_message(
                f"处理失败：{exc.__class__.__name__}",
                **_reply_target(parsed),
            )
        except Exception:  # noqa: BLE001
            logger.exception("wework error notify failed")


def handle_wework_callback_post(
    raw_body: bytes,
    *,
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> bytes:
    """
    解密企业微信 POST 体，异步回复。HTTP 层应立即返回本函数结果（多为空串）。
    """
    if not wework_callback_configured():
        return b"not configured"

    from wechatpy.enterprise.crypto import WeChatCrypto

    crypto = WeChatCrypto(
        app_config.WECHAT_CALLBACK_TOKEN,
        app_config.WECHAT_CALLBACK_AES_KEY,
        app_config.WECHAT_CORP_ID,
    )
    decrypted = crypto.decrypt_message(
        raw_body.decode("utf-8"),
        msg_signature,
        timestamp,
        nonce,
    )
    parsed = parse_inbound_xml(decrypted)
    if not parsed:
        return b"success"

    target = _reply_target(parsed)
    if target.get("touser") or target.get("chatid"):
        send_text_message(_THINKING, **target)

    threading.Thread(target=_process_and_send, args=(parsed,), daemon=True).start()
    return b"success"


def verify_wework_callback_url(
    *,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    if not wework_callback_configured():
        raise RuntimeError("企业微信回调未配置完整")
    from wechatpy.enterprise.crypto import WeChatCrypto

    crypto = WeChatCrypto(
        app_config.WECHAT_CALLBACK_TOKEN,
        app_config.WECHAT_CALLBACK_AES_KEY,
        app_config.WECHAT_CORP_ID,
    )
    return crypto.check_signature(msg_signature, timestamp, nonce, echostr)


def wework_status() -> dict[str, Any]:
    return {
        "app_configured": wework_callback_configured(),
        "corp_id_set": bool(app_config.WECHAT_CORP_ID.strip()),
        "agent_id": app_config.WECHAT_AGENT_ID,
        "callback_path": "/v1/wework/callback",
    }
