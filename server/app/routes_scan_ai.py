"""Phase 2 — AI paper grading: one place that mounts the new Scan & Grade routers
(answer key + rubric, AI grading runs, review summary, export, phone capture, page mapping,
class struggles). Each lives in its own module under app/scan/."""
from __future__ import annotations

from fastapi import APIRouter

from .scan import any_paper, answer_key, capture, diagrams, export, mapping, review, struggles

router = APIRouter()
router.include_router(answer_key.router)
router.include_router(review.router)
router.include_router(export.router)
router.include_router(capture.router)
router.include_router(mapping.router)
router.include_router(struggles.router)
router.include_router(diagrams.router)  # graph / diagram questions
router.include_router(any_paper.router)
