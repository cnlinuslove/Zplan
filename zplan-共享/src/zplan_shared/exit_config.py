"""从 strategy.yaml 加载出场配置。

用法::

    from zplan_shared.exit_config import load_exit_config, ExitConfig

    config = load_exit_config()                        # 默认路径
    config = load_exit_config("config/strategy.yaml")   # 自定义路径

    # 获取默认方案
    plan = config.get_default_plan()
    # 获取指定方案
    plan = config.get_plan("atr_trail_2x")
    # 列出所有可用方案
    for key, plan in config.plans.items():
        print(key, plan.display_name)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from zplan_shared.exit_strategy import (
    ExitPlan,
    ExitRule,
    ExitType,
    build_exit_plan_from_config,
)


@dataclass
class ExitSweepConfig:
    """参数网格搜索配置。"""
    enabled: bool = False
    trailing_pct: list[float] = field(default_factory=lambda: [5, 8, 10, 12, 15])
    atr_multiplier: list[float] = field(default_factory=lambda: [1.5, 2.0, 2.5, 3.0])
    max_holding_days: list[int] = field(default_factory=lambda: [5, 10, 15, 20])


@dataclass
class ExitConfig:
    """出场策略配置（对应 strategy.yaml 的 ``exit`` 段）。"""

    default_plan: str = "static"
    atr_period: int = 14
    plans: dict[str, ExitPlan] = field(default_factory=dict)
    plans_raw: dict[str, dict[str, Any]] = field(default_factory=dict)
    sweep: ExitSweepConfig = field(default_factory=ExitSweepConfig)

    def get_default_plan(self) -> ExitPlan:
        """获取默认出场方案（配置缺失时回退到静态止损止盈）。"""
        if self.default_plan in self.plans:
            return self.plans[self.default_plan]
        return ExitPlan.static_default()

    def get_plan(self, plan_key: str) -> ExitPlan | None:
        """按 key 获取出场方案。"""
        return self.plans.get(plan_key)

    def list_plans(self) -> list[str]:
        """列出所有可用方案 key。"""
        return list(self.plans.keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_plan": self.default_plan,
            "atr_period": self.atr_period,
            "plans": {k: v.to_dict() for k, v in self.plans.items()},
        }


def load_exit_config(path: Path | str | None = None) -> ExitConfig:
    """从 strategy.yaml 加载 exit 段。

    不传 path 时尝试多个默认位置。
    """
    if path is not None:
        p = Path(path)
        if not p.is_file():
            return ExitConfig()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    else:
        # 尝试多个可能路径
        candidates = [
            Path(__file__).resolve().parents[3] / "zplan-选股" / "config" / "strategy.yaml",
            Path("config/strategy.yaml"),
        ]
        data = {}
        for cand in candidates:
            if cand.is_file():
                data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                break
        if not data:
            return ExitConfig()

    exit_section = data.get("exit") or {}
    if not exit_section:
        return ExitConfig()

    config = ExitConfig(
        default_plan=str(exit_section.get("default_plan", "static")),
        atr_period=int(exit_section.get("atr_period", 14)),
    )

    # 解析各方案
    plans_raw = exit_section.get("plans") or {}
    config.plans_raw = plans_raw
    for plan_key, plan_cfg in plans_raw.items():
        plan = build_exit_plan_from_config(plan_key, plan_cfg)
        if plan is not None:
            config.plans[plan_key] = plan

    # 解析 sweep 配置
    sweep_cfg = exit_section.get("optimization") or {}
    if sweep_cfg:
        sweep_params = sweep_cfg.get("sweep_params") or {}
        config.sweep = ExitSweepConfig(
            enabled=bool(sweep_cfg.get("enabled", False)),
            trailing_pct=[float(x) for x in sweep_params.get("trailing_pct", [5, 8, 10, 12, 15])],
            atr_multiplier=[float(x) for x in sweep_params.get("atr_multiplier", [1.5, 2.0, 2.5, 3.0])],
            max_holding_days=[int(x) for x in sweep_params.get("max_holding_days", [5, 10, 15, 20])],
        )

    return config


def generate_sweep_plans(config: ExitConfig) -> list[tuple[str, ExitPlan]]:
    """从 sweep 配置生成待比较的方案列表。

    Returns:
        list of (label, ExitPlan) tuples.
    """
    plans: list[tuple[str, ExitPlan]] = []

    # 始终包含默认方案作为 baseline
    plans.append(("static (baseline)", config.get_default_plan()))

    sc = config.sweep

    # 生成 trailing_stop 变体
    for trail_pct in sc.trailing_pct:
        for days in sc.max_holding_days:
            pct_key = f"trail_{int(trail_pct)}pct_{days}d"
            pct = trail_pct / 100.0
            plan = ExitPlan(
                plan_key=pct_key,
                display_name=f"移动止盈 {trail_pct:.0f}% / {days}天",
                rules=[
                    ExitRule(
                        rule_type=ExitType.TRAILING_STOP,
                        priority=1,
                        params={"trail_pct": pct},
                        activate_after_min_return=0.03,
                    ),
                    ExitRule(
                        rule_type=ExitType.TIME_EXIT,
                        priority=10,
                        params={"max_holding_days": days},
                    ),
                ],
            )
            plans.append((pct_key, plan))

    # 生成 atr_trail 变体
    for atr_mult in sc.atr_multiplier:
        for days in sc.max_holding_days:
            key = f"atr_{atr_mult}x_{days}d"
            plan = ExitPlan(
                plan_key=key,
                display_name=f"ATR {atr_mult}× / {days}天",
                rules=[
                    ExitRule(
                        rule_type=ExitType.ATR_TRAIL,
                        priority=1,
                        params={"atr_multiplier": atr_mult, "atr_period": config.atr_period},
                        activate_after_min_return=0.02,
                    ),
                    ExitRule(
                        rule_type=ExitType.TIME_EXIT,
                        priority=10,
                        params={"max_holding_days": days},
                    ),
                ],
            )
            plans.append((key, plan))

    # 生成 ma_stop 变体 (MA20 only, varying days)
    for days in sc.max_holding_days:
        key = f"ma20_{days}d"
        plan = ExitPlan(
            plan_key=key,
            display_name=f"MA20 止损 / {days}天",
            rules=[
                ExitRule(
                    rule_type=ExitType.MA_STOP,
                    priority=2,
                    params={"ma_period": 20, "exit_on_cross_below": True},
                ),
                ExitRule(
                    rule_type=ExitType.STATIC_STOP,
                    priority=0,
                    params={"stop_pct": -0.05},
                ),
                ExitRule(
                    rule_type=ExitType.TIME_EXIT,
                    priority=10,
                    params={"max_holding_days": days},
                ),
            ],
        )
        plans.append((key, plan))

    return plans
