"""Phase 3 — grading quality: benchmark of human-marked answers, AI-vs-human agreement
metrics (benchmark runs and live production grades), review routing, second marking and
moderation. Routes in .routes; the routing rule `route_item` in .routing."""
from .routing import route_item  # noqa: F401  (public: the review screen calls it)
