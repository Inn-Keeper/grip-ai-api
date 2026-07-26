from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.errors import AppError
from app.gemini import GeminiClient
from app.schemas import GradeRequest, GradeResponse
from app.service import GradeService
from app.supabase import SupabaseGateway


bearer = HTTPBearer(auto_error=False)


def create_app(
    settings: Settings | None = None,
    *,
    grade_service: GradeService | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=settings.ai_timeout_seconds
        ) as http_client:
            if grade_service is None:
                app.state.grade_service = GradeService(
                    settings,
                    SupabaseGateway(settings, http_client=http_client),
                    GeminiClient(settings, http_client=http_client),
                )
            yield

    app = FastAPI(lifespan=lifespan, title="grip-ai-api")
    if grade_service is not None:
        app.state.grade_service = grade_service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def assign_request_id(request: Request, call_next):
        request.state.request_id = str(uuid4())
        return await call_next(request)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": request.state.request_id,
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        required = {
            "SUPABASE_URL": settings.supabase_url,
            "SUPABASE_ANON_KEY": settings.supabase_anon_key,
            "GEMINI_API_KEY": settings.gemini_api_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "missing": missing},
            )
        return {"status": "ready"}

    @app.post("/api/v1/ai/grade-talk-track", response_model=GradeResponse)
    async def grade_talk_track(
        payload: GradeRequest,
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Security(bearer)
        ] = None,
    ):
        return await request.app.state.grade_service.grade(
            payload,
            credentials.credentials if credentials is not None else None,
            request.state.request_id,
        )

    return app


app = create_app()
