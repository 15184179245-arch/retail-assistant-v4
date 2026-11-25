import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.api.assembly import (
    install_application, agent_error_handler, workflow_error_handler, erp_sales_error_handler,
    erp_receipt_error_handler, erp_catalog_error_handler, office_schedule_error_handler,
    office_exception_error_handler, http_error_handler, validation_error_handler, unexpected_error_handler,
)
from backend.app.core.errors import ApiEnvelope
from backend.app.db.session import get_session
from backend.app.workers.outbox import OutboxEvent
from backend.app.modules.agents.runtime_bootstrap import pi_host_lifespan

app = FastAPI(title="Retail Smart Ops API", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=pi_host_lifespan)
install_application(app)


@app.get("/health", response_model=ApiEnvelope)
def health() -> ApiEnvelope:
    return ApiEnvelope(success=True, data={"status": "ok"}, code="OK", error=None, user_message="服务正常。", hint=None)


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.get("/health/live", response_model=ApiEnvelope)
def live() -> ApiEnvelope:
    return ApiEnvelope(success=True, data={"status": "alive", "checked_at": _checked_at()},
                       code="OK", error=None, user_message="进程存活。", hint=None)


@app.get("/health/ready", response_model=ApiEnvelope)
def ready(session: Annotated[Session, Depends(get_session)]) -> ApiEnvelope | JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        body = ApiEnvelope(
            success=False,
            data={"status": "unavailable", "database": "unavailable",
                  "source": "database", "checked_at": _checked_at()},
            code="DATABASE_UNAVAILABLE", error="database_unavailable",
            user_message="数据库暂不可用。",
            hint="请检查数据库连接；核心 API 不应被判定为健康。")
        return JSONResponse(status_code=503, content=body.model_dump())
    return ApiEnvelope(success=True, data={"status": "ready", "database": "ok",
                                           "checked_at": _checked_at()},
                       code="OK", error=None, user_message="数据库可用。", hint=None)


@app.get("/health/business", response_model=ApiEnvelope)
def business_health(session: Annotated[Session, Depends(get_session)]) -> ApiEnvelope | JSONResponse:
    try:
        threshold = int(os.environ.get("OUTBOX_BACKLOG_THRESHOLD", "100"))
        if threshold < 0:
            raise ValueError("negative threshold")
    except (TypeError, ValueError):
        body = ApiEnvelope(
            success=False,
            data={"status": "degraded", "source": "configuration",
                  "checked_at": _checked_at()},
            code="HEALTH_CONFIG_INVALID", error="health_config_invalid",
            user_message="健康检查配置无效。", hint="请检查 OUTBOX_BACKLOG_THRESHOLD。")
        return JSONResponse(status_code=503, content=body.model_dump())
    try:
        rows = session.execute(
            select(OutboxEvent.status, func.count()).group_by(OutboxEvent.status)).all()
    except Exception:
        body = ApiEnvelope(
            success=False,
            data={"status": "unavailable", "outbox": "unavailable",
                  "source": "outbox", "checked_at": _checked_at()},
            code="DATABASE_UNAVAILABLE", error="database_unavailable",
            user_message="Outbox 状态暂不可用。",
            hint="请检查数据库/Outbox 表；不要把它当作普通内部错误。")
        return JSONResponse(status_code=503, content=body.model_dump())
    counts = {status: int(count) for status, count in rows}
    backlog = sum(counts.get(key, 0) for key in ("pending", "leased", "retry", "dead"))
    degraded = backlog > threshold
    data = {"status": "degraded" if degraded else "ok", "outbox": counts,
            "outbox_backlog": backlog, "threshold": threshold,
            "model": "unknown", "checked_at": _checked_at()}
    if degraded:
        body = ApiEnvelope(success=False, data=data, code="BUSINESS_DEGRADED",
                           error="business_degraded", user_message="业务任务积压，核心 API 仍存活。",
                           hint="请检查 Outbox worker 与告警。")
        return JSONResponse(status_code=503, content=body.model_dump())
    return ApiEnvelope(success=True, data=data, code="OK", error=None,
                       user_message="业务任务无异常积压。", hint=None)


def openapi_document() -> dict:
    """Generate the versioned contract for the checked-in snapshot."""
    return app.openapi()


@app.middleware("http")
async def stocktake_cache_control(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/v1/stocktake", "/api/v1/office/exceptions", "/api/v1/office/schedules", "/api/v1/erp/sales", "/api/v1/erp/receiving", "/api/v1/erp/catalog-maintenance", "/api/v1/erp/commands")):
        response.headers["Cache-Control"] = "no-store"
    return response
