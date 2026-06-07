"""股价走势可视化：K 线 + 均线 + 成交量 + MACD + 多空信号标注 + 相似历史形态画廊。

生成一张包含以下面板的 PNG 图表：
- 主图：近 120 日 K 线 + MA5/MA10/MA20/MA60 + 买卖价位 + 信号标注 + 技术解读
- 量能副图：成交量柱 + 均量线 + 量价解读
- 相似形态画廊（可选）：历史最相似片段的小 K 线缩略图 + 后市收益
- MACD 副图：DIF/DEA + 柱状图 + 趋势解读

配色方案：
- 阳线红色（A 股习惯），阴线绿色
- 均线：MA5 白、MA10 黄、MA20 紫、MA60 绿
- 看多信号绿色箭头，看空信号红色箭头
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import mplfinance as mpf
import numpy as np
import pandas as pd
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec
from matplotlib.patches import FancyBboxPatch

from zplan_shared.features import enrich_bars, feature_flag, suggested_price_levels
from zplan_shared.market import get_bars, resolve_ts_code
from zplan_shared.models import init_db

logger = logging.getLogger(__name__)

# ── 中文字体配置 ────────────────────────────────────────────────
_matplotlib_ready = False


def _init_matplotlib() -> None:
    global _matplotlib_ready
    if _matplotlib_ready:
        return
    for name in ("PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS", "SimHei"):
        try:
            matplotlib.font_manager.findfont(name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [name] + plt.rcParams["font.sans-serif"]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False
    _matplotlib_ready = True


# ── 颜色常量 ────────────────────────────────────────────────────
CLR_UP = "#e74c3c"
CLR_DOWN = "#27ae60"
CLR_MA5 = "#ecf0f1"
CLR_MA10 = "#f1c40f"
CLR_MA20 = "#9b59b6"
CLR_MA60 = "#2ecc71"
CLR_BUY = "#2ecc71"
CLR_TARGET = "#e74c3c"
CLR_STOP = "#95a5a6"
CLR_SUPPORT = "#3498db"
CLR_RESISTANCE = "#e67e22"
CLR_BULL = "#27ae60"
CLR_BEAR = "#e74c3c"
CLR_BG = "#1a1a2e"
CLR_GRID = "#2d2d44"
CLR_TEXT = "#bdc3c7"
CLR_TEXT_BRIGHT = "#ecf0f1"
CLR_VOL_UP = "#e74c3c88"
CLR_VOL_DOWN = "#27ae6088"
CLR_MACD_UP = "#e74c3c"
CLR_MACD_DOWN = "#27ae60"
CLR_GALLERY_WIN = "#27ae60"
CLR_GALLERY_LOSE = "#e74c3c"

MA_COLORS = {"ma5": CLR_MA5, "ma10": CLR_MA10, "ma20": CLR_MA20, "ma60": CLR_MA60}

MA_NAMES = {"ma5": "MA5(5日均线)", "ma10": "MA10(10日)", "ma20": "MA20(生命线)", "ma60": "MA60(牛熊线)"}

FIG_DPI = 200


# ── 主入口 ──────────────────────────────────────────────────────


def plot_stock_chart(
    ts_code: str,
    *,
    lookback: int = 120,
    output_dir: str | None = None,
    price_levels: dict[str, float | None] | None = None,
    risk_flags: list[str] | None = None,
    signals: list[str] | None = None,
    similar_patterns: dict[str, Any] | None = None,
    title: str | None = None,
) -> str:
    """生成单票全景分析图，返回 PNG 文件路径。"""
    _init_matplotlib()
    init_db()

    code = resolve_ts_code(ts_code)

    # 1. 获取数据
    bars = get_bars(code)
    if bars.empty:
        raise ValueError(f"无行情数据: {code}")

    enriched = enrich_bars(bars)
    recent = enriched.tail(lookback).copy()
    if recent.empty:
        raise ValueError(f"数据不足: {code}")

    if price_levels is None:
        price_levels = suggested_price_levels(bars)

    name = _stock_name(code)

    # ── 构建技术解读文本 ──
    interpretation = _build_interpretation(recent, price_levels, signals, risk_flags)

    # 2. 面板布局
    has_gallery = bool(similar_patterns and similar_patterns.get("matches"))

    if has_gallery:
        n_matches = len(similar_patterns["matches"])
        fig = plt.figure(figsize=(22, 22 + n_matches * 1.0))
        gs = GridSpec(5, 1, figure=fig, height_ratios=[6, 1.8, 0.12, 2.5, 2.2], hspace=0.28)
        ax_main = fig.add_subplot(gs[0])
        ax_vol = fig.add_subplot(gs[1], sharex=ax_main)
        ax_gap = fig.add_subplot(gs[2])
        ax_gap.axis("off")
        ax_macd = fig.add_subplot(gs[4], sharex=ax_main)
        gs_gallery = GridSpecFromSubplotSpec(1, n_matches, subplot_spec=gs[3], wspace=0.12)
        gallery_axes = [fig.add_subplot(gs_gallery[i]) for i in range(n_matches)]
    else:
        fig = plt.figure(figsize=(22, 15))
        gs = GridSpec(4, 1, figure=fig, height_ratios=[6, 1.8, 0.06, 2.2], hspace=0.22)
        ax_main = fig.add_subplot(gs[0])
        ax_vol = fig.add_subplot(gs[1], sharex=ax_main)
        ax_gap = fig.add_subplot(gs[2])
        ax_gap.axis("off")
        ax_macd = fig.add_subplot(gs[3], sharex=ax_main)
        gallery_axes = []

    # 3. 绘制各面板
    _draw_main_chart(ax_main, recent, price_levels, signals, code, name, risk_flags, title, interpretation)
    _draw_volume(ax_vol, recent, interpretation)
    _draw_macd(ax_macd, recent, interpretation)

    # 4. 相似形态画廊
    if has_gallery:
        _draw_similar_gallery(fig, gallery_axes, similar_patterns)

    # 5. 全局微调
    _stamp_footer(fig, code, recent)

    # 6. 保存
    if output_dir is None:
        from zplan_shared.config import ZPLAN_ROOT
        output_dir = os.path.join(ZPLAN_ROOT, "charts")
    os.makedirs(output_dir, exist_ok=True)
    as_of_str = str(recent.index[-1]).replace("-", "")[:8]
    fname = f"{code}_{as_of_str}.png"
    output_path = os.path.join(output_dir, fname)

    fig.savefig(output_path, dpi=FIG_DPI, bbox_inches="tight", facecolor=CLR_BG, edgecolor="none")
    plt.close(fig)
    logger.info("图表已生成: %s", output_path)
    return output_path


# ── 技术解读生成 ────────────────────────────────────────────────


def _build_interpretation(
    df: pd.DataFrame,
    price_levels: dict[str, float | None],
    signals: list[str] | None,
    risk_flags: list[str] | None,
) -> dict[str, list[str]]:
    """从数据中生成各面板的解读文字。

    返回: {"main": [...], "volume": [...], "macd": [...]}
    """
    interp: dict[str, list[str]] = {"main": [], "volume": [], "macd": []}
    f = {}
    for col in df.columns:
        val = df[col].iloc[-1]
        try:
            f[col] = float(val) if pd.notna(val) else None
        except (ValueError, TypeError):
            pass  # 跳过非数值列（如 adjust_type）

    close = f.get("close")

    # ── 主图解读：趋势 + 均线 + 位置 + 风险 ──
    ma5, ma10, ma20, ma60 = f.get("ma5"), f.get("ma10"), f.get("ma20"), f.get("ma60")

    # 均线排列
    if ma5 and ma10 and ma20 and ma60 and all(x is not None for x in [ma5, ma10, ma20, ma60]):
        if ma5 > ma10 > ma20 > ma60:
            interp["main"].append("均线多头排列，上升趋势明确")
        elif ma5 < ma10 < ma20 < ma60:
            interp["main"].append("均线空头排列，处于下降通道")
        else:
            interp["main"].append("均线交织，趋势方向不明朗")

    # 股价 vs 均线
    if close:
        above = []
        below = []
        for ma_key, ma_name in [("ma5", "MA5"), ("ma20", "MA20"), ("ma60", "MA60")]:
            mav = f.get(ma_key)
            if mav is not None:
                if close > mav:
                    above.append(ma_name)
                else:
                    below.append(ma_name)
        if above:
            interp["main"].append(f"股价站上 {'、'.join(above)}，短线偏强")
        if below:
            interp["main"].append(f"股价低于 {'、'.join(below)}，{'中长期承压' if 'MA60' in below else '短线承压'}")

    # RSI 位置
    rsi = f.get("rsi14")
    if rsi is not None:
        if rsi > 70:
            interp["main"].append(f"RSI={rsi:.0f}，处于超买区域，追高风险")
        elif rsi < 30:
            interp["main"].append(f"RSI={rsi:.0f}，处于超卖区域，反弹机会")
        else:
            interp["main"].append(f"RSI={rsi:.0f}，处于中性区间")

    # 价位
    buy = price_levels.get("suggested_buy")
    target = price_levels.get("target_price")
    stop = price_levels.get("stop_loss")
    if close and buy and target:
        upside = (target / close - 1) * 100
        downside = (close / buy - 1) * 100 if buy > 0 else 0
        interp["main"].append(f"建议买入 ¥{buy:.2f}，目标 ¥{target:.2f}（+{upside:.1f}%），止损 ¥{stop:.2f}" if stop else f"建议买入 ¥{buy:.2f}，目标 ¥{target:.2f}")

    # 信号
    if signals:
        interp["main"].append("信号: " + "；".join(signals[:4]))

    # 风险
    if risk_flags:
        interp["main"].append("风险: " + " / ".join(risk_flags[:3]))

    # ── 量能解读 ──
    vol_ratio = f.get("vol_ratio20")
    if vol_ratio is not None:
        if vol_ratio >= 1.5:
            interp["volume"].append(f"量比 {vol_ratio:.1f}，显著放量，资金活跃")
        elif vol_ratio >= 1.0:
            interp["volume"].append(f"量比 {vol_ratio:.1f}，成交量正常")
        else:
            interp["volume"].append(f"量比 {vol_ratio:.1f}，缩量状态，交投清淡")

    # 量价配合
    pct_chg = f.get("pct_chg")
    if pct_chg is not None and vol_ratio is not None:
        if pct_chg > 0 and vol_ratio > 1.2:
            interp["volume"].append("价涨量增，上涨有资金配合")
        elif pct_chg > 0 and vol_ratio < 0.8:
            interp["volume"].append("价涨量缩，上涨动力不足，警惕回落")
        elif pct_chg < 0 and vol_ratio > 1.2:
            interp["volume"].append("价跌量增，抛压较重")
        elif pct_chg < 0 and vol_ratio < 0.8:
            interp["volume"].append("价跌量缩，抛压减轻，关注止跌")

    # ── MACD 解读 ──
    dif = f.get("macd_dif")
    dea = f.get("macd_dea")
    hist = f.get("macd_hist")
    if dif is not None and dea is not None and hist is not None:
        if dif > 0:
            interp["macd"].append(f"DIF={dif:.2f}>0，多方主导")
        else:
            interp["macd"].append(f"DIF={dif:.2f}<0，空方主导")

        if dif > dea:
            interp["macd"].append("DIF在DEA上方，短期偏强")
        else:
            interp["macd"].append("DIF在DEA下方，短期偏弱")

        if hist > 0:
            if hist > dif * 0.5 if dif > 0 else True:
                interp["macd"].append("红柱放大，多头动能增强")
            else:
                interp["macd"].append("红柱缩小，多头动能减弱")
        else:
            interp["macd"].append("绿柱状态，空头动能持续")

    # KDJ
    k, d, j = f.get("kdj_k"), f.get("kdj_d"), f.get("kdj_j")
    if k is not None and d is not None:
        if k > 80:
            interp["macd"].append(f"KDJ超买(K={k:.0f})，短线过热")
        elif k < 20:
            interp["macd"].append(f"KDJ超卖(K={k:.0f})，短线超跌")
        elif k > d:
            interp["macd"].append("KDJ多头排列(K>D)")
        else:
            interp["macd"].append("KDJ空头排列(K<D)")

    return interp


# ── 子图绘制 ────────────────────────────────────────────────────


def _draw_main_chart(
    ax: plt.Axes,
    df: pd.DataFrame,
    price_levels: dict[str, float | None],
    signals: list[str] | None,
    code: str,
    name: str | None,
    risk_flags: list[str] | None,
    title: str | None,
    interp: dict[str, list[str]],
) -> None:
    """主图：K 线 + 均线 + 价位线 + 信号标注 + 解读框。"""
    n = len(df)

    # ── K 线体 ──
    width = 0.65
    for i, (_, row) in enumerate(df.iterrows()):
        op, hi, lo, cl = row["open"], row["high"], row["low"], row["close"]
        color = CLR_UP if cl >= op else CLR_DOWN
        ax.plot([i, i], [lo, hi], color=color, linewidth=1.0, solid_capstyle="round", alpha=0.9)
        body_lo = min(op, cl)
        body_hi = max(op, cl)
        body_h = body_hi - body_lo
        if body_h > 0:
            ax.add_patch(plt.Rectangle(
                (i - width / 2, body_lo), width, body_h,
                facecolor=color, edgecolor=color, linewidth=0.6, alpha=0.95,
            ))
        else:
            ax.plot([i - width / 2, i + width / 2], [cl, cl], color=color, linewidth=1.2)

    # ── 均线 ──
    for ma_key, ma_color in MA_COLORS.items():
        if ma_key in df.columns:
            ax.plot(range(n), df[ma_key].values, color=ma_color, linewidth=1.5,
                    label=MA_NAMES.get(ma_key, ma_key.upper()), alpha=0.85)

    # ── 价位线 ──
    last_idx = n - 1
    visible_start = max(0, n - 40)
    _hline_annotate(ax, price_levels.get("suggested_buy"), CLR_BUY, "建议买入", last_idx, df, visible_start)
    _hline_annotate(ax, price_levels.get("target_price"), CLR_TARGET, "目标价", last_idx, df, visible_start)
    _hline_annotate(ax, price_levels.get("stop_loss"), CLR_STOP, "止损", last_idx, df, visible_start)
    _hline_annotate(ax, price_levels.get("support_20d"), CLR_SUPPORT, "20日支撑", last_idx, df, visible_start)
    _hline_annotate(ax, price_levels.get("resistance_20d"), CLR_RESISTANCE, "20日阻力", last_idx, df, visible_start)

    # ── 信号标注 ──
    if signals:
        _annotate_signals(ax, df, signals)

    # ── X 轴 ──
    _format_date_axis(ax, df)

    # ── 标题 ──
    display_name = name or code
    close_val = df["close"].iloc[-1]
    pct = df["pct_chg"].iloc[-1] if "pct_chg" in df.columns else None
    pct_str = f"  {pct:+.2f}%" if (pct is not None and not np.isnan(pct)) else ""
    title_text = title or f"{display_name}  {code}  ¥{close_val:.2f}{pct_str}"
    ax.set_title(title_text, fontsize=15, fontweight="bold", color="white", loc="left", pad=10)

    # ── 技术解读框（右上）──
    if interp.get("main"):
        _draw_interpretation_box(ax, interp["main"], "技术解读", position="right")

    ax.set_ylabel("")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.3, edgecolor=CLR_GRID,
              labelcolor=CLR_TEXT, ncol=2)


def _draw_volume(ax: plt.Axes, df: pd.DataFrame, interp: dict[str, list[str]]) -> None:
    """成交量副图 + 量价解读。"""
    n = len(df)
    colors = [
        CLR_VOL_UP if df["close"].iloc[i] >= df["open"].iloc[i] else CLR_VOL_DOWN
        for i in range(n)
    ]
    ax.bar(range(n), df["volume"].values, color=colors, width=0.75, edgecolor="none", alpha=0.85)

    if "vol_ma20" in df.columns:
        ax.plot(range(n), df["vol_ma20"].values, color=CLR_MA20, linewidth=1.5, label="均量(MA20)")

    # 放量标记
    if "vol_ratio20" in df.columns:
        for i in range(n):
            v = df["vol_ratio20"].iloc[i]
            if not np.isnan(v) and v >= 1.5:
                y = df["volume"].iloc[i]
                ax.annotate("放量", (i, y), textcoords="offset points", xytext=(0, 8),
                            fontsize=7, color=CLR_BEAR, ha="center", fontweight="bold")

    _format_date_axis(ax, df)
    ax.set_ylabel("成交量", fontsize=9, color=CLR_TEXT_BRIGHT)

    # ── 量能解读 ──
    if interp.get("volume"):
        _draw_interpretation_box(ax, interp["volume"], "量能分析", position="right")

    ax.legend(loc="upper left", fontsize=8, framealpha=0.3, edgecolor=CLR_GRID)


def _draw_macd(ax: plt.Axes, df: pd.DataFrame, interp: dict[str, list[str]]) -> None:
    """MACD 副图 + 趋势解读。"""
    has_macd = all(k in df.columns for k in ("macd_dif", "macd_dea", "macd_hist"))
    if not has_macd:
        ax.text(0.5, 0.5, "MACD 数据缺失", transform=ax.transAxes, ha="center",
                fontsize=12, color=CLR_TEXT)
        return

    dif = df["macd_dif"].values
    dea = df["macd_dea"].values
    hist = df["macd_hist"].values
    x = range(len(df))
    n = len(df)

    bar_colors = [CLR_MACD_UP if h >= 0 else CLR_MACD_DOWN for h in hist]
    ax.bar(x, hist, color=bar_colors, width=0.65, edgecolor="none", alpha=0.8)

    ax.plot(x, dif, color="#f1c40f", linewidth=1.5, label="DIF(快线)")
    ax.plot(x, dea, color="#e67e22", linewidth=1.5, label="DEA(慢线)")

    ax.axhline(0, color=CLR_GRID, linewidth=1.0, linestyle="--", alpha=0.7)

    # 金叉/死叉标注
    for i in range(1, n):
        if all(not np.isnan(v) for v in (dif[i], dea[i], dif[i-1], dea[i-1])):
            if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
                ax.annotate("金叉", (i, hist[i]), textcoords="offset points",
                            xytext=(0, 12), fontsize=7, color=CLR_BULL, ha="center",
                            fontweight="bold",
                            arrowprops=dict(arrowstyle="->", color=CLR_BULL, lw=1.0))
            elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
                ax.annotate("死叉", (i, hist[i]), textcoords="offset points",
                            xytext=(0, -14), fontsize=7, color=CLR_BEAR, ha="center",
                            fontweight="bold",
                            arrowprops=dict(arrowstyle="->", color=CLR_BEAR, lw=1.0))

    _format_date_axis(ax, df)
    ax.set_ylabel("MACD", fontsize=9, color=CLR_TEXT_BRIGHT)

    # ── MACD 解读 ──
    if interp.get("macd"):
        _draw_interpretation_box(ax, interp["macd"], "MACD / KDJ", position="right")

    ax.legend(loc="upper left", fontsize=8, framealpha=0.3, edgecolor=CLR_GRID)


def _draw_similar_gallery(
    fig: plt.Figure,
    axes: list[plt.Axes],
    similar: dict[str, Any],
) -> None:
    """相似历史形态画廊。"""
    matches = similar.get("matches") or []
    summary = similar.get("summary") or {}

    if not matches or not axes:
        return

    for i, (ax, m) in enumerate(zip(axes, matches)):
        ts = m.get("ts_code", "")
        match_date = m.get("match_date", "")
        sim = m.get("similarity", 0)
        fwd = m.get("forward_return_20d", 0)
        name = m.get("name", ts)

        try:
            bars = get_bars(ts, end=match_date)
            if bars.empty or len(bars) < 20:
                _empty_gallery_cell(ax, name)
                continue
            _mini_candlestick(ax, bars.tail(60))

            edge_color = CLR_GALLERY_WIN if fwd > 0 else CLR_GALLERY_LOSE
            for spine in ax.spines.values():
                spine.set_color(edge_color)
                spine.set_linewidth(2.5)

            date_short = match_date[5:] if len(match_date) >= 10 else match_date
            label = (
                f"{name[:4]}\n{date_short}\n"
                f"相似度 {sim:.0%}\n"
                f"20日后 {fwd:+.1f}%"
            )
            ax.set_title(label, fontsize=7, color=edge_color, fontweight="bold", pad=4)
        except Exception:
            _empty_gallery_cell(ax, name)

    if summary:
        total = summary.get("total", 0)
        win = summary.get("win_count", 0)
        avg_ret = summary.get("avg_return_20d", 0)
        verdict = summary.get("verdict", "")
        verdict_color = CLR_GALLERY_WIN if verdict == "偏多" else (CLR_GALLERY_LOSE if verdict == "偏空" else CLR_TEXT)

        # 构建详细摘要
        best_ret = summary.get("best_return", 0)
        worst_ret = summary.get("worst_return", 0)
        summary_text = (
            f"历史相似形态: {win}/{total} 只上涨 ({win/total*100:.0f}%)  |  "
            f"20日平均收益 {avg_ret:+.1f}%  |  "
            f"最好 {best_ret:+.1f}% / 最差 {worst_ret:+.1f}%  |  "
            f"结论: {verdict}"
        )
        fig.text(0.5, 0.355, summary_text, transform=fig.transFigure,
                 fontsize=11, color=verdict_color, fontweight="bold",
                 ha="center", va="center",
                 bbox=dict(boxstyle="round,pad=0.4", facecolor="#2c3e5044",
                           edgecolor=verdict_color, linewidth=1.0))


def _mini_candlestick(ax: plt.Axes, df: pd.DataFrame) -> None:
    """迷你 K 线。"""
    ax.set_facecolor(CLR_BG)
    width = 0.7
    for i, (_, row) in enumerate(df.iterrows()):
        op, hi, lo, cl = row["open"], row["high"], row["low"], row["close"]
        color = CLR_UP if cl >= op else CLR_DOWN
        ax.plot([i, i], [lo, hi], color=color, linewidth=0.5, solid_capstyle="round")
        body_lo, body_hi = min(op, cl), max(op, cl)
        if body_hi > body_lo:
            ax.add_patch(plt.Rectangle(
                (i - width / 2, body_lo), width, body_hi - body_lo,
                facecolor=color, edgecolor=color, linewidth=0.3,
            ))
        else:
            ax.plot([i - width / 2, i + width / 2], [cl, cl], color=color, linewidth=0.5)
    ax.set_xticks([])
    ax.set_yticks([])
    if len(df) > 0:
        y_min, y_max = df["low"].min(), df["high"].max()
        if y_max > y_min:
            ax.set_ylim(y_min * 0.98, y_max * 1.02)


def _empty_gallery_cell(ax: plt.Axes, name: str) -> None:
    ax.set_facecolor(CLR_BG)
    ax.text(0.5, 0.5, f"{name}\n数据不足", transform=ax.transAxes,
            ha="center", fontsize=8, color=CLR_TEXT)
    ax.set_xticks([])
    ax.set_yticks([])


# ── 辅助函数 ────────────────────────────────────────────────────


def _hline_annotate(ax: plt.Axes, price: float | None, color: str, label: str,
                    last_idx: int, df: pd.DataFrame, visible_start: int) -> None:
    """水平价位线 + 标签。"""
    if price is None or np.isnan(price):
        return
    y_min = df["low"].iloc[visible_start:].min()
    y_max = df["high"].iloc[visible_start:].max()
    if not (y_min * 0.95 <= price <= y_max * 1.05):
        return
    ax.axhline(price, xmin=0, xmax=1, color=color, linewidth=1.2, linestyle="--", alpha=0.8)
    ax.annotate(
        f" {label} ¥{price:.2f}",
        (last_idx, price), textcoords="offset points", xytext=(10, 0),
        fontsize=7.5, color=color, va="center", fontweight="bold",
    )


def _annotate_signals(ax: plt.Axes, df: pd.DataFrame, signals: list[str]) -> None:
    """最近 20 根 K 线内标注多空信号。"""
    n = len(df)
    recent_start = max(0, n - 20)
    signal_idx = recent_start

    for sig_text in signals[:6]:
        is_bull = _is_bullish_signal(sig_text)
        arrow_color = CLR_BULL if is_bull else CLR_BEAR
        arrow_dir = "▲" if is_bull else "▼"
        y_offset = 18 if is_bull else -18

        idx = min(signal_idx, n - 1)
        price = df["low"].iloc[idx] if is_bull else df["high"].iloc[idx]
        ax.annotate(
            f"{arrow_dir} {sig_text[:8]}",
            (idx, price), textcoords="offset points", xytext=(0, y_offset),
            fontsize=7, color=arrow_color, ha="center", fontweight="bold",
        )
        signal_idx += 6


def _is_bullish_signal(text: str) -> bool:
    bullish_kw = ["多头", "金叉", "上穿", "突破", "站上", "放量", "超卖", "背离收窄", "止跌"]
    bearish_kw = ["空头", "死叉", "下穿", "跌破", "超买", "量价背离", "追高", "缩量", "破位"]
    for kw in bearish_kw:
        if kw in text:
            return False
    for kw in bullish_kw:
        if kw in text:
            return True
    return True


def _format_date_axis(ax: plt.Axes, df: pd.DataFrame) -> None:
    n = len(df)
    if n <= 30:
        step = max(1, n // 8)
    elif n <= 90:
        step = max(1, n // 12)
    else:
        step = max(1, n // 16)

    ticks = list(range(0, n, step))
    labels = [str(df.index[i])[5:10] if i < n else "" for i in ticks]
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=8, rotation=30)
    ax.set_xlim(-1, n)
    ax.tick_params(axis="x", colors=CLR_TEXT)


def _draw_interpretation_box(ax: plt.Axes, lines: list[str], title: str,
                              position: str = "right") -> None:
    """在面板内绘制技术解读文本框。

    position: "right" → 右上角；"left" → 左上角
    """
    if not lines:
        return
    text_lines = [f"[{title}]"] + [f"  {l}" for l in lines[:8]]
    text = "\n".join(text_lines)

    if position == "right":
        xy = (0.99, 0.96)
        ha = "right"
    else:
        xy = (0.01, 0.96)
        ha = "left"

    ax.text(
        xy[0], xy[1], text, transform=ax.transAxes,
        fontsize=7.5, color=CLR_TEXT_BRIGHT, ha=ha, va="top",
        bbox=dict(
            boxstyle="round,pad=0.6", facecolor="#1a1a2ecc",
            edgecolor=CLR_GRID, linewidth=1.0, alpha=0.92,
        ),
    )


def _stamp_footer(fig: plt.Figure, code: str, df: pd.DataFrame) -> None:
    as_of = str(df.index[-1])[:10]
    footer = f"数据截止 {as_of}  ·  Z-Plan 选股系统  ·  {code}  ·  仅供参考，不构成投资建议"
    fig.text(0.5, 0.008, footer, transform=fig.transFigure,
             fontsize=7, color=CLR_GRID, ha="center", va="bottom")


def _stock_name(code: str) -> str | None:
    try:
        from sqlalchemy import text
        from zplan_shared.models import SessionLocal
        with SessionLocal() as s:
            return s.execute(
                text("SELECT name FROM stock_list WHERE ts_code = :c"), {"c": code}
            ).scalar()
    except Exception:
        return None
