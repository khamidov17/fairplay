"""Timezone semantics of the contest detail page.

Locks in two behaviours that look contradictory but are both correct:

1. The countdown ("Contest ends in X") is an *absolute duration* —
   ``end_time - now`` — so it must be identical in every timezone. If a
   contest ends in 11 hours in Toronto it also ends in 11 hours in Shanghai.
2. The wall-clock dates rendered from ``contest.start_time``/``end_time``
   (the "16 days long starting on ..." line) *do* shift with the viewer's
   profile timezone, because that is a wall-clock rendering of an instant.

These tests pin both so nobody "fixes" the timezone-independence of the
countdown by accident (which would be a real bug).
"""

import re
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models.tests.util import CommonDataMixin, create_contest, create_user


class ContestDetailTimezoneTestCase(CommonDataMixin, TestCase):
    """A running contest whose detail page has both a countdown and dates."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = create_user(username='tz_viewer')
        _now = timezone.now()
        cls.contest = create_contest(
            key='tzrunning',
            name='TZ Running',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(hours=11),
            is_visible=True,
        )

    def setUp(self):
        # Use the default test host (`testserver`). Pinning a production IP here
        # breaks CI, where ALLOWED_HOSTS is empty (`.ci.settings.py`) and only
        # testserver/.localhost/127.0.0.1 are allowed → every request got a 400.
        self.client = Client()

    def _get_detail(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('contest_view', args=['tzrunning']))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _countdown_secs(self, html):
        m = re.search(r'data-secs="([0-9.]+)"', html)
        self.assertIsNotNone(m, 'countdown data-secs not found in page')
        return float(m.group(1))

    def test_countdown_is_timezone_independent(self):
        """The remaining-time seconds must not depend on the viewer's timezone.

        ``contest.time_before_end`` is ``end_time - now``: an absolute span.
        Showing 11h in Toronto and 13h in Shanghai for the same contest would
        be a bug, so the countdown must stay put when the timezone changes.
        """
        secs = {}
        for tz in ('America/Toronto', 'Asia/Shanghai', 'Pacific/Honolulu'):
            self.user.profile.timezone = tz
            self.user.profile.save()
            secs[tz] = self._countdown_secs(self._get_detail())

        spread = max(secs.values()) - min(secs.values())
        # Allow a couple of seconds for wall-clock time passing between the
        # three requests; anything bigger means the countdown shifted with tz.
        self.assertLess(spread, 5.0, 'countdown drifted across timezones: %r' % secs)

    def test_countdown_matches_model_time_before_end(self):
        """Sanity: the rendered seconds equal ``contest.time_before_end``."""
        self.user.profile.timezone = 'America/Toronto'
        self.user.profile.save()
        html = self._get_detail()
        rendered = self._countdown_secs(html)
        expected = self.contest.time_before_end.total_seconds()
        self.assertAlmostEqual(rendered, expected, delta=5.0)

    def test_wall_clock_dates_shift_with_timezone(self):
        """The "starting on" date is a wall-clock rendering and must shift.

        Same UTC instant, two different zones → different local date strings
        (e.g. 10:56 EDT vs 22:56 CST for the same 02:56 UTC instant).
        """
        dates = {}
        for tz in ('America/Toronto', 'Asia/Shanghai'):
            self.user.profile.timezone = tz
            self.user.profile.save()
            html = self._get_detail()
            m = re.search(r'<div id="time">(.*?)</div>', html, re.S)
            self.assertIsNotNone(m, '#time block not found')
            dates[tz] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', m.group(1))).strip()

        self.assertNotEqual(
            dates['America/Toronto'], dates['Asia/Shanghai'],
            '#time dates should differ across timezones: %r' % dates,
        )
        # Same instant: the two zones are 12h apart, so the minutes must match.
        import re as _re
        m1 = _re.search(r'(\d{2}):(\d{2})', dates['America/Toronto'])
        m2 = _re.search(r'(\d{2}):(\d{2})', dates['Asia/Shanghai'])
        self.assertEqual(m1.group(2), m2.group(2), 'minutes of same instant must match')

    def test_edit_form_echo_uses_profile_timezone(self):
        """The editor's Validity fields echo stored UTC in the profile tz."""
        self.user.profile.timezone = 'America/Toronto'
        self.user.profile.save()
        # Grant object-level editing: edit_own_contest permission + curator role.
        from django.contrib.auth.models import Permission
        perm = Permission.objects.get(codename='edit_own_contest')
        self.user.user_permissions.add(perm)
        self.contest.curators.add(self.user.profile)
        self.client.force_login(self.user)
        response = self.client.get(reverse('contest_edit', args=['tzrunning']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['user_timezone'], 'America/Toronto')
        self.contest.refresh_from_db()
        expected = self.contest.start_time.astimezone(
            __import__('zoneinfo').ZoneInfo('America/Toronto')).strftime('%Y-%m-%dT%H:%M')
        self.assertEqual(response.context['prefill']['start_time'], expected)
