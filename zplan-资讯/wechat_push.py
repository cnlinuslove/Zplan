from __future__ import annotations

import logging

import requests

from config import WECHAT_PUSH_WEBHOOK

logger = logging.getLogger(__name__)


def push_wechat_text(message: str) -> bool:
    if not WECHAT_PUSH_WEBHOOK:
        logger.warning("未配置 WECHAT_PUSH_WEBHOOK，跳过推送。")
        return False
    payload = {"msgtype": "text", "text": {"content": message[:1800]}}
    try:
        resp = requests.post(WECHAT_PUSH_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("errcode", 0) not in (0, None):
            logger.warning("微信推送业务失败: %s", data)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("微信推送失败: %s", exc)
        return False
