"""China time is the default; every user can still pick their own time zone."""
import importlib

from django.apps import apps as global_apps
from django.test import TestCase
from django.urls import reverse

from judge.models import Language, Profile
from judge.models.tests.util import CommonDataMixin, create_user


class DefaultTimeZoneTestCase(CommonDataMixin, TestCase):
    def test_new_profile_defaults_to_china_time(self):
        user = create_user(username='tz_new')
        self.assertEqual(Profile.objects.get(user=user).timezone, 'Asia/Shanghai')

    def test_teacher_changes_their_own_time_zone(self):
        teacher = create_user(username='tz_teacher', user_permissions=('edit_own_contest',))
        self.client.force_login(teacher)
        page = self.client.get(reverse('user_edit_profile'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Asia/Shanghai')
        response = self.client.post(reverse('user_edit_profile'), {
            'timezone': 'Asia/Tashkent', 'language': Language.get_default_language().id,
            'ace_theme': 'auto', 'site_theme': 'auto', 'math_engine': 'auto',
            'about': '', 'user_script': '',
        })
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(Profile.objects.get(user=teacher).timezone, 'Asia/Tashkent')

    def test_migration_moves_only_the_old_default(self):
        toronto = create_user(username='tz_toronto')
        chosen = create_user(username='tz_chosen')
        Profile.objects.filter(user=toronto).update(timezone='America/Toronto')
        Profile.objects.filter(user=chosen).update(timezone='Europe/London')
        migration = importlib.import_module('judge.migrations.0225_default_time_zone_shanghai')
        migration.toronto_to_shanghai(global_apps, None)
        self.assertEqual(Profile.objects.get(user=toronto).timezone, 'Asia/Shanghai')
        self.assertEqual(Profile.objects.get(user=chosen).timezone, 'Europe/London')
