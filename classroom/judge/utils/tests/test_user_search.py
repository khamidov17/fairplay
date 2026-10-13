from django.test import TestCase

from judge.models.tests.util import create_user
from judge.utils.user_search import (
    format_user_label, profile_author_label, profile_choice_label, profile_choice_payload,
    profile_person_name,
)


class UserSearchLabelTest(TestCase):
    def test_format_always_includes_name_and_username(self):
        self.assertEqual(format_user_label('张三', 'alice'), '张三 (alice)')
        self.assertEqual(format_user_label('Name', 'username'), 'Name (username)')
        self.assertEqual(format_user_label('', 'alice'), 'alice (alice)')
        self.assertEqual(format_user_label('alice', 'alice'), 'alice (alice)')

    def test_person_name_prefers_display_override_then_full_name(self):
        self.assertEqual(profile_person_name('Given', 'Family', 'Override', 'alice'), 'Override')
        self.assertEqual(profile_person_name('Given', 'Family', '', 'alice'), 'Given Family')
        self.assertEqual(profile_person_name('张三', '', '', 'alice'), '张三')
        self.assertEqual(profile_person_name('', '', '', 'alice'), 'alice')

    def test_profile_choice_label_and_payload(self):
        user = create_user(username='alice', first_name='张三', last_name='')
        self.assertEqual(profile_choice_label(user.profile), '张三 (alice)')
        payload = profile_choice_payload(user.profile)
        self.assertEqual(payload['id'], user.profile.id)
        self.assertEqual(payload['username'], 'alice')
        self.assertEqual(payload['name'], '张三')
        self.assertEqual(payload['text'], '张三 (alice)')

        user.profile.username_display_override = 'Ada'
        user.profile.save(update_fields=['username_display_override'])
        user.profile.refresh_from_db()
        self.assertEqual(profile_choice_label(user.profile), 'Ada (alice)')

    def test_profile_author_label_uses_real_name_or_name_and_id(self):
        named = create_user(username='alice', first_name='张三')
        self.assertEqual(profile_author_label(named.profile), '张三 (alice)')

        unnamed = create_user(username='bobnet')
        self.assertEqual(profile_author_label(unnamed.profile), 'bobnet')
