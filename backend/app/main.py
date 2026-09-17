import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1 import admin, auth, bili_market, profile
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.errors import AppError
from app.services.bili_market.client import BiliMarketClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.bili_client = BiliMarketClient(settings)
    await application.state.bili_client.start()
    yield
    await application.state.bili_client.close()


app = FastAPI(title="B站市集好价提示系统", version=settings.version_value, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.app_base_url.rstrip("/"), "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Requested-With"],
)


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            allowed = {settings.app_base_url.rstrip("/"), f"{request.url.scheme}://{request.url.netloc}"}
            if origin.rstrip("/") not in allowed:
                return JSONResponse(status_code=403, content={"error": {"code": "ORIGIN_FORBIDDEN", "message": "请求来源无效"}})
    return await call_next(request)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    payload = {"code": exc.code, "message": exc.message}
    if exc.details:
        payload["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content={"error": payload})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [{"loc": list(item.get("loc", ())), "msg": item.get("msg", "请求参数无效"), "type": item.get("type", "value_error")} for item in exc.errors()]
    code = "VALIDATION_ERROR"
    message = "请求参数无效"
    if any("check_interval_seconds" in detail["loc"] for detail in details):
        code, message = "INVALID_CHECK_INTERVAL", "请选择系统支持的监控频率"
    return JSONResponse(status_code=422, content={"error": {"code": code, "message": message, "details": details}})


@app.get("/api/health")
def health():
    db_ok = True
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "version": settings.version_value, "database": db_ok}


app.include_router(auth.router, prefix="/api/v1")
app.include_router(profile.router, prefix="/api/v1")
app.include_router(bili_market.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
