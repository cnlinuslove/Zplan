"""规则分 Top N → 深度规则复核 → LLM 二次打分 → ``pick_runs``。"""
from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any

import pandas as pd

from zplan_shared.fundamentals import get_financials
from zplan_shared.llm.gemini import gemini_available
from zplan_shared.market import get_bars, get_panel, latest_trade_date
from zplan_shared.pick_store import save_scan_run
from zplan_shared.stock_rule_scores import latest_score_date, top_rule_scores

from zplan_shared.pick_context import get_pick_context

from pick_agent.llm_research import brief_review_scan_picks

from pick_agent.scoring import (
    apply_momentum_cap,
    composite_score,
    financial_score_from_rows,
    industry_relative_score,
    intraday_adjust,
    momentum_penalty,
    news_score,
)
from pick_agent.scanner import _load_stock_meta
from pick_agent.ranking import assign_ranks, sort_picks_for_rank
from pick_agent.strategy import PickStrategy, load_strategy
from pick_agent.concept_tags import attach_concepts
from pick_agent.technical import analyze_technical, price_levels

logger = logging.getLogger(__name__)


def _deepen_one_pick(
    p: dict[str, Any],
    *,
    strategy: PickStrategy,
    trade_date,
    industry_map: dict[str, str | None],
    ret_by_industry: dict[str, list[float]],
    panel: pd.DataFrame,
) -> dict[str, Any]:
    code = p["ts_code"]
    tech = analyze_technical(code, min_bars=strategy.min_bars)
    ctx = get_pick_context(code)
    fin_df = get_financials(code, limit=8)
    fin_rows = fin_df.to_dict("records") if not fin_df.empty else []
    fin_sc, _ = financial_score_from_rows(fin_rows)
    news_sc, news_detail = news_score(ctx)
    ind_sc, ind_note = industry_relative_score(
        code,
        tech.features.get("ret_20d"),
        industry_map,
        ret_by_industry,
    )
    intra_adj = intraday_adjust(tech, ctx, strategy)
    final = composite_score(
        tech=tech,
        fin_score=fin_sc,
        news_sc=news_sc,
        industry_sc=ind_sc,
        intraday_adj=intra_adj,
        strategy=strategy,
    )
    final = apply_momentum_cap(
        final,
        tech.features.get("ret_20d"),
        max_ret_20d=strategy.filters.get("max_ret_20d"),
    )
    row_panel = panel.loc[panel["ts_code"] == code]
    close = p.get("close")
    if not row_panel.empty and pd.notna(row_panel.iloc[0].get("close")):
        close = float(row_panel.iloc[0]["close"])
    elif tech.close is not None:
        close = tech.close
    bars = get_bars(code)
    # 截断到 trade_date，确保买入价基于选股日收盘价而非最新K线
    bars_for_price = bars
    if not bars.empty and trade_date is not None:
        # get_bars 返回的 index 是 date 对象，需转为 DatetimeIndex 才能与 Timestamp 比较
        bars.index = pd.DatetimeIndex(pd.to_datetime(bars.index))
        bars_for_price = bars[bars.index <= pd.Timestamp(trade_date)]
        if bars_for_price.empty:
            bars_for_price = bars
    levels = price_levels(bars_for_price) if not bars_for_price.empty else {}
    return attach_concepts({
        **p,
        "name": p.get("name") or ctx.get("name"),
        "industry": industry_map.get(code),
        "close": close,
        "tech_score": tech.score,
        "composite_score": final,
        "rule_composite_score": final,
        "verdict": tech.verdict,
        "signals": tech.signals[:5],
        "kdj_k": tech.features.get("kdj_k"),
        "kdj_d": tech.features.get("kdj_d"),
        "ret_20d": tech.features.get("ret_20d"),
        "high_60d_pct": tech.features.get("high_60d_pct"),
        "vol_ratio20": tech.features.get("vol_ratio20"),
        "industry_relative_note": ind_note,
        "news_mentions_48h": news_detail.get("hits", 0),
        "volume_ratio_vs_prior": (ctx.get("intraday") or {}).get("volume_ratio_vs_prior"),
        "predicted_buy_price": levels.get("suggested_buy"),
        "predicted_target_price": levels.get("target_price"),
        "predicted_stop_loss": levels.get("stop_loss"),
        "price_source": "rule",
        "rank_rule": p.get("rank_rule"),
    })


def _deepen_picks(
    picks: list[dict[str, Any]],
    *,
    strategy: PickStrategy,
    trade_date,
    workers: int | None = None,
) -> list[dict[str, Any]]:
    """对 Top 池做与扫描一致的深度规则综合分（技术+财务+资讯+行业）。"""
    meta = _load_stock_meta()
    industry_map = dict(zip(meta["ts_code"], meta["industry"].fillna("")))
    panel = get_panel(trade_date, fields=["close", "pct_chg", "turnover_rate", "volume"])
    ret_by_industry: dict[str, list[float]] = {}
    for p in picks:
        r20 = p.get("ret_20d")
        ind = industry_map.get(p["ts_code"])
        if ind and r20 is not None:
            ret_by_industry.setdefault(ind, []).append(float(r20))

    n_workers = workers if workers is not None else int(os.getenv("PICK_DEEPEN_WORKERS", "8"))
    n_workers = max(1, min(n_workers, 16))
    logger.info("深度规则复核 %s 只，workers=%s", len(picks), n_workers)

    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [
            pool.submit(
                _deepen_one_pick,
                p,
                strategy=strategy,
                trade_date=trade_date,
                industry_map=industry_map,
                ret_by_industry=ret_by_industry,
                panel=panel,
            )
            for p in picks
        ]
        for i, fut in enumerate(as_completed(futures), 1):
            out.append(fut.result())
            if i % 50 == 0 or i == len(picks):
                logger.info("深度规则复核 %s/%s", i, len(picks))
    return out


def _compute_prompt_hash(strategy: PickStrategy) -> str:
    """计算本次运行的 prompt + 策略指纹（用于回溯：这次 run 用了什么配置）。"""
    from pick_agent.llm_research import _LLM_BRIEF_RULES

    payload = (
        _LLM_BRIEF_RULES
        + str(strategy.rule_version)
        + str(strategy.weights)
        + str(strategy.filters)
        + str(strategy.llm_model)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def run_llm_top_from_rule_scores(
    *,
    top_n: int | None = None,
    batch_size: int | None = None,
    strategy: PickStrategy | None = None,
    trade_date_as_of: date | None = None,
    deepen: bool = True,
    deepen_workers: int | None = None,
    use_llm: bool = True,
    persist: bool = True,
    variant_label: str | None = None,
) -> dict[str, Any]:
    """从 ``stock_rule_scores`` 取 Top N，深度规则 + LLM 简评，写入 ``pick_runs``。

    ``variant_label`` 用于 A/B 实验标记（同一天多策略并行对比）。
    """
    strat = strategy or load_strategy()
    top_n = top_n if top_n is not None else strat.llm_top_n
    batch_size = batch_size if batch_size is not None else strat.llm_batch_size
    prompt_hash = _compute_prompt_hash(strat)
    as_of_d = trade_date_as_of
    if as_of_d is None:
        as_of_d = latest_score_date(rule_version=strat.rule_version)
    if as_of_d is None:
        return {
            "ok": False,
            "message": "无规则分快照，请先运行：main.py init-rule",
        }

    shallow = top_rule_scores(
        trade_date_as_of=as_of_d,
        rule_version=strat.rule_version,
        top_n=top_n,
    )
    if not shallow:
        return {"ok": False, "message": f"{as_of_d} 无规则分记录"}

    trade_date = latest_trade_date()
    picks = (
        _deepen_picks(shallow, strategy=strat, trade_date=trade_date, workers=deepen_workers)
        if deepen
        else list(shallow)
    )
    # 排序对齐 scan：composite_score − momentum_penalty(ret_20d)
    max_ret_20d = strat.filters.get("max_ret_20d") if strat.filters else None
    picks = sorted(
        picks,
        key=lambda x: (
            (x.get("composite_score") or 0)
            - momentum_penalty(x.get("ret_20d"), max_ret_20d=max_ret_20d),
            x.get("tech_score") or 0,
        ),
        reverse=True,
    )

    llm_usage = None
    if use_llm and gemini_available() and strat.llm_enabled:
        picks, llm_usage = brief_review_scan_picks(
            picks,
            as_of=str(as_of_d),
            model=strat.llm_model,
            batch_size=batch_size,
        )
    elif use_llm and not gemini_available():
        logger.warning("DEEPSEEK_API_KEY 未配置，仅输出深度规则分")

    if strat.resort_after_llm:
        picks = sort_picks_for_rank(picks, strat)
    assign_ranks(picks)

    result: dict[str, Any] = {
        "ok": True,
        "agent": "pick",
        "run_kind": "llm_top300",
        "as_of": str(as_of_d),
        "rule_version": strat.rule_version,
        "source": "stock_rule_scores",
        "top_n": top_n,
        "deepen": deepen,
        "picks": picks,
        "qualified": len(picks),
        "llm_scan_brief": use_llm and gemini_available(),
        "llm_usage": llm_usage,
        "variant_label": variant_label,
        "prompt_hash": prompt_hash,
    }

    if persist:
        run_id = save_scan_run(
            result,
            params={
                "top_n": top_n,
                "batch_size": batch_size,
                "source": "stock_rule_scores",
                "deepen": deepen,
            },
            variant_label=variant_label,
            prompt_hash=prompt_hash,
        )
        result["run_id"] = run_id

    return result
