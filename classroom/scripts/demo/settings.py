# Appended to dmoj/local_settings.py by scripts/demo/prepare.sh (Dockerfile.demo only).
import os as _os  # noqa: E402

# Off unless CLASSROOM_DEBUG=1 (only the seed step at start sets it).
DEBUG = _os.environ.get('CLASSROOM_DEBUG', '0') == '1'
ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = ['https://*.onrender.com', 'http://localhost:8000', 'http://127.0.0.1:8000'] + [
    o for o in _os.environ.get('DEMO_TRUSTED_ORIGINS', '').split(',') if o]
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
