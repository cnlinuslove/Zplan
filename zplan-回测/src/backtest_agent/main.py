from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.market import get_bars, resolve_ts_code
from zplan_shared.models import init_db
from zplan_shared.pick_predictions import list_outcomes, validate_entries

from backtest_agent.ab_backtest import (
    _fridays_in_range,
    _load_variant_file,
    _parse_variants_arg,
    run_ab_backtest,
)
from backtest_agent.calibration import build_calibration_report, print_calibration
from backtest_agent.data_audit import audit_market_data, run_catchup_if_needed, score_deviation_report
from backtest_agent.iterate import (
    print_iteration_diff,
    print_iteration_history,
    run_iteration_cycle,
)
from backtest_agent.llm_review import default_review_filename, print_llm_eval
from backtest_agent.sim_trade import SimEngine, SimStrategy, format_sim_report
from backtest_agent.paper_trade import (
    morning as paper_morning,
    close as paper_close,
    status as paper_status,
    format_morning_report,
    format_close_report,
    format_status_report,
)

logger = logging.getLogger(__name__)


def run_backtest_agent(*, ts_code: str = "000001") -> dict:
    """烟测：确认行情可读。"""
    init_db()
    code = resolve_ts_code(ts_code)
    df = get_bars(code)
    if df.empty:
        return {
            "ok": True,
            "agent": "backtest",
            "zplan_root": str(ZPLAN_ROOT),
            "ts_code": code,
            "bars": 0,
            "from": None,
            "to": None,
        }
    return {
        "ok": True,
        "agent": "backtest",
        "zplan_root": str(ZPLAN_ROOT),
        "ts_code": code,
        "bars": len(df),
        "from": str(df.index.min()),
        "to": str(df.index.max()),
    }


def run_validate(
    *,
    run_id: int | None = None,
    entry_id: int | None = None,
    horizons: list[int] | None = None,
    limit: int = 500,
) -> dict:
    """将选股预测价与后续实际行情比对并落库。"""
    stats = validate_entries(
        run_id=run_id,
        entry_id=entry_id,
        horizons=horizons,
        limit=limit,
        backfill_prices=True,
    )
    stats["ok"] = True
    stats["agent"] = "backtest_validate"
    return stats


def _print_calibration_report(result: dict) -> None:
    """打印置信度校准报告（人类可读）。"""
    print("=" * 60)
    print(f"置信度校准报告 (horizon={result.get('horizon_days')}d)")
    print(f"总评估: {result.get('total')} 次 | 正确: {result.get('correct')} | 准确率: {result.get('accuracy_pct')}%")
    print("-" * 60)
    bins = result.get("confidence_bins") or []
    for b in bins:
        bar = "█" * int((b.get("accuracy_pct") or 0) / 5)
        print(f"  {b['bin']:>8s} | 预期 ~{b['bin_expected']:.0f}% | 实际 {b.get('accuracy_pct', 'N/A')}% ({b['accurate']}/{b['count']}) {bar}")
    print("-" * 60)
    by_dir = result.get("by_direction") or {}
    if by_dir:
        print("方向对称性:")
        for d in ["bullish", "bearish", "range-bound"]:
            c = by_dir.get(f"{d}_count", 0)
            acc = by_dir.get(f"{d}_correct", 0)
            pct = round(acc / c * 100, 1) if c > 0 else "N/A"
            print(f"  {d}: {c}次预测, {acc}次正确 ({pct}%)")
    per_idx = result.get("per_index_accuracy") or {}
    if per_idx:
        print("-" * 60)
        print("各指数准确率:")
        for code, v in sorted(per_idx.items()):
            print(f"  {code}: {v['correct']}/{v['total']} ({v.get('pct', 'N/A')}%)")
    print("=" * 60)


def _print_trend(trend: list[dict]) -> None:
    """打印滚动准确率趋势。"""
    if not trend:
        print("无趋势数据")
        return
    print(f"{'日期':<12s} {'正确':^6s} {'5期滚动准确率':>15s}")
    print("-" * 40)
    for t in trend[-20:]:
        mark = "✅" if t["correct"] else "❌"
        roll = f"{t['rolling_accuracy_5']}%" if t.get("rolling_accuracy_5") is not None else "N/A"
        print(f"{t['date']:<12s} {mark:^6s} {roll:>15s}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan 回测 Agent")
    sub = parser.add_subparsers(dest="command")

    smoke = sub.add_parser("smoke", help="行情烟测（默认）")
    smoke.add_argument("--code", default="000001", help="股票代码")

    val = sub.add_parser("validate", help="比对选股预测买入价 vs 实际走势")
    val.add_argument("--run-id", type=int, help="仅验证某次 pick run")
    val.add_argument("--entry-id", type=int, help="仅验证单条 entry")
    val.add_argument(
        "--horizon",
        type=int,
        action="append",
        dest="horizons",
        help="向前验证的交易日数，可多次指定（默认 5/10/20）",
    )
    val.add_argument("--limit", type=int, default=500, help="最多处理 entry 条数")

    cal = sub.add_parser("calibrate", help="输出预测偏差聚合与优化建议")
    cal.add_argument("--horizon", type=int, default=10)
    cal.add_argument("--json", action="store_true", dest="as_json")

    lst = sub.add_parser("list", help="列出最近验证结果")
    lst.add_argument("--limit", type=int, default=30)
    lst.add_argument("--horizon", type=int, default=None)
    lst.add_argument("--status", default=None, choices=["complete", "partial", "pending"])

    llm = sub.add_parser("llm-eval", help="LLM Top 池失败诊断与优化建议")
    llm.add_argument("--run-id", type=int, default=None, help="默认最近一次 llm_top300")
    llm.add_argument("--top", type=int, default=10, help="评估排名前 N（默认 10）")
    llm.add_argument("--horizon", type=int, default=5, help="向前验证交易日数")
    llm.add_argument("--json", action="store_true", dest="as_json")
    llm.add_argument(
        "-o",
        "--output",
        default=None,
        help="写入 Markdown 报告（默认目录 ZPLAN_ROOT/backtest_review/）",
    )

    ab = sub.add_parser("ab-backtest", help="A/B 历史回放：多变体 × 历史日期 → 统计对比")
    ab.add_argument("--variants", type=str, default=None,
                    help="逗号分隔变体名（无 overrides 模式），如 baseline,strict,value")
    ab.add_argument("--variant-file", type=str, default=None,
                    help="变体定义 YAML/JSON 文件路径")
    ab.add_argument("--as-of", type=str, default=None,
                    help="单日模式：指定交易日 YYYY-MM-DD")
    ab.add_argument("--from", type=str, default=None, dest="from_date",
                    help="区间模式起始日 YYYY-MM-DD")
    ab.add_argument("--to", type=str, default=None, dest="to_date",
                    help="区间模式结束日 YYYY-MM-DD")
    ab.add_argument("--top", type=int, default=100,
                    help="每变体选股数（默认 100）")
    ab.add_argument("--horizon", type=int, default=5,
                    help="验证期交易日（默认 5）")
    ab.add_argument("--batch-size", type=int, default=10,
                    help="LLM 批大小（默认 10）")

    chk = sub.add_parser("check-data", help="检查行情截面完整性")
    chk.add_argument("--json", action="store_true", dest="as_json")

    catch = sub.add_parser("catchup-data", help="补齐缺最新截面的股票（调用股价 ETL）")
    catch.add_argument("--limit", type=int, default=None)
    catch.add_argument("--workers", type=int, default=8)

    audit = sub.add_parser("audit", help="行情 + 上次打分偏差综合审计")
    audit.add_argument("--run-id", type=int, default=None)
    audit.add_argument("--top", type=int, default=10)
    audit.add_argument("--horizon", type=int, default=5)
    audit.add_argument("--json", action="store_true", dest="as_json")
    audit.add_argument("-o", "--output", default=None)

    # ── 模拟交易 ──
    sim = sub.add_parser("sim-trade", help="历史模拟交易：用真实K线回放策略，输出绩效报告")
    sim.add_argument("--run-id", type=int, default=None, help="指定 pick_run（默认最近一次）")
    sim.add_argument("--from", type=str, default=None, dest="from_date",
                    help="历史区间起始日 YYYY-MM-DD（批量回放）")
    sim.add_argument("--to", type=str, default=None, dest="to_date",
                    help="历史区间结束日 YYYY-MM-DD")
    sim.add_argument("--capital", type=float, default=100_000, help="初始资金（默认 100,000）")
    sim.add_argument("--top", type=int, default=5, help="每期最多持仓数（默认 5）")
    sim.add_argument("--holding-days", type=int, default=5, help="持仓天数上限（默认 5）")
    sim.add_argument("--stop-loss-pct", type=float, default=-5.0,
                    help="止损线 %（默认 -5，即下跌 5% 止损；传正值会被反转）")
    sim.add_argument("--take-profit-pct", type=float, default=None,
                    help="止盈线 %（可选，如 15）")
    sim.add_argument("--sizing", default="equal",
                    choices=["equal", "score_weighted", "risk_parity"],
                    help="资金分配方式（默认 equal）")
    sim.add_argument("--slippage", type=float, default=0.0,
                    help="买入滑点 %（默认 0，如 0.1 表示买价上浮 0.1%%）")
    sim.add_argument("--entry-rule", default="next_open",
                    choices=["next_open", "limit"],
                    help="买入规则：next_open=次日开盘价成交；limit=按 predicted_buy 限价")
    sim.add_argument("--exclude-tags", default=None,
                    help="逗号分隔的失败标签，排除含这些标签的 pick（如 momentum_chase,buy_unreachable）")
    sim.add_argument("--min-llm-score", type=float, default=None,
                    help="最低 LLM 分数过滤")
    sim.add_argument("--min-rule-score", type=float, default=None,
                    help="最低规则分数过滤")
    sim.add_argument("--json", action="store_true", dest="as_json",
                    help="JSON 输出（供程序消费）")
    sim.add_argument("-o", "--output", default=None,
                    help="写入 Markdown 报告文件")
    sim.add_argument("--root-cause", action="store_true",
                    help="附加根因分析（亏损交易逐只归因）")
    sim.add_argument("--rule-version", default=None,
                    help="按策略版本过滤（如 pick-2026-06-anti-chase-v2）")
    sim.add_argument("--variant", default=None,
                    help="按变体过滤（如 baseline, deep_value, llm_primary）")
    sim.add_argument("--llm-only", action="store_true",
                    help="仅模拟有 LLM 评分的选股（排除纯规则扫描）")

    # ── 每日纸质交易 ──
    pt = sub.add_parser("paper-trade", help="每日纸质交易（盘前计划 + 盘后结算）")
    pt_sub = pt.add_subparsers(dest="pt_cmd")

    pt_m = pt_sub.add_parser("morning", help="盘前：加载最新选股 → 生成模拟买单")
    pt_m.add_argument("--top", type=int, default=5, help="最多持仓数（默认 5）")
    pt_m.add_argument("--capital", type=float, default=100_000, help="初始资金（仅首次）")
    pt_m.add_argument("--run-id", type=int, default=None, help="指定 pick_run（默认最新）")
    pt_m.add_argument("--dry-run", action="store_true", help="仅预览不保存")

    pt_c = pt_sub.add_parser("close", help="盘后：执行买单 + 检查出场 + 结算")
    pt_c.add_argument("--dry-run", action="store_true", help="仅预览不保存")

    pt_s = pt_sub.add_parser("status", help="当前账户状态")
    pt_s.add_argument("--json", action="store_true", dest="as_json")

    pt.set_defaults(pt_cmd="status")

    it = sub.add_parser("iterate", help="选股↔回测迭代闭环")
    it_sub = it.add_subparsers(dest="iterate_cmd")

    it_v = it_sub.add_parser("verify", help="行情检查 + 审计 + 落库 + 对比（每日）")
    it_v.add_argument("--run-id", type=int, default=None)
    it_v.add_argument("--top", type=int, default=10)
    it_v.add_argument("--horizon", type=int, default=5)
    it_v.add_argument("--no-catchup", action="store_true")
    it_v.add_argument("--note", default="")

    it_f = it_sub.add_parser("full", help="补齐行情 + init-rule + llm-top + 审计（每周）")
    it_f.add_argument("--top", type=int, default=10)
    it_f.add_argument("--horizon", type=int, default=5)
    it_f.add_argument("--note", default="")

    it_h = it_sub.add_parser("history", help="迭代历史指标")
    it_h.add_argument("--limit", type=int, default=15)

    it_sub.add_parser("diff", help="最近两轮对比")

    it.set_defaults(iterate_cmd="verify")

    # 大盘预测回测
    fe = sub.add_parser("forecast-eval", help="大盘预测回测评估")
    fe_sub = fe.add_subparsers(dest="fe_cmd")

    fe_v = fe_sub.add_parser("verify", help="验证所有未验证预测 + 多周期评估")
    fe_v.add_argument("--threshold", type=float, default=None, help="涨跌阈值（默认使用配置值）")
    fe_v.add_argument("--horizons", default="1,3,5,20", help="多周期 horizon 列表")
    fe_v.add_argument("--dry-run", action="store_true")

    fe_c = fe_sub.add_parser("calibrate", help="置信度校准报告")
    fe_c.add_argument("--horizon", type=int, default=1, help="horizon 天数")
    fe_c.add_argument("--json", action="store_true", dest="as_json")

    fe_h = fe_sub.add_parser("history", help="预测准确率趋势")
    fe_h.add_argument("--days", type=int, default=60)
    fe_h.add_argument("--json", action="store_true", dest="as_json")

    fe.set_defaults(fe_cmd="verify")

    # ── 出场策略回测 ──
    ec = sub.add_parser("exit-compare", help="出场策略对比：同一批 pick 跑多套方案")
    ec.add_argument("--run-id", type=int, required=True, help="pick_runs.id")
    ec.add_argument("--plans", type=str, default="static,trailing_8pct,atr_trail_2x",
                    help="逗号分隔的方案 key（默认 static,trailing_8pct,atr_trail_2x）")
    ec.add_argument("--top", type=int, default=10, help="取前 N 只票（默认 10）")
    ec.add_argument("--json", action="store_true", dest="as_json")

    eo = sub.add_parser("exit-optimize", help="出场参数网格搜索：找最优参数组合")
    eo.add_argument("--run-id", type=int, required=True, help="pick_runs.id")
    eo.add_argument("--top", type=int, default=10, help="取前 N 只票（默认 10）")
    eo.add_argument("--json", action="store_true", dest="as_json")

    # 兼容旧用法：main.py --code
    parser.add_argument("--code", default=None, help="(兼容) 等同 smoke --code")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cmd = args.command
    if cmd is None and args.code is not None:
        cmd = "smoke"

    if cmd == "validate":
        result = run_validate(
            run_id=args.run_id,
            entry_id=args.entry_id,
            horizons=args.horizons,
            limit=args.limit,
        )
        logger.info("验证完成: %s", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "calibrate":
        print_calibration(horizon_days=args.horizon, as_json=args.as_json)
    elif cmd == "list":
        rows = list_outcomes(limit=args.limit, status=args.status, horizon_days=args.horizon)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    elif cmd == "llm-eval":
        from backtest_agent.llm_review import default_review_filename

        out = args.output
        if out is True or (out is None and not args.as_json):
            out = default_review_filename(args.run_id)
        print_llm_eval(
            run_id=args.run_id,
            top_n=args.top,
            horizon_days=args.horizon,
            as_json=args.as_json,
            output=None if args.as_json else out,
        )
    elif cmd == "ab-backtest":
        if not args.variants and not args.variant_file:
            print("请指定 --variants baseline,strict 或 --variant-file variants.yaml", file=sys.stderr)
            raise SystemExit(2)

        if args.variant_file:
            variants = _load_variant_file(args.variant_file)
        else:
            variants = _parse_variants_arg(args.variants)

        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        from_d = date.fromisoformat(args.from_date) if args.from_date else None
        to_d = date.fromisoformat(args.to_date) if args.to_date else None

        import sys
        result = run_ab_backtest(
            variants=variants,
            as_of=as_of,
            from_date=from_d,
            to_date=to_d,
            top_n=args.top,
            horizon_days=args.horizon,
            batch_size=args.batch_size,
        )
        if result.get("markdown"):
            print(result["markdown"])
        if result.get("report_path"):
            print(f"\n---\n完整报告: {result['report_path']}")
        if not result.get("ok"):
            raise SystemExit(1)

    elif cmd == "sim-trade":
        import sys as _sys

        # 参数解析
        stop_loss_pct = args.stop_loss_pct
        if stop_loss_pct is not None and stop_loss_pct > 0:
            stop_loss_pct = -stop_loss_pct  # 用户习惯写正数

        exclude_tags = None
        if args.exclude_tags:
            exclude_tags = [t.strip() for t in args.exclude_tags.split(",") if t.strip()]

        strategy = SimStrategy(
            top_n=args.top,
            holding_days=args.holding_days,
            stop_loss_pct=stop_loss_pct / 100.0 if stop_loss_pct is not None else -0.05,
            take_profit_pct=args.take_profit_pct / 100.0 if args.take_profit_pct else None,
            sizing_mode=args.sizing,
            entry_rule=args.entry_rule,
            exclude_tags=exclude_tags,
            min_llm_score=args.min_llm_score,
            min_rule_score=args.min_rule_score,
        )

        engine = SimEngine(
            strategy=strategy,
            initial_capital=args.capital,
            max_positions=args.top,
        )

        from_d = date.fromisoformat(args.from_date) if args.from_date else None
        to_d = date.fromisoformat(args.to_date) if args.to_date else None

        if args.run_id:
            result = engine.run_single(args.run_id)
        elif from_d or to_d:
            result = engine.run_batch(
                from_date=from_d, to_date=to_d,
                rule_version=args.rule_version,
                variant_label=args.variant,
                llm_only=args.llm_only,
            )
        else:
            # 默认：最近一次 llm run
            from zplan_shared.models import PickRun as _PR, SessionLocal as _SL
            init_db()
            with _SL() as _s:
                _run = _s.execute(
                    select(_PR)
                    .where(_PR.run_kind.in_(["llm_top300", "scan"]), _PR.llm_enabled.is_(True))
                    .order_by(desc(_PR.id))
                    .limit(1)
                ).scalar_one_or_none()
            if _run:
                result = engine.run_single(_run.id)
            else:
                result = {"ok": False, "message": "无 LLM pick run"}

        if args.as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            md = format_sim_report(result, with_root_cause=args.root_cause)
            print(md)
            if args.output:
                from pathlib import Path as _Path
                out_path = _Path(args.output)
                if not out_path.is_absolute():
                    from zplan_shared.config import ZPLAN_ROOT as _ZR
                    out_path = _Path(_ZR) / "backtest_review" / out_path.name
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(md, encoding="utf-8")
                print(f"\n---\n已写入: {out_path}")

    elif cmd == "paper-trade":
        pt_cmd = getattr(args, "pt_cmd", "status") or "status"
        if pt_cmd == "morning":
            result = paper_morning(
                top_n=args.top, capital=args.capital,
                run_id=args.run_id, dry_run=args.dry_run,
            )
            print(format_morning_report(result))
        elif pt_cmd == "close":
            result = paper_close(dry_run=args.dry_run)
            print(format_close_report(result))
        else:
            result = paper_status()
            if getattr(args, "as_json", False):
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print(format_status_report(result))

    elif cmd == "check-data":
        result = audit_market_data()
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            h = result["health"]
            print(f"行情：{'OK' if result['ok'] else '需补齐'} — {h['message']}")
            print(f"  max日 {result['raw_latest_date']} ({result['raw_latest_symbols']} 只)")
            print(f"  有效截面 {result['effective_latest_date']} ({result['effective_panel_rows']} 只)")
            if result["missing_on_raw_latest"]:
                print(f"  缺 {result['missing_on_raw_latest']} 只 → {result['catchup_hint']}")
    elif cmd == "catchup-data":
        result = run_catchup_if_needed(limit=args.limit, workers=args.workers)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif cmd == "audit":
        report = score_deviation_report(
            run_id=args.run_id,
            top_n=args.top,
            horizon_days=args.horizon,
        )
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            fname = args.output or f"audit_run{report.get('run_id')}_{date.today().isoformat()}.md"
            out_path = Path(fname)
            if not out_path.is_absolute():
                out_path = Path(ZPLAN_ROOT) / "backtest_review" / out_path.name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report["markdown"], encoding="utf-8")
            print(report["markdown"])
            print(f"\n---\n已写入: {out_path}")
    elif cmd == "iterate":
        ic = args.iterate_cmd or "verify"
        if ic == "history":
            print_iteration_history(limit=args.limit)
        elif ic == "diff":
            print_iteration_diff()
        elif ic == "full":
            result = run_iteration_cycle(
                phase="full",
                top_n=args.top,
                horizon_days=args.horizon,
                with_pick=True,
                note=args.note,
            )
            print(result.get("markdown", json.dumps(result, ensure_ascii=False, indent=2, default=str)))
            if result.get("cycle_path"):
                print(f"\n---\n迭代记录: {result['cycle_path']}")
        else:
            result = run_iteration_cycle(
                phase="verify",
                run_id=getattr(args, "run_id", None),
                top_n=getattr(args, "top", 10),
                horizon_days=getattr(args, "horizon", 5),
                catchup=not getattr(args, "no_catchup", False),
                note=getattr(args, "note", ""),
            )
            print(result.get("markdown", json.dumps(result, ensure_ascii=False, indent=2, default=str)))
            if result.get("cycle_path"):
                print(f"\n---\n迭代记录: {result['cycle_path']}")
    elif cmd == "forecast-eval":
        fc = args.fe_cmd or "verify"
        if fc == "calibrate":
            from zplan_shared.forecast_evaluate import forecast_calibration_summary
            result = forecast_calibration_summary(horizon_days=args.horizon)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                _print_calibration_report(result)
        elif fc == "history":
            from zplan_shared.forecast_evaluate import forecast_accuracy_trend
            trend = forecast_accuracy_trend(days=args.days)
            if args.as_json:
                print(json.dumps(trend, ensure_ascii=False, indent=2, default=str))
            else:
                _print_trend(trend)
        else:
            # verify: 调用 forecast_verify.py 子进程
            import subprocess as _sp, sys as _sys
            forecast_script = ZPLAN_ROOT / "scripts" / "forecast_verify.py"
            forecast_python = ZPLAN_ROOT / ".venv" / "bin" / "python"
            if not forecast_python.is_file():
                forecast_python = Path(_sys.executable)
            cmd_parts = [str(forecast_python), str(forecast_script)]
            if args.threshold is not None:
                cmd_parts += ["--threshold", str(args.threshold)]
            if args.dry_run:
                cmd_parts.append("--dry-run")
            print(f"[forecast-eval verify] 执行: {' '.join(cmd_parts)}")
            proc = _sp.run(cmd_parts, cwd=str(ZPLAN_ROOT), capture_output=True, text=True, timeout=600)
            print(proc.stdout)
            if proc.stderr:
                print(proc.stderr, file=_sys.stderr)
            if proc.returncode != 0:
                raise SystemExit(proc.returncode)
            # 接着跑多周期评估
            from zplan_shared.forecast_evaluate import save_forecast_evals
            horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
            eval_result = save_forecast_evals(horizons=horizons)
            print(f"\n多周期评估完成: {eval_result}")
    elif cmd == "exit-compare":
        from backtest_agent.exit_backtest import run_exit_compare
        from zplan_shared.exit_config import load_exit_config
        from zplan_shared.exit_strategy import ExitPlan

        plan_keys = [k.strip() for k in (args.plans or "static,trailing_8pct,atr_trail_2x").split(",") if k.strip()]
        config = load_exit_config()
        plans: list[tuple[str, ExitPlan]] = []
        for key in plan_keys:
            plan = config.get_plan(key)
            if plan:
                plans.append((key, plan))
            else:
                print(f"[WARN] 方案 '{key}' 在 strategy.yaml exit.plans 中未定义，跳过")
        if not plans:
            plans.append(("static (fallback)", ExitPlan.static_default()))

        result = run_exit_compare(args.run_id, plans, top_n=args.top, output_md=True)
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            if result.get("report_path"):
                report_content = Path(result["report_path"]).read_text(encoding="utf-8")
                print(report_content)
                print(f"\n报告: {result['report_path']}")
            else:
                print(result.get("message", json.dumps(result, ensure_ascii=False, indent=2, default=str)))
    elif cmd == "exit-optimize":
        from backtest_agent.exit_backtest import run_exit_sweep

        result = run_exit_sweep(args.run_id, top_n=args.top, output_md=True)
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            if result.get("report_path"):
                report_content = Path(result["report_path"]).read_text(encoding="utf-8")
                print(report_content)
                print(f"\n报告: {result['report_path']}")
            else:
                print(result.get("message", json.dumps(result, ensure_ascii=False, indent=2, default=str)))
            if result.get("top_params"):
                print("\n── Top 参数组合 ──")
                for tp in result["top_params"]:
                    print(f"  {tp['plan']}: avg_return={tp['avg_return']:+.2f}% sharpe={tp.get('sharpe', '-')}")
    else:
        code = args.code or "000001"
        result = run_backtest_agent(ts_code=code)
        logger.info("完成: %s", result)


if __name__ == "__main__":
    main()
