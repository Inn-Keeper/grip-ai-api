from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.errors import AppError
from app.gemini import GeminiClient
from app.import_schemas import (
    ImportRequest,
    ImportResponse,
    PostingsRequest,
    PostingsResponse,
)
from app.import_service import ImportService
from app.playbook_schemas import ModelPlaybook, PlaybookRequest
from app.playbook_service import PlaybookService
from app.poe_schemas import PoeChatRequest, PoeReply
from app.poe_service import PoeChatService
from app.posting_service import PostingService
from app.ollama import OllamaClient
from app.schemas import GradeRequest, GradeResponse
from app.service import GradeService
from app.supabase import SupabaseGateway


bearer = HTTPBearer(auto_error=False)


def bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> str | None:
    return credentials.credentials if credentials else None


def attach_services(app: FastAPI, grade_service: GradeService) -> None:
    app.state.grade_service = grade_service
    app.state.import_service = ImportService(grade_service)
    app.state.posting_service = PostingService(grade_service)
    app.state.playbook_service = PlaybookService(grade_service)
    app.state.poe_service = PoeChatService(grade_service)


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
                attach_services(
                    app,
                    GradeService(
                        settings,
                        SupabaseGateway(settings, http_client=http_client),
                        (
                            OllamaClient
                            if settings.ai_provider == "ollama"
                            else GeminiClient
                        )(settings, http_client=http_client),
                    ),
                )
            yield

    app = FastAPI(lifespan=lifespan, title="grip-ai-api")
    if grade_service is not None:
        attach_services(app, grade_service)

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
            headers=(
                {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Locations may contain user-provided dictionary keys; expose only the
        # validation type, never submitted values, keys or exception context.
        kinds = sorted({error["type"] for error in exc.errors()})
        message = "Check required fields, field types and length limits in the request schema."
        # These root validators have fixed rules; never forward their exception
        # messages, which may acquire submitted values in future validators.
        if "value_error" in kinds:
            if request.url.path == "/api/v1/ai/grade-talk-track":
                message = "Keep each talk-track section within 8,000 characters."
            elif request.url.path == "/api/v1/ai/import/parse":
                message = "Send either text or both filename and content_base64, not both sources."
        elif "json_invalid" in kinds:
            message = "Send valid JSON with Content-Type: application/json."
        elif any(kind.startswith("uuid_") for kind in kinds):
            message = "Use a valid UUID for board_id."
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": message,
                    "request_id": request.state.request_id,
                    "validation_types": kinds,
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
        }
        if settings.ai_provider == "gemini":
            required["GEMINI_API_KEY"] = settings.gemini_api_key
        missing = [name for name, value in required.items() if not value]
        if missing:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "missing": missing},
            )
        return {"status": "ready"}

    # Unauthenticated on purpose: it reveals nothing about any user, and the
    # board asks on every load. Answered from memory, so it spends no quota.
    @app.get("/api/v1/ai/status")
    async def grading_status(request: Request) -> dict:
        return request.app.state.grade_service.grading_status()

    @app.post("/api/v1/ai/grade-talk-track", response_model=GradeResponse)
    async def grade_talk_track(
        payload: GradeRequest,
        request: Request,
        token: Annotated[str | None, Depends(bearer_token)],
    ):
        return await request.app.state.grade_service.grade(
            payload,
            token,
            request.state.request_id,
        )

    @app.post("/api/v1/ai/import/parse", response_model=ImportResponse)
    async def parse_ledger(
        payload: ImportRequest,
        request: Request,
        token: Annotated[str | None, Depends(bearer_token)],
    ):
        return await request.app.state.import_service.parse(
            payload,
            token,
            request.state.request_id,
        )

    @app.post("/api/v1/ai/import/postings", response_model=PostingsResponse)
    async def read_postings(
        payload: PostingsRequest,
        request: Request,
        token: Annotated[str | None, Depends(bearer_token)],
    ):
        return await request.app.state.posting_service.read(
            payload,
            token,
            request.state.request_id,
        )

    @app.post("/api/v1/ai/first-run/playbook", response_model=ModelPlaybook)
    async def first_run_playbook(
        payload: PlaybookRequest,
        request: Request,
        token: Annotated[str | None, Depends(bearer_token)],
    ):
        return await request.app.state.playbook_service.generate(
            payload,
            token,
            request.state.request_id,
        )

    @app.post("/api/v1/ai/poe/chat", response_model=PoeReply)
    async def poe_chat(
        payload: PoeChatRequest,
        request: Request,
        token: Annotated[str | None, Depends(bearer_token)],
    ):
        return await request.app.state.poe_service.chat(
            payload,
            token,
            request.state.request_id,
        )

    return app


app = create_app()
