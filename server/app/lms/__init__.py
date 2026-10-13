"""LMS sync (slice 5): Canvas (ready), Moodle / Blackboard / Brightspace (beta)."""
from __future__ import annotations

from .base import (GradeError, GradeRow, LmsAuthError, LmsCourse, LmsError, LmsIdentity,
                   LmsProvider, LmsStudent, ProviderInfo, normalize_base_url, set_sleep,
                   set_transport_factory, username_from)
from .blackboard import BlackboardProvider
from .brightspace import BrightspaceProvider
from .canvas import CanvasProvider
from .moodle import MoodleProvider

PROVIDERS: dict[str, type[LmsProvider]] = {
    "canvas": CanvasProvider,
    "moodle": MoodleProvider,
    "blackboard": BlackboardProvider,
    "brightspace": BrightspaceProvider,
}


def provider_list() -> list[dict]:
    return [cls.info.as_dict() for cls in PROVIDERS.values()]


def make_provider(provider_id: str, base_url: str, credential: str,
                  allow_private: bool = False) -> LmsProvider:
    cls = PROVIDERS.get(provider_id)
    if cls is None:
        raise LmsError(f"Unknown LMS “{provider_id}”")
    return cls(base_url, credential, allow_private=allow_private)


__all__ = ["PROVIDERS", "GradeError", "GradeRow", "LmsAuthError", "LmsCourse", "LmsError",
           "LmsIdentity", "LmsProvider", "LmsStudent", "ProviderInfo", "make_provider",
           "normalize_base_url", "provider_list", "set_sleep", "set_transport_factory",
           "username_from"]
