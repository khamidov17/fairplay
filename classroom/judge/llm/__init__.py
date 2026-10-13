"""Reusable LLM access layer, shared by all LLM-backed features.

Callers should use the module-level :func:`chat` / :func:`is_configured`
helpers and stay provider-agnostic; the concrete provider is chosen by the
``LLM_PROVIDER`` setting.  Per-provider profiles live in
``{PROVIDER}_PROFILES`` (e.g. ``LITELLM_PROFILES``).
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from judge.llm.base import Backend, LLMError, LLMResult

__all__ = ['Backend', 'LLMError', 'LLMResult', 'get_backend', 'chat', 'is_configured']

_backend = None


def _get_provider() -> str:
    """Return the current LLM provider identifier in upper case."""
    return getattr(settings, 'LLM_PROVIDER', 'azure').upper()


def _resolve_profile(name: str) -> dict:
    """Resolve a named profile from the active provider's profile config.

    Reads ``{PROVIDER}_PROFILES``.  Merge order (later wins):
      1. ``{PROVIDER}_PROFILES['default']``  – optional base
      2. ``{PROVIDER}_PROFILES[name]``       – requested profile

    This lets every feature pick a profile like ``profile='hint'`` without
    caring which provider is configured.
    """
    profiles = getattr(settings, f'{_get_provider()}_PROFILES', {})
    cfg = dict(profiles.get('default', {}))
    if name != 'default':
        cfg.update(profiles.get(name, {}))
    return cfg


def get_backend() -> Backend:
    """Return the configured LLM backend (cached for the process)."""
    global _backend
    if _backend is None:
        provider = getattr(settings, 'LLM_PROVIDER', 'azure').lower()
        if provider == 'azure':
            from judge.llm.azure import AzureBackend
            _backend = AzureBackend()
        elif provider == 'litellm':
            from judge.llm.litellm import LiteLLMBackend
            _backend = LiteLLMBackend()
        else:
            raise ImproperlyConfigured(f'Unknown LLM_PROVIDER: {provider!r}')
    return _backend


def chat(messages, *, profile='default', **overrides) -> LLMResult:
    """Run a chat completion.

    The ``profile`` selects a named configuration from the active provider's
    :setting:`{PROVIDER}_PROFILES`.  Extra keyword arguments override whatever
    was already set by the profile.
    """
    cfg = _resolve_profile(profile)
    cfg.update(overrides)
    return get_backend().chat(messages, **cfg)


def is_configured() -> bool:
    return get_backend().is_configured()
