"""نقطة دخول تطبيق الويب (FastAPI).

التطبيق يعرض ويقرأ فقط: لا يوجد فيه أي مسار يشغّل محبسًا أو يوقفه
(SRS §5.4، §19). الجدولة والجمع يعملان في عملية Worker منفصلة حتى لا
تتكرر الطلبات عند تشغيل أكثر من نسخة من الويب (SRS §6.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import auth, dashboard, events, public, reports, settings_admin, system, zones
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.templating import STATIC_DIR
from app.db.session import dispose_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    logger.info(
        "web.start",
        extra={"env": settings.app_env, "tz": settings.report_timezone},
    )
    yield
    dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth.router)
    app.include_router(public.router)
    app.include_router(dashboard.router)
    app.include_router(zones.router)
    app.include_router(events.router)
    app.include_router(reports.router)
    app.include_router(settings_admin.router)
    app.include_router(system.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # الصفحات مكتفية ذاتيًا: لا نصوص ولا خطوط ولا صور من خارج الأصل.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        # طلب صفحة بلا جلسة يذهب إلى الدخول؛ طلب API يبقى JSON.
        is_api = request.url.path.startswith("/api/")
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and not is_api:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return app


app = create_app()
