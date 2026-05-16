from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agents.news_agent import (
    XApiHttpError,
    get_history_payload,
    map_exception_to_user_error,
    run_news_cycle,
)
from topic_admin import add_topic, delete_topic, list_topics, update_topic
from wechat_http_bridge import process_wechat_reply_request
from wechat_interact import handle_inbound_text


ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "bin" / "python"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_run_cycle() -> None:
    stats = run_news_cycle()
    _assert(stats["topics"] >= 7, "run_cycle topics should be >= 7")
    _assert(stats["saved_runs"] >= 7, "run_cycle saved_runs should be >= 7")


def test_history_payload() -> None:
    latest = get_history_payload(mode="latest")
    _assert(latest["count"] > 0, "latest history should not be empty")
    _assert("items" in latest and isinstance(latest["items"], list), "latest items missing")

    seven_days = get_history_payload(mode="7d", topic_key="trump_updates")
    _assert(seven_days["mode"] == "7d", "7d mode mismatch")
    _assert(seven_days["topic_key"] == "trump_updates", "topic key mismatch")


def test_topic_crud() -> None:
    key = "smoke_topic"
    delete_topic(key, echo=False)

    add_res = add_topic(
        topic_key=key,
        display_name="Smoke Topic",
        query="smoke test query",
        enabled=True,
        echo=False,
    )
    _assert(add_res["topic_key"] == key, "topic add failed")

    upd_res = update_topic(
        topic_key=key,
        display_name="Smoke Topic Updated",
        query="smoke test query updated",
        enabled=False,
        echo=False,
    )
    _assert(upd_res["enabled"] is False, "topic update failed")

    topics = list_topics(echo=False)
    _assert(any(t["topic_key"] == key for t in topics), "topic list missing added topic")

    del_res = delete_topic(key, echo=False)
    _assert(del_res["deleted"] is True, "topic delete failed")


def test_wechat_interact() -> None:
    h = handle_inbound_text("帮助")
    _assert(h["intent"] == "help", "wechat help intent")
    _assert("最新" in h.get("reply_text", ""), "wechat help should mention 最新")

    lst = handle_inbound_text("列表")
    _assert(lst["intent"] == "topic_list", "wechat topic list intent")
    _assert("Topic 列表" in lst.get("reply_text", ""), "wechat list body")

    qa = handle_inbound_text("美联储")
    _assert(qa["intent"] == "info_query", "wechat info_query intent")
    _assert(qa.get("reply_text"), "info_query should return text")

    via_http = process_wechat_reply_request({"text": "帮助", "push": False})
    _assert(via_http.get("ok") is True and via_http.get("intent") == "help", "wechat http bridge parity")

    from wework_callback import parse_inbound_xml

    sample = (
        "<xml><MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[北向资金]]></Content>"
        "<FromUserName><![CDATA[user1]]></FromUserName>"
        "<ChatId><![CDATA[chat1]]></ChatId></xml>"
    )
    parsed = parse_inbound_xml(sample)
    _assert(parsed and parsed["content"] == "北向资金" and parsed["chat_id"] == "chat1", "wework xml parse")


def test_error_mapping() -> None:
    err = XApiHttpError(status_code=401, body="authorization failed")
    mapped = map_exception_to_user_error(err)
    _assert(mapped["code"] == "X_AUTH_INVALID", "error mapping for 401 failed")


def test_scheduler_once() -> None:
    if not PYTHON.exists():
        raise AssertionError("missing .venv python, run dependency install first")
    cmd = [str(PYTHON), "daily_update.py", "--once"]
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    _assert(result.returncode == 0, "scheduler once command failed")


def main() -> None:
    tests = [
        ("run_cycle", test_run_cycle),
        ("history_payload", test_history_payload),
        ("topic_crud", test_topic_crud),
        ("wechat_interact", test_wechat_interact),
        ("error_mapping", test_error_mapping),
        ("scheduler_once", test_scheduler_once),
    ]
    report: list[dict] = []
    for name, fn in tests:
        fn()
        report.append({"test": name, "ok": True})

    print(json.dumps({"ok": True, "tests": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": exc.__class__.__name__, "detail": str(exc)}},
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(1)
