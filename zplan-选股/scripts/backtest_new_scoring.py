"""用新版打分重新评估 2026-05-21 的全市场，对比旧版 run 8 的前向收益。

纯规则分验证，不调用 LLM。验证新版打分是否有预测力改善。

用法：
  cd zplan-选股 && .venv/bin/python scripts/backtest_new_scoring.py
"""

from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "zplan-共享", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_new")

import pandas as pd
from zplan_shared.market import get_panel, get_bars, latest_trade_date
from zplan_shared.features import scan_universe_features, enrich_bars, latest_features
from zplan_shared.models import SessionLocal, StockList, init_db
from pick_agent.scoring import quick_technical_score, apply_momentum_cap, momentum_penalty

AS_OF = "2026-05-21"
FWD_END = "2026-06-05"
TOP_N = 219  # match new pipeline output size
OLD_RUN_ID = 8


def main():
    # 1. 加载 as_of 截面
    panel = get_panel(AS_OF, fields=["close", "pct_chg", "turnover_rate", "volume"])
    if panel.empty:
        logger.error("截面为空: %s", AS_OF)
        return 1
    logger.info("截面 %s: %s 只股票", AS_OF, len(panel))

    # 2. 加载元数据
    init_db()
    from sqlalchemy import text
    with SessionLocal() as session:
        rows = session.execute(
            text("SELECT ts_code, name, industry FROM stock_list WHERE market = 'a'")
        ).all()
    meta = pd.DataFrame(rows, columns=["ts_code", "name", "industry"])
    df = panel.merge(meta, on="ts_code", how="left")

    # 3. 预过滤
    df = df.dropna(subset=["close"])
    df = df[~df["name"].fillna("").str.contains("ST", case=False, na=False)]
    codes = df["ts_code"].tolist()
    logger.info("预过滤后: %s 只", len(codes))

    # 4. 批量计算特征
    from zplan_shared.market import get_history_window
    history = get_history_window(end=AS_OF, calendar_days=150, ts_codes=codes)
    feat_df = scan_universe_features(history, min_bars=60)
    logger.info("特征计算完成: %s 只", len(feat_df))

    # 5. 过滤 max_ret_20d > 5% + 合并元数据
    feat_df = feat_df[feat_df["ts_code"].isin(df["ts_code"])]
    # 合并 name/industry（scan_universe_features 不保留这些字段）
    meta_lookup = dict(zip(df["ts_code"], zip(df["name"], df["industry"], df["close"])))
    before = len(feat_df)
    r20 = pd.to_numeric(feat_df["ret_20d"], errors="coerce")
    feat_df = feat_df[r20.isna() | (r20 <= 5.0)]
    logger.info("max_ret_20d>5 过滤: %s → %s 只", before, len(feat_df))

    # 6. 用新版打分
    scores = []
    for _, row in feat_df.iterrows():
        fdict = row.to_dict()
        ts_code = str(fdict.get("ts_code", ""))
        name, industry, close_val = meta_lookup.get(ts_code, ("", "", 0.0))
        raw = quick_technical_score(fdict)
        final = apply_momentum_cap(raw, fdict.get("ret_20d"), max_ret_20d=5.0,
                                    vol_ratio20=fdict.get("vol_ratio20"))
        scores.append({
            "ts_code": ts_code,
            "name": str(name),
            "industry": str(industry),
            "close": float(close_val) if close_val else float(fdict.get("close", 0)),
            "ret_20d": float(fdict.get("ret_20d", 0)) if fdict.get("ret_20d") == fdict.get("ret_20d") else None,
            "score": float(final),
        })

    new_picks = sorted(scores, key=lambda x: x["score"], reverse=True)

    # 行业分散：每行业最多 10 只（纯后过滤，不改分）
    from collections import Counter
    seen_ind: dict[str, int] = {}
    diversified = []
    for p in new_picks:
        ind = p["industry"]
        if seen_ind.get(ind, 0) >= 10:
            continue
        seen_ind[ind] = seen_ind.get(ind, 0) + 1
        diversified.append(p)

    new_picks = diversified[:TOP_N]
    logger.info("新打分 Top%s: 均分 %.1f, 范围 %.1f-%.1f",
                 TOP_N,
                 sum(p["score"] for p in new_picks) / len(new_picks),
                 new_picks[-1]["score"], new_picks[0]["score"])

    # 7. 获取前向收益
    fwd_close = {}
    fwd_panel = get_panel(FWD_END, fields=["close"])
    for _, row in fwd_panel.iterrows():
        fwd_close[str(row["ts_code"])] = float(row["close"])

    # 8. 计算前向收益
    for p in new_picks:
        fc = fwd_close.get(p["ts_code"])
        if fc and p["close"]:
            p["fwd_return"] = round((fc - p["close"]) / p["close"] * 100, 2)
        else:
            p["fwd_return"] = None

    # 9. 加载旧版 run 8 做对比
    old_entries = []
    from sqlalchemy import text
    with SessionLocal() as session:
        result = session.execute(
            text("SELECT ts_code, name, rule_composite_score, llm_composite_score, close_price "
                 f"FROM pick_entries WHERE run_id = {OLD_RUN_ID} ORDER BY rank_in_run")
        ).all()
    for r in result:
        fc = fwd_close.get(r[0])
        fwd = round((fc - r[4]) / r[4] * 100, 2) if fc and r[4] else None
        old_entries.append({
            "ts_code": r[0], "name": r[1],
            "rule_score": r[2], "llm_score": r[3],
            "close": r[4], "fwd_return": fwd,
        })

    # 10. 对比分析
    new_valid = [p for p in new_picks if p["fwd_return"] is not None]
    old_valid = [e for e in old_entries if e["fwd_return"] is not None]

    new_avg = sum(p["fwd_return"] for p in new_valid) / len(new_valid) if new_valid else 0
    old_avg = sum(e["fwd_return"] for e in old_valid) / len(old_valid) if old_valid else 0

    new_win = sum(1 for p in new_valid if p["fwd_return"] > 0) / len(new_valid) * 100 if new_valid else 0
    old_win = sum(1 for e in old_valid if e["fwd_return"] > 0) / len(old_valid) * 100 if old_valid else 0

    # Decile analysis for new picks
    new_valid_sorted = sorted(new_valid, key=lambda x: x["score"], reverse=True)
    n = len(new_valid_sorted)
    deciles = []
    for i in range(5):
        start = i * n // 5
        end = (i + 1) * n // 5
        chunk = new_valid_sorted[start:end]
        deciles.append({
            "label": f"Q{i+1}",
            "n": len(chunk),
            "avg_score": sum(p["score"] for p in chunk) / len(chunk),
            "avg_ret": sum(p["fwd_return"] for p in chunk) / len(chunk),
            "win_rate": sum(1 for p in chunk if p["fwd_return"] > 0) / len(chunk) * 100,
        })

    # Buy price reachability: new formula close*0.98 vs actual lows
    from zplan_shared.market import get_bars as gb
    buy_reached = 0
    total_with_low = 0
    for p in new_picks[:50]:
        bars = gb(p["ts_code"])
        if bars.empty:
            continue
        after = bars[pd.to_datetime(bars.index).date > pd.Timestamp(AS_OF).date()]
        if not after.empty:
            total_with_low += 1
            buy_price = p["close"] * 0.98
            if float(after["low"].min()) <= buy_price:
                buy_reached += 1

    # Industry diversity
    from collections import Counter
    ind_cnt = Counter(p["industry"] for p in new_picks)
    top_inds = ind_cnt.most_common(5)

    # Print results
    print()
    print("=" * 70)
    print(f"  新版打分历史回测: {AS_OF} → {FWD_END} (11 个交易日)")
    print("=" * 70)
    print()
    print(f"  全市场预筛后: {len(scores)} 只")
    print(f"  max_ret_20d>5% 剔除: {before - len(feat_df)} 只")
    print(f"  Top {TOP_N} 入选（行业分散前）")
    print()
    print(f"  {'指标':<25} {'旧版 (Run 8)':<20} {'新版 (改进后)':<20}")
    print(f"  {'-'*25} {'-'*20} {'-'*20}")
    print(f"  {'入选股票数':<25} {len(old_valid):<20} {len(new_valid):<20}")
    print(f"  {'平均前向收益':<25} {old_avg:>+.2f}%{'':16} {new_avg:>+.2f}%")
    print(f"  {'胜率 (>0%)':<25} {old_win:.1f}%{'':16} {new_win:.1f}%")
    print(f"  {'买价触及率':<25} {'—':<20} {buy_reached}/{total_with_low} ({buy_reached/total_with_low*100:.0f}%)" if total_with_low else "")
    print()
    print(f"  ── 新版五分位单调性 ──")
    print(f"  {'分位':<8} {'数量':<8} {'均分':<8} {'均收益':<10} {'胜率':<8}")
    for d in deciles:
        mono = "✓" if deciles.index(d) == 0 or d["avg_ret"] >= deciles[deciles.index(d)-1]["avg_ret"] else ""
        print(f"  {d['label']:<8} {d['n']:<8} {d['avg_score']:<8.1f} {d['avg_ret']:>+7.2f}%  {d['win_rate']:.1f}%{'':3} {mono}")
    print()
    print(f"  ── 行业分布 (Top5) ──")
    for ind, cnt in top_inds:
        print(f"  {ind:<20} {cnt} 只")
    print()
    print(f"  ── 新版 Top10 选股 ──")
    print(f"  {'Rank':<5} {'名称':<10} {'行业':<12} {'得分':<8} {'ret20':<8} {'前向收益':<10}")
    for i, p in enumerate(new_valid_sorted[:10], 1):
        ret20 = f"{p['ret_20d']:+.1f}%" if p['ret_20d'] else "—"
        print(f"  {i:<5} {p['name']:<10} {p['industry']:<12} {p['score']:<8.1f} {ret20:<8} {p['fwd_return']:>+7.2f}%")
    print()
    print(f"  ── 新版 Bottom10 选股 ──")
    for i, p in enumerate(new_valid_sorted[-10:], len(new_valid_sorted) - 9):
        ret20 = f"{p['ret_20d']:+.1f}%" if p['ret_20d'] else "—"
        print(f"  {i:<5} {p['name']:<10} {p['industry']:<12} {p['score']:<8.1f} {ret20:<8} {p['fwd_return']:>+7.2f}%")

    return 0



if __name__ == "__main__":
    raise SystemExit(main())
