"""Proctora API server. Run from server/: `uvicorn app.main:app --port 8000`."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from .http_guard import GuardMiddleware, SignPrivateUploads
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
from .seed import refuse_public_demo_logins, seed, seed_demo_class
from .routes_classes import student_router as classes_student_router, teacher_router as classes_teacher_router
from .store import make_store, set_store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("proctora")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.check_secure()
        db = Database(settings)
        await db.create_all()
        await seed(db, settings)
        if settings.dev:
            await seed_demo_class(db)
        try:
            await refuse_public_demo_logins(db, settings)
        except RuntimeError:
            await db.dispose()
            raise
        store = make_store(settings.redis_url)
        set_db(db)
        set_store(store)
        # Audit L-06: resume scan batches a restart or crash left unfinished.
        sweeper = None
        if settings.feature("scan"):
            from .routes_scan import sweep_forever
            sweeper = asyncio.get_running_loop().create_task(sweep_forever())
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                try:                       # wait for it: disposing the DB under it can hang
                    await sweeper
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await store.close()
            await db.dispose()
            set_db(None)
            set_store(None)

    # Audit 2026-10-13 (hardening): the API explorer only on a dev server.
    docs = settings.dev
    app = FastAPI(title="Proctora", version="0.1", lifespan=lifespan,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo the rejected input back: it can be large, private (answers), or not
        # even JSON-encodable (NaN / Infinity), which used to turn a 422 into a 500.
        detail = [{k: e[k] for k in ("type", "loc", "msg") if k in e} for e in exc.errors()]
        return JSONResponse({"detail": detail}, status_code=422)
    app.state.settings = settings
    app.add_middleware(SignPrivateUploads)     # audit L-18: expiring links to scan images
    app.add_middleware(GuardMiddleware)        # audit L-04 / S-16: size limits + headers
    # Audit #12: no wildcard CORS. The UI is served by this server (same origin); other
    # origins only when listed in FAIRPLAY_CORS_ORIGINS (dev: the Vite dev server).
    origins = list(settings.cors_origins)
    if settings.dev and not origins:
        origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                           allow_headers=["*"])

    app.include_router(auth_router)
    app.include_router(classes_teacher_router)   # audit S-03
    app.include_router(classes_student_router)
    app.include_router(teacher_router)
    app.include_router(student_router)
    app.include_router(grading_router)
    app.include_router(gradebook_router)
    if settings.feature("lms"):
        app.include_router(lms_router)  # slice 5 (before the /api catch-all below)
    app.include_router(insights_router)  # slice 6 F/G: insights + student profiles
    if settings.feature("practice"):
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
    if settings.feature("scan"):
        from .routes_scan import router as scan_router  # slice 6 D: Scan & Grade (paper exams)
        app.include_router(scan_router)

    @app.get("/api/health")
    async def health():
        # demo_logins: the login page may show the demo passwords (dev / demo servers only).
        # features: optional parts switched on (FAIRPLAY_FEATURES); the UI hides the rest.
        return {"ok": True, "demo_logins": settings.demo_logins, "features": list(settings.features)}

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
