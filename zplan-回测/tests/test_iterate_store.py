"""迭代 store 单元测试。"""
from __future__ import annotations

from zplan_shared.pick_iterate_store import compare_iterations


def test_compare_improved_when_fail_rate_down():
    older = {"iteration_id": "a", "metrics": {"fail_rate": 1.0, "mean_fwd_return": -5.0}}
    newer = {"iteration_id": "b", "metrics": {"fail_rate": 0.6, "mean_fwd_return": -1.0}}
    c = compare_iterations(older, newer)
    assert c is not None
    assert c["deltas"]["fail_rate"] == -0.4
    assert c["improved"] is True
