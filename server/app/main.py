"""Proctora API server. Run from server/: `uvicorn app.main:app --port 8000`."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .config import Settings
from .db import Database, set_db
from .routes_auth import router as auth_router
from .routes_gradebook import gradebook_router, grading_router
from .routes_insights import router as insights_router  # slice 6 F/G
from .routes_lms import router as lms_router
from .routes_practice import router as practice_router, student_router as practice_student_router
from .routes_student import router as student_router
from .routes_teacher import router as teacher_router
from .seed import seed
from .store import make_store, set_store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("proctora")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate()  # phase 1a: production refuses unsafe settings (ConfigError)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.warn_insecure()
        db = Database(settings)
        await db.create_all()
        await seed(db, settings)
        store = make_store(settings.redis_url)
        set_db(db)
        set_store(store)
        try:
            yield
        finally:
            await store.close()
            await db.dispose()
            set_db(None)
            set_store(None)

    app = FastAPI(title="Proctora", version="0.1", lifespan=lifespan)
    app.state.settings = settings
    # phase 1a: CORS from FAIRPLAY_CORS_ORIGINS ("*" only in development; production
    # default is same-origin only = no CORS headers at all) + security headers.
    origins = settings.allowed_origins()
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                           allow_headers=["*"])
    from .hardening import SecurityHeadersMiddleware
    app.add_middleware(SecurityHeadersMiddleware, production=settings.production)

    app.include_router(auth_router)
    app.include_router(teacher_router)
    app.include_router(student_router)
    app.include_router(grading_router)
    app.include_router(gradebook_router)
    app.include_router(lms_router)  # slice 5 (before the /api catch-all below)
    app.include_router(insights_router)  # slice 6 F/G: insights + student profiles
    app.include_router(practice_router)  # slice 6 H: practice assignments (formative)
    app.include_router(practice_student_router)
    # slice 6 A/B/C: offline packages, uploads, topics
    from .offline import router as offline_router
    from .routes_topics import router as topics_router
    from .uploads import router as uploads_router, set_uploads_dir
    set_uploads_dir(settings.uploads_dir)
    app.include_router(offline_router)
    app.include_router(uploads_router)
    app.include_router(topics_router)
    from .routes_scan import router as scan_router  # slice 6 D: Scan & Grade (paper exams)
    app.include_router(scan_router)
    # phase 1a: audit timeline, password change / sign out everywhere, invigilator exit code
    from .account import router as account_router
    from .audit import router as audit_router
    from .exitcode import router as exitcode_router
    app.include_router(audit_router)
    app.include_router(account_router)
    app.include_router(exitcode_router)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
                   include_in_schema=False)
    async def api_not_found(rest: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    static_root = os.path.realpath(settings.static_dir)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/teacher", status_code=307)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request):
        candidate = os.path.realpath(os.path.join(static_root, path))
        if (candidate.startswith(static_root + os.sep) and os.path.isfile(candidate)):
            return FileResponse(candidate)
        index = os.path.join(static_root, "index.html")
        if os.path.isfile(index):
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "UI not built"}, status_code=404)

    return app


app = create_app()
