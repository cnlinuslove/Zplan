#!/usr/bin/env python3
"""大盘预测验证脚本：批量验证所有未验证预测 + 多周期评估。

用法:
    cd zplan-资讯 && .venv/bin/python scripts/forecast_verify.py
    cd zplan-资讯 && .venv/bin/python scripts/forecast_verify.py --dry-run
    cd zplan-资讯 && .venv/bin/python scripts/forecast_verify.py --no-push
    cd zplan-资讯 && .venv/bin/python scripts/forecast_verify.py --threshold 0.5

调度: launchd 17:45 自动触发，或集成到 run_full_pipeline.sh。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NEWS_ROOT = Path(os.environ.get("ZPLAN_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(NEWS_ROOT))

from sqlalchemy import desc, select

from zplan_shared.config import FORECAST_VERIFY_THRESHOLD_PCT, ZPLAN_ROOT
from zplan_shared.market import get_index_panel, latest_index_trade_date
from zplan_shared.models import MarketForecast, SessionLocal, init_db

BEIJING_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)

# 七指数代码
_A_INDEX_CODES = ["000001", "399001", "399006", "000688", "000300", "000905", "000852"]


def _resolve_direction(pct_chg: float, threshold: float) -> str:
    """将涨跌幅映射为方向标签。"""
    if pct_chg > threshold:
        return "bullish"
    elif pct_chg < -threshold:
        return "bearish"
    return "range-bound"


def _resolve_direction_cn(pct_chg: float, threshold: float) -> str:
    """中文方向标签（指数级）。"""
    if pct_chg > threshold:
        return "偏多"
    elif pct_chg < -threshold:
        return "偏空"
    return "震荡"


def _verify_single(
    session,
    mf: MarketForecast,
    today: date,
    threshold: float,
) -> dict[str, Any]:
    """验证单条预测记录，返回验证结果字典。"""
    # 获取今日指数实际走势
    panel = get_index_panel(as_of=today)
    if panel.empty:
        return {"forecast_date": str(mf.as_of_date), "error": "今日指数数据未入库"}

    # 解析预测 JSON
    try:
        forecast = json.loads(mf.forecast_json) if isinstance(mf.forecast_json, str) else mf.forecast_json
    except (json.JSONDecodeError, TypeError):
        return {"forecast_date": str(mf.as_of_date), "error": "预测 JSON 解析失败"}

    # 对照指数预测 vs 实际
    index_forecasts = forecast.get("index_forecasts") or []
    index_results = []
    correct_count = 0
    total = 0

    for ix in index_forecasts:
        code = ix.get("code", "")
        predicted = ix.get("direction", "")
        row = panel[panel["index_code"] == code]
        if row.empty:
            continue
        actual_pct = float(row.iloc[0].get("pct_chg", 0) or 0)
        actual_dir = _resolve_direction_cn(actual_pct, threshold)
        matched = (
            (predicted == "偏多" and actual_dir == "偏多")
            or (predicted == "偏空" and actual_dir == "偏空")
            or (predicted == "震荡" and actual_dir == "震荡")
        )
        if matched:
            correct_count += 1
        total += 1
        index_results.append({
            "code": code,
            "name": ix.get("name", code),
            "predicted": predicted,
            "actual": actual_dir,
            "actual_pct": round(actual_pct, 2),
            "correct": matched,
        })

    # 总体方向对照 — 用上证指数代表大盘实际方向
    market_dir = forecast.get("market_direction", {})
    predicted_dir = market_dir.get("direction", "?")
    sh_row = panel[panel["index_code"] == "000001"]
    actual_market_pct = float(sh_row.iloc[0].get("pct_chg", 0) or 0) if not sh_row.empty else 0
    actual_market_dir = _resolve_direction(actual_market_pct, threshold)
    direction_matched = predicted_dir == actual_market_dir

    # 回填验证结果
    mf.verified_at = datetime.now(timezone.utc)
    mf.actual_direction = actual_market_dir
    mf.actual_pct_chg = round(actual_market_pct, 2)
    mf.direction_correct = direction_matched
    session.commit()

    return {
        "forecast_date": str(mf.as_of_date),
        "verify_date": str(today),
        "predicted_direction": predicted_dir,
        "actual_direction": actual_market_dir,
        "actual_pct_chg": round(actual_market_pct, 2),
        "direction_correct": direction_matched,
        "index_results": index_results,
        "index_correct": f"{correct_count}/{total}" if total else "N/A",
        "index_correct_count": correct_count,
        "index_total": total,
    }


def verify_all_outstanding(
    session,
    threshold_pct: float | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """验证所有未验证的预测记录（as_of_date < latest_trade_date）。

    返回: {"verified": N, "skipped": M, "results": [...], "errors": [...]}
    """
    if threshold_pct is None:
        threshold_pct = FORECAST_VERIFY_THRESHOLD_PCT

    today = latest_index_trade_date()
    if not today:
        return {"verified": 0, "skipped": 0, "results": [], "errors": ["无法确定最新交易日"]}

    # 找到所有未验证的预测（as_of_date < today）
    mfs = session.execute(
        select(MarketForecast)
        .where(
            MarketForecast.as_of_date < today,
            MarketForecast.verified_at.is_(None),
        )
        .order_by(MarketForecast.as_of_date.asc())  # 从旧到新
    ).scalars().all()

    if not mfs:
        return {"verified": 0, "skipped": 0, "results": [], "today": str(today), "message": "无待验证预测"}

    results = []
    errors = []
    for mf in mfs:
        try:
            if dry_run:
                results.append({
                    "forecast_date": str(mf.as_of_date),
                    "predicted": mf.market_direction,
                    "confidence": mf.direction_confidence,
                    "status": "dry_run",
                })
            else:
                r = _verify_single(session, mf, today, threshold_pct)
                results.append(r)
        except Exception as exc:
            errors.append({
                "forecast_date": str(mf.as_of_date),
                "error": str(exc),
            })
            logger.exception(f"验证预测 {mf.as_of_date} 失败")

    return {
        "verified": len(results) if not dry_run else 0,
        "dry_run_count": len(results) if dry_run else 0,
        "skipped": 0,
        "results": results,
        "errors": errors,
        "today": str(today),
        "threshold_pct": threshold_pct,
    }


def _format_verification_markdown(result: dict[str, Any]) -> str:
    """将验证结果格式化为企微推送用 markdown。"""
    if result.get("error"):
        return f"> ⚠️ 预测验证: {result['error']}"

    direction_map = {"bullish": "🟢看涨", "bearish": "🔴看跌", "range-bound": "🟡震荡"}
    matched = result.get("direction_correct", False)
    predicted = result.get("predicted_direction", "?")
    actual = result.get("actual_direction", "?")

    lines = [
        "### 🎯 大盘预测验证",
        "",
        f"> 预测日期: **{result['forecast_date']}** · 验证日期: **{result['verify_date']}**",
        "",
        f"| 预测方向 | 实际方向 | 结果 |",
        f"|----------|----------|------|",
        f"| {direction_map.get(predicted, predicted)} | "
        f"{direction_map.get(actual, actual)} ({result.get('actual_pct_chg', 0):+.2f}%) | "
        f"{'✅ 正确' if matched else '❌ 偏差'} |",
    ]

    # 各指数对照
    index_results = result.get("index_results") or []
    if index_results:
        lines.append("")
        lines.append("**各指数对照：**")
        lines.append("| 指数 | 预测 | 实际 |")
        lines.append("|------|------|------|")
        for ix in index_results:
            icon = "✅" if ix.get("correct") else "❌"
            lines.append(
                f"| {ix['name']} | {ix['predicted']} | "
                f"{icon} {ix['actual']} ({ix.get('actual_pct', 0):+.2f}%) |"
            )

    correct_str = result.get("index_correct", "N/A")
    lines.append(f"\n> 指数方向准确率: **{correct_str}**")
    return "\n".join(lines)


def main():
    dry_run = "--dry-run" in sys.argv
    no_push = "--no-push" in sys.argv
    threshold = FORECAST_VERIFY_THRESHOLD_PCT

    # 解析 --threshold
    for i, arg in enumerate(sys.argv):
        if arg == "--threshold" and i + 1 < len(sys.argv):
            threshold = float(sys.argv[i + 1])
            break

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    init_db()

    with SessionLocal() as session:
        outcome = verify_all_outstanding(session, threshold_pct=threshold, dry_run=dry_run)

    ts = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    if dry_run:
        print(f"[{ts}] 🔍 DRY RUN — {outcome.get('dry_run_count', 0)} 条待验证预测:")
        for r in outcome.get("results", []):
            print(f"  {r['forecast_date']}: 预测 {r.get('predicted', '?')}, 置信度 {r.get('confidence', '?')}%")
        return

    verified = outcome.get("verified", 0)
    errors = outcome.get("errors", [])
    print(f"[{ts}] 已验证 {verified} 条预测 (阈值 ±{outcome.get('threshold_pct', threshold)}%)")
    if errors:
        print(f"[{ts}] ⚠️ {len(errors)} 条验证失败: {errors}")

    # 打印摘要
    for r in outcome.get("results", []):
        matched = r.get("direction_correct", False)
        print(f"  {r['forecast_date']}: {r['predicted_direction']} → {r['actual_direction']} ({r.get('actual_pct_chg', 0):+.2f}%) {'✅' if matched else '❌'}")

    # 企微推送
    if not no_push and verified > 0:
        try:
            from wechat_push import push_wechat_markdown
            for r in outcome.get("results", []):
                md = _format_verification_markdown(r)
                if md:
                    push_wechat_markdown(md)
        except Exception as exc:
            logger.warning(f"企微推送失败: {exc}")


if __name__ == "__main__":
    main()
