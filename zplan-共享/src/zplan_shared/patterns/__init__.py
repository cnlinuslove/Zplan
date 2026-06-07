"""股价曲线模式学习 — 标签生成、数据集构建、模型训练、推理。

两阶段路线：
- 方案 A（纯图形）：仅从 OHLCV 序列学习
- 方案 B（多维）：图形 + 市值/板块/基本面等上下文

用法：
    from zplan_shared.patterns.labeling import detect_events, assign_labels
    from zplan_shared.patterns.dataset import build_event_dataset, temporal_split
"""

__all__ = [
    "LabelingConfig",
    "detect_events",
    "assign_labels",
    "build_event_dataset",
    "temporal_split",
]
