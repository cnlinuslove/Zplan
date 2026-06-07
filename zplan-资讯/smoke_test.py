from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 烟测：轻量模式（须在业务 import 之前设置）
os.environ.setdefault("SMOKE_TEST", "1")
os.environ.setdefault("DEEPSEEK_MIN_SECONDS_BETWEEN_CALLS", "0")
os.environ.setdefault("GEMINI_MIN_SECONDS_BETWEEN_TOPICS", "0")
os.environ.setdefault("GEMINI_MIN_SECONDS_BETWEEN_CALLS", "0")
os.environ.setdefault("LLM_SUMMARY_ENABLED", "false")

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
    _assert(stats["topics"] >= 2, "run_cycle topics should be >= 2 in smoke mode")
    _assert(stats["saved_runs"] >= 2, "run_cycle saved_runs should be >= 2 in smoke mode")


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

    via_http = process_wechat_reply_request({"text": "帮助", "push": False})
    _assert(via_http.get("ok") is True and via_http.get("intent") == "help", "wechat http bridge parity")

    import os

    os.environ.setdefault("PICK_WECHAT_USE_LLM", "false")
    pick = handle_inbound_text("选股 爱普股份")
    _assert(
        str(pick.get("intent", "")).startswith("pick"),
        f"wechat pick intent, got {pick.get('intent')}",
    )
    _assert("爱普" in pick.get("reply_text", "") or "603" in pick.get("reply_text", ""), "wechat pick body")

    from wework_callback import parse_inbound_xml

    sample = (
        "<xml><MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[北向资金]]></Content>"
        "<FromUserName><![CDATA[user1]]></FromUserName>"
        "<ChatId><![CDATA[chat1]]></ChatId></xml>"
    )
    parsed = parse_inbound_xml(sample)
    _assert(parsed and parsed["content"] == "北向资金" and parsed["chat_id"] == "chat1", "wework xml parse")


def test_news_stock_link() -> None:
    from zplan_shared.news_linker import match_stocks_in_text

    hits = match_stocks_in_text(
        "5月18日北向资金净买入768万元，600519贵州茅台获增持",
        alias_dict={"贵州茅台": "600519", "茅台": "600519"},
    )
    codes = {h.ts_code for h in hits}
    _assert("600519" in codes, "regex/name_dict should match 600519")
    _assert(any(h.matched_by == "regex_code" for h in hits), "regex_code match")


def test_error_mapping() -> None:
    err = XApiHttpError(status_code=401, body="authorization failed")
    mapped = map_exception_to_user_error(err)
    _assert(mapped["code"] == "X_AUTH_INVALID", "error mapping for 401 failed")


def test_scheduler_once() -> None:
    """daily_update CLI 可加载且 --help 正常（完整 --once 由 test_run_cycle 覆盖）。"""
    if not PYTHON.exists():
        raise AssertionError("missing .venv python, run dependency install first")
    result = subprocess.run(
        [str(PYTHON), "daily_update.py", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    _assert(result.returncode == 0, "daily_update --help failed")
    _assert("--once" in (result.stdout or ""), "daily_update missing --once flag")


def main() -> None:
    tests = [
        ("run_cycle", test_run_cycle),
        ("history_payload", test_history_payload),
        ("topic_crud", test_topic_crud),
        ("wechat_interact", test_wechat_interact),
        ("news_stock_link", test_news_stock_link),
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
