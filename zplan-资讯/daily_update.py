from __future__ import annotations

import argparse
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agents.news_agent import run_news_cycle
from config import NEWS_SCHEDULE_HOURS


def setup_logging() -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daily_update.log"

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="News agent scheduler")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    parser.add_argument("--max-cycles", type=int, default=0, help="最多执行轮数，0为无限")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="覆盖默认调度间隔（秒），仅用于测试",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()
    interval_seconds = args.interval_seconds if args.interval_seconds > 0 else NEWS_SCHEDULE_HOURS * 3600
    logging.info("[INFO] 资讯Agent调度启动，间隔=%s小时", NEWS_SCHEDULE_HOURS)

    cycles = 0
    while True:
        try:
            stats = run_news_cycle()
            logging.info("[INFO] 本轮任务完成: %s", stats)
        except Exception as exc:
            logging.warning("[WARN] 本轮任务异常: %s", exc)
        cycles += 1
        if args.once:
            logging.info("[INFO] --once 模式，任务结束。")
            break
        if args.max_cycles > 0 and cycles >= args.max_cycles:
            logging.info("[INFO] 已达到最大轮数 %s，任务结束。", args.max_cycles)
            break
        logging.info("[INFO] 休眠 %s 秒，等待下一轮。", interval_seconds)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
