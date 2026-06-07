"""LightGBM 模式分类器 — 方案 A 和方案 B 的训练 wrapper。

提供：
- 四分类模型（REAL_RALLY / BULL_TRAP / REAL_BREAKDOWN / PANIC_SHAKEOUT）
- 二分类模型（上升后：真涨/诱多；下跌后：真崩/洗盘）
- 类别权重 + 简单 SMOTE 过采样处理不平衡
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np

logger = logging.getLogger(__name__)

LABEL_NAMES = {
    0: "REAL_RALLY",
    1: "BULL_TRAP",
    2: "REAL_BREAKDOWN",
    3: "PANIC_SHAKEOUT",
}


def smote_oversample(
    X: np.ndarray,
    y: np.ndarray,
    *,
    sampling_strategy: dict[int, int] | None = None,
    k_neighbors: int = 5,
    random_state: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """简单的 SMOTE 实现 — 对少数类合成新样本。

    Parameters
    ----------
    X : 特征矩阵 (n_samples, n_features)
    y : 标签 (n_samples,)
    sampling_strategy : 目标样本数 {class_label: target_count}
    k_neighbors : KNN 的 K 值

    Returns
    -------
    (X_resampled, y_resampled)
    """
    rng = np.random.RandomState(random_state)
    unique, counts = np.unique(y, return_counts=True)

    if sampling_strategy is None:
        max_count = counts.max()
        sampling_strategy = {
            int(cls): max_count for cls, cnt in zip(unique, counts) if cnt < max_count
        }

    X_list = [X]
    y_list = [y]

    for cls, target in sampling_strategy.items():
        mask = y == cls
        X_cls = X[mask]
        n_current = len(X_cls)
        n_synthetic = target - n_current

        if n_synthetic <= 0:
            continue

        # KNN
        k = min(k_neighbors, n_current - 1)
        if k < 1:
            continue

        # 简单欧氏距离找近邻
        from sklearn.neighbors import NearestNeighbors
        nn = NearestNeighbors(n_neighbors=k + 1, metric='euclidean')
        nn.fit(X_cls)
        _, indices = nn.kneighbors(X_cls)

        synthetic = np.zeros((n_synthetic, X.shape[1]), dtype=X.dtype)
        for i in range(n_synthetic):
            idx = rng.randint(0, n_current)
            neighbor_idx = indices[idx, rng.randint(1, k + 1)]  # 排除自身
            diff = X_cls[neighbor_idx] - X_cls[idx]
            gap = rng.random()
            synthetic[i] = X_cls[idx] + gap * diff

        X_list.append(synthetic)
        y_list.append(np.full(n_synthetic, cls, dtype=y.dtype))

    return np.vstack(X_list), np.concatenate(y_list)


@dataclass
class LightGBMConfig:
    """LightGBM 训练参数。"""

    objective: str = "multiclass"
    num_class: int = 4
    metric: str = "multi_logloss"
    boosting_type: str = "gbdt"
    num_leaves: int = 31
    learning_rate: float = 0.05
    feature_fraction: float = 0.8
    bagging_fraction: float = 0.8
    bagging_freq: int = 5
    min_data_in_leaf: int = 20
    lambda_l1: float = 0.01
    lambda_l2: float = 0.01
    num_boost_round: int = 500
    early_stopping_rounds: int = 50
    verbosity: int = -1
    random_state: int = 42
    extra_params: dict[str, Any] = field(default_factory=dict)


def train_multiclass(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    config: LightGBMConfig | None = None,
    class_weight: dict[int, float] | None = None,
    sample_weight: np.ndarray | None = None,
) -> lgb.Booster:
    """训练四分类 LightGBM 模型。

    Parameters
    ----------
    X_train, y_train : 训练数据（y 为 0-3 的整数标签）
    X_val, y_val : 验证数据
    config : 训练配置
    class_weight : 类别权重 dict（如 {1: 2.0} 增加 BULL_TRAP 的权重）
    sample_weight : 逐样本权重（与 SMOTE 配合使用）

    Returns
    -------
    lgb.Booster
    """
    cfg = config or LightGBMConfig()

    params = {
        "objective": cfg.objective,
        "num_class": cfg.num_class,
        "metric": cfg.metric,
        "boosting_type": cfg.boosting_type,
        "num_leaves": cfg.num_leaves,
        "learning_rate": cfg.learning_rate,
        "feature_fraction": cfg.feature_fraction,
        "bagging_fraction": cfg.bagging_fraction,
        "bagging_freq": cfg.bagging_freq,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "lambda_l1": cfg.lambda_l1,
        "lambda_l2": cfg.lambda_l2,
        "verbosity": cfg.verbosity,
        "random_state": cfg.random_state,
        **cfg.extra_params,
    }

    # 类别权重 → sample_weight（LightGBM 不支持 class_weight 参数）
    sw = sample_weight
    if class_weight and sw is None:
        sw = np.ones(len(y_train), dtype=np.float64)
        for c, w in class_weight.items():
            sw[y_train == c] = w

    train_data = lgb.Dataset(
        X_train, label=y_train, weight=sw,
    )
    val_data = lgb.Dataset(
        X_val, label=y_val, reference=train_data,
    )

    logger.info(
        "开始训练: n_train=%d, n_val=%d, n_features=%d, n_classes=%d",
        X_train.shape[0], X_val.shape[0], X_train.shape[1], cfg.num_class,
    )

    model = lgb.train(
        params,
        train_data,
        valid_sets=[train_data, val_data],
        valid_names=["train", "val"],
        num_boost_round=cfg.num_boost_round,
        callbacks=[
            lgb.early_stopping(cfg.early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )

    logger.info("训练完成: best_iteration=%d", model.best_iteration)
    return model


def train_binary_upward(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    config: LightGBMConfig | None = None,
    class_weight: dict[int, float] | None = None,
) -> lgb.Booster:
    """训练二分类模型：上涨后 → 真涨(0) vs 诱多(1)。

    只对 event_type='peak' 的样本训练。
    """
    cfg = config or LightGBMConfig()

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting_type": cfg.boosting_type,
        "num_leaves": cfg.num_leaves,
        "learning_rate": cfg.learning_rate,
        "feature_fraction": cfg.feature_fraction,
        "bagging_fraction": cfg.bagging_fraction,
        "bagging_freq": cfg.bagging_freq,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "lambda_l1": cfg.lambda_l1,
        "lambda_l2": cfg.lambda_l2,
        "verbosity": cfg.verbosity,
        "random_state": cfg.random_state,
        **cfg.extra_params,
    }

    if class_weight:
        params["scale_pos_weight"] = class_weight.get(1, 1.0) / max(class_weight.get(0, 1.0), 1e-8)

    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    logger.info(
        "训练二分类(上涨): n_train=%d, n_val=%d",
        X_train.shape[0], X_val.shape[0],
    )

    model = lgb.train(
        params,
        train_data,
        valid_sets=[train_data, val_data],
        valid_names=["train", "val"],
        num_boost_round=cfg.num_boost_round,
        callbacks=[
            lgb.early_stopping(cfg.early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )

    logger.info("训练完成: best_iteration=%d", model.best_iteration)
    return model


def train_binary_downward(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    config: LightGBMConfig | None = None,
    class_weight: dict[int, float] | None = None,
) -> lgb.Booster:
    """训练二分类模型：下跌后 → 真崩(0) vs 洗盘(1)。

    只对 event_type='trough' 的样本训练。
    """
    cfg = config or LightGBMConfig()

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting_type": cfg.boosting_type,
        "num_leaves": cfg.num_leaves,
        "learning_rate": cfg.learning_rate,
        "feature_fraction": cfg.feature_fraction,
        "bagging_fraction": cfg.bagging_fraction,
        "bagging_freq": cfg.bagging_freq,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "lambda_l1": cfg.lambda_l1,
        "lambda_l2": cfg.lambda_l2,
        "verbosity": cfg.verbosity,
        "random_state": cfg.random_state,
        **cfg.extra_params,
    }

    if class_weight:
        params["scale_pos_weight"] = class_weight.get(1, 1.0) / max(class_weight.get(0, 1.0), 1e-8)

    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    logger.info(
        "训练二分类(下跌): n_train=%d, n_val=%d",
        X_train.shape[0], X_val.shape[0],
    )

    model = lgb.train(
        params,
        train_data,
        valid_sets=[train_data, val_data],
        valid_names=["train", "val"],
        num_boost_round=cfg.num_boost_round,
        callbacks=[
            lgb.early_stopping(cfg.early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )

    return model


def compute_class_weights(y: np.ndarray) -> dict[int, float]:
    """根据类别频率计算平衡权重（inverse frequency）。"""
    unique, counts = np.unique(y, return_counts=True)
    total = len(y)
    weights = {int(u): total / (len(unique) * float(c)) for u, c in zip(unique, counts)}
    return weights


def train_regressor(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    *,
    config: LightGBMConfig | None = None,
    sample_weight: np.ndarray | None = None,
) -> lgb.Booster:
    """训练 LightGBM 回归模型 — 预测连续 forward return。

    回归 vs 分类的核心差异：
    - 分类问"它属于哪一类？"→ 模型学会猜多数类
    - 回归问"它能涨多少？"→ 模型被迫学排序，即使数值不准，
      只要排序比随机好，就能用来选 Top N 股票。

    Parameters
    ----------
    X_train, y_train : 训练数据（y 为连续 forward return，如 +5.2, -3.1）
    X_val, y_val : 验证数据
    config : 训练配置
    sample_weight : 逐样本权重（给近期事件更高权重等）

    Returns
    -------
    lgb.Booster
    """
    cfg = config or LightGBMConfig()

    params = {
        "objective": "regression",
        "metric": "rmse",
        "boosting_type": cfg.boosting_type,
        "num_leaves": cfg.num_leaves,
        "learning_rate": cfg.learning_rate,
        "feature_fraction": cfg.feature_fraction,
        "bagging_fraction": cfg.bagging_fraction,
        "bagging_freq": cfg.bagging_freq,
        "min_data_in_leaf": cfg.min_data_in_leaf,
        "lambda_l1": cfg.lambda_l1,
        "lambda_l2": cfg.lambda_l2,
        "verbosity": cfg.verbosity,
        "random_state": cfg.random_state,
        **cfg.extra_params,
    }

    train_data = lgb.Dataset(X_train, label=y_train, weight=sample_weight)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    logger.info(
        "训练回归: n_train=%d, n_val=%d, y_mean=%.2f, y_std=%.2f",
        X_train.shape[0], X_val.shape[0],
        float(y_train.mean()), float(y_train.std()),
    )

    model = lgb.train(
        params,
        train_data,
        valid_sets=[train_data, val_data],
        valid_names=["train", "val"],
        num_boost_round=cfg.num_boost_round,
        callbacks=[
            lgb.early_stopping(cfg.early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )

    logger.info("训练完成: best_iteration=%d", model.best_iteration)
    return model


def evaluate_ranking(
    model: lgb.Booster,
    X_test: np.ndarray,
    y_test: np.ndarray,
    *,
    top_n: int = 20,
) -> dict:
    """评估回归模型的排序质量。

    关键指标：
    - Spearman ρ: 预测排序与真实收益的秩相关性
    - IC (Information Coefficient): Pearson 相关系数
    - Top-N Return: 预测 Top N 的平均实际收益 vs 全市场平均
    - Top vs Bottom spread: 预测最好 vs 最差的收益差

    Returns
    -------
    dict with spearman_r, ic, top_return, bottom_return, spread, hit_rate
    """
    y_pred = model.predict(X_test)

    from scipy import stats as _stats
    import numpy as np

    # Spearman 秩相关
    spearman_r, spearman_p = _stats.spearmanr(y_test, y_pred)

    # Pearson IC
    ic = float(np.corrcoef(y_test, y_pred)[0, 1])

    # 按预测排序
    order = np.argsort(y_pred)[::-1]  # 预测最高的在前
    top_idx = order[:top_n]
    bottom_idx = order[-top_n:]

    top_return = float(np.mean(y_test[top_idx]))
    bottom_return = float(np.mean(y_test[bottom_idx]))
    all_mean = float(np.mean(y_test))

    # Top-N 中真涨（正收益）的比例
    hit_rate = float(np.mean(y_test[top_idx] > 0))

    # 按预测分位数分组看收益单调性
    n_bins = 5
    bin_returns = []
    for i in range(n_bins):
        start = i * len(y_test) // n_bins
        end = (i + 1) * len(y_test) // n_bins
        bin_idx = order[start:end]
        bin_returns.append(float(np.mean(y_test[bin_idx])))

    return {
        "spearman_r": round(float(spearman_r), 4),
        "spearman_p": round(float(spearman_p), 6),
        "ic": round(ic, 4),
        "top_return": round(top_return, 4),
        "bottom_return": round(bottom_return, 4),
        "all_mean": round(all_mean, 4),
        "spread": round(top_return - bottom_return, 4),
        "hit_rate": round(hit_rate, 4),
        "bin_returns": [round(r, 4) for r in bin_returns],
    }
