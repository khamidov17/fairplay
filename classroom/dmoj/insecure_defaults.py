"""Start-up refusal of the well-known development secrets (CA-9).

Kept out of settings.py so the rule can be unit-tested without re-importing settings.
"""
import sys

ALLOW_INSECURE_DEV_ENV = 'CLASSROOM_ALLOW_INSECURE_DEV'


def running_tests(argv=None):
    argv = sys.argv if argv is None else argv
    return len(argv) > 1 and argv[1] == 'test'


def insecure_defaults_allowed(argv=None, environ=None):
    """Only the test runner, or an explicit opt-in for a throwaway local box, may use them.

    DEBUG does not matter: a DEBUG = True site is still reachable and its sessions,
    CSRF tokens and password-reset links are just as forgeable.
    """
    if environ is None:
        import os
        environ = os.environ
    return running_tests(argv) or environ.get(ALLOW_INSECURE_DEV_ENV) == '1'


def insecure_default_errors(secret_key, insecure_secret_key, event_daemon_use,
                            event_daemon_submission_key, insecure_submission_key):
    errors = []
    if not secret_key or secret_key == insecure_secret_key:
        errors.append(
            'SECRET_KEY is still the well-known development default; set a unique value in '
            'local_settings.py (or set %s=1 on a throwaway local machine).' % ALLOW_INSECURE_DEV_ENV)
    if event_daemon_use and event_daemon_submission_key == insecure_submission_key:
        errors.append(
            'EVENT_DAEMON_SUBMISSION_KEY is still the well-known development default; set a unique '
            'value in local_settings.py before enabling the event daemon.')
    return errors
