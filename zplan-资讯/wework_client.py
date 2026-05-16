"""企业微信应用：access_token 缓存与主动发消息。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

import config as app_config

logger = logging.getLogger(__name__)

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}


def wework_app_configured() -> bool:
    return bool(
        app_config.WECHAT_CORP_ID.strip()
        and app_config.WECHAT_CORP_SECRET.strip()
        and app_config.WECHAT_AGENT_ID > 0
    )


def wework_callback_configured() -> bool:
    return bool(
        wework_app_configured()
        and app_config.WECHAT_CALLBACK_TOKEN.strip()
        and app_config.WECHAT_CALLBACK_AES_KEY.strip()
    )


def get_access_token(*, force: bool = False) -> str:
    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return str(_token_cache["token"])
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        resp = requests.get(
            url,
            params={
                "corpid": app_config.WECHAT_CORP_ID,
                "corpsecret": app_config.WECHAT_CORP_SECRET,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"gettoken failed: {data}")
        token = str(data["access_token"])
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + int(data.get("expires_in", 7200))
        return token


def send_text_message(
    content: str,
    *,
    touser: str | None = None,
    chatid: str | None = None,
) -> dict[str, Any]:
    """向成员或群聊（chatid）发送文本。"""
    if not wework_app_configured():
        return {"ok": False, "reason": "未配置企业微信应用 WECHAT_CORP_ID/SECRET/AGENT_ID"}
    text = (content or "")[:1800]
    body: dict[str, Any] = {
        "msgtype": "text",
        "agentid": app_config.WECHAT_AGENT_ID,
        "text": {"content": text},
        "safe": 0,
    }
    if chatid:
        body["chatid"] = chatid
    elif touser:
        body["touser"] = touser
    else:
        return {"ok": False, "reason": "missing touser or chatid"}

    token = get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    resp = requests.post(url, json=body, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode", 0) != 0:
        logger.warning("企业微信发消息失败: %s", data)
        return {"ok": False, "errcode": data.get("errcode"), "errmsg": data.get("errmsg")}
    return {"ok": True, "msgid": data.get("msgid")}
