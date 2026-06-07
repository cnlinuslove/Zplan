"""Z-Plan Web 应用 — FastAPI 入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import CORS_ORIGINS, FRONTEND_DIST, HOST, PORT

# ── 日志 ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("zplan-web")


# ── 生命周期 ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Z-Plan Web 启动中...")
    # 延迟导入，避免启动时连不上数据库就崩
    from zplan_shared.models import init_db

    init_db()
    logger.info("数据库已就绪")
    yield
    logger.info("Z-Plan Web 关闭")


# ── FastAPI 应用 ──
app = FastAPI(
    title="Z-Plan Web",
    description="A 股量化多 Agent Web 控制台",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — 开发时允许 Vite dev server 跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 路由注册 ──
@app.get("/api/health")
async def health():
    from zplan_shared.config import DB_URL as SHARED_DB_URL

    return {
        "ok": True,
        "service": "zplan-web",
        "db_url": SHARED_DB_URL.split("///")[-1] if "///" in SHARED_DB_URL else SHARED_DB_URL,
        "llm_model": __import__("config").DEEPSEEK_MODEL,
    }


from api.chat import router as chat_router
from api.market import router as market_router
from api.watchlist import router as watchlist_router
from api.picks import router as picks_router
from api.dashboard import router as dashboard_router

app.include_router(chat_router, prefix="/api/v1")
app.include_router(market_router, prefix="/api/v1")
app.include_router(watchlist_router, prefix="/api/v1")
app.include_router(picks_router, prefix="/api/v1")
app.include_router(dashboard_router, prefix="/api/v1")

# 静态文件（前端 build 产物）—— 必须在最后
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    logger.info(f"前端静态文件: {FRONTEND_DIST}")
else:
    logger.info("前端未构建（frontend/dist/ 不存在），仅 API 可用")


# ── 入口 ──
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
