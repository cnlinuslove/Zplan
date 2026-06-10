"""规则分初始化与 Top N LLM 二次打分 CLI。"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from zplan_shared.llm.gemini import GeminiError

from pick_agent.llm_top300 import run_llm_top_from_rule_scores
from pick_agent.pipeline_full import run_deep_reports_top, run_full_pipeline
from pick_agent.rule_universe import build_rule_scores_universe
from pick_agent.strategy import load_strategy


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="规则分全市场初始化 / Top300 LLM")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-rule", help="全市场规则分写入 stock_rule_scores")
    p_init.add_argument("--skip-health-check", action="store_true")
    p_init.add_argument("--as-of", type=str, default=None, help="指定截面日期 YYYY-MM-DD（默认最新）")
    p_init.add_argument("--v2", action="store_true", help="使用 v2 评分（反转+资金流+概念热度）替代默认动量评分")
    p_init.add_argument("--strategy", type=str, default=None)

    p_llm = sub.add_parser("llm-top", help="规则分 Top N → 深度规则 + LLM 简评入库")
    p_llm.add_argument("--top", type=int, default=300, help="从 stock_rule_scores 取前 N（默认 300）")
    p_llm.add_argument("--batch-size", type=int, default=None, help="LLM 每批只数（默认 15，见 strategy.yaml）")
    p_llm.add_argument("--as-of", type=str, default=None, help="指定截面日期 YYYY-MM-DD（默认最新）")
    p_llm.add_argument("--variant", type=str, default=None, help="A/B 实验标签（如 baseline / strict / value）")
    p_llm.add_argument("--no-llm", action="store_true")
    p_llm.add_argument("--no-deepen", action="store_true", help="跳过深度规则复核")
    p_llm.add_argument("--no-save", action="store_true")
    p_llm.add_argument("--strategy", type=str, default=None)

    p_pipe = sub.add_parser("pipeline", help="init-rule 后 llm-top（一步完成）")
    p_pipe.add_argument("--top", type=int, default=300)
    p_pipe.add_argument("--batch-size", type=int, default=None)
    p_pipe.add_argument("--variant", type=str, default=None, help="A/B 实验标签")
    p_pipe.add_argument("--skip-health-check", action="store_true")
    p_pipe.add_argument("--no-llm", action="store_true")
    p_pipe.add_argument("--no-deepen", action="store_true")
    p_pipe.add_argument("--no-save", action="store_true")
    p_pipe.add_argument("--strategy", type=str, default=None)

    p_full = sub.add_parser(
        "pipeline-full",
        help="init-rule → llm-top 300 → 深度研报 Top30",
    )
    p_full.add_argument("--top", type=int, default=300, help="LLM 简评数量")
    p_full.add_argument("--deep-top", type=int, default=30, help="深度研报数量")
    p_full.add_argument("--batch-size", type=int, default=None)
    p_full.add_argument("--skip-health-check", action="store_true")
    p_full.add_argument("--catch-up-panel", action="store_true", help="先跑股价 Agent 补齐截面")
    p_full.add_argument("--catch-up-limit", type=int, default=None)
    p_full.add_argument("--catch-up-workers", type=int, default=6)
    p_full.add_argument("--deepen-workers", type=int, default=8)
    p_full.add_argument("--deep-llm-workers", type=int, default=2)
    p_full.add_argument("--no-deepen", action="store_true")
    p_full.add_argument("--variant", type=str, default=None, help="A/B 实验标签")
    p_full.add_argument("--strategy", type=str, default=None)

    p_deep = sub.add_parser("deep-top", help="对最近一次 llm-top 结果或指定 run 的 Top N 出深度研报")
    p_deep.add_argument("--top", type=int, default=30)
    p_deep.add_argument("--from-run", type=int, default=None, help="pick_runs.run_id（llm_top300）")
    p_deep.add_argument("--skip-health-check", action="store_true")
    p_deep.add_argument("--strategy", type=str, default=None)

    args = parser.parse_args(argv)
    strat = load_strategy(args.strategy) if getattr(args, "strategy", None) else load_strategy()

    try:
        if args.cmd == "init-rule":
            as_of = date.fromisoformat(args.as_of) if getattr(args, "as_of", None) else None
            result = build_rule_scores_universe(
                strategy=strat,
                skip_health_check=args.skip_health_check,
                use_v2=getattr(args, "v2", False),
                as_of=as_of,
            )
            if not result.get("ok"):
                print(result.get("message", "失败"), file=sys.stderr)
                raise SystemExit(1)
            print(
                f"规则分已写入 stock_rule_scores：as_of={result['as_of']} "
                f"共 {result['table_total']} 只（预筛 {result['prefiltered']}，"
                f"有效指标 {result['scored']}）rule_version={result['rule_version']}"
            )
            print("下一步：main.py llm-top --top 300")

        elif args.cmd == "llm-top":
            as_of = date.fromisoformat(args.as_of) if getattr(args, "as_of", None) else None
            result = run_llm_top_from_rule_scores(
                top_n=args.top,
                batch_size=args.batch_size,
                strategy=strat,
                trade_date_as_of=as_of,
                deepen=not args.no_deepen,
                use_llm=not args.no_llm,
                persist=not args.no_save,
                variant_label=getattr(args, "variant", None),
            )
            _print_llm_top_result(result)

        elif args.cmd == "pipeline":
            init_r = build_rule_scores_universe(
                strategy=strat,
                skip_health_check=args.skip_health_check,
                use_v2=getattr(args, "v2", False),
            )
            if not init_r.get("ok"):
                print(init_r.get("message", "init-rule 失败"), file=sys.stderr)
                raise SystemExit(1)
            print(
                f"[1/2] init-rule 完成：{init_r['table_total']} 只 @ {init_r['as_of']}"
            )
            result = run_llm_top_from_rule_scores(
                top_n=args.top,
                batch_size=args.batch_size,
                strategy=strat,
                deepen=not args.no_deepen,
                use_llm=not args.no_llm,
                persist=not args.no_save,
                variant_label=getattr(args, "variant", None),
            )
            print("[2/2] llm-top")
            _print_llm_top_result(result)

        elif args.cmd == "pipeline-full":
            result = run_full_pipeline(
                strategy=strat,
                skip_health_check=args.skip_health_check,
                catchup_panel=args.catch_up_panel,
                catchup_limit=args.catch_up_limit,
                catchup_workers=args.catch_up_workers,
                llm_top_n=args.top,
                llm_batch_size=args.batch_size,
                deep_top_n=args.deep_top,
                deepen=not args.no_deepen,
                deepen_workers=args.deepen_workers,
                deep_llm_workers=args.deep_llm_workers,
                variant_label=getattr(args, "variant", None),
            )
            _print_pipeline_full_result(result)

        elif args.cmd == "deep-top":
            from zplan_shared.pick_store import get_run

            if args.from_run:
                data = get_run(args.from_run)
                if not data:
                    print(f"未找到 run_id={args.from_run}", file=sys.stderr)
                    raise SystemExit(2)
                picks = [
                    {
                        "ts_code": e["ts_code"],
                        "name": e.get("name"),
                        "llm_composite_score": e.get("llm_composite_score"),
                        "rule_composite_score": e.get("rule_composite_score"),
                        "composite_score": e.get("final_composite_score"),
                    }
                    for e in data["entries"]
                ]
            else:
                from zplan_shared.pick_store import get_run, list_runs

                run_id = None
                for r in list_runs(limit=50):
                    if r.get("run_kind") == "llm_top300":
                        run_id = r["run_id"]
                        break
                if not run_id:
                    print(
                        "未找到 llm_top300 运行记录，请先 pipeline-full / llm-top，"
                        "或指定 --from-run <run_id>",
                        file=sys.stderr,
                    )
                    raise SystemExit(2)
                data = get_run(run_id)
                picks = [
                    {
                        "ts_code": e["ts_code"],
                        "name": e.get("name"),
                        "llm_composite_score": e.get("llm_composite_score"),
                        "rule_composite_score": e.get("rule_composite_score"),
                        "composite_score": e.get("final_composite_score"),
                    }
                    for e in (data or {}).get("entries") or []
                ]
                print(f"使用 llm_top300 run_id={run_id}，共 {len(picks)} 只", file=sys.stderr)

            deep_r = run_deep_reports_top(
                picks,
                top_n=args.top,
                strategy=strat,
                skip_health_check=args.skip_health_check,
            )
            _print_deep_top_result(deep_r)

    except GeminiError as e:
        print(f"LLM 错误: {e}", file=sys.stderr)
        raise SystemExit(1) from e


def _print_llm_top_result(result: dict) -> None:
    if not result.get("ok"):
        print(result.get("message", "失败"), file=sys.stderr)
        raise SystemExit(1)
    if result.get("run_id"):
        print(f"已入库 run_id={result['run_id']}（main.py --show-run {result['run_id']}）", file=sys.stderr)
    picks = result.get("picks") or []
    print(f"\n规则分 Top{len(picks)} → LLM 简评（as_of={result.get('as_of')}）\n")
    for p in picks[:30]:
        brief = p.get("llm_brief") or {}
        llm_s = p.get("llm_composite_score") or p.get("adjusted_score")
        rule_s = p.get("rule_composite_score") or p.get("composite_score")
        risk_penalty = brief.get("risk_penalty") or 0
        risk_str = f" -{risk_penalty:.0f}" if risk_penalty > 0 else ""
        flags = ", ".join(brief.get("risk_flags") or [])
        flag_str = f" [{flags}]" if flags and flags != "无明显风险" else ""
        print(
            f"{p.get('rank_in_run', '-'):3} {p.get('name') or '—':8} {p['ts_code']} "
            f"规则={rule_s} LLM={llm_s or '—'}{risk_str} {brief.get('recommendation') or p.get('verdict') or ''}{flag_str}"
        )
    if len(picks) > 30:
        print(f"... 另有 {len(picks) - 30} 只，见 --show-run")


def _print_deep_top_result(result: dict) -> None:
    if not result.get("ok"):
        print(result.get("message", "失败"), file=sys.stderr)
        raise SystemExit(1)
    print(f"\n深度研报 Top{result.get('deep_top_n')} 完成")
    print(f"约费用: ${result.get('cost_estimate_usd')} / ¥{result.get('cost_estimate_cny')}")
    for r in result.get("reports") or []:
        print(
            f"  [{r.get('rank')}] {r.get('name')} {r['ts_code']} "
            f"LLM={r.get('llm_composite_score')} run_id={r.get('run_id')}"
        )


def _print_pipeline_full_result(result: dict) -> None:
    if not result.get("ok"):
        print(result.get("message", "失败"), file=sys.stderr)
        raise SystemExit(1)
    init_r = result.get("init_rule") or {}
    llm_r = result.get("llm_top") or {}
    deep_r = result.get("deep_top") or {}
    print("\n=== pipeline-full 完成 ===")
    print(f"[1] init-rule: {init_r.get('table_total')} 只 @ {init_r.get('as_of')}")
    print(f"[2] llm-top: run_id={llm_r.get('run_id')} Top{llm_r.get('top_n') or len(llm_r.get('picks') or [])}")
    print(f"[3] deep-top: {deep_r.get('deep_top_n')} 份研报")
    for r in (deep_r.get("reports") or [])[:10]:
        print(f"      [{r.get('rank')}] {r.get('name')} run_id={r.get('run_id')}")
    if len(deep_r.get("reports") or []) > 10:
        print(f"      ... 共 {len(deep_r['reports'])} 只")
    ce = result.get("cost_estimate") or {}
    print(
        f"\n费用粗算: 合计 ~${ce.get('total_usd_approx')} (¥{ce.get('total_cny_approx')}) "
        f"| 简评 ~${ce.get('llm_top_usd_approx')} + 深度 ~${ce.get('deep_top_usd')}"
    )


if __name__ == "__main__":
    main()
