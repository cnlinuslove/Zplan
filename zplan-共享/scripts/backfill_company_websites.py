#!/usr/bin/env python3
"""用 Web Search 回填 company_profiles.website 字段。

用法::

    # 预览（不写库）
    cd zplan-共享 && .venv/bin/python scripts/backfill_company_websites.py --limit 10 --dry-run

    # 正式回填
    cd zplan-共享 && .venv/bin/python scripts/backfill_company_websites.py --limit 100

    # 全量回填（耗时较长，建议分批）
    cd zplan-共享 && .venv/bin/python scripts/backfill_company_websites.py
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

# 确保 zplan-共享 在 sys.path
_shared_src = Path(__file__).resolve().parents[1] / "src"
if str(_shared_src) not in sys.path:
    sys.path.insert(0, str(_shared_src))

from zplan_shared.web_search import search_company_website

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

DB_PATH = Path(__file__).resolve().parents[2] / "zplan-资讯" / "zplan.db"


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def main() -> None:
    parser = argparse.ArgumentParser(description="回填公司官网到 company_profiles.website")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 家公司（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写库")
    parser.add_argument("--sleep", type=float, default=0.5, help="搜索间隔秒数（默认 0.5）")
    args = parser.parse_args()

    db = get_db()

    # 确保 website 列存在
    existing_cols = {
        row[1]
        for row in db.execute("PRAGMA table_info(company_profiles)").fetchall()
    }
    if "website" not in existing_cols:
        print("[ERROR] company_profiles 无 website 列，请先更新 enrich_company.py")
        db.close()
        sys.exit(1)

    where = "WHERE website IS NULL OR website = '' OR website = 'None'"
    limit_clause = f"LIMIT {args.limit}" if args.limit > 0 else ""
    rows = db.execute(
        f"SELECT ts_code, full_name, short_name FROM company_profiles {where} "
        f"ORDER BY ts_code {limit_clause}"
    ).fetchall()

    total = len(rows)
    print(f"[INFO] 待处理: {total} 家公司")

    ok, fail, skip = 0, 0, 0
    for i, row in enumerate(rows, 1):
        code = row["ts_code"]
        name = row["short_name"] or row["full_name"] or code

        try:
            url = search_company_website(name)
        except Exception as exc:
            logger.warning("%s %s 搜索异常: %s", code, name, exc)
            fail += 1
            continue

        if url:
            if not args.dry_run:
                db.execute(
                    "UPDATE company_profiles SET website = ? WHERE ts_code = ?",
                    (url, code),
                )
                db.commit()
            print(f"[{i}/{total}] {code} {name} → {url}")
            ok += 1
        else:
            print(f"[{i}/{total}] {code} {name} → （未找到）")
            skip += 1

        if i < total and args.sleep > 0:
            time.sleep(args.sleep)

    db.close()
    print(f"\n[DONE] ok={ok} skip={skip} fail={fail} total={total}")


if __name__ == "__main__":
    main()
