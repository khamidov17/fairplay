"""Tests for the reusable LLM access layer (judge.llm)."""
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from judge import llm
from judge.llm import LLMError, LLMResult
from judge.llm.azure import AzureBackend
from judge.llm.litellm import LiteLLMBackend

AZURE_SETTINGS = {
    'LLM_PROVIDER': 'azure',
    'AZURE_OPENAI_ENDPOINT': 'https://example.openai.azure.com/',
    'AZURE_OPENAI_API_KEY': 'test-key',
    'AZURE_OPENAI_DEPLOYMENT': 'gpt-test',
}

LITELLM_SETTINGS = {
    'LLM_PROVIDER': 'litellm',
    'LITELLM_API_BASE': 'http://192.168.1.100:4000',
}


def _fake_azure_response(content='Hello.', prompt=10, completion=5, total=15, model='gpt-test'):
    response = mock.MagicMock()
    response.id = 'resp-1'
    response.model = model
    choice = mock.MagicMock()
    choice.message.content = content
    choice.finish_reason = 'stop'
    response.choices = [choice]
    response.usage.prompt_tokens = prompt
    response.usage.completion_tokens = completion
    response.usage.total_tokens = total
    return response


def _fake_litellm_response(content='Hello.', prompt=10, completion=5, total=15, model='gpt-test'):
    response = mock.MagicMock()
    response.id = 'resp-1'
    response.model = model
    choice = mock.MagicMock()
    choice.message.content = content
    choice.finish_reason = 'stop'
    response.choices = [choice]
    response.usage.prompt_tokens = prompt
    response.usage.completion_tokens = completion
    response.usage.total_tokens = total
    return response


# ---------------------------------------------------------------------------
# Azure backend tests
# ---------------------------------------------------------------------------
@override_settings(**AZURE_SETTINGS)
class AzureBackendTestCase(SimpleTestCase):
    def test_chat_returns_result(self):
        backend = AzureBackend()
        with mock.patch('judge.llm.azure.AzureOpenAI') as client_cls:
            create = client_cls.return_value.chat.completions.create
            create.return_value = _fake_azure_response('  Use a set.  ')
            result = backend.chat([{'role': 'user', 'content': 'hi'}])

        self.assertIsInstance(result, LLMResult)
        self.assertEqual(result.text, 'Use a set.')
        self.assertEqual(result.prompt_tokens, 10)
        self.assertEqual(result.completion_tokens, 5)
        self.assertEqual(result.total_tokens, 15)
        self.assertEqual(result.model_name, 'gpt-test')
        self.assertIsNotNone(result.response_time_ms)

        _, kwargs = create.call_args
        self.assertEqual(kwargs['model'], 'gpt-test')
        self.assertEqual(kwargs['messages'], [{'role': 'user', 'content': 'hi'}])

    def test_chat_empty_response_raises(self):
        backend = AzureBackend()
        with mock.patch('judge.llm.azure.AzureOpenAI') as client_cls:
            client_cls.return_value.chat.completions.create.return_value = (
                _fake_azure_response('   ')
            )
            with self.assertRaises(LLMError):
                backend.chat([{'role': 'user', 'content': 'hi'}])

    def test_is_configured_true_with_endpoint(self):
        self.assertTrue(AzureBackend().is_configured())

    @override_settings(AZURE_OPENAI_ENDPOINT=None)
    def test_is_configured_false_without_endpoint(self):
        self.assertFalse(AzureBackend().is_configured())

    def test_chat_passes_kwargs(self):
        backend = AzureBackend()
        with mock.patch('judge.llm.azure.AzureOpenAI') as client_cls:
            create = client_cls.return_value.chat.completions.create
            create.return_value = _fake_azure_response('ok')
            backend.chat([{'role': 'user', 'content': 'hi'}], temperature=0.3, seed=42)

        _, kwargs = create.call_args
        self.assertEqual(kwargs['temperature'], 0.3)
        self.assertEqual(kwargs['seed'], 42)
        self.assertEqual(kwargs['max_completion_tokens'], 2048)  # default

    def test_chat_wraps_exceptions_as_llm_error(self):
        backend = AzureBackend()
        with mock.patch('judge.llm.azure.AzureOpenAI') as client_cls:
            client_cls.return_value.chat.completions.create.side_effect = TimeoutError('timed out')
            with self.assertRaises(LLMError) as ctx:
                backend.chat([{'role': 'user', 'content': 'hi'}])
            self.assertIn('TimeoutError', str(ctx.exception))

    def test_chat_omits_max_completion_tokens_when_none(self):
        backend = AzureBackend()
        with override_settings(AZURE_OPENAI_MAX_TOKENS=None):
            with mock.patch('judge.llm.azure.AzureOpenAI') as client_cls:
                create = client_cls.return_value.chat.completions.create
                create.return_value = _fake_azure_response('ok')
                backend.chat([{'role': 'user', 'content': 'hi'}], max_tokens=None)

            _, kwargs = create.call_args
            self.assertNotIn('max_completion_tokens', kwargs)

    def test_chat_empty_choices_raises(self):
        backend = AzureBackend()
        with mock.patch('judge.llm.azure.AzureOpenAI') as client_cls:
            response = _fake_azure_response('ok')
            response.choices = []
            client_cls.return_value.chat.completions.create.return_value = response
            with self.assertRaises(LLMError) as ctx:
                backend.chat([{'role': 'user', 'content': 'hi'}])
            self.assertIn('no choices', str(ctx.exception))


# ---------------------------------------------------------------------------
# LiteLLM backend tests
# ---------------------------------------------------------------------------
@override_settings(**LITELLM_SETTINGS)
class LiteLLMBackendTestCase(SimpleTestCase):
    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_returns_result(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response(
            '  Try DP.  ', model='gpt-4o',
        )

        backend = LiteLLMBackend()
        result = backend.chat([{'role': 'user', 'content': 'hint'}])

        self.assertIsInstance(result, LLMResult)
        self.assertEqual(result.text, 'Try DP.')
        self.assertEqual(result.model_name, 'gpt-4o')
        self.assertIsNotNone(result.response_time_ms)

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_passes_kwargs(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response('ok')

        backend = LiteLLMBackend()
        backend.chat([{'role': 'user', 'content': 'hi'}],
                     model='gpt-4o', temperature=0.3, seed=42)

        create = client.chat.completions.create
        _, kwargs = create.call_args
        self.assertEqual(kwargs['model'], 'gpt-4o')
        self.assertEqual(kwargs['temperature'], 0.3)
        self.assertEqual(kwargs['seed'], 42)

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_empty_response_raises(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response('   ')

        backend = LiteLLMBackend()
        with self.assertRaises(LLMError):
            backend.chat([{'role': 'user', 'content': 'hi'}])

    def test_is_configured_true_with_api_base(self):
        self.assertTrue(LiteLLMBackend().is_configured())

    @override_settings(LITELLM_API_BASE=None)
    def test_is_configured_false_without_api_base(self):
        self.assertFalse(LiteLLMBackend().is_configured())

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_wraps_exceptions_as_llm_error(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.side_effect = ConnectionError('refused')

        backend = LiteLLMBackend()
        with self.assertRaises(LLMError) as ctx:
            backend.chat([{'role': 'user', 'content': 'hi'}])
        self.assertIn('ConnectionError', str(ctx.exception))

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_falls_back_to_default_model(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response('ok')

        with override_settings(LITELLM_DEFAULT_MODEL='fallback-model'):
            backend = LiteLLMBackend()
            backend.chat([{'role': 'user', 'content': 'hi'}])

        _, kwargs = client.chat.completions.create.call_args
        self.assertEqual(kwargs['model'], 'fallback-model')

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_null_usage_handled(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        response = _fake_litellm_response('ok')
        response.usage = None
        client.chat.completions.create.return_value = response

        backend = LiteLLMBackend()
        result = backend.chat([{'role': 'user', 'content': 'hi'}])
        self.assertIsNone(result.prompt_tokens)
        self.assertIsNone(result.completion_tokens)
        self.assertIsNone(result.total_tokens)

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_api_key_from_profile_routes_to_client(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response('ok')

        backend = LiteLLMBackend()
        backend.chat([{'role': 'user', 'content': 'hi'}], api_key='sk-profile-key')

        # api_key went to _build_client(), not to the API call
        mock_openai_cls.assert_called_once()
        _, client_kwargs = mock_openai_cls.call_args
        self.assertEqual(client_kwargs['api_key'], 'sk-profile-key')

        create = client.chat.completions.create
        _, api_kwargs = create.call_args
        self.assertNotIn('api_key', api_kwargs)

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_max_tokens_becomes_max_completion_tokens(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response('ok')

        backend = LiteLLMBackend()
        backend.chat([{'role': 'user', 'content': 'hi'}], max_tokens=512)

        _, kwargs = client.chat.completions.create.call_args
        self.assertEqual(kwargs['max_completion_tokens'], 512)

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_omits_max_completion_tokens_when_none(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        client.chat.completions.create.return_value = _fake_litellm_response('ok')

        backend = LiteLLMBackend()
        backend.chat([{'role': 'user', 'content': 'hi'}], max_tokens=None)

        _, kwargs = client.chat.completions.create.call_args
        self.assertNotIn('max_completion_tokens', kwargs)

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_api_base_v1_detection_adds_v1(self, mock_openai_cls):
        with override_settings(LITELLM_API_BASE='http://host:4000'):
            backend = LiteLLMBackend()
            backend.chat([{'role': 'user', 'content': 'hi'}])
        mock_openai_cls.assert_called_once()
        _, client_kwargs = mock_openai_cls.call_args
        self.assertEqual(client_kwargs['base_url'], 'http://host:4000/v1')

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_api_base_v1_detection_already_has_v1(self, mock_openai_cls):
        with override_settings(LITELLM_API_BASE='http://host:4000/v1'):
            backend = LiteLLMBackend()
            backend.chat([{'role': 'user', 'content': 'hi'}])
        _, client_kwargs = mock_openai_cls.call_args
        self.assertEqual(client_kwargs['base_url'], 'http://host:4000/v1')

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_api_base_v1_detection_trailing_slash(self, mock_openai_cls):
        with override_settings(LITELLM_API_BASE='http://host:4000/'):
            backend = LiteLLMBackend()
            backend.chat([{'role': 'user', 'content': 'hi'}])
        _, client_kwargs = mock_openai_cls.call_args
        self.assertEqual(client_kwargs['base_url'], 'http://host:4000/v1')

    @mock.patch('judge.llm.litellm.OpenAI')
    def test_chat_empty_choices_raises(self, mock_openai_cls):
        client = mock_openai_cls.return_value
        response = _fake_litellm_response('ok')
        response.choices = []
        client.chat.completions.create.return_value = response

        backend = LiteLLMBackend()
        with self.assertRaises(LLMError) as ctx:
            backend.chat([{'role': 'user', 'content': 'hi'}])
        self.assertIn('no choices', str(ctx.exception))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
class ModuleLevelTestCase(SimpleTestCase):
    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)

    @override_settings(**AZURE_SETTINGS)
    def test_is_configured_wraps_backend(self):
        self.assertTrue(llm.is_configured())

    @override_settings(LLM_PROVIDER='azure', AZURE_OPENAI_ENDPOINT=None)
    def test_is_configured_false_no_backend_creds(self):
        self.assertFalse(llm.is_configured())

    @override_settings(LLM_PROVIDER='aZuRe', **{k: v for k, v in AZURE_SETTINGS.items() if k != 'LLM_PROVIDER'})
    def test_get_backend_case_insensitive(self):
        self.assertIsInstance(llm.get_backend(), AzureBackend)

    @override_settings(**LITELLM_SETTINGS)
    def test_module_chat_with_litellm(self):
        mock_backend = mock.MagicMock()
        mock_backend.chat.return_value = LLMResult(text='hi')
        with mock.patch('judge.llm.get_backend', return_value=mock_backend):
            result = llm.chat([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(result.text, 'hi')

    @override_settings(LLM_PROVIDER='bogus')
    def test_get_backend_invalid_provider(self):
        with self.assertRaises(ImproperlyConfigured):
            llm.get_backend()


# ---------------------------------------------------------------------------
# Profile resolution edge cases
# ---------------------------------------------------------------------------
class ProfileEdgeCaseTestCase(SimpleTestCase):
    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)

    @override_settings(LLM_PROVIDER='litellm')
    def test_resolve_without_any_profiles(self):
        cfg = llm._resolve_profile('hint')
        self.assertEqual(cfg, {})

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={
            'default': {'temperature': 0.5},
            'hint': {},
        },
    )
    def test_profile_inherits_default_keys(self):
        cfg = llm._resolve_profile('hint')
        self.assertEqual(cfg, {'temperature': 0.5})

    @override_settings(
        LLM_PROVIDER='azure',
        AZURE_PROFILES={
            'default': {'model': 'base', 'temperature': 0.5},
        },
    )
    def test_default_profile_resolution(self):
        cfg = llm._resolve_profile('default')
        self.assertEqual(cfg, {'model': 'base', 'temperature': 0.5})

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={
            'default': {'model': 'base'},
            'hint': {'temperature': 0.3, 'max_tokens': 512},
        },
    )
    def test_profile_merges_default_with_named(self):
        cfg = llm._resolve_profile('hint')
        self.assertEqual(cfg, {'model': 'base', 'temperature': 0.3, 'max_tokens': 512})

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={
            'hint': {'model': 'standalone'},
        },
    )
    def test_profile_without_default_key(self):
        cfg = llm._resolve_profile('hint')
        self.assertEqual(cfg, {'model': 'standalone'})


# ---------------------------------------------------------------------------
# Profile resolution tests
# ---------------------------------------------------------------------------
class ProfileResolutionTestCase(SimpleTestCase):
    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={
            'default': {'model': 'base', 'temperature': 0.5},
            'hint': {'model': 'litellm-hint'},
        },
    )
    def test_resolve_hint_profile(self):
        cfg = llm._resolve_profile('hint')
        self.assertEqual(cfg, {'model': 'litellm-hint', 'temperature': 0.5})

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={'default': {'model': 'base'}},
    )
    def test_resolve_default_profile(self):
        cfg = llm._resolve_profile('default')
        self.assertEqual(cfg, {'model': 'base'})

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={'default': {'model': 'base', 'temperature': 0.5}},
    )
    def test_resolve_missing_profile_falls_back(self):
        cfg = llm._resolve_profile('nonexistent')
        self.assertEqual(cfg, {'model': 'base', 'temperature': 0.5})

    @override_settings(
        LLM_PROVIDER='azure',
        AZURE_PROFILES={
            'default': {'model': 'base'},
            'hint': {'model': 'azure-hint', 'temperature': 0.2},
        },
    )
    def test_azure_profiles_independent(self):
        cfg = llm._resolve_profile('hint')
        self.assertEqual(cfg, {'model': 'azure-hint', 'temperature': 0.2})

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={
            'default': {'model': 'base', 'temperature': 0.5},
            'hint': {'model': 'hint-model'},
        },
    )
    def test_chat_resolves_profile_and_calls_backend(self):
        mock_backend = mock.MagicMock()
        mock_backend.chat.return_value = LLMResult(text='ok')
        mock_backend.is_configured.return_value = True

        with mock.patch('judge.llm.get_backend', return_value=mock_backend):
            _ = llm.chat([{'role': 'user', 'content': 'hi'}], profile='hint')

        mock_backend.chat.assert_called_once_with(
            [{'role': 'user', 'content': 'hi'}],
            model='hint-model',
            temperature=0.5,
        )

    @override_settings(
        LLM_PROVIDER='litellm',
        LITELLM_PROFILES={
            'default': {'model': 'base', 'temperature': 0.5},
        },
    )
    def test_chat_overrides_profile_args(self):
        mock_backend = mock.MagicMock()
        mock_backend.chat.return_value = LLMResult(text='ok')

        with mock.patch('judge.llm.get_backend', return_value=mock_backend):
            llm.chat([{'role': 'user', 'content': 'hi'}], profile='hint', temperature=0.9)

        mock_backend.chat.assert_called_once_with(
            [{'role': 'user', 'content': 'hi'}],
            model='base',
            temperature=0.9,
        )


# ---------------------------------------------------------------------------
# Backend dispatch tests
# ---------------------------------------------------------------------------
class BackendDispatchTestCase(SimpleTestCase):
    def setUp(self):
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)

    @override_settings(LLM_PROVIDER='azure')
    def test_azure_provider_selected(self):
        self.assertIsInstance(llm.get_backend(), AzureBackend)

    @override_settings(**LITELLM_SETTINGS)
    def test_litellm_provider_selected(self):
        self.assertIsInstance(llm.get_backend(), LiteLLMBackend)

    @override_settings(LLM_PROVIDER='bogus')
    def test_unknown_provider_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            llm.get_backend()
