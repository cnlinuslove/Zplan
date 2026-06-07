from __future__ import annotations

import base64
import hashlib
import logging

import requests

from config import WECHAT_PUSH_WEBHOOK

from wechat_limits import WECHAT_TEXT_MAX_BYTES, truncate_wechat_utf8

logger = logging.getLogger(__name__)


WECHAT_MARKDOWN_MAX_BYTES = 3800  # 企微 markdown 上限 4096，留余量


def push_wechat_text(message: str) -> bool:
    if not WECHAT_PUSH_WEBHOOK:
        logger.warning("未配置 WECHAT_PUSH_WEBHOOK，跳过推送。")
        return False
    payload = {
        "msgtype": "text",
        "text": {"content": truncate_wechat_utf8(message, WECHAT_TEXT_MAX_BYTES)},
    }
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


def push_wechat_markdown(message: str) -> bool:
    """企微群机器人 markdown 推送（上限 4096 字节）。超长自动截断。"""
    if not WECHAT_PUSH_WEBHOOK:
        logger.warning("未配置 WECHAT_PUSH_WEBHOOK，跳过推送。")
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": truncate_wechat_utf8(message, WECHAT_MARKDOWN_MAX_BYTES)},
    }
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


def push_wechat_image(image_path: str) -> bool:
    """企微群机器人图片推送（msgtype: image，base64 编码）。"""
    if not WECHAT_PUSH_WEBHOOK:
        logger.warning("未配置 WECHAT_PUSH_WEBHOOK，跳过图片推送。")
        return False

    try:
        with open(image_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")
        md5 = hashlib.md5(raw).hexdigest()
    except FileNotFoundError:
        logger.warning("图片文件不存在: %s", image_path)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取图片失败: %s", exc)
        return False

    payload = {
        "msgtype": "image",
        "image": {"base64": b64, "md5": md5},
    }
    try:
        resp = requests.post(WECHAT_PUSH_WEBHOOK, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("errcode", 0) not in (0, None):
            logger.warning("微信图片推送业务失败: %s", data)
            return False
        logger.info("微信图片推送成功: %s", image_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("微信图片推送失败: %s", exc)
        return False


def push_wechat_file(file_path: str) -> bool:
    """推送文件到企微群（PDF 报告等）。先上传获取 media_id，再发送 file 消息。

    Returns:
        bool: True 表示推送成功。
    """
    if not WECHAT_PUSH_WEBHOOK:
        logger.warning("未配置 WECHAT_PUSH_WEBHOOK，跳过文件推送。")
        return False
    if not os.path.exists(file_path):
        logger.warning("文件不存在: %s", file_path)
        return False

    # 从 webhook URL 提取 key
    # URL 格式: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=XXXXX
    import re
    match = re.search(r"key=([^&]+)", WECHAT_PUSH_WEBHOOK)
    if not match:
        logger.warning("无法从 webhook URL 提取 key")
        return False
    key = match.group(1)

    try:
        # Step 1: 上传文件获取 media_id
        upload_url = (
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
            f"?key={key}&type=file"
        )
        with open(file_path, "rb") as f:
            resp = requests.post(
                upload_url,
                files={"media": (os.path.basename(file_path), f)},
                timeout=30,
            )
        resp.raise_for_status()
        upload_data = resp.json()
        media_id = upload_data.get("media_id")
        if not media_id:
            logger.warning("文件上传失败，未获取 media_id: %s", upload_data)
            return False
        logger.info("文件上传成功，media_id=%s", media_id)

        # Step 2: 发送文件消息
        payload = {
            "msgtype": "file",
            "file": {"media_id": media_id},
        }
        resp2 = requests.post(WECHAT_PUSH_WEBHOOK, json=payload, timeout=15)
        resp2.raise_for_status()
        data2 = resp2.json()
        if isinstance(data2, dict) and data2.get("errcode", 0) not in (0, None):
            logger.warning("微信文件推送业务失败: %s", data2)
            return False
        logger.info("微信文件推送成功: %s", file_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("微信文件推送失败: %s", exc)
        return False
