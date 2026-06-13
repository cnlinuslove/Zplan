"""全市场规则分初始化 → ``stock_rule_scores``。

支持 v1（动量）和 v2（反转+资金流+概念热度）两套评分。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from zplan_shared.feature_store import get_features_panel
from zplan_shared.features import feature_flag, scan_universe_features
from zplan_shared.market import get_history_window, get_panel, latest_trade_date
from zplan_shared.market_health import check_market_health
from zplan_shared.models import SessionLocal, StockConceptMember, StockList, init_db
from zplan_shared.stock_rule_scores import count_scores, upsert_rule_scores

from pick_agent.scanner import (
    _apply_snapshot_filters,
    _load_stock_meta,
    _prefilter_panel,
)
from pick_agent.scoring import apply_momentum_cap, quick_technical_score, verdict_from_score
from pick_agent.scoring_v2 import (
    PRESET_SCHEMES,
    compute_score_v2,
    clear_quality_cache,
    set_quality_cache,
)
from pick_agent.strategy import PickStrategy, load_strategy

logger = logging.getLogger(__name__)


def _safe_float(val: Any) -> float | None:
    """安全转 float，None / NaN → None。"""
    if val is None:
        return None
    try:
        f = float(val)
        return None if (isinstance(f, float) and np.isnan(f)) else f
    except (TypeError, ValueError):
        return None


# ── 概念热度（生产模式）─────────────────────────────

_concept_members_cache: dict[str, list[str]] | None = None


def _load_concept_members_prod() -> dict[str, list[str]]:
    """加载概念→股票映射（首次调用后缓存）。"""
    global _concept_members_cache
    if _concept_members_cache is not None:
        return _concept_members_cache
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockConceptMember.ts_code, StockConceptMember.concept_name)
        ).all()
    result: dict[str, list[str]] = {}
    for code, concept in rows:
        result.setdefault(code, []).append(concept)
    _concept_members_cache = result
    logger.info("概念数据加载: %s 只股票", len(result))
    return result


def _compute_concept_heat_prod(
    feat_df: pd.DataFrame,
    concept_map: dict[str, list[str]],
) -> dict[str, float]:
    """从特征 DataFrame 计算概念热度（生产模式）。"""
    if "ret_20d" not in feat_df.columns or "ts_code" not in feat_df.columns:
        return {}
    code_ret = dict(zip(feat_df["ts_code"], feat_df["ret_20d"]))
    concept_rets: dict[str, list[float]] = {}
    for code, concepts in concept_map.items():
        ret = code_ret.get(code)
        if ret is None or pd.isna(ret):
            continue
        for c in concepts:
            concept_rets.setdefault(c, []).append(float(ret))
    heat: dict[str, float] = {}
    for c, rets in concept_rets.items():
        if len(rets) >= 3:
            heat[c] = sum(rets) / len(rets)
    logger.info("概念热度计算: %s 个概念", len(heat))
    return heat


# ── 行业动量（板块轮动核心信号）─────────────────────

_industry_map_cache: dict[str, str] | None = None


def _load_industry_map() -> dict[str, str]:
    """加载股票→行业映射（首次调用后缓存）。"""
    global _industry_map_cache
    if _industry_map_cache is not None:
        return _industry_map_cache
    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(StockList.ts_code, StockList.industry)
        ).all()
    result: dict[str, str] = {}
    for code, industry in rows:
        if industry:
            result[code] = industry
    _industry_map_cache = result
    logger.info("行业映射加载: %s 只股票, %s 个行业",
                len(result), len(set(result.values())))
    return result


def _compute_industry_signals(
    feat_df: pd.DataFrame,
    industry_map: dict[str, str],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """计算行业级别信号。

    Returns:
        industry_heat: {industry_name: avg_ret_20d}
        industry_rank_pct: {industry_name: rank_pctile (0-100)}
        code_relative_rank: {ts_code: within_industry_ret_20d_pctile (0-100)}
    """
    if "ret_20d" not in feat_df.columns or "ts_code" not in feat_df.columns:
        return {}, {}, {}

    # 1. 计算每个行业的平均 ret_20d
    industry_rets: dict[str, list[float]] = {}
    for _, row in feat_df.iterrows():
        code = str(row["ts_code"])
        ret = row.get("ret_20d")
        if ret is None or pd.isna(ret):
            continue
        ind = industry_map.get(code)
        if not ind:
            continue
        industry_rets.setdefault(ind, []).append(float(ret))

    industry_heat: dict[str, float] = {}
    for ind, rets in industry_rets.items():
        if len(rets) >= 5:  # 至少5只股票才有效
            industry_heat[ind] = sum(rets) / len(rets)

    if not industry_heat:
        logger.warning("行业热度计算失败：无有效行业数据")
        return {}, {}, {}

    # 2. 行业排名（百分位）
    sorted_inds = sorted(industry_heat.items(), key=lambda x: x[1])
    n = len(sorted_inds)
    industry_rank_pct: dict[str, float] = {}
    for rank, (ind, _) in enumerate(sorted_inds):
        industry_rank_pct[ind] = round(rank / (n - 1) * 100, 1) if n > 1 else 50.0

    # 3. 行业内个股相对排名
    code_relative_rank: dict[str, float] = {}
    for ind, rets in industry_rets.items():
        if len(rets) < 3:
            continue
        # 对行业内的 ret_20d 排序，计算每个值的百分位
        sorted_vals = sorted(rets)
        m = len(sorted_vals)
        # 用 scipy 风格的分位数映射
        val_to_rank = {}
        for i, v in enumerate(sorted_vals):
            val_to_rank[v] = round(i / (m - 1) * 100, 1) if m > 1 else 50.0

        # 回填到个股
        for _, row in feat_df.iterrows():
            code = str(row["ts_code"])
            if industry_map.get(code) != ind:
                continue
            ret = row.get("ret_20d")
            if ret is None or pd.isna(ret) or float(ret) not in val_to_rank:
                continue
            code_relative_rank[code] = val_to_rank[float(ret)]

    top5 = sorted(industry_heat.items(), key=lambda x: -x[1])[:5]
    bottom5 = sorted(industry_heat.items(), key=lambda x: x[1])[:5]
    logger.info(
        "行业动量: %s 个行业, Top5: %s, Bottom5: %s",
        len(industry_heat),
        [(ind, f"{v:+.1f}%") for ind, v in top5],
        [(ind, f"{v:+.1f}%") for ind, v in bottom5],
    )

    return industry_heat, industry_rank_pct, code_relative_rank


def _signals_from_features(features: dict[str, float | None]) -> list[str]:
    signals: list[str] = []
    ma5, ma20, ma60 = features.get("ma5"), features.get("ma20"), features.get("ma60")
    if ma5 and ma20 and ma60 and ma5 > ma20 > ma60:
        signals.append("均线多头排列（MA5>MA20>MA60）")
    if feature_flag(features, "ma5_cross_ma20"):
        signals.append("MA5 上穿 MA20")
    if feature_flag(features, "kdj_golden_cross"):
        signals.append("KDJ 金叉")
    elif feature_flag(features, "kdj_death_cross"):
        signals.append("KDJ 死叉")
    if feature_flag(features, "macd_cross_up"):
        signals.append("MACD 柱由负转正")
    if feature_flag(features, "vol_breakout") and (features.get("ret_5d") or 0) > 0:
        signals.append("放量上涨")
    h60 = features.get("high_60d_pct")
    if h60 is not None and h60 >= 98:
        signals.append("接近 60 日新高")
    ret20 = features.get("ret_20d")
    if ret20 is not None and -3 <= ret20 <= 2:
        signals.append("20 日回撤低吸区")
    return signals[:5]


def build_rule_scores_universe(
    *,
    strategy: PickStrategy | None = None,
    skip_health_check: bool = False,
    use_v2: bool = False,
    as_of: date | None = None,
    v2_preset: str | None = None,
) -> dict[str, Any]:
    """向量化规则分写入 ``stock_rule_scores``（全预筛池，非仅 Top N）。

    Args:
        use_v2: True 时使用 scoring_v2 的预设方案（默认 reversal_flow_concept）。
        as_of: 指定截面日期（默认最新）。A/B 回放用历史日期。
        v2_preset: v2 预设方案名，覆盖默认（如 "reversal_only", "tech_plus_financial"）。
    """
    strat = strategy or load_strategy()
    # v2_preset: 显式传入 > strategy.yaml 配置 > 默认 reversal_flow_concept
    if v2_preset is None:
        v2_preset = getattr(strat, "v2_preset", None) or "reversal_flow_concept"

    if not skip_health_check and as_of is None:
        health = check_market_health(
            min_panel_rows=strat.min_panel_rows,
            max_stale_days=strat.max_stale_days,
        )
        if not health.ok:
            return {"ok": False, "message": health.message, "health": health.__dict__}

    trade_date = as_of or latest_trade_date()
    if trade_date is None:
        return {"ok": False, "message": "无日线数据，请先运行 zplan-股价"}

    panel = get_panel(trade_date, fields=["close", "pct_chg", "turnover_rate", "volume"])
    if panel.empty:
        return {"ok": False, "message": "截面为空"}

    meta = _load_stock_meta()
    filtered = _prefilter_panel(panel, meta, strat)
    filtered = _apply_snapshot_filters(filtered, strat, trade_date)
    if filtered.empty:
        return {"ok": False, "message": "预过滤后无标的"}

    codes = filtered["ts_code"].tolist()
    feat_source = "computed"
    feat_panel = get_features_panel(trade_date)
    if not feat_panel.empty:
        feat_df = feat_panel.merge(
            filtered[["ts_code"]],
            on="ts_code",
            how="inner",
        )
        if len(feat_df) >= len(codes) * 0.5:
            feat_source = "daily_features"
            logger.info(
                "规则初始化：使用 daily_features 物化表 %s 只（预筛 %s）",
                len(feat_df),
                len(codes),
            )
        else:
            feat_df = pd.DataFrame()
    else:
        feat_df = pd.DataFrame()

    if feat_df.empty:
        return {
            "ok": False,
            "message": f"daily_features 不完整，请等待 daily_features 物化完成后再运行 init-rule（预筛 {len(codes)} 只）",
        }

    max_ret = strat.filters.get("max_ret_20d")
    if max_ret is not None and "ret_20d" in feat_df.columns:
        feat_df = feat_df[
            feat_df["ret_20d"].isna() | (feat_df["ret_20d"] <= float(max_ret))
        ]
    if feat_df.empty:
        return {"ok": False, "message": "预筛后无标的（20日涨幅过滤过严）"}

    name_map = dict(zip(meta["ts_code"], meta["name"].fillna("")))
    close_map = dict(zip(filtered["ts_code"], filtered["close"]))

    # ── v2 模式：概念热度 + 行业动量 + 新评分 ──
    if use_v2:
        concept_map = _load_concept_members_prod()
        concept_heat = _compute_concept_heat_prod(feat_df, concept_map)
        industry_map = _load_industry_map()
        industry_heat, industry_rank_pct, code_relative_rank = _compute_industry_signals(
            feat_df, industry_map
        )
        v2_factors, v2_weights = PRESET_SCHEMES[v2_preset]
        logger.info(
            "v2 模式启用: %s 个因子, 概念 %s 个, 行业 %s 个, 热门概念 Top5: %s",
            len(v2_factors),
            len(concept_heat),
            len(industry_heat),
            sorted(concept_heat.items(), key=lambda x: -x[1])[:5] if concept_heat else [],
        )
        # 板块轮动日志（关键监控信息）
        if industry_heat:
            top_inds = sorted(industry_heat.items(), key=lambda x: -x[1])[:5]
            bottom_inds = sorted(industry_heat.items(), key=lambda x: x[1])[:5]
            logger.info("📈 领涨行业: %s", [(ind, f"{v:+.1f}%") for ind, v in top_inds])
            logger.info("📉 领跌行业: %s", [(ind, f"{v:+.1f}%") for ind, v in bottom_inds])

        # ── 筹码峰注入（灰度开关）──
        _chip_lookup: dict[str, dict[str, float | None]] = {}
        if strat.filters.get("enable_chip_factors"):
            try:
                from zplan_shared.market import get_chip_panel

                _chip_df = get_chip_panel(as_of=trade_date)
                if not _chip_df.empty:
                    for _, cr in _chip_df.iterrows():
                        ccode = str(cr["ts_code"])
                        avg = cr.get("avg_cost")
                        close_v = close_map.get(ccode) if ccode in close_map else None
                        cp_val = None
                        if (
                            close_v is not None
                            and avg is not None
                            and float(avg) > 0
                            and not pd.isna(close_v)
                        ):
                            cp_val = (float(close_v) - float(avg)) / float(avg) * 100.0
                        _chip_lookup[ccode] = {
                            "_profit_ratio": _safe_float(cr.get("profit_ratio")),
                            "_avg_cost": _safe_float(avg),
                            "_concentration_90": _safe_float(cr.get("concentration_90")),
                            "_concentration_70": _safe_float(cr.get("concentration_70")),
                            "_cost_proximity": cp_val,
                        }
                    logger.info(
                        "筹码峰注入: %s/%s 票命中",
                        len(_chip_lookup),
                        len(feat_df),
                    )
                    # 启用筹码因子时切换 preset（除非显式指定了 preset）
                    if not v2_preset:
                        v2_factors, v2_weights = PRESET_SCHEMES["full_tech_plus_chip"]
            except Exception:
                logger.warning("筹码峰数据加载失败，降级为无筹码模式", exc_info=True)

    # ── 分数稳定性数据注入 ──
    as_of_d = date.fromisoformat(str(trade_date)[:10])

    _stability_lookup: dict[str, dict[str, float | None]] = {}
    strat_cfg = strat.dict().get("penalty_weights", {}) if hasattr(strat, 'dict') else {}
    stab_cfg = strat_cfg.get("stability", {}) if isinstance(strat_cfg, dict) else {}
    if stab_cfg.get("enabled", True):
        try:
            from pick_agent.stability import load_stability_for_stocks
            codes_list = [str(r["ts_code"]) for _, r in feat_df.iterrows()]
            if codes_list:
                stab_data = load_stability_for_stocks(
                    codes_list, as_of_d,
                    lookback_days=10, rule_version=strat.rule_version,
                )
                for code, info in stab_data.items():
                    _stability_lookup[code] = {
                        "_stability_std_10d": info.get("score_std_10d"),
                        "_stability_slope_5d": info.get("score_slope_5d"),
                        "_stability_slope_10d": info.get("score_slope_10d"),
                        "_stability_direction_flips": info.get("score_direction_flips"),
                        "_stability_rank_std": info.get("rank_stability_10d"),
                    }
                logger.info(
                    "稳定性注入: %s/%s 票命中",
                    len(_stability_lookup), len(codes_list),
                )
        except Exception:
            logger.warning("稳定性数据加载失败，降级为无稳定性模式", exc_info=True)

    rows: list[dict[str, Any]] = []
    for _, r in feat_df.iterrows():
        code = str(r["ts_code"])
        features = {k: r[k] for k in r.index if k != "ts_code" and pd.notna(r[k])}
        ret20 = features.get("ret_20d")

        if use_v2:
            # 注入概念数据到特征
            concepts = concept_map.get(code, [])
            heats = [concept_heat.get(c) for c in concepts if c in concept_heat]
            features["_concept_heat"] = float(np.mean(heats)) if heats else 0.0
            features["_concept_count"] = float(len(concepts))
            # 注入行业动量数据
            code_ind = industry_map.get(code, "")
            if code_ind:
                features["_industry_heat"] = industry_heat.get(code_ind, 0.0)
                features["_industry_rank_pct"] = industry_rank_pct.get(code_ind)
            else:
                features["_industry_heat"] = 0.0
                features["_industry_rank_pct"] = None
            features["_industry_relative_rank"] = code_relative_rank.get(code)
            # 注入筹码峰数据
            chip_data = _chip_lookup.get(code)
            if chip_data:
                features.update(chip_data)
            # 注入分数稳定性数据
            stability_data = _stability_lookup.get(code)
            if stability_data:
                features.update(stability_data)
            # v2 评分
            score = compute_score_v2(
                features, factors=v2_factors, weights=v2_weights, code=code
            )
        else:
            score = quick_technical_score(features)
            score = apply_momentum_cap(score, ret20, max_ret_20d=max_ret)

        score = round(score, 1)

        # ── 行业信号（用于展示）──
        ind_signals = []
        code_ind = industry_map.get(code, "")
        if code_ind:
            ind_heat_val = industry_heat.get(code_ind)
            ind_rank = industry_rank_pct.get(code_ind)
            ind_rel = code_relative_rank.get(code)
            if ind_heat_val is not None:
                ind_signals.append(f"行业「{code_ind}」20日涨幅 {ind_heat_val:+.1f}%")
            if ind_rank is not None:
                if ind_rank >= 80:
                    ind_signals.append(f"领涨板块（前{100-ind_rank:.0f}%）")
                elif ind_rank < 20:
                    ind_signals.append(f"领跌板块（后{ind_rank:.0f}%）")
            if ind_rel is not None and ind_rel >= 80:
                ind_signals.append("行业龙头溢价")

        rows.append(
            {
                "ts_code": code,
                "name": name_map.get(code),
                "tech_score": score,
                "composite_score": score,
                "verdict": verdict_from_score(score),
                "close": float(close_map[code]) if code in close_map and pd.notna(close_map.get(code)) else features.get("close"),
                "signals": _signals_from_features(features) + ind_signals,
                "features": {
                    k: features[k]
                    for k in (
                        "ret_5d",
                        "ret_20d",
                        "kdj_k",
                        "kdj_d",
                        "ma5",
                        "ma20",
                        "macd_hist",
                        "high_60d_pct",
                        "_industry_heat",
                        "_industry_rank_pct",
                        "_industry_relative_rank",
                    )
                    if k in features
                },
            }
        )

    n = upsert_rule_scores(rows, trade_date_as_of=as_of_d, rule_version=strat.rule_version)
    total = count_scores(trade_date_as_of=as_of_d, rule_version=strat.rule_version)

    return {
        "ok": True,
        "as_of": str(trade_date),
        "rule_version": strat.rule_version,
        "panel_rows": len(panel),
        "prefiltered": len(filtered),
        "scored": n,
        "table_total": total,
        "feat_source": feat_source,
    }
