"""Phase 2 — AI paper grading: one place that mounts the new Scan & Grade routers
(answer key + rubric, AI grading runs, review summary, export, phone capture, page mapping,
class struggles). Each lives in its own module under app/scan/."""
from __future__ import annotations

from fastapi import APIRouter

from .scan import answer_key

router = APIRouter()
router.include_router(answer_key.router)
