"""持仓订阅 CLI：watch add | list | remove | daily"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from zplan_shared.llm.gemini import GeminiError
from zplan_shared.models import SessionLocal, StockList, init_db
from zplan_shared.pick_watchlist import add_watch_resolved, delete_watch, list_watch, remove_watch

from pick_agent.resolve import SymbolAmbiguousError, SymbolNotFoundError, resolve_symbol
from pick_agent.watchlist_daily import run_watchlist_daily


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="持仓订阅与每日简报")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="加入持仓订阅")
    p_add.add_argument("symbol", help="代码或名称")
    p_add.add_argument("--note", type=str, default=None)

    sub.add_parser("list", help="列出订阅")

    p_rm = sub.add_parser("remove", help="停用订阅（软删除）")
    p_rm.add_argument("symbol")

    p_del = sub.add_parser("delete", help="从订阅表删除")
    p_del.add_argument("symbol")

    p_daily = sub.add_parser("daily", help="同步行情并生成每日简报")
    p_daily.add_argument("--skip-sync", action="store_true", help="跳过日线/分时同步")
    p_daily.add_argument("--skip-news", action="store_true", help="跳过资讯补链")
    p_daily.add_argument("--no-llm", action="store_true")
    p_daily.add_argument("--no-save", action="store_true")
    p_daily.add_argument("--no-file", action="store_true", help="不写 pick_digest/*.md")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "add":
            code = resolve_symbol(args.symbol)
            init_db()
            with SessionLocal() as s:
                name = s.execute(
                    select(StockList.name).where(StockList.ts_code == code)
                ).scalar_one_or_none()
            row = add_watch_resolved(code, name, note=args.note)
            print(f"已订阅：{row.get('name')}（{row['ts_code']}）" + (f" 备注={args.note}" if args.note else ""))

        elif args.cmd == "list":
            items = list_watch(enabled_only=False)
            if not items:
                print("（暂无订阅）")
                return
            for w in items:
                flag = "✓" if w["enabled"] else "✗"
                print(
                    f"{flag} {w['ts_code']} {w.get('name') or '—':8} "
                    f"sync={str(w.get('last_sync_at_utc') or '')[:19]} "
                    f"brief={str(w.get('last_brief_at_utc') or '')[:19]} "
                    f"{w.get('note') or ''}"
                )

        elif args.cmd == "remove":
            code = resolve_symbol(args.symbol)
            if remove_watch(code):
                print(f"已停用：{code}")
            else:
                print(f"未找到订阅：{code}", file=sys.stderr)
                raise SystemExit(2)

        elif args.cmd == "delete":
            code = resolve_symbol(args.symbol)
            if delete_watch(code):
                print(f"已删除：{code}")
            else:
                print(f"未找到：{code}", file=sys.stderr)
                raise SystemExit(2)

        elif args.cmd == "daily":
            result = run_watchlist_daily(
                skip_sync=args.skip_sync,
                skip_news_link=args.skip_news,
                use_llm=not args.no_llm,
                persist=not args.no_save,
                write_digest_file=not args.no_file,
            )
            if not result.get("ok"):
                print(result.get("message", "失败"), file=sys.stderr)
                raise SystemExit(1)
            print(result["markdown"])
            if result.get("digest_path"):
                print(f"\n---\n已写入：{result['digest_path']}", file=sys.stderr)
            if result.get("run_id"):
                print(f"已入库 run_id={result['run_id']}（main.py --show-run {result['run_id']}）", file=sys.stderr)

    except SymbolNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    except SymbolAmbiguousError as e:
        print(f"错误: {e}", file=sys.stderr)
        for m in e.matches:
            print(f"  - {m['name']} ({m['ts_code']})")
        raise SystemExit(2) from e
    except GeminiError as e:
        print(f"LLM 错误: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
