from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(Exception):
    """Raised when an LLM backend returns an empty or otherwise unusable response."""


@dataclass
class LLMResult:
    """Provider-agnostic result of a single chat completion."""
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    model_name: str = ''
    response_time_ms: int | None = None


class Backend(ABC):
    """A pluggable LLM provider. Concrete backends are selected by LLM_PROVIDER."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this backend has the settings it needs to make a call."""

    @abstractmethod
    def chat(self, messages, *, model=None, max_tokens=None, **kwargs) -> LLMResult:
        """Run a chat completion. ``messages`` is a list of {'role', 'content'} dicts.

        Extra keyword arguments (``temperature``, ``top_p``, ``seed``, …)
        are forwarded to the underlying provider as-is.
        """
