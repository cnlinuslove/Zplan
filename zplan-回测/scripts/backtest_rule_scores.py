#!/usr/bin/env python3
"""规则分历史回测：用 3 年日线数据评估 quick_technical_score 的预测力。

用法::

    cd zplan-回测 && .venv/bin/python scripts/backtest_rule_scores.py \
        --freq 10 --horizons 5 10 20 --top-n 10 30 50 100 \
        --output ./results

不加参数时使用默认值跑全量回测。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

import math
import numpy as np
import pandas as pd

# 确保 zplan-共享 在 path 中
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)  # zplan-回测
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)  # monorepo root
_SHARED_SRC = os.path.join(_REPO_ROOT, "zplan-共享", "src")
if _SHARED_SRC not in sys.path:
    sys.path.insert(0, _SHARED_SRC)

from zplan_shared.db_engine import build_engine
from zplan_shared.features import enrich_bars, latest_features

# 选股 agent 的评分函数（与生产一致）
_PICK_AGENT_SRC = os.path.join(_REPO_ROOT, "zplan-选股", "src")
if _PICK_AGENT_SRC not in sys.path:
    sys.path.insert(0, _PICK_AGENT_SRC)

from pick_agent.scoring import apply_momentum_cap, quick_technical_score
from pick_agent.scoring_v2 import (
    TECH_FACTORS,
    PRESET_SCHEMES,
    compute_score_v2,
    clear_quality_cache,
    set_quality_cache,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── 常量 ──────────────────────────────────────────────
CALENDAR_DAYS_WINDOW = 150  # 每日期评分所需的回看窗口（日历日）
MIN_BARS = 60               # 最少 K 线数
BAR_COLS = [
    "ts_code", "trade_date",
    "open", "high", "low", "close", "volume", "pct_chg",
]


# ── 纯 numpy Spearman（避免 scipy 依赖） ──────────────

def _spearmanr(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Spearman 等级相关系数与近似 p 值（t 分布逼近）。"""
    n = len(x)
    if n < 3:
        return (0.0, 1.0)
    # 等级（用 pandas rank 处理平级）
    x_rank = pd.Series(x).rank().values
    y_rank = pd.Series(y).rank().values
    # Pearson 相关系数
    rho = float(np.corrcoef(x_rank, y_rank)[0, 1])
    if np.isnan(rho) or abs(rho) >= 1.0:
        return (0.0 if np.isnan(rho) else rho, 0.0 if abs(rho) >= 1.0 else 1.0)
    # t 统计量
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    # Student t CDF 用正则化不完全 beta 函数（或正态逼近：大 n 时近似）
    # 这里用正态逼近（n >= 30 时精度足够）
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return (rho, max(min(p, 1.0), 0.0))


# ── Step 1: 数据加载 ──────────────────────────────────

def load_all_bars(
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """一次性加载全部 OHLCV 日线到内存（前复权）。

    返回 DataFrame，列见 ``BAR_COLS``，已按 (ts_code, trade_date) 排序。
    """
    engine = build_engine()
    sql = """
        SELECT ts_code, trade_date, "open", high, low, "close", volume, pct_chg
        FROM daily_prices
        WHERE adjust_type = 'qfq'
    """
    params: dict[str, Any] = {}
    if start_date is not None:
        sql += " AND trade_date >= :start_date"
        params["start_date"] = start_date
    if end_date is not None:
        sql += " AND trade_date <= :end_date"
        params["end_date"] = end_date
    sql += " ORDER BY ts_code, trade_date"

    t0 = time.time()
    logger.info("正在加载全量日线数据（可能需 30-60 秒）...")
    df = pd.read_sql_query(sql, engine, params=params)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    elapsed = time.time() - t0
    logger.info(
        "加载完成：%s 行，%s 只股票，%s 个交易日，耗时 %.1f 秒",
        len(df), df["ts_code"].nunique(), df["trade_date"].nunique(), elapsed,
    )
    return df


def get_trade_dates(all_data: pd.DataFrame) -> list[date]:
    """返回排序后的全部交易日列表。"""
    dates = sorted(all_data["trade_date"].unique())
    return [d for d in dates if isinstance(d, date)]


def get_sample_dates(
    trade_dates: list[date],
    freq: int = 10,
    min_warmup_days: int = CALENDAR_DAYS_WINDOW,
    max_lookahead_days: int = 60,
) -> list[date]:
    """从交易日列表中按频率采样，确保前后有足够数据。

    - 跳过前 ``min_warmup_days`` 日历日（保证回看窗口充足）
    - 跳过最后 ``max_lookahead_days`` 日历日（保证前向收益可计算）
    """
    if not trade_dates:
        return []
    first = trade_dates[0]
    last = trade_dates[-1]

    warmup_cutoff = first + timedelta(days=min_warmup_days)
    lookahead_cutoff = last - timedelta(days=max_lookahead_days)

    eligible = [d for d in trade_dates if d >= warmup_cutoff and d <= lookahead_cutoff]
    if not eligible:
        logger.warning("采样日期为空：请检查数据范围与 warmup/lookahead 参数")
        return []

    samples = eligible[::freq]
    # 确保最后一个采样日期也被包含（如果 freq 不能整除）
    if samples[-1] != eligible[-1]:
        samples.append(eligible[-1])

    logger.info(
        "交易日 %s ~ %s，共 %s 天；采样频率每 %s 天 → %s 个样本日期",
        first, last, len(trade_dates), freq, len(samples),
    )
    return samples


# ── Step 2: 核心回测循环（预计算优化版）──────────────

def _precompute_enriched(
    all_data: pd.DataFrame,
    sample_dates: list[date],
    min_bars: int = MIN_BARS,
) -> dict[str, pd.DataFrame]:
    """预计算：每只股票 enrich_bars 一次，存为 trade_date 索引的 DataFrame。

    返回 {ts_code: enriched_df}，只包含至少有 min_bars 根 K 线的股票。
    这是全流程的性能关键——避免 336K 次重复的 rolling 计算。
    """
    t0 = time.time()
    cache: dict[str, pd.DataFrame] = {}
    codes = sorted(all_data["ts_code"].unique())
    n_total = len(codes)
    skipped = 0

    for i, code in enumerate(codes):
        grp = all_data[all_data["ts_code"] == code].sort_values("trade_date")
        if len(grp) < min_bars:
            skipped += 1
            continue
        enriched = enrich_bars(grp.set_index("trade_date"))
        if not enriched.empty:
            cache[code] = enriched

        if (i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            logger.info(
                "  预计算特征: %s/%s 只股票 (%.1f%%), %s 只跳过, 耗时 %.1f 秒",
                i + 1, n_total, (i + 1) / n_total * 100, skipped, elapsed,
            )

    elapsed = time.time() - t0
    logger.info(
        "预计算完成: %s 只股票的特征已缓存, %s 只跳过（K线不足 %s）, 总耗时 %.1f 秒",
        len(cache), skipped, min_bars, elapsed,
    )
    return cache


def _build_close_lookup(all_data: pd.DataFrame) -> dict[str, dict[date, float]]:
    """构建快速收盘价查找表：{ts_code: {trade_date: close}}。"""
    lookup: dict[str, dict[date, float]] = {}
    for (code, d), grp in all_data.groupby(["ts_code", "trade_date"], sort=False):
        val = grp["close"].iloc[0]
        if not pd.isna(val):
            lookup.setdefault(code, {})[d] = float(val)
    logger.info("收盘价查找表: %s 只股票", len(lookup))
    return lookup


# ── 财务质量数据加载 ──────────────────────────────────

FINANCIAL_PUBLICATION_LAG_DAYS = 120  # 财报发布滞后（4 个月）

# CSRC 字母代码 → 简化板块（约 12 个）
_CSRC_SECTOR_MAP: dict[str, str] = {
    "A": "农业", "B": "资源", "C": "制造业", "D": "公用事业",
    "E": "建筑", "F": "商贸", "G": "交通运输", "H": "消费服务",
    "I": "信息技术", "J": "金融地产", "K": "金融地产",
    "L": "商务服务", "M": "科研服务", "N": "公用事业",
    "O": "消费服务", "P": "消费服务", "Q": "医药卫生", "R": "文化传媒", "S": "综合",
}

# 中文行业关键词 → 简化板块（用于映射非字母编码的股票）
_INDUSTRY_KEYWORD_MAP: dict[str, str] = {
    "半导体": "信息技术", "芯片": "信息技术", "计算机": "信息技术",
    "软件": "信息技术", "通信": "信息技术", "电子": "信息技术",
    "互联网": "信息技术", "人工智能": "信息技术", "机器人": "信息技术",
    "医药": "医药卫生", "医疗": "医药卫生", "制药": "医药卫生",
    "生物": "医药卫生", "中药": "医药卫生", "疫苗": "医药卫生",
    "银行": "金融地产", "保险": "金融地产", "证券": "金融地产",
    "房地产": "金融地产", "地产": "金融地产",
    "食品": "消费", "饮料": "消费", "白酒": "消费", "家电": "消费",
    "汽车": "制造业", "钢铁": "资源", "有色": "资源", "煤炭": "资源",
    "石油": "资源", "化工": "资源", "化学": "资源", "材料": "资源",
    "电力": "公用事业", "环保": "公用事业", "水务": "公用事业",
    "建筑": "建筑", "建材": "建筑", "装饰": "建筑",
    "农业": "农业", "畜牧": "农业", "种业": "农业",
    "航空": "交通运输", "铁路": "交通运输", "公路": "交通运输", "港口": "交通运输",
}


def _load_concept_members() -> dict[str, list[str]]:
    """加载概念→股票映射：{ts_code: [concept_name, ...]}。"""
    engine = build_engine()
    cdf = pd.read_sql_query(
        "SELECT ts_code, concept_name FROM stock_concept_members", engine
    )
    result: dict[str, list[str]] = {}
    for _, row in cdf.iterrows():
        result.setdefault(row["ts_code"], []).append(row["concept_name"])
    logger.info("概念数据加载: %s 只股票, %s 个概念", len(result), cdf["concept_name"].nunique())
    return result


def _compute_concept_heat(
    concept_members: dict[str, list[str]],
    enriched_cache: dict[str, pd.DataFrame],
    close_lookup: dict[str, dict[date, float]],
    as_of: date,
) -> dict[str, float]:
    """计算每个概念在 as_of 日的"热度"（概念内所有股票的平均 ret_20d）。

    热度 > 0 → 资金流入该概念；热度 < 0 → 资金流出。
    返回 {concept_name: avg_ret_20d}。
    """
    # 收集每只股票的 ret_20d
    code_ret: dict[str, float] = {}
    for code, enriched in enriched_cache.items():
        hist = enriched[enriched.index <= as_of]
        if hist.empty:
            continue
        if code not in close_lookup or as_of not in close_lookup.get(code, {}):
            continue
        if "ret_20d" in enriched.columns:
            last_ret = hist["ret_20d"].iloc[-1]
            if not pd.isna(last_ret):
                code_ret[code] = float(last_ret)

    # 聚合到概念
    concept_rets: dict[str, list[float]] = {}
    for code, concepts in concept_members.items():
        ret = code_ret.get(code)
        if ret is None:
            continue
        for c in concepts:
            concept_rets.setdefault(c, []).append(ret)

    # 计算均值
    heat: dict[str, float] = {}
    for c, rets in concept_rets.items():
        if len(rets) >= 3:
            heat[c] = sum(rets) / len(rets)

    return heat


def _load_sector_map() -> dict[str, str]:
    """加载全量股票的板块映射。

    - 有 CSRC 字母代码的（如 'C 制造业'）→ 用 _CSRC_SECTOR_MAP 映射
    - 中文行业名的 → 关键词匹配
    - 仍无法分类 → '其他'
    """
    engine = build_engine()
    sdf = pd.read_sql_query("SELECT ts_code, industry FROM stock_list WHERE industry IS NOT NULL", engine)
    sector_map: dict[str, str] = {}
    stats: dict[str, int] = {}

    for _, row in sdf.iterrows():
        code, ind = row["ts_code"], (row["industry"] or "")
        if not ind:
            sector_map[code] = "其他"
            continue

        # 尝试 CSRC 字母代码
        if len(ind) >= 2 and ind[1] == " " and ind[0].isalpha():
            sector = _CSRC_SECTOR_MAP.get(ind[0], ind[:2].strip())
        else:
            # 中文关键词匹配
            sector = "其他"
            for kw, sec in _INDUSTRY_KEYWORD_MAP.items():
                if kw in ind:
                    sector = sec
                    break
        sector_map[code] = sector
        stats[sector] = stats.get(sector, 0) + 1

    logger.info("板块映射完成: %s 只股票 → %s 个板块 %s",
                 len(sector_map), len(stats), {k: v for k, v in sorted(stats.items(), key=lambda x: -x[1])})
    return sector_map


def _load_financial_quality(sector_map: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """加载财务数据并计算板块内百分位排名。

    返回 {ts_code: [{report_date, revenue, net_profit, margin, margin_pctile, ...}, ...]}
    """
    engine = build_engine()
    sql = """
        SELECT ts_code, report_date, revenue, net_profit
        FROM financial_indicators
        WHERE revenue IS NOT NULL OR net_profit IS NOT NULL
        ORDER BY ts_code, report_date
    """
    fdf = pd.read_sql_query(sql, engine)
    fdf["report_date"] = pd.to_datetime(fdf["report_date"]).dt.date
    fdf["margin"] = np.where(
        (fdf["revenue"].notna() & (fdf["revenue"] != 0)),
        fdf["net_profit"] / fdf["revenue"] * 100,
        np.nan,
    )
    fdf["sector"] = fdf["ts_code"].map(sector_map).fillna("其他")

    # 按 report_date + sector 计算百分位
    for metric in ["margin", "revenue", "net_profit"]:
        col_pct = f"{metric}_sector_pctile"
        # 每个报告期+板块内排名
        fdf[col_pct] = fdf.groupby(["report_date", "sector"])[metric].transform(
            lambda x: x.rank(pct=True) * 100 if x.notna().sum() >= 5 else np.nan
        )

    # 转换为嵌套 dict
    result: dict[str, list[dict[str, Any]]] = {}
    for _, row in fdf.iterrows():
        code = row["ts_code"]
        entry = {
            "report_date": row["report_date"],
            "revenue": float(row["revenue"]) if not pd.isna(row["revenue"]) else None,
            "net_profit": float(row["net_profit"]) if not pd.isna(row["net_profit"]) else None,
            "margin": float(row["margin"]) if not pd.isna(row["margin"]) else None,
            "margin_sector_pctile": float(row["margin_sector_pctile"]) if not pd.isna(row.get("margin_sector_pctile")) else None,
            "revenue_sector_pctile": float(row["revenue_sector_pctile"]) if not pd.isna(row.get("revenue_sector_pctile")) else None,
            "profit_sector_pctile": float(row["profit_sector_pctile"]) if not pd.isna(row.get("profit_sector_pctile")) else None,
        }
        result.setdefault(code, []).append(entry)

    n = len(result)
    logger.info("财务数据(板块增强)加载: %s 只股票 (%s 个板块)", n, fdf["sector"].nunique())
    return result


def _get_quality_for_date(
    fin_data: dict[str, list[dict[str, Any]]],
    code: str,
    as_of: date,
    lag_days: int = FINANCIAL_PUBLICATION_LAG_DAYS,
) -> dict[str, Any]:
    """获取 as_of 日期可用的最新财务质量指标（含板块内百分位）。"""
    reports = fin_data.get(code)
    if not reports:
        return {}

    available_cutoff = as_of - timedelta(days=lag_days)
    available = [r for r in reports if r["report_date"] <= available_cutoff]
    if len(available) < 2:
        return {}

    latest = available[-1]
    prev_year = [r for r in available if abs(
        (r["report_date"] - latest["report_date"]).days - 365
    ) < 60]
    prev_year_r = prev_year[-1] if prev_year else None

    # 营收 YoY（绝对值）
    revenue_yoy = None
    if latest["revenue"] and prev_year_r and prev_year_r["revenue"] and prev_year_r["revenue"] > 0:
        revenue_yoy = round((latest["revenue"] / prev_year_r["revenue"] - 1) * 100, 2)

    # 利润 YoY
    profit_yoy = None
    if latest["net_profit"] and prev_year_r and prev_year_r["net_profit"] and prev_year_r["net_profit"] != 0:
        profit_yoy = round((latest["net_profit"] / prev_year_r["net_profit"] - 1) * 100, 2)

    # 绝对净利率
    margin = latest.get("margin")
    if margin is None and latest["revenue"] and latest["net_profit"] and latest["revenue"] > 0:
        margin = round(latest["net_profit"] / latest["revenue"] * 100, 2)

    recent_profits = [r["net_profit"] for r in available[-4:] if r["net_profit"] is not None]

    return {
        "recent_profits": recent_profits,
        "revenue_yoy_pct": revenue_yoy,
        "profit_yoy_pct": profit_yoy,
        "profit_margin_pct": margin,
        # 板块内百分位（关键：用百分位替代绝对值）
        "margin_sector_pctile": latest.get("margin_sector_pctile"),
        "revenue_sector_pctile": latest.get("revenue_sector_pctile"),
        "profit_sector_pctile": latest.get("profit_sector_pctile"),
    }


def _load_chip_panel_by_dates(
    sample_dates: list[date],
) -> dict[str, dict[str, dict[str, float | None]]]:
    """批量加载历史筹码峰数据。

    Returns:
        {trade_date_str: {ts_code: {profit_ratio, avg_cost, concentration_90, concentration_70}}}
        若 ``daily_chip`` 表不存在或无数据则返回空 dict。
    """
    if not sample_dates:
        return {}
    try:
        from zplan_shared.db_engine import build_engine as _build_eng
        engine = _build_eng()
        start_d = sample_dates[0]
        end_d = sample_dates[-1]
        sql = """
            SELECT ts_code, trade_date, profit_ratio, avg_cost,
                   concentration_90, concentration_70,
                   cost_90_low, cost_90_high
            FROM daily_chip
            WHERE market = 'a' AND trade_date >= :start AND trade_date <= :end
            ORDER BY ts_code, trade_date
        """
        cdf = pd.read_sql_query(sql, engine, params={"start": start_d, "end": end_d})
    except Exception:
        logger.info("筹码峰表 daily_chip 不可用，跳过筹码因子")
        return {}

    if cdf.empty:
        return {}

    result: dict[str, dict[str, dict[str, float | None]]] = {}
    for d, grp in cdf.groupby("trade_date"):
        date_key = str(d)
        result[date_key] = {}
        for _, row in grp.iterrows():
            code = str(row["ts_code"])
            result[date_key][code] = {
                "profit_ratio": _safe_float_backtest(row.get("profit_ratio")),
                "avg_cost": _safe_float_backtest(row.get("avg_cost")),
                "concentration_90": _safe_float_backtest(row.get("concentration_90")),
                "concentration_70": _safe_float_backtest(row.get("concentration_70")),
            }
    logger.info("筹码峰历史数据: %s 个交易日, %s 条记录", len(result), len(cdf))
    return result


def _safe_float_backtest(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _build_bars_by_code(all_data: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """预分割 OHLCV 数据：{ts_code: DataFrame_sorted_by_trade_date}。

    用于 O(1) 前向收益计算，避免每次过滤全量 3.8M 行。
    """
    t0 = time.time()
    result: dict[str, pd.DataFrame] = {}
    for code, grp in all_data.groupby("ts_code", sort=False):
        result[code] = grp.sort_values("trade_date").reset_index(drop=True)
    elapsed = time.time() - t0
    logger.info("按股票分割完成: %s 只，耗时 %.1f 秒", len(result), elapsed)
    return result


def run_backtest(
    all_data: pd.DataFrame,
    sample_dates: list[date],
    horizons: list[int],
    factor_sets: dict[str, tuple[list[str], dict[str, float]]] | None = None,
    fin_data: dict[str, list[dict[str, Any]]] | None = None,
    concept_members: dict[str, list[str]] | None = None,
    chip_data: dict[str, dict[str, dict[str, float | None]]] | None = None,
) -> pd.DataFrame:
    """主回测循环：预计算 enrich_bars → 遍历采样日期提取特征 → 评分 → 前向收益。

    Args:
        factor_sets: {方案名: (因子列表, 权重dict)}
        fin_data: 财务质量数据
        concept_members: 概念-股票映射（用于计算概念热度）
        chip_data: 筹码峰历史数据 {trade_date: {ts_code: {field: value}}}
    """
    t_start = time.time()

    # Phase 1: 预计算全部股票的特征
    logger.info("Phase 1/4: 预计算全量 enrich_bars...")
    enriched_cache = _precompute_enriched(all_data, sample_dates)

    # Phase 2: 构建快速查找结构
    logger.info("Phase 2/4: 构建收盘价查找表...")
    close_lookup = _build_close_lookup(all_data)

    logger.info("Phase 3/4: 按股票分割前向数据...")
    bars_by_code = _build_bars_by_code(all_data)

    # Phase 4: 遍历采样日期
    logger.info("Phase 4/4: 遍历 %s 个采样日期...", len(sample_dates))
    all_rows: list[pd.DataFrame] = []
    n_dates = len(sample_dates)
    max_horizon = max(horizons)

    # 筹码峰覆盖率日志
    if chip_data:
        chip_dates = set(chip_data.keys())
        covered = [d for d in sample_dates if d in chip_dates]
        logger.info(
            "筹码峰数据: %s/%s 采样日期命中 (%.1f%%)",
            len(covered), len(sample_dates),
            len(covered) / len(sample_dates) * 100 if sample_dates else 0,
        )

    for i, d in enumerate(sample_dates):
        t_iter = time.time()

        # 该日期的筹码峰数据（预取当日截面）
        chip_today: dict[str, dict[str, float | None]] = {}
        if chip_data:
            chip_today = chip_data.get(d, {})

        # 该日期的财务质量数据预注入
        if fin_data and factor_sets:
            clear_quality_cache()
            for code in enriched_cache:
                q = _get_quality_for_date(fin_data, code, d)
                if q:
                    set_quality_cache(code, q)

        # 该日期的概念热度计算（按需）
        concept_heat: dict[str, float] = {}
        if concept_members and factor_sets:
            concept_heat = _compute_concept_heat(concept_members, enriched_cache, close_lookup, d)
            # 为每只股票计算其概念平均热度
            stock_concept_heat: dict[str, float] = {}
            for code in enriched_cache:
                concepts = concept_members.get(code, [])
                heats = [concept_heat.get(c) for c in concepts if c in concept_heat]
                if heats:
                    stock_concept_heat[code] = sum(heats) / len(heats)

        scores: list[dict[str, Any]] = []

        for code, enriched in enriched_cache.items():
            # 取 <= d 的最后一行特征
            hist = enriched[enriched.index <= d]
            if hist.empty:
                continue

            # 确认该股票在 as_of 日有交易
            code_closes = close_lookup.get(code)
            if code_closes is None or d not in code_closes:
                continue
            close_val = code_closes[d]

            feat = latest_features(hist)
            if not feat:
                continue

            # 注入概念热度到特征中（供 v2 因子使用）
            ch = stock_concept_heat.get(code)
            if ch is not None:
                feat["_concept_heat"] = ch
            # 注入概念数量
            if concept_members:
                feat["_concept_count"] = float(len(concept_members.get(code, [])))

            # 注入筹码峰数据
            if chip_today and code in chip_today:
                c = chip_today[code]
                feat["_profit_ratio"] = c.get("profit_ratio")
                feat["_avg_cost"] = c.get("avg_cost")
                feat["_concentration_90"] = c.get("concentration_90")
                feat["_concentration_70"] = c.get("concentration_70")
                avg_cost = c.get("avg_cost")
                if (
                    close_val is not None
                    and avg_cost is not None
                    and float(avg_cost) > 0
                ):
                    feat["_cost_proximity"] = (float(close_val) - float(avg_cost)) / float(avg_cost) * 100.0

            raw = quick_technical_score(feat)
            ret_20d = feat.get("ret_20d")
            capped = apply_momentum_cap(raw, ret_20d)

            entry = {
                "ts_code": code,
                "sample_date": d,
                "score_v1_raw": round(raw, 1),
                "score_v1": round(capped, 1),
                "close": round(float(close_val), 4),
                "ret_20d": round(float(ret_20d), 2) if ret_20d is not None and not pd.isna(ret_20d) else None,
            }
            # 计算 v2 各方案评分
            if factor_sets:
                for scheme_name, (factors, weights) in factor_sets.items():
                    v2 = compute_score_v2(feat, factors=factors, weights=weights, code=code)
                    entry[f"score_{scheme_name}"] = v2
            scores.append(entry)

        if not scores:
            logger.info("[%3d/%3d] %s ─ 无有效数据，跳过", i + 1, n_dates, d)
            continue

        snap = pd.DataFrame(scores)

        # 计算前向收益（向量化，使用预分割的 bars_by_code）
        fwd_cache: dict[str, dict[int, float | None]] = {}
        for code in snap["ts_code"].unique():
            stock_bars = bars_by_code.get(code)
            if stock_bars is None:
                continue
            close_0 = close_lookup.get(code, {}).get(d)
            if close_0 is None:
                continue
            # 找到 as_of 之后的行
            future = stock_bars[stock_bars["trade_date"] > d]
            fwd_cache[code] = {}
            for h in horizons:
                if len(future) >= h:
                    close_h = future["close"].iloc[h - 1]
                    if not pd.isna(close_h) and close_h != 0:
                        fwd_cache[code][h] = round((close_h / close_0 - 1) * 100, 4)
                    else:
                        fwd_cache[code][h] = None
                else:
                    fwd_cache[code][h] = None

        for h in horizons:
            col = f"ret_{h}d_fwd"
            snap[col] = snap["ts_code"].map(lambda c: fwd_cache.get(c, {}).get(h))

        all_rows.append(snap)

        elapsed_iter = time.time() - t_iter
        elapsed_total = time.time() - t_start
        avg_per_date = elapsed_total / (i + 1)
        eta = avg_per_date * (n_dates - i - 1)
        logger.info(
            "[%3d/%3d] %s ─ %s 只股票评分，耗时 %.1f 秒 | 累计 %.1f 分 | 预计剩余 %.1f 分",
            i + 1, n_dates, d, len(snap), elapsed_iter, elapsed_total / 60, eta / 60,
        )

    if not all_rows:
        raise RuntimeError("无任何有效回测数据！请检查数据范围。")

    results = pd.concat(all_rows, ignore_index=True)

    # 对每个评分列分配五分位和十分位
    score_cols = ["score_v1"] + [c for c in results.columns if c.startswith("score_") and c != "score_v1" and not c.startswith("score_v1_")]
    for sc in score_cols:
        if sc not in results.columns:
            continue
        q_col = f"{sc}_quintile"
        d_col = f"{sc}_decile"

        def _make_quintile(col_name):
            def _assign(x):
                try:
                    bins, _ = pd.qcut(x, 5, labels=False, duplicates="drop", retbins=True)
                    return pd.Series(bins + 1, index=x.index)
                except ValueError:
                    return pd.Series(3, index=x.index)
            return _assign
        results[q_col] = results.groupby("sample_date")[sc].transform(_make_quintile(sc))

        def _make_decile(col_name):
            def _assign(x):
                try:
                    bins, _ = pd.qcut(x, 10, labels=False, duplicates="drop", retbins=True)
                    return pd.Series(bins + 1, index=x.index)
                except ValueError:
                    return pd.Series(5, index=x.index)
            return _assign
        results[d_col] = results.groupby("sample_date")[sc].transform(_make_decile(sc))

    total_elapsed = time.time() - t_start
    logger.info(
        "回测完成：%s 个日期，%s 条股票-日期记录，总耗时 %.1f 分",
        results["sample_date"].nunique(), len(results), total_elapsed / 60,
    )
    return results


def _fwd_return_fast(
    bars_by_code: dict[str, pd.DataFrame],
    ts_code: str,
    as_of: date,
    horizon: int,
) -> float | None:
    """快速前向收益：利用预分割的 bars_by_code dict（O(1) 查找）。"""
    stock_bars = bars_by_code.get(ts_code)
    if stock_bars is None:
        return None

    future = stock_bars[stock_bars["trade_date"] > as_of]
    if len(future) < horizon:
        return None

    close_0_rows = stock_bars[stock_bars["trade_date"] == as_of]
    if close_0_rows.empty:
        return None
    close_0 = close_0_rows["close"].iloc[0]
    if pd.isna(close_0) or close_0 == 0:
        return None

    close_h = future["close"].iloc[horizon - 1]
    if pd.isna(close_h) or close_h == 0:
        return None
    return round((close_h / close_0 - 1) * 100, 4)


# ── Step 3: 分析函数 ──────────────────────────────────

def _get_score_cols(results: pd.DataFrame) -> list[str]:
    """检测 results 中所有评分列（score_* 但不含 quintile/decile 后缀）。"""
    return sorted([
        c for c in results.columns
        if c.startswith("score_") and "_quintile" not in c and "_decile" not in c
        and not c.endswith("_raw")
    ])


def analyze_spearman(
    results: pd.DataFrame, horizons: list[int], score_col: str = "score_v1"
) -> dict[str, Any]:
    """Spearman 等级相关：评分 vs 前向收益。"""
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna() & results[score_col].notna()]
        if valid.empty:
            out[f"spearman_{h}d"] = {"mean_rho": None, "mean_p": None, "n_significant": 0, "n_dates": 0}
            continue
        rhos, pvals = [], []
        for d, grp in valid.groupby("sample_date"):
            if len(grp) < 10:
                continue
            rho, p = _spearmanr(grp[score_col].values, grp[col].values)
            rhos.append(rho)
            pvals.append(p)
        significant = sum(1 for p in pvals if p < 0.05)
        out[f"spearman_{h}d"] = {
            "mean_rho": round(float(np.mean(rhos)), 4) if rhos else None,
            "mean_p": round(float(np.mean(pvals)), 4) if pvals else None,
            "n_significant": significant,
            "n_dates": len(rhos),
            "significant_pct": round(significant / len(rhos) * 100, 1) if rhos else 0,
        }
    return out


def analyze_quintile(
    results: pd.DataFrame, horizons: list[int], score_col: str = "score_v1"
) -> dict[str, Any]:
    """五分位分析：每个分位的跨日期均值前向收益。"""
    q_col = f"{score_col}_quintile"
    if q_col not in results.columns:
        return {}
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna() & results[q_col].notna()]
        if valid.empty:
            out[f"quintile_{h}d"] = {}
            continue
        grp = valid.groupby(q_col)[col].agg(["mean", "std", "count"])
        q_data = {}
        for q_idx, row in grp.iterrows():
            q_data[int(q_idx)] = {
                "mean_return": round(float(row["mean"]), 4),
                "std": round(float(row["std"]), 4),
                "count": int(row["count"]),
            }
        if 1 in q_data and 5 in q_data:
            q_data["monotonic"] = q_data[5]["mean_return"] > q_data[1]["mean_return"]
        out[f"quintile_{h}d"] = q_data
    return out


def analyze_top_n(
    results: pd.DataFrame,
    horizons: list[int],
    top_n_list: list[int],
    score_col: str = "score_v1",
    n_random_samples: int = 1000,
    random_seed: int = 42,
) -> dict[str, Any]:
    """Top N 分析：最高分 N 只 vs 随机抽样基线。"""
    rng = np.random.default_rng(random_seed)
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna() & results[score_col].notna()]
        out_h: dict[str, Any] = {}
        for n in top_n_list:
            top_returns, random_returns = [], []
            for d, grp in valid.groupby("sample_date"):
                grp_sorted = grp.sort_values(score_col, ascending=False)
                if len(grp_sorted) < n:
                    continue
                top_n = grp_sorted.head(n)
                top_mean = top_n[col].mean()
                if pd.isna(top_mean):
                    continue
                top_returns.append(top_mean)
                pool = grp[col].dropna().values
                if len(pool) < n:
                    continue
                rand_means = [
                    float(np.mean(rng.choice(pool, size=n, replace=False)))
                    for _ in range(n_random_samples)
                ]
                random_returns.append(np.mean(rand_means))
            if top_returns:
                top_arr = np.array(top_returns)
                rand_arr = np.array(random_returns)
                excess = top_arr - rand_arr
                out_h[f"top_{n}"] = {
                    "mean_top_return": round(float(np.mean(top_arr)), 4),
                    "mean_random_return": round(float(np.mean(rand_arr)), 4),
                    "mean_excess": round(float(np.mean(excess)), 4),
                    "excess_std": round(float(np.std(excess, ddof=1)), 4),
                    "win_rate": round(float(np.mean(top_arr > rand_arr)) * 100, 1),
                    "n_dates": len(top_returns),
                }
        out[f"top_n_{h}d"] = out_h
    return out


def analyze_decay(
    results: pd.DataFrame, horizons: list[int], score_col: str = "score_v1"
) -> dict[str, Any]:
    """衰减分析。"""
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna() & results[score_col].notna()]
        if valid.empty:
            out[f"decay_{h}d"] = {}
            continue
        rhos = []
        for d, grp in valid.groupby("sample_date"):
            if len(grp) < 20:
                continue
            rho, _ = _spearmanr(grp[score_col].values, grp[col].values)
            rhos.append(rho)
        out[f"decay_{h}d"] = {
            "mean_rho": round(float(np.mean(rhos)), 4) if rhos else None,
            "n_dates": len(rhos),
        }
    return out


def analyze_market_regime(
    results: pd.DataFrame, horizons: list[int], score_col: str = "score_v1"
) -> dict[str, Any]:
    """按季度/年度分解 Spearman 相关性。"""
    results_copy = results.copy()
    results_copy["quarter"] = results_copy["sample_date"].apply(
        lambda d: f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    )
    results_copy["year"] = results_copy["sample_date"].apply(lambda d: d.year)
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = results_copy[results_copy[col].notna() & results_copy[score_col].notna()]
        if valid.empty:
            out[f"regime_{h}d"] = {}
            continue
        quarterly: dict[str, dict[str, Any]] = {}
        for q, grp in valid.groupby("quarter"):
            if len(grp) < 30:
                continue
            rho, p = _spearmanr(grp[score_col].values, grp[col].values)
            quarterly[q] = {"rho": round(float(rho), 4), "n": len(grp)}
        yearly: dict[str, dict[str, Any]] = {}
        for y, grp in valid.groupby("year"):
            if len(grp) < 50:
                continue
            rho, p = _spearmanr(grp[score_col].values, grp[col].values)
            yearly[str(y)] = {"rho": round(float(rho), 4), "n": len(grp)}
        out[f"regime_{h}d"] = {"quarterly": quarterly, "yearly": yearly}
    return out


def analyze_win_rate_by_decile(
    results: pd.DataFrame, horizons: list[int], score_col: str = "score_v1"
) -> dict[str, Any]:
    """十分位胜率。"""
    d_col = f"{score_col}_decile"
    if d_col not in results.columns:
        return {}
    out: dict[str, Any] = {}
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = results[results[col].notna() & results[d_col].notna()]
        if valid.empty:
            out[f"win_rate_{h}d"] = {}
            continue
        wr = valid.groupby(d_col)[col].apply(
            lambda x: round(float(np.mean(x > 0)) * 100, 1)
        )
        out[f"win_rate_{h}d"] = {int(k): v for k, v in wr.items()}
    return out


def run_all_analysis(
    results: pd.DataFrame,
    horizons: list[int],
    top_n_list: list[int],
) -> dict[str, Any]:
    """汇总全部分析维度，对每个评分列独立产出指标。"""
    logger.info("开始分析...")
    score_cols = _get_score_cols(results)
    if not score_cols:
        score_cols = ["score_v1"]
    logger.info("发现 %s 个评分列: %s", len(score_cols), score_cols)

    sc0 = score_cols[0]
    analysis: dict[str, Any] = {
        "summary": {
            "n_sample_dates": int(results["sample_date"].nunique()),
            "n_records": int(len(results)),
            "n_stocks_per_date_mean": round(float(results.groupby("sample_date").size().mean()), 1),
            "score_cols": score_cols,
        },
        "by_score": {},
    }

    for sc in score_cols:
        valid_count = int(results[sc].notna().sum())
        analysis["by_score"][sc] = {
            "score_mean": round(float(results[sc].mean()), 2),
            "score_std": round(float(results[sc].std()), 2),
            "score_range": [round(float(results[sc].min()), 1), round(float(results[sc].max()), 1)],
            "n_valid": valid_count,
            "spearman": analyze_spearman(results, horizons, sc),
            "quintile": analyze_quintile(results, horizons, sc),
            "top_n": analyze_top_n(results, horizons, top_n_list, sc),
            "decay": analyze_decay(results, horizons, sc),
            "market_regime": analyze_market_regime(results, horizons, sc),
            "win_rate_decile": analyze_win_rate_by_decile(results, horizons, sc),
        }

    logger.info("分析完成。")
    return analysis


# ── Step 4: 输出 ──────────────────────────────────────

def _first_score(analysis: dict[str, Any]) -> dict[str, Any]:
    """兼容新旧分析格式：返回第一个评分列的完整子分析。"""
    if "by_score" in analysis:
        sc0 = list(analysis["by_score"].keys())[0]
        return analysis["by_score"][sc0]
    return analysis


def print_summary(analysis: dict[str, Any], horizons: list[int]) -> None:
    """控制台输出核心结论。"""
    s = analysis["summary"]
    score_cols = s.get("score_cols", ["score_v1"])
    multi = len(score_cols) > 1

    print("\n" + "=" * 72)
    print("  规则分历史回测结果")
    print("=" * 72)
    print(f"  样本日期: {s['n_sample_dates']} 个")
    print(f"  总记录数: {s['n_records']:,}")
    print(f"  每日期平均股票数: {s['n_stocks_per_date_mean']}")
    if not multi and "by_score" in analysis:
        sc0 = score_cols[0]
        bs = analysis["by_score"][sc0]
        print(f"  评分均值: {bs['score_mean']}  标准差: {bs['score_std']}  范围: {bs['score_range']}")

    # ── 多方案对比表 ──
    if multi and "by_score" in analysis:
        print("\n── 方案对比 (20日前向) ──")
        header = f"  {'方案':<22} {'Spearman ρ':>10} {'单调性':>6} {'Top10超额':>10} {'Top10胜率':>8}"
        print(header)
        print(f"  {'-'*58}")
        for sc in score_cols:
            bs = analysis["by_score"][sc]
            sp = bs["spearman"].get("spearman_20d", {})
            qd = bs["quintile"].get("quintile_20d", {})
            tn = bs["top_n"].get("top_n_20d", {}).get("top_10", {})
            rho = f"{sp.get('mean_rho', 0):.4f}" if sp.get("mean_rho") is not None else "N/A"
            mono = "✓" if qd.get("monotonic") else "✗"
            excess = f"{tn.get('mean_excess', 0):+.2f}%" if tn.get("mean_excess") is not None else "N/A"
            wr = f"{tn.get('win_rate', 0):.1f}%" if tn.get("win_rate") is not None else "N/A"
            print(f"  {sc:<22} {rho:>10} {mono:>6} {excess:>10} {wr:>8}")
        print()

    # ── 第一个方案的详细信息 ──
    a = _first_score(analysis)

    # Spearman
    print("\n── Spearman 相关性 ──")
    for h in horizons:
        sp = a.get("spearman", {}).get(f"spearman_{h}d", {})
        if sp.get("mean_rho") is not None:
            print(
                f"  {h}日前向:  ρ = {sp['mean_rho']:.4f}  "
                f"显著日期: {sp['n_significant']}/{sp['n_dates']} ({sp['significant_pct']}%)"
            )
        else:
            print(f"  {h}日前向:  数据不足")

    # 五分位
    print("\n── 五分位分析 ──")
    for h in horizons:
        qdata = a.get("quintile", {}).get(f"quintile_{h}d", {})
        if not qdata:
            continue
        print(f"\n  {h}日前向收益:")
        print(f"    {'分位':<6} {'均值收益':>8}    {'标准差':>8}    {'样本数':>8}")
        print(f"    {'-'*40}")
        for q in sorted([k for k in qdata if isinstance(k, int)]):
            d = qdata[q]
            print(f"    Q{q:<5} {d['mean_return']:>8.2f}%   {d['std']:>8.2f}    {d['count']:>8,}")
        if "monotonic" in qdata:
            print(f"    单调性 (Q5 > Q1): {'✓' if qdata['monotonic'] else '✗'}")

    # Top N
    print("\n── Top N vs 随机基线 ──")
    for h in horizons:
        tn = a.get("top_n", {}).get(f"top_n_{h}d", {})
        if not tn:
            continue
        print(f"\n  {h}日前向收益:")
        print(f"    {'Top N':<8} {'均值':>8}    {'随机基线':>8}    {'超额':>8}    {'胜率':>6}")
        print(f"    {'-'*48}")
        for key in sorted(tn.keys(), key=lambda x: int(x.split("_")[1])):
            d = tn[key]
            n_val = key.split("_")[1]
            print(
                f"    Top {n_val:<4} {d['mean_top_return']:>8.2f}%   "
                f"{d['mean_random_return']:>8.2f}%   "
                f"{d['mean_excess']:>+8.2f}%   "
                f"{d['win_rate']:>5.1f}%"
            )

    # 衰减
    print("\n── 衰减曲线 ──")
    for h in horizons:
        dc = a.get("decay", {}).get(f"decay_{h}d", {})
        rho = dc.get("mean_rho", "N/A")
        print(f"  {h}日:  ρ = {rho}")

    # 市场制度
    print("\n── 市场制度分解（年度 Spearman ρ）──")
    for h in horizons:
        regime = a.get("market_regime", {}).get(f"regime_{h}d", {})
        yearly = regime.get("yearly", {})
        if yearly:
            print(f"  {h}日前向:")
            for y in sorted(yearly.keys()):
                print(f"    {y}: ρ = {yearly[y]['rho']:.4f}  (n={yearly[y]['n']})")

    print("\n" + "=" * 72)


def save_results(results: pd.DataFrame, analysis: dict[str, Any], output_dir: str) -> None:
    """保存完整结果到 JSON 文件。"""
    os.makedirs(output_dir, exist_ok=True)

    # 保存分析摘要
    analysis_path = os.path.join(output_dir, "rule_score_backtest_analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2, default=str)
    logger.info("分析结果已保存至 %s", analysis_path)

    # 保存原始回测数据（压缩 Parquet，更省空间）
    data_path = os.path.join(output_dir, "rule_score_backtest_data.parquet")
    # 转换 date 列为字符串以避免 parquet 兼容问题
    data_to_save = results.copy()
    data_to_save["sample_date"] = data_to_save["sample_date"].astype(str)
    data_to_save.to_parquet(data_path, index=False)
    logger.info("回测原始数据已保存至 %s (%s 行)", data_path, len(results))


# ── CLI ───────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="规则分历史回测：评估 quick_technical_score 的预测力，支持 v2 多因子对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--freq", type=int, default=10, help="采样频率（每 N 个交易日），默认 10")
    p.add_argument("--start", type=str, default=None, help="起始日期 YYYY-MM-DD，默认数据最早")
    p.add_argument("--end", type=str, default=None, help="结束日期 YYYY-MM-DD，默认数据最晚")
    p.add_argument("--horizons", type=int, nargs="+", default=[5, 10, 20], help="前向收益窗口（交易日），默认 5 10 20")
    p.add_argument("--top-n", type=int, nargs="+", default=[10, 30, 50, 100], help="Top N 组大小，默认 10 30 50 100")
    p.add_argument("--output", type=str, default="./results", help="输出目录，默认 ./results")
    p.add_argument("--llm-eval", action="store_true", help="同时评估历史 LLM 选股记录（实验性）")
    p.add_argument("--dry-run", action="store_true", help="仅加载数据 + 采样 1 个日期，快速验证")
    p.add_argument("--factors", type=str, default=None,
                   help="v2 方案名或因子列表（逗号分隔），如 'reversal_only' 或 'ret_20d_reversal,rsi_oversold'。"
                        "为空时默认仅跑 v1。设为 'compare' 时跑 v1+所有预定义方案对比。")
    p.add_argument("--preset", type=str, default=None,
                   help="使用预定义方案: reversal_only, reversal_plus_quality, all_technical")
    return p.parse_args(argv)


def _build_factor_sets(args: argparse.Namespace) -> dict[str, tuple[list[str], dict[str, float]]] | None:
    """从 CLI 参数构建因子方案字典。"""
    if args.preset:
        if args.preset not in PRESET_SCHEMES:
            logger.error("未知预定义方案: %s，可用: %s", args.preset, list(PRESET_SCHEMES.keys()))
            sys.exit(1)
        factors, weights = PRESET_SCHEMES[args.preset]
        return {args.preset: (factors, weights)}

    if args.factors:
        if args.factors == "compare":
            # 对比模式：v1 + 所有预定义方案
            result = {}
            for name, (factors, weights) in PRESET_SCHEMES.items():
                result[name] = (factors, weights)
            return result
        else:
            # 自定义因子列表，等权
            factor_names = [f.strip() for f in args.factors.split(",")]
            invalid = [f for f in factor_names if f not in TECH_FACTORS and f not in ALL_FACTOR_NAMES]
            if invalid:
                logger.error("未知因子: %s，可用技术因子: %s", invalid, list(TECH_FACTORS.keys()))
                sys.exit(1)
            return {"custom": (factor_names, {f: 1.0 for f in factor_names})}

    return None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    start_d = date.fromisoformat(args.start) if args.start else None
    end_d = date.fromisoformat(args.end) if args.end else None

    QUALITY_FACTOR_NAMES = {"profit_positive", "revenue_growth", "profit_growth",
                             "profit_margin", "profit_stability"}
    factor_sets = _build_factor_sets(args)
    if factor_sets:
        logger.info("因子方案: %s", list(factor_sets.keys()))

    # 检测是否需要财务数据
    need_financial = False
    if factor_sets:
        for _name, (f_list, _w) in factor_sets.items():
            if any(f in QUALITY_FACTOR_NAMES for f in f_list):
                need_financial = True
                break

    sector_map = _load_sector_map() if need_financial else {}
    fin_data = _load_financial_quality(sector_map) if need_financial else None

    # 概念数据（用于概念热度因子——独立于财务数据，始终加载）
    concept_members = _load_concept_members() if factor_sets else None

    # 加载数据
    all_data = load_all_bars(start_date=start_d, end_date=end_d)
    if all_data.empty:
        logger.error("无日线数据！请先运行 zplan-股价 ETL。")
        sys.exit(1)

    trade_dates = get_trade_dates(all_data)
    max_horizon = max(args.horizons)
    sample_dates = get_sample_dates(trade_dates, freq=args.freq, max_lookahead_days=max_horizon + 20)

    # 筹码峰数据（用于筹码因子——需在 sample_dates 确定后加载）
    chip_data = _load_chip_panel_by_dates(sample_dates) if factor_sets else None

    if args.dry_run:
        test_date = sample_dates[0]
        logger.info("Dry-run 模式：先预计算特征，再测试 1 个日期 %s", test_date)
        cache = _precompute_enriched(all_data, sample_dates)
        close_lk = _build_close_lookup(all_data)
        scores = []
        for code, enriched in cache.items():
            hist = enriched[enriched.index <= test_date]
            if hist.empty:
                continue
            feat = latest_features(hist)
            if not feat:
                continue
            code_closes = close_lk.get(code)
            if code_closes is None or test_date not in code_closes:
                continue
            close_val = code_closes[test_date]
            raw = quick_technical_score(feat)
            ret_20d = feat.get("ret_20d")
            capped = apply_momentum_cap(raw, ret_20d)
            entry = {
                "ts_code": code,
                "sample_date": test_date,
                "score_v1": round(capped, 1),
                "close": round(float(close_val), 4),
                "ret_20d": round(float(ret_20d), 2) if ret_20d is not None and not pd.isna(ret_20d) else None,
            }
            if factor_sets:
                for scheme_name, (factors, weights) in factor_sets.items():
                    v2 = compute_score_v2(feat, factors=factors, weights=weights, code=code)
                    entry[f"score_{scheme_name}"] = v2
            scores.append(entry)
        snap = pd.DataFrame(scores)
        logger.info("快照结果 (%s 个评分列)：%s 行\n%s",
                     1 + len(factor_sets or {}), len(snap), snap.head(10).to_string())
        return

    # 回测
    results = run_backtest(all_data, sample_dates, args.horizons, factor_sets, fin_data, concept_members, chip_data)

    # 分析
    analysis = run_all_analysis(results, args.horizons, args.top_n)

    # 输出
    print_summary(analysis, args.horizons)
    save_results(results, analysis, args.output)

    # 可选：LLM 对比
    if args.llm_eval:
        eval_llm_vs_rule(all_data, args.horizons, args.output)


# ── 可选扩展：LLM 对比 ────────────────────────────────

def eval_llm_vs_rule(
    all_data: pd.DataFrame,
    horizons: list[int],
    output_dir: str,
) -> None:
    """加载历史 LLM 选股记录，对比规则分 vs LLM 分的预测力。"""
    logger.info("加载 LLM 选股历史记录...")
    engine = build_engine()
    sql = """
        SELECT
            r.trade_date_as_of,
            e.ts_code,
            e.rank_in_run,
            e.rule_composite_score,
            e.llm_composite_score,
            e.final_composite_score,
            e.recommendation
        FROM pick_entries e
        JOIN pick_runs r ON e.run_id = r.id
        WHERE r.run_kind = 'llm_top300'
          AND r.llm_enabled = 1
          AND e.llm_composite_score IS NOT NULL
        ORDER BY r.trade_date_as_of, e.rank_in_run
    """
    llm_df = pd.read_sql_query(sql, engine)
    if llm_df.empty:
        logger.warning("无可用 LLM 选股记录，跳过 LLM 对比。")
        return

    llm_df["trade_date_as_of"] = pd.to_datetime(llm_df["trade_date_as_of"]).dt.date
    logger.info("加载到 %s 条 LLM 选股记录，%s 个日期", len(llm_df), llm_df["trade_date_as_of"].nunique())

    # 计算前向收益
    llm_bars = _build_bars_by_code(all_data)
    for h in horizons:
        col = f"ret_{h}d_fwd"
        llm_df[col] = llm_df.apply(
            lambda row: _fwd_return_fast(llm_bars, row["ts_code"], row["trade_date_as_of"], h),
            axis=1,
        )

    # 配对比较
    comparisons: list[dict[str, Any]] = []
    for h in horizons:
        col = f"ret_{h}d_fwd"
        valid = llm_df[llm_df[col].notna()].copy()
        if valid.empty:
            continue

        # 规则分 vs 前向收益
        rho_rule, p_rule = _spearmanr(valid["rule_composite_score"], valid[col])
        # LLM 分 vs 前向收益
        rho_llm, p_llm = _spearmanr(valid["llm_composite_score"], valid[col])

        # LLM 是否抬分了下跌的票？
        valid["score_delta"] = valid["llm_composite_score"] - valid["rule_composite_score"]
        worse = ((valid[col] < 0) & (valid["score_delta"] > 3)).sum()
        total = len(valid)

        comparisons.append({
            "horizon": h,
            "n_entries": total,
            "spearman_rule": round(float(rho_rule), 4),
            "spearman_rule_p": round(float(p_rule), 4),
            "spearman_llm": round(float(rho_llm), 4),
            "spearman_llm_p": round(float(p_llm), 4),
            "llm_worse_than_rule": int(worse),
            "llm_worse_pct": round(float(worse / total * 100), 1) if total > 0 else 0,
            "mean_rule_score": round(float(valid["rule_composite_score"].mean()), 2),
            "mean_llm_score": round(float(valid["llm_composite_score"].mean()), 2),
            "mean_score_delta": round(float(valid["score_delta"].mean()), 2),
        })

    logger.info("LLM vs 规则 对比:")
    for c in comparisons:
        logger.info("  %s日: 规则 ρ=%.4f  LLM ρ=%.4f  LLM抬分致亏=%s/%s (%.1f%%)",
                     c["horizon"], c["spearman_rule"], c["spearman_llm"],
                     c["llm_worse_than_rule"], c["n_entries"], c["llm_worse_pct"])

    # 保存
    llm_out = {
        "comparisons": comparisons,
        "n_dates": int(llm_df["trade_date_as_of"].nunique()),
        "n_entries": len(llm_df),
    }
    llm_path = os.path.join(output_dir, "llm_vs_rule_comparison.json")
    with open(llm_path, "w", encoding="utf-8") as f:
        json.dump(llm_out, f, ensure_ascii=False, indent=2, default=str)
    logger.info("LLM 对比结果已保存至 %s", llm_path)


if __name__ == "__main__":
    main()
