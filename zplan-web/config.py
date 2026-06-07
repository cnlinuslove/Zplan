"""Z-Plan Web 配置（独立于 zplan_shared.config，Web 专属）。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ── 复用 zplan_shared 的 ZPLAN_ROOT 解析逻辑 ──
# 先加载 .env，让 zplan_shared.config 能读到 DEEPSEEK_API_KEY 等变量
_PROJECT_ROOT = Path(__file__).resolve().parent
_MONO_ROOT = _PROJECT_ROOT.parent

# 尝试加载 zplan-资讯/.env（主配置文件）
_ENV_PATH = _MONO_ROOT / "zplan-资讯" / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
load_dotenv()  # 也加载当前目录的 .env（覆盖用）

# ── 路径 ──
ZPLAN_ROOT = Path(os.getenv("ZPLAN_ROOT", str(_MONO_ROOT / "zplan-资讯"))).resolve()
WEB_ROOT = _PROJECT_ROOT
FRONTEND_DIST = WEB_ROOT / "frontend" / "dist"

# ── 数据库 ──
# 和 zplan_shared 共用同一个 SQLite
DB_URL = os.getenv("DB_URL", f"sqlite:///{ZPLAN_ROOT / 'zplan.db'}")

# ── LLM ──
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_BASE_URL = os.getenv("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MAX_OUTPUT_TOKENS = int(os.getenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "8192"))
DEEPSEEK_TIMEOUT_SECONDS = int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "90"))

# ── Web 专属 ──
HOST = os.getenv("WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_PORT", "8000"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:8000").split(",")

# LLM 缓存 TTL（秒）
LLM_CACHE_TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL_SECONDS", "3600"))

# 后台任务最大并发
MAX_BG_TASKS = int(os.getenv("MAX_BG_TASKS", "3"))
