#!/usr/bin/env python3
"""股价日更完成后触发选股 pipeline（init-rule → llm-top）。"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from zplan_shared.market_health import check_market_health
from zplan_shared.models import init_db

logger = logging.getLogger(__name__)


def _mono_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _notify(text: str) -> bool:
    news = _mono_root() / "zplan-资讯"
    if str(news) not in sys.path:
        sys.path.insert(0, str(news))
    try:
        from wechat_push import push_wechat_text  # type: ignore[import-untyped]

        return push_wechat_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("企微通知失败: %s", exc)
        return False


def _run_pick_pipeline(*, top: int, skip_health: bool, no_llm: bool) -> int:
    pick_root = _mono_root() / "zplan-选股"
    py = pick_root / ".venv/bin/python"
    if not py.is_file():
        raise FileNotFoundError(f"缺少选股 venv: {py}")

    cmd = [str(py), str(pick_root / "main.py"), "pipeline", "--top", str(top)]
    if skip_health:
        cmd.append("--skip-health-check")
    if no_llm:
        cmd.extend(["--no-llm"])

    env = os.environ.copy()
    news = _mono_root() / "zplan-资讯"
    env["ZPLAN_ROOT"] = str(news)
    env["PATH"] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"

    logger.info("执行: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(pick_root), env=env, check=False)
    return int(proc.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="股价完成后启动选股 pipeline")
    parser.add_argument("--top", type=int, default=int(os.getenv("PICK_PIPELINE_TOP", "300")))
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--notify", action="store_true", help="企微推送开始/结束")
    parser.add_argument(
        "--min-panel",
        type=int,
        default=int(os.getenv("PICK_MIN_PANEL_ROWS", "4000")),
        help="低于此截面则退出（不跑选股）",
    )
    parser.add_argument(
        "--allow-skip-health",
        action="store_true",
        default=os.getenv("PICK_ALLOW_SKIP_HEALTH", "true").lower() == "true",
        help="截面不足 min-panel 但>=3000 时带 --skip-health-check 仍跑",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_db()

    health = check_market_health(production=True)
    skip_health = False
    if not health.ok:
        if health.panel_rows < args.min_panel:
            if args.allow_skip_health and health.panel_rows >= 3000:
                skip_health = True
                logger.warning(
                    "截面 %s < %s，带 --skip-health-check 继续 pipeline",
                    health.panel_rows,
                    args.min_panel,
                )
            else:
                logger.error("行情未就绪: %s", health.message)
                if args.notify:
                    _notify(
                        f"[Z-Plan] 股价已跑完但截面不足（{health.panel_rows}<{args.min_panel}），"
                        "选股 pipeline 暂缓。"
                    )
                raise SystemExit(2)
        else:
            logger.warning("门禁未通过但截面规模足够，继续 pipeline: %s", health.message)

    msg_start = (
        f"[Z-Plan] 股价更新完成，开始选股 pipeline（Top{args.top}）\n"
        f"最新交易日 {health.latest}，截面 {health.panel_rows} 只"
    )
    logger.info("%s", msg_start.replace("\n", " | "))
    if args.notify:
        _notify(msg_start)

    code = _run_pick_pipeline(top=args.top, skip_health=skip_health, no_llm=args.no_llm)
    if code == 0:
        tail = f"[Z-Plan] 选股 pipeline 完成（Top{args.top}）。查看：cd zplan-选股 && .venv/bin/python main.py --list-runs"
        logger.info(tail)
        if args.notify:
            _notify(tail)
    else:
        err = f"[Z-Plan] 选股 pipeline 失败 exit={code}"
        logger.error(err)
        if args.notify:
            _notify(err)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
