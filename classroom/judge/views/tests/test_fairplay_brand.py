"""FairPlay name and look: the site no longer presents itself as DMOJ, ships its own icons and
fonts, and is light unless the user picks the dark theme in their profile."""
import re
from pathlib import Path

from django.conf import settings
from django.test import RequestFactory, TestCase

from judge.models.tests.util import CommonDataMixin, create_user

CLASSROOM = Path(settings.BASE_DIR)
RESOURCES = CLASSROOM / 'resources'


class BrandNameTestCase(CommonDataMixin, TestCase):
    def test_site_is_called_fairplay(self):
        self.assertEqual(settings.SITE_NAME, 'FairPlay')
        response = self.client.get('/')
        self.assertContains(response, '- FairPlay Classroom</title>')
        self.assertContains(response, 'class="site-brand-name">FairPlay<')
        self.assertNotContains(response, 'Modern Online Judge')
        self.assertNotContains(response, 'proudly powered by')

    def test_footer_keeps_the_dmoj_credit_and_source_link(self):
        # DMOJ is AGPL-3.0: the credit and the link to the source stay.
        response = self.client.get('/')
        self.assertContains(response, 'href="https://github.com/DMOJ/online-judge"')

    def test_no_dku_oj_name_in_user_visible_strings(self):
        offenders = []
        for root in (CLASSROOM / 'templates', CLASSROOM / 'judge'):
            for path in root.rglob('*'):
                if path.suffix not in ('.html', '.py', '.txt') or 'tests' in path.parts or 'migrations' in path.parts:
                    continue
                for line_no, line in enumerate(path.read_text(errors='ignore').splitlines(), 1):
                    # Internal identifiers (DKUOJ_*, window.DKUOJ...) and code comments are not shown
                    # to anyone; in Python only translated strings and output defaults count.
                    if path.suffix == '.py' and '_(' not in line and 'Score' not in line and '<p>' not in line:
                        continue
                    if re.search(r'DKU OJ|DKUOJ Score|Modern Online Judge', line):
                        offenders.append('%s:%d' % (path.relative_to(CLASSROOM), line_no))
        self.assertEqual(offenders, [])


class BrandAssetsTestCase(TestCase):
    def test_every_favicon_url_has_a_file(self):
        from dmoj.urls import favicon_paths
        missing = [name for name in favicon_paths if not (RESOURCES / 'icons' / name).is_file()]
        self.assertEqual(missing, [])

    def test_favicon_redirects_to_the_static_icon(self):
        response = self.client.get('/android-chrome-192x192.png')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('icons/android-chrome-192x192.png'))

    def test_fonts_are_self_hosted(self):
        css = (RESOURCES / 'fonts' / 'fonts.css').read_text()
        files = re.findall(r'url\(\./files/([^)]+)\)', css)
        self.assertTrue(files)
        self.assertEqual([f for f in files if not (RESOURCES / 'fonts' / 'files' / f).is_file()], [])
        self.assertIn("font-family: 'Plus Jakarta Sans'", css)
        self.assertNotIn('http', css.split('*/', 1)[1])

    def test_admin_loads_the_fairplay_theme(self):
        admin = create_user(username='fp_admin', is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        response = self.client.get('/admin/')
        self.assertContains(response, 'fairplay-admin.css')


class LightByDefaultTestCase(TestCase):
    def context_for(self, theme=None):
        from judge.template_context import site_theme
        request = RequestFactory().get('/')
        if theme is None:
            from django.contrib.auth.models import AnonymousUser
            request.user = AnonymousUser()
        else:
            user = create_user(username='fp_theme_' + theme)
            user.profile.site_theme = theme
            request.user = user
            request.profile = user.profile
        return site_theme(request)

    def test_anonymous_and_auto_get_the_light_theme(self):
        for theme in (None, 'auto', 'light'):
            ctx = self.context_for(theme)
            self.assertEqual(ctx['PREFERRED_STYLE_CSS'], settings.DMOJ_THEME_CSS['light'])
            self.assertFalse(ctx['DARK_MODE'])
            self.assertEqual(ctx['DARK_MEDIA'], 'not all')

    def test_dark_only_when_chosen(self):
        ctx = self.context_for('dark')
        self.assertEqual(ctx['PREFERRED_STYLE_CSS'], settings.DMOJ_THEME_CSS['dark'])
        self.assertTrue(ctx['DARK_MODE'])
        self.assertEqual(ctx['DARK_MEDIA'], 'all')

    def test_page_loads_one_stylesheet_and_marks_dark(self):
        response = self.client.get('/')
        self.assertNotContains(response, 'dark/style.css')
        self.assertNotContains(response, 'theme-dark')
        user = create_user(username='fp_dark_page')
        user.profile.site_theme = 'dark'
        user.profile.save()
        self.client.force_login(user)
        response = self.client.get('/')
        self.assertContains(response, 'dark/style.css')
        self.assertContains(response, 'class="theme-dark ')

    def test_ace_follows_the_site_theme(self):
        user = create_user(username='fp_ace')
        profile = user.profile
        profile.ace_theme = 'auto'
        profile.site_theme = 'auto'
        self.assertEqual(profile.resolved_ace_theme, settings.DMOJ_THEME_DEFAULT_ACE_THEME['light'])

    def test_nothing_follows_the_computers_dark_mode(self):
        offenders = []
        for root in (CLASSROOM / 'templates', RESOURCES):
            for path in root.rglob('*'):
                if path.suffix not in ('.html', '.scss', '.js') or 'libs' in path.parts or 'vendor' in path.parts:
                    continue
                if path.name in ('rich_editor.js',) or path.name.endswith('.min.js'):
                    continue
                for line_no, line in enumerate(path.read_text(errors='ignore').splitlines(), 1):
                    if 'prefers-color-scheme' in line:
                        offenders.append('%s:%d' % (path.relative_to(CLASSROOM), line_no))
        self.assertEqual(offenders, [])
