"""模型序列化与版本管理。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from zplan_shared.config import ZPLAN_ROOT

logger = logging.getLogger(__name__)

MODEL_DIR = ZPLAN_ROOT / "patterns" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def save_model(
    model: Any,
    version: str,
    metadata: dict[str, Any],
    *,
    feature_names: list[str] | None = None,
) -> Path:
    """保存模型及元数据。

    Parameters
    ----------
    model : 训练好的模型对象（LightGBM Booster / sklearn estimator）
    version : 版本号（如 "v1"）
    metadata : 包含训练配置、评估指标等
    feature_names : 特征名列表

    Returns
    -------
    Path to model directory
    """
    version_dir = MODEL_DIR / version
    version_dir.mkdir(parents=True, exist_ok=True)

    # 保存模型
    model_path = version_dir / f"{version}_approach_a.lgb"
    joblib.dump(model, model_path)
    logger.info("模型已保存: %s", model_path)

    # 保存元数据
    meta = {
        "version": version,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "model_file": str(model_path.name),
        **metadata,
    }
    if feature_names:
        meta["feature_names"] = feature_names

    meta_path = version_dir / f"{version}_metadata.json"
    meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    logger.info("元数据已保存: %s", meta_path)

    return version_dir


def load_model(version: str = "v1") -> tuple[Any, dict[str, Any]]:
    """加载模型及元数据（带内存缓存）。

    Returns
    -------
    (model, metadata_dict)
    """
    version_dir = MODEL_DIR / version
    if not version_dir.is_dir():
        raise FileNotFoundError(f"模型版本不存在: {version_dir}")

    meta_path = version_dir / f"{version}_metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"元数据不存在: {meta_path}")

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    model_file = metadata.get("model_file", f"{version}_approach_a.lgb")
    model_path = version_dir / model_file

    model = joblib.load(model_path)
    logger.info("模型已加载: %s (version=%s)", model_path, version)

    return model, metadata


def save_scaler(scaler: Any, version: str, name: str = "feature_scaler") -> Path:
    """保存特征缩放器。"""
    version_dir = MODEL_DIR / version
    version_dir.mkdir(parents=True, exist_ok=True)
    path = version_dir / f"{version}_{name}.pkl"
    joblib.dump(scaler, path)
    return path


def load_scaler(version: str, name: str = "feature_scaler") -> Any | None:
    """加载特征缩放器。"""
    path = MODEL_DIR / version / f"{version}_{name}.pkl"
    if not path.exists():
        return None
    return joblib.load(path)


def list_versions() -> list[str]:
    """列出所有已保存的模型版本。"""
    if not MODEL_DIR.is_dir():
        return []
    return sorted(
        [d.name for d in MODEL_DIR.iterdir() if d.is_dir() and (d / f"{d.name}_metadata.json").exists()]
    )
