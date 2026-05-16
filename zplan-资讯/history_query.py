from __future__ import annotations

import argparse

from agents.news_agent import format_runs_for_wechat, query_summary_last_days, query_summary_latest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query news summary history")
    parser.add_argument("--mode", choices=["latest", "7d"], default="latest")
    parser.add_argument("--topic")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "latest":
        rows = query_summary_latest(topic_key=args.topic)
    else:
        rows = query_summary_last_days(days=7, topic_key=args.topic)
    print(format_runs_for_wechat(rows))


if __name__ == "__main__":
    main()
