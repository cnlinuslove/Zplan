"""模型评估 — 分类指标、混淆矩阵、特征重要性、vs 规则引擎基线对比。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import (
    auc,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)

logger = logging.getLogger(__name__)

LABEL_NAMES = {
    0: "REAL_RALLY",
    1: "BULL_TRAP",
    2: "REAL_BREAKDOWN",
    3: "PANIC_SHAKEOUT",
}


@dataclass
class EvalResult:
    """评估结果容器。"""

    accuracy: float
    auc_ovr: float | None  # One-vs-Rest AUC (多分类)
    per_class: dict[str, dict[str, float]]  # precision / recall / f1 per class
    confusion: list[list[int]]
    feature_importance: list[tuple[str, float]]  # top-20
    summary: str


def evaluate_multiclass(
    model,
    X_test: np.ndarray,
    y_test: np.ndarray,
    feature_names: list[str] | None = None,
) -> EvalResult:
    """评估四分类模型。

    Returns
    -------
    EvalResult
    """
    y_pred = model.predict(X_test)
    # LightGBM 多分类返回 logits → 取 argmax；或直接类别
    if y_pred.ndim > 1:
        y_pred_class = np.argmax(y_pred, axis=1)
        y_proba = y_pred
    else:
        y_pred_class = y_pred.astype(np.int64)
        y_proba = None

    accuracy = float(np.mean(y_pred_class == y_test))

    # 每类指标
    per_class: dict[str, dict[str, float]] = {}
    for c in sorted(np.unique(y_test)):
        name = LABEL_NAMES.get(int(c), f"class_{c}")
        per_class[name] = {
            "precision": round(precision_score(y_test == c, y_pred_class == c, zero_division=0), 4),
            "recall": round(recall_score(y_test == c, y_pred_class == c, zero_division=0), 4),
            "f1": round(f1_score(y_test == c, y_pred_class == c, zero_division=0), 4),
            "support": int((y_test == c).sum()),
        }

    # AUC (one-vs-rest)
    auc_ovr = None
    if y_proba is not None and y_proba.shape[1] >= 2:
        try:
            auc_ovr = round(roc_auc_score(y_test, y_proba, multi_class="ovr", average="weighted"), 4)
        except Exception:
            pass

    # 混淆矩阵
    cm = confusion_matrix(y_test, y_pred_class).tolist()

    # 特征重要性
    importance = []
    if hasattr(model, "feature_importance"):
        fi = model.feature_importance(importance_type="gain")
        if feature_names and len(feature_names) >= len(fi):
            named = sorted(
                zip(feature_names[: len(fi)], fi), key=lambda x: x[1], reverse=True
            )
        else:
            named = [(f"f{i}", float(v)) for i, v in enumerate(fi)]
        importance = [(n, round(float(v), 2)) for n, v in named[:20]]

    # 文本摘要
    summary_lines = [
        f"Accuracy: {accuracy:.4f}",
        f"AUC (OvR): {auc_ovr}" if auc_ovr else "AUC: N/A",
        "",
        "Per-class metrics:",
    ]
    for cls_name, metrics in per_class.items():
        summary_lines.append(
            f"  {cls_name:20s}  P={metrics['precision']:.3f}  "
            f"R={metrics['recall']:.3f}  F1={metrics['f1']:.3f}  "
            f"n={metrics['support']}"
        )
    summary_lines.append("")
    summary_lines.append("Top-10 features (by gain):")
    for name, gain in importance[:10]:
        summary_lines.append(f"  {name:40s} {gain:8.2f}")

    return EvalResult(
        accuracy=accuracy,
        auc_ovr=auc_ovr,
        per_class=per_class,
        confusion=cm,
        feature_importance=importance,
        summary="\n".join(summary_lines),
    )


def evaluate_binary(
    model,
    X_test: np.ndarray,
    y_test: np.ndarray,
    feature_names: list[str] | None = None,
    positive_class: int = 1,
) -> dict[str, Any]:
    """评估二分类模型。

    Returns
    -------
    dict with accuracy, auc, precision, recall, f1, confusion, feature_importance
    """
    y_proba = model.predict(X_test)
    y_pred = (y_proba > 0.5).astype(int)

    accuracy = float(np.mean(y_pred == y_test))

    try:
        auc_val = roc_auc_score(y_test, y_proba)
    except Exception:
        auc_val = None

    precision = precision_score(y_test, y_pred, zero_division=0)
    recall = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)

    cm = confusion_matrix(y_test, y_pred).tolist()

    importance = []
    if hasattr(model, "feature_importance"):
        fi = model.feature_importance(importance_type="gain")
        if feature_names and len(feature_names) >= len(fi):
            named = sorted(
                zip(feature_names[: len(fi)], fi), key=lambda x: x[1], reverse=True
            )
        else:
            named = [(f"f{i}", float(v)) for i, v in enumerate(fi)]
        importance = [(n, round(float(v), 2)) for n, v in named[:20]]

    return {
        "accuracy": round(accuracy, 4),
        "auc": round(float(auc_val), 4) if auc_val else None,
        "precision": round(float(precision), 4),
        "recall": round(float(recall), 4),
        "f1": round(float(f1), 4),
        "confusion": cm,
        "feature_importance": importance,
    }


def evaluate_vs_rule_engine(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    rule_scores: np.ndarray | None = None,
) -> dict[str, Any]:
    """对比 ML 模型 vs 规则引擎的排序能力。

    rule_scores 可以是 quick_technical_score 或 composite_score。
    正类为 TRAP（BULL_TRAP=1 + PANIC_SHAKEOUT=3）。
    """
    # 将真实标签转为二分类：trap=1, non_trap=0
    y_trap = np.isin(y_true, [1, 3]).astype(int)

    if y_trap.sum() == 0 or (1 - y_trap).sum() == 0:
        return {"error": "only one class present in y_trap"}

    # ML 模型的 trap 概率（BULL_TRAP + PANIC_SHAKEOUT 的概率和）
    if y_proba.ndim > 1 and y_proba.shape[1] >= 4:
        ml_trap_proba = y_proba[:, 1] + y_proba[:, 3]  # BULL_TRAP + PANIC_SHAKEOUT
    else:
        ml_trap_proba = y_proba.flatten()

    ml_auc = roc_auc_score(y_trap, ml_trap_proba)

    result: dict[str, Any] = {
        "ml_auc": round(float(ml_auc), 4),
    }

    if rule_scores is not None:
        # 规则引擎分数：分数越高表示越看好 → 取负号作为 trap 指标
        # （低分=高风险=更像 trap）
        rule_clean = np.nan_to_num(rule_scores, nan=50.0)
        rule_trap_proba = -rule_clean  # negate: low score → high trap probability
        rule_auc = roc_auc_score(y_trap, rule_trap_proba)
        result["rule_auc"] = round(float(rule_auc), 4)
        result["delta_auc"] = round(float(ml_auc - rule_auc), 4)
        result["ml_wins"] = ml_auc > rule_auc

    return result


def feature_importance_report(
    model,
    feature_names: list[str],
    top_n: int = 30,
) -> str:
    """生成特征重要性报告（Markdown 表格）。"""
    if not hasattr(model, "feature_importance"):
        return "模型不支持特征重要性"

    fi = model.feature_importance(importance_type="gain")
    named = sorted(
        zip(feature_names[: len(fi)], fi), key=lambda x: x[1], reverse=True
    )

    lines = ["| # | Feature | Gain |", "|---|---------|------|"]
    for i, (name, gain) in enumerate(named[:top_n], 1):
        lines.append(f"| {i} | {name} | {gain:.2f} |")

    return "\n".join(lines)
