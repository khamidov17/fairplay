"""CA-9 (default secrets / DEBUG) and CA-10 (CI secrets and image pinning)."""
import os
import re
import subprocess
import sys

import yaml
from django.conf import settings
from django.test import SimpleTestCase

from dmoj.insecure_defaults import ALLOW_INSECURE_DEV_ENV, insecure_default_errors, insecure_defaults_allowed

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_KEY = 'default-key'
DEFAULT_EVENT_KEY = 'default-event-key'


class InsecureDefaultsTest(SimpleTestCase):
    def test_default_secret_key_refused(self):
        self.assertTrue(insecure_default_errors(DEFAULT_KEY, DEFAULT_KEY, False, 'x', DEFAULT_EVENT_KEY))
        self.assertTrue(insecure_default_errors('', DEFAULT_KEY, False, 'x', DEFAULT_EVENT_KEY))
        self.assertEqual(insecure_default_errors('unique', DEFAULT_KEY, False, 'x', DEFAULT_EVENT_KEY), [])

    def test_default_event_daemon_key_refused_when_daemon_used(self):
        self.assertTrue(insecure_default_errors('unique', DEFAULT_KEY, True, DEFAULT_EVENT_KEY, DEFAULT_EVENT_KEY))
        self.assertEqual(insecure_default_errors('unique', DEFAULT_KEY, True, 'other', DEFAULT_EVENT_KEY), [])

    def test_only_tests_or_explicit_opt_in_are_exempt(self):
        # DEBUG plays no part any more: a runserver/gunicorn start is refused.
        self.assertFalse(insecure_defaults_allowed(['manage.py', 'runserver'], {}))
        self.assertFalse(insecure_defaults_allowed(['gunicorn'], {'CLASSROOM_DEBUG': '1'}))
        self.assertTrue(insecure_defaults_allowed(['manage.py', 'test'], {}))
        self.assertTrue(insecure_defaults_allowed(['manage.py', 'runserver'], {ALLOW_INSECURE_DEV_ENV: '1'}))

    def test_settings_ship_debug_off(self):
        with open(os.path.join(ROOT, 'dmoj', 'settings.py')) as f:
            source = f.read()
        self.assertRegex(source, r'(?m)^DEBUG = False$')
        self.assertNotRegex(source, r'(?m)^DEBUG = True$')

    def test_settings_refuse_default_key_even_with_debug(self):
        """Import dmoj.settings in a child process with a local_settings that keeps the default key."""
        code = (
            'import sys, builtins\n'
            'real_open = builtins.open\n'
            'def fake_open(path, *a, **k):\n'
            '    if str(path).endswith("local_settings.py"):\n'
            '        import io; return io.StringIO("DEBUG = True\\n")\n'
            '    return real_open(path, *a, **k)\n'
            'builtins.open = fake_open\n'
            'sys.argv = ["manage.py", "runserver"]\n'
            'try:\n'
            '    import dmoj.settings\n'
            'except Exception as e:\n'
            '    print(type(e).__name__); sys.exit(3)\n'
            'print("started"); sys.exit(0)\n'
        )
        env = {k: v for k, v in os.environ.items() if k != ALLOW_INSECURE_DEV_ENV}
        proc = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn('ImproperlyConfigured', proc.stdout)

    def test_impersonation_logging_enabled(self):
        self.assertFalse(settings.IMPERSONATE.get('DISABLE_LOGGING', False))


class GitLabCITest(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(os.path.join(ROOT, '.gitlab-ci.yml')) as f:
            cls.ci = yaml.safe_load(f)

    def _images(self):
        for name, job in self.ci.items():
            if not isinstance(job, dict):
                continue
            image = job.get('image')
            if isinstance(image, dict):
                image = image.get('name')
            if image and not image.startswith('$'):
                yield name, image
            for service in job.get('services', ()) or ():
                yield name, service['name'] if isinstance(service, dict) else service
            for entry in (job.get('parallel') or {}).get('matrix', ()) or ():
                for value in entry.get('PY_IMAGE', ()):
                    yield name, value

    def test_images_are_pinned(self):
        images = list(self._images())
        self.assertTrue(images)
        for job, image in images:
            with self.subTest(job=job, image=image):
                self.assertNotIn(':latest', image)
                self.assertTrue('@sha256:' in image or re.search(r':v?\d+\.\d+', image),
                                'image must be pinned to an exact version or digest')

    def test_secret_jobs_run_on_protected_refs_only(self):
        for name, job in self.ci.items():
            if not isinstance(job, dict) or name.startswith('.'):
                continue
            text = yaml.safe_dump(job)
            uses_secret = any(s in text for s in ('GITLAB_PERSONAL_ACCESS_TOKEN', 'LLM_API_KEY',
                                                  'CI_REGISTRY_PASSWORD', 'BACKUP_S3_SECRET_ACCESS_KEY'))
            deploy_runner = 'dku-deploy' in text or job.get('extends') in ('.deploy', '.build-image')
            if not (uses_secret or deploy_runner):
                continue
            rules = job.get('rules')
            if rules is None and job.get('extends'):
                rules = self.ci[job['extends']].get('rules')
            with self.subTest(job=name):
                self.assertTrue(rules)
                for rule in rules:
                    if rule.get('when') == 'never':
                        continue
                    self.assertIn('$CI_COMMIT_REF_PROTECTED == "true"', rule.get('if', ''))
