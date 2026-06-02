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

from backtest_agent.calibration import build_calibration_report, print_calibration
from backtest_agent.data_audit import audit_market_data, run_catchup_if_needed, score_deviation_report
from backtest_agent.iterate import (
    print_iteration_diff,
    print_iteration_history,
    run_iteration_cycle,
)
from backtest_agent.llm_review import default_review_filename, print_llm_eval

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
    else:
        code = args.code or "000001"
        result = run_backtest_agent(ts_code=code)
        logger.info("完成: %s", result)


if __name__ == "__main__":
    main()
