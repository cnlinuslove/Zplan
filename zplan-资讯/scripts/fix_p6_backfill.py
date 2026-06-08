"""P6: daily_features ORM 级历史回填。

使用现有的 run_daily_features_update，传入 Python date 对象（非字符串），
回填最近 N 个未覆盖的交易日。

用法：
  .venv/bin/python3 fix_p6_backfill.py --days 60 --workers 2
"""
import logging, sys, os
from datetime import date, timedelta

sys.path.insert(0, os.path.expanduser("~/my_stock_ai/zplan-共享/src"))
sys.path.insert(0, os.path.expanduser("~/my_stock_ai/zplan-股价/src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 60

    from zplan_shared.market import latest_trade_date
    from zplan_shared.etl_daily_features import run_daily_features_update

    end = latest_trade_date()
    if end is None:
        logger.error("无日线数据")
        return

    logger.info("最新交易日: %s, 回填 %s 天", end, days)

    done = skipped = failed = 0
    for i in range(days, 0, -1):
        d = end - timedelta(days=i)
        d = date(d.year, d.month, d.day)  # 确保是 date 对象

        try:
            stats = run_daily_features_update(
                as_of=d,
                calendar_days=90,
                min_bars=60,
                market='a',
            )
            if stats.get("rows", 0) > 0:
                done += 1
                if done % 5 == 0:
                    logger.info("已处理 %s, 最新 %s (%s 行)", done, d, stats["rows"])
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.warning("%s 失败: %s", d, e)

    logger.info("回填完成: done=%s skipped=%s failed=%s", done, skipped, failed)


if __name__ == '__main__':
    main()
