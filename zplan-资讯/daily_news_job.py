"""
每日资讯一键任务：快讯/RSS/情绪 ETL → 新闻个股关联 → X Topic 摘要（可选微信推送）。

用法：
  .venv/bin/python daily_news_job.py
  .venv/bin/python daily_news_job.py --etl-only
  .venv/bin/python daily_news_job.py --no-x
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def setup_logging() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.handlers[0].setFormatter(formatter)
    fh = RotatingFileHandler(
        log_dir / "daily_news_job.log",
        maxBytes=5_000_000,
        backupCount=5,
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)


def _acquire_lock() -> Path | None:
    lock = ROOT / "logs" / "daily_news_job.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            logging.warning("[WARN] 已有任务在运行 (pid=%s)，跳过本次", pid)
            return None
        except (OSError, ValueError):
            lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()))
    return lock


def _release_lock(lock: Path | None) -> None:
    if lock and lock.exists():
        try:
            if lock.read_text().strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


def run_daily_news_job(
    *,
    run_etl: bool | None = None,
    run_x: bool | None = None,
    push_wechat: bool | None = None,
    link_hours: int | None = None,
) -> dict:
    """执行每日资讯流水线，返回各阶段统计。"""
    from zplan_shared.models import init_db

    init_db()
    run_etl = _env_bool("DAILY_NEWS_RUN_ETL", True) if run_etl is None else run_etl
    run_x = _env_bool("DAILY_NEWS_RUN_X_TOPICS", False) if run_x is None else run_x
    push_wechat = _env_bool("DAILY_NEWS_PUSH_WECHAT", True) if push_wechat is None else push_wechat
    link_hours = link_hours or int(os.getenv("DAILY_NEWS_LINK_HOURS", "168") or "168")

    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": {},
        "ok": True,
    }

    if run_etl:
        from sentiment_etl.runner import run_sentiment_etl

        logging.info("[INFO] 阶段 1/2: sentiment_etl（快讯/RSS/情绪）")
        etl_stats = run_sentiment_etl(push_wechat=push_wechat)
        report["stages"]["sentiment_etl"] = {
            "inserted": etl_stats.get("inserted"),
            "alerts": etl_stats.get("alerts"),
            "coverage_48h": etl_stats.get("coverage_48h"),
            "news_link": etl_stats.get("news_link"),
        }
        if etl_stats.get("alerts"):
            logging.warning("[WARN] ETL 告警: %s", etl_stats["alerts"])
    else:
        logging.info("[INFO] 跳过 sentiment_etl（DAILY_NEWS_RUN_ETL=false）")
        from zplan_shared.news_linker import link_recent_news, news_link_coverage_stats

        relink = os.getenv("DAILY_NEWS_LINK_RELINK", "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        link_limit = int(os.getenv("DAILY_NEWS_LINK_LIMIT", "800") or "800")
        report["stages"]["news_link_only"] = {
            "news_link": link_recent_news(
                hours=link_hours,
                limit_per_table=link_limit,
                relink=relink,
            ),
            "coverage_48h": news_link_coverage_stats(hours=48),
        }

    if run_x:
        from agents.news_agent import run_news_cycle

        logging.info("[INFO] 阶段 2/2: X Topic 摘要轮次")
        x_stats = run_news_cycle(push_wechat=push_wechat)
        report["stages"]["news_cycle"] = x_stats
    else:
        logging.info("[INFO] 跳过 X Topic 摘要（DAILY_NEWS_RUN_X_TOPICS=false）")

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    logging.info("[INFO] 每日资讯任务完成: %s", json.dumps(report["stages"], ensure_ascii=False)[:500])
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Z-Plan 每日资讯更新（ETL + 关联 + X 摘要）")
    p.add_argument("--etl-only", action="store_true", help="仅跑 sentiment_etl（含补链）")
    p.add_argument("--no-x", action="store_true", help="不跑 X Topic 摘要")
    p.add_argument("--no-push", action="store_true", help="不推送到企业微信")
    p.add_argument("--link-hours", type=int, default=0, help="仅补链时的回溯小时（默认读 .env）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    lock = _acquire_lock()
    if lock is None:
        return 0

    try:
        run_x: bool | None = None
        if args.etl_only or args.no_x:
            run_x = False
        report = run_daily_news_job(
            run_etl=True,
            run_x=run_x,
            push_wechat=False if args.no_push else None,
            link_hours=args.link_hours or None,
        )
        print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        logging.exception("[ERROR] 每日资讯任务失败: %s", exc)
        print(
            json.dumps(
                {"ok": False, "error": {"type": exc.__class__.__name__, "detail": str(exc)}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        _release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
