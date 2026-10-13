import logging
import time as _time

from django.conf import settings
from openai import OpenAI

from judge.llm.base import Backend, LLMError, LLMResult

logger = logging.getLogger(__name__)


class LiteLLMBackend(Backend):
    """Backend for a LiteLLM gateway (OpenAI-compatible API).

    LiteLLM proxies requests to upstream LLM providers using the standard
    ``/v1/chat/completions`` endpoint.  The ``model`` field determines which
    upstream model is called.
    """

    def is_configured(self) -> bool:
        result = bool(getattr(settings, 'LITELLM_API_BASE', None))
        logger.debug('is_configured=%s, LITELLM_API_BASE=%r', result,
                     getattr(settings, 'LITELLM_API_BASE', None))
        return result

    def _build_client(self, api_key=None) -> OpenAI:
        api_base = settings.LITELLM_API_BASE.rstrip('/')
        if 'v1' not in api_base.split('/'):
            api_base = api_base + '/v1'

        api_key = api_key or getattr(settings, 'LITELLM_API_KEY', '') or 'sk-no-key-required'
        timeout = getattr(settings, 'LITELLM_TIMEOUT', 120)

        logger.debug('building OpenAI client: base_url=%s, timeout=%s', api_base, timeout)
        return OpenAI(
            base_url=api_base,
            api_key=api_key,
            timeout=timeout,
            max_retries=1,
        )

    def chat(self, messages, *, model=None, max_tokens=None, **kwargs) -> LLMResult:
        api_key = kwargs.pop('api_key', None)

        logger.debug('chat() called: messages=%d, model=%s, max_tokens=%s, kwargs=%s',
                     len(messages), model, max_tokens, kwargs)

        client = self._build_client(api_key=api_key)
        model = model or getattr(settings, 'LITELLM_DEFAULT_MODEL', None)

        logger.debug('sending request: model=%s, max_completion_tokens=%s', model, max_tokens)

        start = _time.monotonic()
        try:
            api_kwargs = dict(model=model, messages=messages, **kwargs)
            if max_tokens is not None:
                api_kwargs['max_completion_tokens'] = max_tokens
            response = client.chat.completions.create(**api_kwargs)
        except Exception as e:
            logger.exception('LiteLLM API request failed after %.2fs', _time.monotonic() - start)
            raise LLMError(f'API request failed: {type(e).__name__}') from e

        elapsed_ms = int((_time.monotonic() - start) * 1000)

        if not response.choices:
            raise LLMError('AI service returned no choices. Please try again.')

        choice = response.choices[0]
        logger.debug('LLM response: id=%s, model=%s, finish_reason=%s',
                     response.id, response.model, choice.finish_reason)

        content = choice.message.content
        if not content or not content.strip():
            raise LLMError('AI service returned an empty response. Please try again.')

        usage = response.usage
        logger.debug('success: elapsed=%dms, text_length=%d, prompt_tokens=%s, '
                     'completion_tokens=%s, total_tokens=%s',
                     elapsed_ms, len(content),
                     usage.prompt_tokens if usage else None,
                     usage.completion_tokens if usage else None,
                     usage.total_tokens if usage else None)

        return LLMResult(
            text=content.strip(),
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            model_name=response.model or '',
            response_time_ms=elapsed_ms,
        )
