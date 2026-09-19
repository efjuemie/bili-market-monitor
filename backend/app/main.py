import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1 import admin, auth, bili_market, notifications, profile
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
    details = [
        {
            "loc": list(item.get("loc", ())),
            "msg": _localized_validation_message(item),
            "type": item.get("type", "value_error"),
        }
        for item in exc.errors()
    ]
    code = "VALIDATION_ERROR"
    message = details[0]["msg"] if details else "请求参数无效"
    if any("check_interval_seconds" in detail["loc"] for detail in details):
        code, message = "INVALID_CHECK_INTERVAL", "请选择系统支持的监控频率"
    return JSONResponse(status_code=422, content={"error": {"code": code, "message": message, "details": details}})


def _localized_validation_message(error: dict) -> str:
    """Return safe Chinese validation text instead of exposing Pydantic English."""
    loc = error.get("loc", ())
    field = loc[-1] if loc else None
    error_type = error.get("type", "")
    context = error.get("ctx") or {}
    labels = {
        "username": "用户名",
        "password": "密码",
        "cluster_id": "商品ID",
        "email": "邮箱",
        "code": "验证码",
        "check_interval_seconds": "监控频率",
    }
    label = labels.get(field)
    if field == "username" and error_type == "string_too_short":
        return "用户名至少需要3个字符"
    if field == "username" and error_type == "string_too_long":
        return "用户名不能超过32个字符"
    if field == "password" and error_type == "string_too_short":
        return "密码至少需要8个字符"
    if field == "password" and error_type == "string_too_long":
        return "密码不能超过128个字符"
    if field == "cluster_id" and error_type in {"int_parsing", "int_type", "int_from_float"}:
        return "商品ID格式不正确"
    if field == "cluster_id" and error_type in {"greater_than", "less_equal", "greater_than_equal", "less_than"}:
        return "商品ID必须是正整数且不超过9999999999999"
    if error_type == "missing" and label:
        return f"请输入{label}"
    if error_type in {"string_type", "string_unicode"} and label:
        return f"{label}必须是文本"
    raw_message = error.get("msg")
    if error_type == "value_error" and isinstance(raw_message, str) and any("\u4e00" <= char <= "\u9fff" for char in raw_message):
        return raw_message
    if field == "email" and error_type == "value_error":
        return "请输入有效的邮箱地址"
    if field == "code" and error_type == "string_pattern_mismatch":
        return "验证码格式不正确"
    if label and context.get("min_length"):
        return f"{label}长度不符合要求"
    if label and context.get("max_length"):
        return f"{label}长度不符合要求"
    return "请求参数无效"


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
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
