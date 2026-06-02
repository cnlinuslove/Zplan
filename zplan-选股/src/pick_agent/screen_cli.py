"""screen 子命令：题材 / 行业 / 规则分 固定筛选。"""
from __future__ import annotations

import argparse
import sys

from zplan_shared.concept_screen import list_cached_concepts, sync_concept_members

from pick_agent.screen import run_screen


def main(argv: list[str] | None = None) -> None:
    argv = list(argv or [])
    if argv and argv[0].startswith("-"):
        argv = ["run", *argv]

    parser = argparse.ArgumentParser(description="条件筛选（题材/行业/规则分）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("concepts", help="列出已缓存概念板")
    p_list.add_argument("keyword", nargs="?", default=None)

    p_sync = sub.add_parser("sync-concept", help="同步东财概念成份到本地库")
    p_sync.add_argument("name", help="概念板全称，如：脑机接口")

    p_scr = sub.add_parser("run", help="执行筛选")
    p_scr.add_argument("--concept", type=str, default=None, help="题材关键词，如 脑机接口")
    p_scr.add_argument("--industry", type=str, default=None)
    p_scr.add_argument("--name", type=str, default=None, help="名称模糊")
    p_scr.add_argument("--min-rule-score", type=float, default=None)
    p_scr.add_argument("--max-ret-20d", type=float, default=None, help="过滤20日涨幅过高(防追高)")
    p_scr.add_argument("--llm-run-id", type=int, default=None, help="附加 LLM 分，如 8")
    p_scr.add_argument("--refresh", action="store_true", help="强制刷新概念成份")
    p_scr.add_argument("-o", "--output", type=str, default=None)

    args = parser.parse_args(argv)

    if args.cmd == "concepts":
        names = list_cached_concepts(keyword=args.keyword, limit=100)
        if not names:
            print("（无缓存，可先：main.py screen sync-concept 脑机接口）")
            return
        for n in names:
            print(n)
        return

    if args.cmd == "sync-concept":
        try:
            r = sync_concept_members(args.name)
        except Exception as exc:
            print(
                f"同步失败（东财网络/代理）：{exc}\n"
                "提示：关闭失效代理或设置 AKSHARE_EASTMONEY_PROXY_FALLBACK=false；"
                "也可在能访问东财的环境执行本命令后再筛选。",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        print(f"已同步 {r['concept_name']}：{r['count']} 只")
        return

    if args.cmd == "run":
        if not any([args.concept, args.industry, args.name]):
            print("请至少指定 --concept / --industry / --name", file=sys.stderr)
            raise SystemExit(2)
        result = run_screen(
            concept=args.concept,
            industry=args.industry,
            name_like=args.name,
            min_rule_score=args.min_rule_score,
            max_ret_20d=args.max_ret_20d,
            llm_run_id=args.llm_run_id,
            refresh_concept=args.refresh,
            output=args.output,
        )
        df = result["dataframe"]
        if df.empty:
            print("无匹配标的")
            raise SystemExit(1)
        print(df.to_string(index=False, max_rows=50))
        if len(df) > 50:
            print(f"... 共 {len(df)} 只")
        if result.get("path"):
            print(f"\n已导出：{result['path']}", file=sys.stderr)


if __name__ == "__main__":
    main()
