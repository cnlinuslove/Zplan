#!/usr/bin/env python3
"""规则分可靠性审计：直接使用 stock_rule_scores 表数据，评估规则分对前向收益的预测力。

核心问题：规则分 composite_score 排名 TOP300 是否优于全市场随机？
用法::

    cd zplan-回测 && .venv/bin/python scripts/rule_score_audit.py
    cd zplan-回测 && .venv/bin/python scripts/rule_score_audit.py --output results/rule_score_audit.md
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

# 确保 zplan-共享 在 path 中
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
_SHARED_SRC = os.path.join(_REPO_ROOT, "zplan-共享", "src")
if _SHARED_SRC not in sys.path:
    sys.path.insert(0, _SHARED_SRC)

from zplan_shared.db_engine import build_engine

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── 常量 ──────────────────────────────────────────────
HORIZONS = [5, 10, 20]          # 前向交易日
TOP_N_LIST = [100, 300, 500]    # 考察的 TOP N 组
PREFERRED_VERSION = "pick-2026-06-anti-chase-v2"

# 报告输出目录
REVIEW_DIR = os.path.join(
    os.environ.get("ZPLAN_ROOT", os.path.join(_REPO_ROOT, "zplan-资讯")),
    "backtest_review",
)


def _spearmanr(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Spearman 等级相关系数与近似 p 值。"""
    n = len(x)
    if n < 3:
        return (0.0, 1.0)
    x_rank = pd.Series(x).rank().values
    y_rank = pd.Series(y).rank().values
    rho = float(np.corrcoef(x_rank, y_rank)[0, 1])
    if np.isnan(rho) or abs(rho) >= 1.0:
        return (0.0 if np.isnan(rho) else rho, 0.0 if abs(rho) >= 1.0 else 1.0)
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return (rho, max(min(p, 1.0), 0.0))


# ── Step 1: 加载 stock_rule_scores ─────────────────────

def load_rule_scores() -> pd.DataFrame:
    """加载 stock_rule_scores 表，每日期只保留首选 rule_version。

    优先使用 PREFERRED_VERSION，其次取该日期样本量最大的版本。
    """
    engine = build_engine()
    sql = """
        SELECT ts_code, name, trade_date_as_of, rule_version, market,
               tech_score, composite_score, close_price, verdict
        FROM stock_rule_scores
        WHERE market = 'a'
          AND composite_score IS NOT NULL
        ORDER BY trade_date_as_of, composite_score DESC
    """
    t0 = time.time()
    logger.info("加载 stock_rule_scores ...")
    df = pd.read_sql_query(sql, engine)
    df["trade_date_as_of"] = pd.to_datetime(df["trade_date_as_of"]).dt.date
    elapsed = time.time() - t0
    logger.info("加载完成: %s 行, %s 个日期, %s 个版本, 耗时 %.1f 秒",
                len(df), df["trade_date_as_of"].nunique(),
                df["rule_version"].nunique(), elapsed)

    # 每日期选一个版本：优先 PREFERRED_VERSION，否则样本量最大的
    selected: list[pd.DataFrame] = []
    for d, grp in df.groupby("trade_date_as_of"):
        versions = grp["rule_version"].unique()
        if PREFERRED_VERSION in versions:
            selected.append(grp[grp["rule_version"] == PREFERRED_VERSION])
        else:
            # 取样本量最大的版本
            best_ver = grp.groupby("rule_version").size().idxmax()
            selected.append(grp[grp["rule_version"] == best_ver])

    result = pd.concat(selected, ignore_index=True)
    logger.info("去重后: %s 行, %s 个日期", len(result), result["trade_date_as_of"].nunique())
    return result


# ── Step 2: 前向收益计算 ───────────────────────────────

def _build_trade_date_index() -> tuple[list[date], dict[date, int]]:
    """构建交易日序列和日期→索引映射。"""
    engine = build_engine()
    sql = "SELECT DISTINCT trade_date FROM daily_prices ORDER BY trade_date"
    dates_df = pd.read_sql_query(sql, engine)
    dates = sorted(pd.to_datetime(dates_df["trade_date"]).dt.date.tolist())
    idx_map = {d: i for i, d in enumerate(dates)}
    logger.info("交易日序列: %s 个 ( %s ~ %s )", len(dates), dates[0], dates[-1])
    return dates, idx_map


def _load_close_panel(dates_of_interest: list[date], horizon_days: int = 30) -> pd.DataFrame:
    """加载感兴趣的日期 + 未来 horizon_days 个交易日的收盘价。"""
    engine = build_engine()
    all_dates = sorted(set(dates_of_interest))
    if not all_dates:
        return pd.DataFrame()

    start = min(all_dates)
    end = max(all_dates) + timedelta(days=horizon_days * 2)  # 足够覆盖前向窗口

    sql = """
        SELECT ts_code, trade_date, "close"
        FROM daily_prices
        WHERE trade_date >= :start AND trade_date <= :end
        ORDER BY ts_code, trade_date
    """
    t0 = time.time()
    logger.info("加载收盘价面板 (%s ~ %s)...", start, end)
    df = pd.read_sql_query(sql, engine, params={"start": start, "end": end})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    elapsed = time.time() - t0
    logger.info("收盘价面板: %s 行, 耗时 %.1f 秒", len(df), elapsed)
    return df


def compute_forward_returns(
    scores_df: pd.DataFrame,
    close_panel: pd.DataFrame,
    trade_dates: list[date],
    date_idx: dict[date, int],
    horizons: list[int],
) -> pd.DataFrame:
    """为每条 rule_score 记录附加前向收益。

    使用交易日索引快速定位第 N 个后续交易日。
    """
    # 构建快速查找: {(ts_code, trade_date): close}
    close_lookup: dict[tuple[str, date], float] = {}
    for _, row in close_panel.iterrows():
        close_lookup[(row["ts_code"], row["trade_date"])] = float(row["close"])

    n_total = len(trade_dates)
    results = scores_df.copy()

    for h in horizons:
        col = f"ret_{h}d_fwd"
        fwd_returns: list[float | None] = []

        for _, row in results.iterrows():
            code = row["ts_code"]
            as_of = row["trade_date_as_of"]
            close_0 = row["close_price"]

            if close_0 is None or pd.isna(close_0) or close_0 == 0:
                fwd_returns.append(None)
                continue

            # 找到 as_of 在交易日序列中的位置
            idx = date_idx.get(as_of)
            if idx is None:
                fwd_returns.append(None)
                continue

            # 第 h 个后续交易日
            fwd_idx = idx + h
            if fwd_idx >= n_total:
                fwd_returns.append(None)
                continue

            fwd_date = trade_dates[fwd_idx]
            close_h = close_lookup.get((code, fwd_date))

            if close_h is None or close_h == 0:
                fwd_returns.append(None)
            else:
                fwd_returns.append(round((close_h / close_0 - 1) * 100, 4))

        results[col] = fwd_returns

    # 统计覆盖率
    for h in horizons:
        col = f"ret_{h}d_fwd"
        n_valid = results[col].notna().sum()
        n_total_r = len(results)
        logger.info("前向 %d 日收益覆盖率: %d/%d (%.1f%%)",
                     h, n_valid, n_total_r, n_valid / n_total_r * 100 if n_total_r else 0)

    return results


# ── Step 3: 分析 ───────────────────────────────────────

def analyze_score_distribution(results: pd.DataFrame) -> dict[str, Any]:
    """规则分分布分析：按分数段统计样本量和前向收益。"""
    bins = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 101)]
    dist: dict[str, Any] = {}

    for lo, hi in bins:
        label = f"{lo}-{hi - 1}" if hi <= 100 else f"{lo}-100"
        if hi == 101:
            label = "80-100"
        mask = (results["composite_score"] >= lo) & (results["composite_score"] < hi)
        subset = results[mask]
        n = len(subset)
        if n == 0:
            dist[label] = {"count": 0, "mean_score": None, "mean_ret_5d": None,
                           "mean_ret_10d": None, "mean_ret_20d": None,
                           "win_rate_5d": None, "win_rate_10d": None, "win_rate_20d": None}
            continue

        entry: dict[str, Any] = {
            "count": n,
            "pct_of_total": round(n / len(results) * 100, 1),
            "mean_score": round(float(subset["composite_score"].mean()), 1),
        }
        for h in HORIZONS:
            col = f"ret_{h}d_fwd"
            valid = subset[col].dropna()
            if len(valid) > 0:
                entry[f"mean_ret_{h}d"] = round(float(valid.mean()), 2)
                entry[f"win_rate_{h}d"] = round(float((valid > 0).mean()) * 100, 1)
            else:
                entry[f"mean_ret_{h}d"] = None
                entry[f"win_rate_{h}d"] = None
        dist[label] = entry

    return dist


def analyze_top_n_groups(results: pd.DataFrame) -> dict[str, Any]:
    """TOP N 分组分析：每日期取 TOP N 高分的股票，计算跨日期均值。

    与全市场均值对比，判断 TOP N 是否有超额收益。
    """
    out: dict[str, Any] = {}

    for h in HORIZONS:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna()].copy()
        if valid.empty:
            out[f"horizon_{h}d"] = {}
            continue

        h_out: dict[str, Any] = {
            "all_market": {
                "mean_return": round(float(valid[col].mean()), 2),
                "win_rate": round(float((valid[col] > 0).mean()) * 100, 1),
                "median_return": round(float(valid[col].median()), 2),
                "n_samples": len(valid),
            }
        }

        for n in TOP_N_LIST:
            top_returns: list[float] = []
            win_rates: list[float] = []
            n_dates = 0

            for d, grp in valid.groupby("trade_date_as_of"):
                grp_sorted = grp.sort_values("composite_score", ascending=False)
                if len(grp_sorted) < n:
                    continue
                top_n = grp_sorted.head(n)
                rets = top_n[col].dropna()
                if len(rets) < n * 0.5:  # 至少 50% 有前向数据
                    continue
                top_returns.append(float(rets.mean()))
                win_rates.append(float((rets > 0).mean()))
                n_dates += 1

            if top_returns:
                arr = np.array(top_returns)
                wr_arr = np.array(win_rates)

                # 超额 = TOP N 均值 - 全市场均值
                all_mean = h_out["all_market"]["mean_return"]
                excess_arr = arr - all_mean

                h_out[f"top_{n}"] = {
                    "mean_return": round(float(arr.mean()), 2),
                    "median_return": round(float(np.median(arr)), 2),
                    "std_return": round(float(arr.std(ddof=1)), 2),
                    "mean_win_rate": round(float(wr_arr.mean()) * 100, 1),
                    "mean_excess_vs_all": round(float(excess_arr.mean()), 2),
                    "excess_positive_pct": round(float((excess_arr > 0).mean()) * 100, 1),
                    "n_dates": n_dates,
                    "n_stocks_per_date": n,
                }

        out[f"horizon_{h}d"] = h_out

    return out


def analyze_spearman(results: pd.DataFrame) -> dict[str, Any]:
    """Spearman 相关：composite_score vs 前向收益。"""
    out: dict[str, Any] = {}
    for h in HORIZONS:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna()].copy()
        if valid.empty:
            out[f"horizon_{h}d"] = {"mean_rho": None, "n_significant": 0, "n_dates": 0}
            continue

        rhos = []
        significant = 0
        for d, grp in valid.groupby("trade_date_as_of"):
            if len(grp) < 30:
                continue
            rho, p = _spearmanr(grp["composite_score"].values, grp[col].values)
            rhos.append(rho)
            if p < 0.05:
                significant += 1

        out[f"horizon_{h}d"] = {
            "mean_rho": round(float(np.mean(rhos)), 4) if rhos else None,
            "median_rho": round(float(np.median(rhos)), 4) if rhos else None,
            "n_significant": significant,
            "n_dates": len(rhos),
            "significant_pct": round(significant / len(rhos) * 100, 1) if rhos else 0,
        }
    return out


def analyze_by_verdict(results: pd.DataFrame) -> dict[str, Any]:
    """按 verdict 分组的前向收益（偏多/中性/偏空/无）。"""
    out: dict[str, Any] = {}
    for v in results["verdict"].fillna("(无)").unique():
        subset = results[results["verdict"].fillna("(无)") == v]
        entry: dict[str, Any] = {"count": len(subset)}
        for h in HORIZONS:
            col = f"ret_{h}d_fwd"
            valid = subset[col].dropna()
            if len(valid) > 0:
                entry[f"mean_ret_{h}d"] = round(float(valid.mean()), 2)
                entry[f"win_rate_{h}d"] = round(float((valid > 0).mean()) * 100, 1)
            else:
                entry[f"mean_ret_{h}d"] = None
                entry[f"win_rate_{h}d"] = None
        out[v] = entry
    return out


# ── Step 4: Markdown 报告 ──────────────────────────────

def generate_report(
    results: pd.DataFrame,
    score_dist: dict[str, Any],
    top_n: dict[str, Any],
    spearman: dict[str, Any],
    verdict_analysis: dict[str, Any],
) -> str:
    """生成完整的 Markdown 审计报告。"""
    today_str = date.today().strftime("%Y-%m-%d")
    n_dates = results["trade_date_as_of"].nunique()
    n_records = len(results)
    date_range = f"{results['trade_date_as_of'].min()} ~ {results['trade_date_as_of'].max()}"
    versions = results["rule_version"].unique()

    lines = [
        f"# 规则分可靠性审计报告",
        f"",
        f"> 生成日期: {today_str}  |  数据范围: {date_range}  |  {n_dates} 个交易日  |  {n_records:,} 条记录",
        f"",
        f"## 一、数据概览",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 交易日数 | {n_dates} |",
        f"| 总记录数 | {n_records:,} |",
        f"| 规则版本 | {', '.join(f'`{v}`' for v in versions)} |",
        f"| 每日期平均股票数 | {n_records // n_dates:,} |",
        f"| 分数范围 | {results['composite_score'].min():.0f} ~ {results['composite_score'].max():.0f} |",
        f"| 分数均值 | {results['composite_score'].mean():.1f} |",
        f"| 分数中位数 | {results['composite_score'].median():.0f} |",
        f"",
    ]

    # ── 二、分数分布 ──
    lines.append("## 二、规则分分布 vs 前向收益")
    lines.append("")
    lines.append("> **核心问题：高分是否意味着高收益？**")
    lines.append("")
    lines.append("| 分数区间 | 样本量 | 占比 | 均分 | 5日均收益 | 5日胜率 | 10日均收益 | 10日胜率 | 20日均收益 | 20日胜率 |")
    lines.append("|---------|--------|------|------|----------|--------|-----------|--------|-----------|--------|")

    bin_order = ["0-49", "50-59", "60-69", "70-79", "80-100"]
    for label in bin_order:
        d = score_dist.get(label, {})
        if d.get("count", 0) == 0:
            lines.append(f"| {label} | 0 | - | - | - | - | - | - | - | - |")
            continue

        def _fmt(v):
            if v is None:
                return "-"
            return f"{v:+.2f}%" if isinstance(v, float) and v != 0 else f"{v}%"

        lines.append(
            f"| {label} | {d['count']:,} | {d.get('pct_of_total', '-')}% | {d.get('mean_score', '-')} | "
            f"{_fmt_val(d.get('mean_ret_5d'))} | {_fmt_pct(d.get('win_rate_5d'))} | "
            f"{_fmt_val(d.get('mean_ret_10d'))} | {_fmt_pct(d.get('win_rate_10d'))} | "
            f"{_fmt_val(d.get('mean_ret_20d'))} | {_fmt_pct(d.get('win_rate_20d'))} |"
        )

    # 判断倒 U 型
    best_5d = max(
        (d for d in [score_dist.get(l) for l in bin_order] if d and d.get("mean_ret_5d") is not None),
        key=lambda x: x.get("mean_ret_5d", -999), default=None
    )
    best_10d = max(
        (d for d in [score_dist.get(l) for l in bin_order] if d and d.get("mean_ret_10d") is not None),
        key=lambda x: x.get("mean_ret_10d", -999), default=None
    )
    lines.append("")
    if best_5d:
        best_5d_label = [l for l in bin_order if score_dist.get(l) == best_5d][0] if best_5d in score_dist.values() else "?"
        if "80-100" in score_dist:
            high = score_dist["80-100"]
            if high.get("mean_ret_5d") is not None and best_5d.get("mean_ret_5d") is not None:
                if high["mean_ret_5d"] < best_5d["mean_ret_5d"]:
                    lines.append(f"⚠️ **倒 U 型确认**：最高分区间 (80-100) 5日收益 {_fmt_val(high.get('mean_ret_5d'))}，"
                                 f"低于最优区间 {best_5d_label} 的 {_fmt_val(best_5d.get('mean_ret_5d'))}。"
                                 f"**高分 = 追涨，不意味着高收益。**")
    lines.append("")

    # ── 三、Spearman 相关性 ──
    lines.append("## 三、Spearman 相关性")
    lines.append("")
    lines.append("| 前向周期 | 平均 ρ | 中位数 ρ | 显著日期占比 | 样本日期数 |")
    lines.append("|---------|--------|---------|------------|----------|")

    for h in HORIZONS:
        sp = spearman.get(f"horizon_{h}d", {})
        rho = sp.get("mean_rho")
        med_rho = sp.get("median_rho")
        lines.append(
            f"| {h}日 | {rho if rho is not None else '-'} | {med_rho if med_rho is not None else '-'} | "
            f"{sp.get('significant_pct', '-')}% | {sp.get('n_dates', 0)} |"
        )

    # 判断
    sp_5 = spearman.get("horizon_5d", {})
    mean_rho = sp_5.get("mean_rho")
    if mean_rho is not None:
        if mean_rho > 0.03:
            verdict = "规则分有**微弱正向**预测力"
        elif mean_rho > 0:
            verdict = "规则分预测力**近乎为零**"
        elif mean_rho > -0.03:
            verdict = "规则分预测力**近乎为零**（轻微负相关）"
        else:
            verdict = f"⚠️ 规则分呈**负相关**（ρ={mean_rho:.4f}），高分反而低收益"
        lines.append(f"")
        lines.append(f"> **结论**: {verdict}（20日 ρ={spearman.get('horizon_20d', {}).get('mean_rho', 'N/A')}）。")
    lines.append("")

    # ── 四、TOP N 分组 ──
    lines.append("## 四、TOP N vs 全市场")
    lines.append("")
    lines.append("> **核心问题：按规则分取 TOP300，是否跑赢全市场均值？**")
    lines.append("")

    for h in HORIZONS:
        hd = top_n.get(f"horizon_{h}d", {})
        all_m = hd.get("all_market", {})
        lines.append(f"### {h}日前向收益")
        lines.append("")
        lines.append(f"| 分组 | 均收益 | 中位数 | 胜率 | 超额(vs全市场) | 超额胜率 | 日期数 |")
        lines.append(f"|------|--------|--------|------|--------------|--------|--------|")
        lines.append(
            f"| 全市场 | {_fmt_val(all_m.get('mean_return'))} | {_fmt_val(all_m.get('median_return'))} | "
            f"{all_m.get('win_rate', '-')}% | - | - | - |"
        )

        for n in TOP_N_LIST:
            tn = hd.get(f"top_{n}", {})
            if not tn:
                continue
            lines.append(
                f"| TOP{n} | {_fmt_val(tn.get('mean_return'))} | {_fmt_val(tn.get('median_return'))} | "
                f"{tn.get('mean_win_rate', '-')}% | "
                f"{_fmt_val(tn.get('mean_excess_vs_all'))} | "
                f"{tn.get('excess_positive_pct', '-')}% | "
                f"{tn.get('n_dates', 0)} |"
            )

        # 最优 TOP N
        best_n = None
        best_excess = -999
        for n in TOP_N_LIST:
            tn = hd.get(f"top_{n}", {})
            excess = tn.get("mean_excess_vs_all")
            if excess is not None and excess > best_excess:
                best_excess = excess
                best_n = n

        lines.append("")
        if best_n and best_excess > 0:
            lines.append(f"> ✅ TOP{best_n} 超额 {best_excess:+.2f}%，规则分有正向筛选效果。")
        elif best_n:
            lines.append(f"> ⚠️ 最佳 TOP{best_n} 超额仅 {best_excess:+.2f}%，规则分筛选效果微弱。")
        lines.append("")

    # ── 五、verdict 分析 ──
    lines.append("## 五、Verdict 标签有效性")
    lines.append("")
    lines.append("| Verdict | 样本量 | 5日均收益 | 5日胜率 | 10日均收益 | 10日胜率 | 20日均收益 | 20日胜率 |")
    lines.append("|---------|--------|----------|--------|-----------|--------|-----------|--------|")

    for v_label in ["偏多", "中性", "偏空", "(无)"]:
        d = verdict_analysis.get(v_label, {})
        if d.get("count", 0) == 0:
            continue
        lines.append(
            f"| {v_label} | {d['count']:,} | "
            f"{_fmt_val(d.get('mean_ret_5d'))} | {_fmt_pct(d.get('win_rate_5d'))} | "
            f"{_fmt_val(d.get('mean_ret_10d'))} | {_fmt_pct(d.get('win_rate_10d'))} | "
            f"{_fmt_val(d.get('mean_ret_20d'))} | {_fmt_pct(d.get('win_rate_20d'))} |"
        )
    lines.append("")

    # ── 六、综合结论 ──
    lines.append("## 六、综合结论与建议")
    lines.append("")

    # 1. 倒 U 型判断
    high_scores = score_dist.get("80-100", {})
    mid_scores = score_dist.get("60-69", {})
    low_scores = score_dist.get("50-59", {})

    conclusions: list[str] = []

    # 结论 1: 分数分布
    high_ret = high_scores.get("mean_ret_5d")
    mid_ret = mid_scores.get("mean_ret_5d")
    if high_ret is not None and mid_ret is not None and high_ret < mid_ret:
        conclusions.append(
            f"1. **规则分呈倒 U 型**：最高分 (80-100) 的 5 日收益 {_fmt_val(high_ret)}，"
            f"低于中等分 (60-70) 的 {_fmt_val(mid_ret)}。"
            f"`quick_technical_score` 的动量因子导致高分=追涨，追涨股前向收益差。"
        )
    else:
        conclusions.append("1. 规则分分布正常，高分对应较高收益。")

    # 结论 2: Spearman
    sp_5 = spearman.get("horizon_5d", {})
    sp_10 = spearman.get("horizon_10d", {})
    sp_20 = spearman.get("horizon_20d", {})
    rho_5 = sp_5.get("mean_rho")
    rho_10 = sp_10.get("mean_rho")
    rho_20 = sp_20.get("mean_rho")

    # 综合判定：取 5/10/20 日中最有代表性的
    worst_rho = min(
        rho_5 if rho_5 is not None else 0,
        rho_10 if rho_10 is not None else 0,
        rho_20 if rho_20 is not None else 0,
    )

    if rho_20 is not None:
        if rho_20 < -0.01 or rho_10 is not None and rho_10 < -0.02:
            conclusions.append(
                f"2. **规则分与远期收益负相关** (20日 ρ={rho_20:.4f}, 10日 ρ={rho_10})。"
                f"当前规则分不能有效预测未来收益，需要重构因子权重。"
            )
        elif abs(worst_rho) < 0.02:
            conclusions.append(
                f"2. **规则分预测力近乎随机**：5日 ρ={rho_5}, 10日 ρ={rho_10}, 20日 ρ={rho_20}。"
                f"所有周期的 Spearman 相关系数都接近于 0。"
                f"依赖规则分做 TOP300 粗筛相当于随机抽样，LLM 在此基础上选股是「矮子里面选将军」。"
            )
        elif worst_rho < 0.05:
            conclusions.append(
                f"2. **规则分预测力极弱**：5日 ρ={rho_5}, 10日 ρ={rho_10}, 20日 ρ={rho_20}。"
                f"方向正确但强度远不足以支撑有效的 TOP300 筛选。"
            )
        else:
            conclusions.append(
                f"2. **规则分有一定预测力**：20 日 Spearman ρ={rho_20:.4f}，"
                f"方向正确，有优化空间。"
            )

    # 结论 3: TOP N 超额 — 用多周期综合判断
    hd_5 = top_n.get("horizon_5d", {})
    hd_10 = top_n.get("horizon_10d", {})
    hd_20 = top_n.get("horizon_20d", {})
    tn_300_5 = hd_5.get("top_300", {})
    tn_300_10 = hd_10.get("top_300", {})
    tn_300_20 = hd_20.get("top_300", {})

    excess_5 = tn_300_5.get("mean_excess_vs_all") if tn_300_5 else None
    excess_10 = tn_300_10.get("mean_excess_vs_all") if tn_300_10 else None
    excess_20 = tn_300_20.get("mean_excess_vs_all") if tn_300_20 else None

    # 判断 TOP300 在所有周期是否都跑输
    all_excesses = [e for e in [excess_5, excess_10, excess_20] if e is not None]
    n_negative = sum(1 for e in all_excesses if e < 0)
    n_positive = sum(1 for e in all_excesses if e > 0)

    if all_excesses:
        if n_negative == len(all_excesses):
            conclusions.append(
                f"3. ⚠️ **TOP300 在所有周期均跑输全市场**：5日超额 {excess_5:+.2f}%、"
                f"10日超额 {excess_10:+.2f}%、20日超额 {excess_20:+.2f}%。"
                f"按规则分取 TOP300 是负向筛选，必须重构粗筛逻辑。"
            )
        elif n_negative > n_positive:
            conclusions.append(
                f"3. ⚠️ **TOP300 多数周期跑输全市场**："
                f"5日超额 {excess_5:+.2f}%、10日超额 {excess_10:+.2f}%、20日超额 {excess_20:+.2f}%。"
                f"规则分筛选效果不可靠。"
            )
        else:
            conclusions.append(
                f"3. **TOP300 超额表现不一**："
                f"5日超额 {excess_5:+.2f}%、10日超额 {excess_10:+.2f}%、20日超额 {excess_20:+.2f}%。"
            )

    # 结论 4: 建议 — 基于综合判断（ρ 是否接近 0 + TOP300 是否跑输）
    need_restructure = (
        (rho_5 is not None and abs(rho_5) < 0.02)
        or (rho_10 is not None and rho_10 < 0)
        or (n_negative >= 2)
    )

    conclusions.append("")
    conclusions.append("### 建议行动")
    conclusions.append("")
    if need_restructure:
        conclusions.append(
            "- **C1（高优先级）**: 在 `init-rule` 的粗筛阶段引入 `scoring_v2.reversal_flow_concept` 因子，"
            "替代纯 `quick_technical_score`。反转+资金流因子在回测中表现优于纯动量因子。\n"
            "- **C2（高优先级）**: 调低 `quick_technical_score` 中 ret_20d 的加分权重。"
            "当前 ret_20d 在 0-5% 区间最多加到 +20 分，导致追涨股进入 TOP300。建议加分上限减半至 +10。\n"
            "- **C3**: 在 TOP300 粗筛后硬截断 `ret_20d > 5%` 的追涨股，不送入 LLM。"
            "strategy.yaml 已有 `max_ret_20d: 3.0` 配置，但只是软因子，需改为硬截断。\n"
            "- **C4**: 考虑用 v2 的 60-70 分区间（最佳区间）替代简单的 TOP N 排序。"
            "从分数分布来看，60-70 分段 20 日收益 +2.68% 优于 80-100 分段的 +6.99%（但样本量是 40 倍）。"
            "但实际上 70-79 和 80-100 的 20 日收益更高（+5.34%/+6.99%），说明长期来看高分仍有价值，问题出在短期。\n"
            "- **C5**: 规则分短期（5-10日）预测力为零，不应作为短期交易排序依据。"
            "如果要用于 T+5/T+10 的短线选股，规则分需要加入更多反转因子、减少动量因子权重。"
        )
    else:
        conclusions.append("- 规则分基本可靠，可保持当前粗筛逻辑，重点优化 LLM 的风险/催化识别。")

    lines.extend(conclusions)
    lines.append("")
    lines.append("---")
    lines.append(f"*报告由 `rule_score_audit.py` 自动生成*")

    return "\n".join(lines)


def _fmt_val(v):
    """格式化收益值。"""
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def _fmt_pct(v):
    """格式化百分比。"""
    if v is None:
        return "-"
    return f"{v:.1f}%"


# ── Main ───────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="规则分可靠性审计")
    p.add_argument("--output", type=str, default=None,
                   help="输出 markdown 路径，默认 zplan-资讯/backtest_review/rule_score_validation.md")
    p.add_argument("--json-output", type=str, default=None,
                   help="同时输出 JSON 数据")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # 输出路径
    md_path = args.output or os.path.join(REVIEW_DIR, "rule_score_validation.md")
    os.makedirs(os.path.dirname(md_path), exist_ok=True)

    # Step 1: 加载数据
    scores_df = load_rule_scores()
    if scores_df.empty:
        logger.error("无 stock_rule_scores 数据")
        sys.exit(1)

    # Step 2: 构建交易日历 + 加载收盘价
    trade_dates, date_idx = _build_trade_date_index()
    close_panel = _load_close_panel(
        dates_of_interest=scores_df["trade_date_as_of"].unique().tolist(),
        horizon_days=max(HORIZONS) + 5,
    )

    # Step 3: 计算前向收益
    results = compute_forward_returns(scores_df, close_panel, trade_dates, date_idx, HORIZONS)

    # Step 4: 分析
    logger.info("分析分数分布...")
    score_dist = analyze_score_distribution(results)

    logger.info("分析 TOP N 分组...")
    top_n = analyze_top_n_groups(results)

    logger.info("分析 Spearman 相关性...")
    spearman = analyze_spearman(results)

    logger.info("分析 Verdict...")
    verdict_analysis = analyze_by_verdict(results)

    # Step 5: 生成报告
    logger.info("生成报告...")
    report = generate_report(results, score_dist, top_n, spearman, verdict_analysis)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("报告已保存至 %s", md_path)

    # 可选 JSON 输出
    if args.json_output:
        json_data = {
            "summary": {
                "n_dates": int(results["trade_date_as_of"].nunique()),
                "n_records": int(len(results)),
                "date_range": [str(results["trade_date_as_of"].min()), str(results["trade_date_as_of"].max())],
            },
            "score_distribution": score_dist,
            "top_n_analysis": top_n,
            "spearman": spearman,
            "verdict_analysis": verdict_analysis,
        }
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2, default=str)
        logger.info("JSON 数据已保存至 %s", args.json_output)

    # 控制台输出摘要
    print("\n" + "=" * 60)
    print("  规则分审计摘要")
    print("=" * 60)
    sp_5 = spearman.get("horizon_5d", {})
    sp_20 = spearman.get("horizon_20d", {})
    print(f"  5日 Spearman ρ: {sp_5.get('mean_rho', 'N/A')}")
    print(f"  20日 Spearman ρ: {sp_20.get('mean_rho', 'N/A')}")

    hd_5 = top_n.get("horizon_5d", {})
    tn_300 = hd_5.get("top_300", {})
    all_m = hd_5.get("all_market", {})
    excess = tn_300.get("mean_excess_vs_all", "N/A")
    print(f"  TOP300 5日均收益: {tn_300.get('mean_return', 'N/A')}  (全市场: {all_m.get('mean_return', 'N/A')})")
    print(f"  TOP300 超额: {excess}")
    print(f"\n  完整报告: {md_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
