"""Z-Plan Web 应用 — FastAPI 入口。"""

from __future__ import annotations

import json
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import CORS_ORIGINS, FRONTEND_DIST, HOST, PORT


class SafeJSONEncoder(json.JSONEncoder):
    """将 NaN/Inf 转为 null 的 JSON 编码器。"""

    def encode(self, o):
        return super().encode(self._sanitize(o))

    def iterencode(self, o, _one_shot=False):
        return super().iterencode(self._sanitize(o), _one_shot)

    def _sanitize(self, obj):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        if isinstance(obj, dict):
            return {k: self._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._sanitize(v) for v in obj]
        return obj


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            cls=SafeJSONEncoder,
        ).encode("utf-8")

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
    default_response_class=SafeJSONResponse,
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

# 静态资源（JS/CSS 等）—— 必须在 SPA 兜底之前注册
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")
    favicon_path = FRONTEND_DIST / "favicon.svg"
    if favicon_path.exists():
        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon():
            return FileResponse(favicon_path)
    logger.info(f"前端静态文件: {FRONTEND_DIST}")
else:
    logger.info("前端未构建（frontend/dist/ 不存在），仅 API 可用")


# SPA 兜底：非 /api、非 /assets 的 GET → index.html
@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    """把前端路由（/picks, /market/xxx）交给 React Router。"""
    if full_path.startswith("api/") or full_path.startswith("assets/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "Not Found"}, status_code=404)


# ── 入口 ──
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
