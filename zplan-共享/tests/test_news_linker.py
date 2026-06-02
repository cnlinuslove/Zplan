"""news_linker 规则单测。"""
from zplan_shared.news_linker import (
    _alias_is_source_attribution,
    match_stocks_in_text,
)


def test_attribution_skips_media_alias():
    text = "凌晨大涨！美联储突传大消息！ - 东方财富"
    assert _alias_is_source_attribution(text, "东方财富")
    matches = match_stocks_in_text(text, alias_dict={"东方财富": "300059", "比亚迪": "002594"})
    codes = {m.ts_code for m in matches}
    assert "300059" not in codes


def test_code_patterns():
    text = "关注 SH:600519 与【000001】及 300059.SZ"
    matches = match_stocks_in_text(text, alias_dict={})
    codes = {m.ts_code for m in matches}
    assert "600519" in codes
    assert "000001" in codes
    assert "300059" in codes


def test_suffix_alias():
    matches = match_stocks_in_text(
        "比亚迪发布业绩预告",
        alias_dict={"比亚迪": "002594"},
    )
    assert any(m.ts_code == "002594" for m in matches)
