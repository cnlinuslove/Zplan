from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from zplan_shared.config import ZPLAN_ROOT
from zplan_shared.llm.gemini import GeminiError, gemini_available
from zplan_shared.models import init_db
from zplan_shared.pick_store import (
    get_entry_report,
    get_run,
    history_for_stock,
    list_runs,
    save_report_run,
    save_scan_run,
)

from pick_agent.export import build_signal_export, write_json, write_picks_csv
from pick_agent.llm_cost import (
    estimate_from_usage,
    estimate_full_report,
    estimate_scan_brief,
    format_cost_table,
)
from pick_agent.llm_research import (
    brief_review_scan_picks,
    format_llm_report_markdown,
    research_with_llm,
)
from pick_agent.report import InsufficientBarsError, build_research_report, format_report_markdown
from pick_agent.resolve import SymbolAmbiguousError, SymbolNotFoundError, resolve_symbol
from pick_agent.scanner import scan_universe
from pick_agent.strategy import load_strategy


logger = logging.getLogger(__name__)


def _resolve_use_llm(
    *,
    explicit_llm: bool,
    no_llm: bool,
    strat,
    has_symbol: bool,
) -> bool:
    if no_llm:
        return False
    if explicit_llm:
        return True
    if has_symbol and strat.llm_enabled:
        return True
    return False


def _resolve_scan_llm(*, no_llm: bool, strat) -> bool:
    if no_llm:
        return False
    return bool(strat.llm_enabled and strat.llm_scan_brief)


def run_pick_agent(
    *,
    top_n: int = 20,
    min_score: float | None = None,
    symbol: str | None = None,
    report_format: str = "markdown",
    strategy_path: str | None = None,
    skip_health_check: bool = False,
    industry: str | None = None,
    symbols: list[str] | None = None,
    output: str | None = None,
    use_llm: bool = False,
    no_llm: bool = False,
    persist: bool = True,
) -> dict:
    """选股 Agent：扫描或单票/批量研报。"""
    init_db()
    strat = load_strategy(strategy_path)
    if min_score is not None:
        strat.min_score = min_score

    want_llm = _resolve_use_llm(
        explicit_llm=use_llm,
        no_llm=no_llm,
        strat=strat,
        has_symbol=bool(symbol or symbols),
    )

    if symbols:
        reports: list[dict] = []
        run_ids: list[int] = []
        usages: list[dict] = []
        for sym in symbols:
            code = resolve_symbol(sym)
            if want_llm:
                r = research_with_llm(code, strategy=strat, skip_health_check=skip_health_check)
                md = format_llm_report_markdown(r)
                u = (r.get("llm") or {}).get("usage")
                if u:
                    usages.append(u)
            else:
                r = build_research_report(code, strategy=strat, skip_health_check=skip_health_check)
                md = format_report_markdown(r)
            reports.append(r)
            if persist:
                rid = save_report_run(
                    r,
                    symbol_query=sym,
                    markdown=md,
                    params={"llm": want_llm},
                    llm_enabled=want_llm,
                    llm_model=strat.llm_model,
                )
                run_ids.append(rid)
        payload: dict = {
            "ok": True,
            "agent": "pick",
            "zplan_root": str(ZPLAN_ROOT),
            "rule_version": strat.rule_version,
            "llm": want_llm,
            "reports": reports,
            "run_ids": run_ids,
        }
        if usages:
            payload["llm_usage_total"] = {
                "calls": len(usages),
                "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
                "output_tokens": sum(int(u.get("output_tokens") or 0) for u in usages),
            }
        if output:
            write_json(Path(output), payload)
        return payload

    if symbol:
        code = resolve_symbol(symbol)
        if want_llm:
            report = research_with_llm(
                code,
                strategy=strat,
                skip_health_check=skip_health_check,
            )
            md = format_llm_report_markdown(report)
        else:
            report = build_research_report(
                code,
                strategy=strat,
                skip_health_check=skip_health_check,
            )
            md = format_report_markdown(report)

        if report_format == "json":
            payload = {
                "ok": True,
                "agent": "pick",
                "zplan_root": str(ZPLAN_ROOT),
                "llm": want_llm,
                "report": report,
            }
        else:
            payload = {
                "ok": True,
                "agent": "pick",
                "zplan_root": str(ZPLAN_ROOT),
                "llm": want_llm,
                "ts_code": code,
                "markdown": md,
                "report": report,
            }
        if output:
            out = Path(output)
            if report_format == "json" or out.suffix == ".json":
                write_json(out, report)
            else:
                out.write_text(md, encoding="utf-8")
        if persist:
            run_id = save_report_run(
                report,
                symbol_query=symbol,
                markdown=md,
                params={"format": report_format, "llm": want_llm},
                llm_enabled=want_llm,
                llm_model=strat.llm_model,
            )
            payload["run_id"] = run_id
            payload["entry_id_hint"] = f"见 pick_entries where run_id={run_id}"
        return payload

    result = scan_universe(
        top_n=top_n,
        min_score=strat.min_score,
        strategy=strat,
        skip_health_check=skip_health_check,
    )
    result["agent"] = "pick"
    result["zplan_root"] = str(ZPLAN_ROOT)

    if industry and result.get("picks"):
        key = industry.strip()
        result["picks"] = [p for p in result["picks"] if (p.get("industry") or "").find(key) >= 0]
        result["industry_filter"] = key

    scan_llm = _resolve_scan_llm(no_llm=no_llm, strat=strat)
    if scan_llm and result.get("picks") and gemini_available():
        picks, usage = brief_review_scan_picks(
            result["picks"],
            as_of=result.get("as_of"),
            model=strat.llm_model,
        )
        result["picks"] = picks
        result["llm_scan_brief"] = True
        if usage:
            result["llm_usage"] = usage
            est = estimate_from_usage(usage, label="本次扫描简评")
            if est:
                result["llm_cost_estimate"] = est.to_dict()
    elif scan_llm and not gemini_available():
        result["llm_scan_brief_skipped"] = "GEMINI_API_KEY 未配置"

    if persist and result.get("ok"):
        run_id = save_scan_run(
            result,
            params={
                "top_n": top_n,
                "min_score": strat.min_score,
                "llm_scan_brief": scan_llm,
            },
        )
        result["run_id"] = run_id
        result["db"] = f"sqlite://{ZPLAN_ROOT}/zplan.db (pick_runs / pick_entries)"

    if output and result.get("ok"):
        out = Path(output)
        if out.suffix == ".csv":
            write_picks_csv(
                out,
                result.get("picks") or [],
                meta={"as_of": result.get("as_of"), "rule_version": result.get("rule_version")},
            )
        elif out.suffix == ".json":
            write_json(out, build_signal_export(result))
        else:
            write_json(out, result)

    return result


def _print_cost_estimates(top_n: int) -> None:
    full = estimate_full_report()
    scan = estimate_scan_brief(top_n, batch=True)
    print(format_cost_table(full, scan))
    print(f"- 单票深度研报：约 **¥{full.cny_approx:.2f}** / 只（~{full.input_tokens + full.output_tokens:,} tokens）")
    print(f"- 扫描 Top{top_n} 简评（批量 1 次）：约 **¥{scan.cny_approx:.2f}** / 次")
    print(f"- 若 Top{top_n} 每只都出深度研报：约 **¥{full.cny_approx * top_n:.2f}** / 次扫描后全分析")
    print("")
    if not gemini_available():
        print("当前环境未检测到 GEMINI_API_KEY，以上仅为理论估算。")


def _print_run_detail(run_id: int) -> None:
    data = get_run(run_id)
    if not data:
        print(f"未找到 run_id={run_id}", file=sys.stderr)
        raise SystemExit(2)
    run = data["run"]
    print(
        f"run_id={run['run_id']} kind={run['run_kind']} as_of={run['trade_date_as_of']} "
        f"created={run['created_at_utc']} llm={run['llm_enabled']}"
    )
    for e in data["entries"]:
        print(
            f"  [{e.get('rank') or '-'}] {e['ts_code']} {e.get('name') or ''} "
            f"综合={e.get('final_composite_score')} 技术={e.get('rule_tech_score')} "
            f"LLM={e.get('llm_composite_score')} {e.get('recommendation') or ''} "
            f"entry_id={e['entry_id']}"
        )


def _print_entry_report(entry_id: int) -> None:
    data = get_entry_report(entry_id)
    if not data:
        print(f"未找到 entry_id={entry_id}", file=sys.stderr)
        raise SystemExit(2)
    if data.get("markdown"):
        print(data["markdown"])
    elif data.get("report"):
        print(json.dumps(data["report"], ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Plan 选股 Agent")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("-s", "--symbol", type=str, default=None)
    parser.add_argument("--symbols-file", type=str, default=None, help="每行一只：代码或名称")
    parser.add_argument("--industry", type=str, default=None, help="扫描结果按行业名过滤")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--strategy", type=str, default=None, help="strategy.yaml 路径")
    parser.add_argument("--output", "-o", type=str, default=None, help="picks.json / picks.csv / report.md")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="强制启用 Gemini（单票默认已开，见 strategy.yaml llm.enabled）",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="禁用 LLM，仅用规则引擎",
    )
    parser.add_argument(
        "--estimate-cost",
        action="store_true",
        help="打印 Gemini 2.5 Pro 成本估算后退出",
    )
    parser.add_argument("--skip-health-check", action="store_true")
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="不写入 zplan.db（pick_runs / pick_entries）",
    )
    parser.add_argument("--list-runs", type=int, nargs="?", const=30, metavar="N")
    parser.add_argument("--show-run", type=int, metavar="RUN_ID")
    parser.add_argument("--show-entry", type=int, metavar="ENTRY_ID")
    parser.add_argument("--history", type=str, metavar="TS_CODE", help="某只股票历史打分")
    args = parser.parse_args()

    init_db()

    if args.list_runs is not None:
        for r in list_runs(limit=args.list_runs):
            s = r.get("summary") or {}
            print(
                f"#{r['run_id']:4} {r['run_kind']:6} {r['created_at_utc'][:19]} "
                f"as_of={r.get('trade_date_as_of') or '—':10} "
                f"llm={r['llm_enabled']} "
                f"{s.get('qualified', s.get('name', ''))}"
            )
        return

    if args.show_run is not None:
        _print_run_detail(args.show_run)
        return

    if args.show_entry is not None:
        _print_entry_report(args.show_entry)
        return

    if args.history:
        code = resolve_symbol(args.history)
        for h in history_for_stock(code):
            print(
                f"{h['created_at_utc'][:19]} run={h['run_id']} {h['run_kind']:6} "
                f"分={h.get('final_composite_score')} {h.get('recommendation') or ''} "
                f"entry_id={h['entry_id']}"
            )
        return

    if args.estimate_cost:
        _print_cost_estimates(args.top)
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    symbols: list[str] | None = None
    if args.symbols_file:
        text = Path(args.symbols_file).read_text(encoding="utf-8")
        symbols = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]

    try:
        result = run_pick_agent(
            top_n=args.top,
            min_score=args.min_score,
            symbol=args.symbol,
            report_format=args.format,
            strategy_path=args.strategy,
            skip_health_check=args.skip_health_check,
            industry=args.industry,
            symbols=symbols,
            output=args.output,
            use_llm=args.llm,
            no_llm=args.no_llm,
            persist=not args.no_save,
        )
    except GeminiError as e:
        print(f"LLM 错误: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    except SymbolNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    except SymbolAmbiguousError as e:
        print(f"错误: {e}", file=sys.stderr)
        for m in e.matches:
            print(f"  - {m['name']} ({m['ts_code']})")
        raise SystemExit(2) from e
    except InsufficientBarsError as e:
        print(f"错误: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    except RuntimeError as e:
        print(f"行情门禁: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if not result.get("ok", True):
        print(result.get("message", "失败"), file=sys.stderr)
        raise SystemExit(1)

    if args.symbol and args.format == "markdown" and "markdown" in result:
        print(result["markdown"])
        usage = (result.get("report") or {}).get("llm", {}).get("usage")
        est = estimate_from_usage(usage, label="本次单票")
        if est:
            print(f"\n---\n**本次 API 用量**：输入 {est.input_tokens:,} / 输出 {est.output_tokens:,} tok，约 ${est.usd:.4f}（≈¥{est.cny_approx:.2f}）")
        if result.get("run_id"):
            print(
                f"\n---\n**已入库** run_id={result['run_id']} "
                f"（查看：main.py --show-run {result['run_id']}）"
            )
    elif args.symbol and args.format == "json":
        print(json.dumps(result.get("report", result), ensure_ascii=False, indent=2, default=str))
    elif symbols:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        logger.info("完成: %s", {k: v for k, v in result.items() if k != "picks"})
        if result.get("message"):
            print(result["message"])
        if result.get("llm_cost_estimate"):
            c = result["llm_cost_estimate"]
            print(
                f"LLM 简评用量：输入 {c.get('input_tokens', 0):,} / 输出 {c.get('output_tokens', 0):,} tok，"
                f"约 ${c.get('usd', 0):.4f}（≈¥{c.get('cny_approx', 0):.2f}）"
            )
        if result.get("run_id"):
            print(
                f"已入库 run_id={result['run_id']} "
                f"（main.py --show-run {result['run_id']}）"
            )
        picks = result.get("picks") or []
        if not picks:
            print(
                f"无符合条件的标的（扫描 {result.get('scanned', 0)} 只，"
                f"预过滤 {result.get('prefiltered', '—')} 只）"
            )
            raise SystemExit(0)
        for i, p in enumerate(picks, 1):
            brief = p.get("llm_brief") or {}
            trend = brief.get("trend") or ""
            line = (
                f"{i:2}. {p.get('name') or '—':8} {p['ts_code']} "
                f"综合={p.get('composite_score', p['tech_score']):.1f} "
                f"技术={p['tech_score']:.1f} {p['verdict']} "
            )
            if brief.get("recommendation"):
                line += f"LLM={brief['recommendation']} "
            if trend:
                line += f"| {trend[:48]}"
            else:
                line += f"信号={','.join(p.get('signals') or [])}"
            print(line)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        from pick_agent.watch_cli import main as watch_main

        watch_main(sys.argv[2:])
    else:
        main()
