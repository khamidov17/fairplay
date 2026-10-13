# Local development / test settings for FairPlay Classroom.
#
# Copy to dmoj/local_settings.py (gitignored) and adjust. dmoj/settings.py exec()s this file
# into its own namespace, so names such as os, BASE_DIR and STATICFILES_FINDERS are in scope.
#
# Nothing in here is a secret: SECRET_KEY comes from the CLASSROOM_SECRET_KEY environment
# variable, or is generated once and kept in dmoj/.dev_secret_key (gitignored).
#
# Test runs mirror the upstream CI settings (.ci.settings.py): MySQL/MariaDB, a local-memory
# cache, COMPRESS_OUTPUT_DIR='cache', STATIC_ROOT=<repo>/static.
import os
import secrets as _secrets
import sys as _sys

_env = os.environ.get
_TESTING = len(_sys.argv) > 1 and _sys.argv[1] == 'test'


def _secret_key():
    key = _env('CLASSROOM_SECRET_KEY')
    if key:
        return key
    path = os.path.join(BASE_DIR, 'dmoj', '.dev_secret_key')  # noqa: F821 (BASE_DIR from settings.py)
    try:
        with open(path) as f:
            key = f.read().strip()
    except OSError:
        key = ''
    if not key:
        key = _secrets.token_urlsafe(50)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(key)
        except OSError:
            pass  # read-only checkout: a per-process key still works
    return key


SECRET_KEY = _secret_key()

# DEBUG is for local development only. Never deploy with this file.
DEBUG = _env('CLASSROOM_DEBUG', '1') == '1'
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]'] + [h for h in _env('CLASSROOM_ALLOWED_HOSTS', '').split(',') if h]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': _env('CLASSROOM_DB_NAME', 'classroom'),
        'USER': _env('CLASSROOM_DB_USER', 'classroom'),
        'PASSWORD': _env('CLASSROOM_DB_PASSWORD', 'classroom'),
        'HOST': _env('CLASSROOM_DB_HOST', '127.0.0.1'),
        'PORT': _env('CLASSROOM_DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
        },
        'TEST': {
            'CHARSET': 'utf8mb4',
            # Parallel worktrees each need their own test database.
            'NAME': _env('CLASSROOM_TEST_DB_NAME', 'test_classroom'),
        },
    },
}

_REDIS_URL = _env('CLASSROOM_REDIS_URL', 'redis://127.0.0.1:6379')

if _TESTING and _env('CLASSROOM_TEST_CACHE', 'locmem') != 'redis':
    # Same as upstream CI: tests expect a private in-process cache.
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': _REDIS_URL + '/0',
        },
    }

# Celery broker for dev. No worker runs unless started (scripts/dev-server.sh --celery);
# tests do not need a broker.
CELERY_BROKER_URL = _REDIS_URL + '/1'
CELERY_RESULT_BACKEND = _REDIS_URL + '/1'

# Static files / compressor (same as .ci.settings.py).
COMPRESS_OUTPUT_DIR = 'cache'
STATICFILES_FINDERS += ('compressor.finders.CompressorFinder',)  # noqa: F821
STATIC_ROOT = os.path.join(BASE_DIR, 'static')  # noqa: F821

# Mail goes to the console in dev.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# CLASSROOM_JUNIT=1 writes JUnit XML to test-reports/ like upstream CI (unittest-xml-reporting).
if _env('CLASSROOM_JUNIT') == '1':
    TEST_RUNNER = 'xmlrunner.extra.djangotestrunner.XMLTestRunner'
    TEST_OUTPUT_DIR = 'test-reports'
    TEST_OUTPUT_VERBOSE = 2

# Not configured locally (left at the defaults in dmoj/settings.py): the judge bridge and
# judge servers, the event daemon, S3/Box submission storage (falls back to local disk),
# PDF export via pdfoid, MOSS, and every LLM provider (LITELLM_*/AZURE_*).
