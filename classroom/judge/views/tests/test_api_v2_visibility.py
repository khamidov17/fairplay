"""CA-6: API v2 must respect scoreboard visibility and course rosters."""
from django.test import TestCase
from django.utils import timezone

from judge.models import Contest
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, \
    create_organization, create_user


class APIScoreboardVisibilityTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        past = {'start_time': now - timezone.timedelta(days=10), 'end_time': now - timezone.timedelta(days=5),
                'is_visible': True}
        cls.hidden = create_contest(key='api_hidden_hw', scoreboard_visibility=Contest.SCOREBOARD_HIDDEN, **past)
        cls.visible = create_contest(key='api_visible_hw', scoreboard_visibility=Contest.SCOREBOARD_VISIBLE, **past)
        cls.after = create_contest(key='api_after_hw', scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST, **past)
        cls.alice = create_user(username='api_alice')
        cls.bob = create_user(username='api_bob')
        for contest in (cls.hidden, cls.visible, cls.after):
            p = create_contest_participation(contest=contest, user='api_alice')
            p.score = 42
            p.save()

    def _participation_keys(self, user=None, **params):
        if user is not None:
            self.client.force_login(user)
        response = self.client.get('/api/v2/participations', params)
        self.assertEqual(response.status_code, 200)
        return {row['contest'] for row in response.json()['data']['objects']}

    def test_hidden_scoreboard_participations_not_listed(self):
        keys = self._participation_keys(self.bob)
        self.assertNotIn('api_hidden_hw', keys)
        self.assertIn('api_visible_hw', keys)
        self.assertIn('api_after_hw', keys)
        self.assertEqual(self._participation_keys(contest='api_hidden_hw'), set())

    def test_hidden_scoreboard_visible_to_editor(self):
        editor = create_user(username='api_editor', user_permissions=('edit_own_contest',))
        self.hidden.authors.add(editor.profile)
        self.assertIn('api_hidden_hw', self._participation_keys(editor))

    def test_user_detail_contest_history_respects_hidden_scoreboard(self):
        self.client.force_login(self.bob)
        data = self.client.get('/api/v2/user/api_alice').json()['data']['object']
        keys = {row['key'] for row in data['contests']}
        self.assertNotIn('api_hidden_hw', keys)
        self.assertIn('api_visible_hw', keys)


class APIRosterTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.course = create_organization(name='api_course', admins=('staff_organization_admin',))
        cls.student = create_user(username='api_roster_student')
        cls.course.members.add(cls.student.profile)

    def test_anonymous_and_students_cannot_filter_users_by_course(self):
        response = self.client.get('/api/v2/users', {'organization': self.course.id})
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.users['normal'])
        response = self.client.get('/api/v2/users', {'organization': self.course.id})
        self.assertEqual(response.status_code, 403)

    def test_course_admin_can_list_roster(self):
        self.client.force_login(self.users['staff_organization_admin'])
        response = self.client.get('/api/v2/users', {'organization': self.course.id})
        self.assertEqual(response.status_code, 200)
        names = {row['username'] for row in response.json()['data']['objects']}
        self.assertIn('api_roster_student', names)

    def test_user_detail_hides_course_ids_from_other_students(self):
        self.client.force_login(self.users['normal'])
        data = self.client.get('/api/v2/user/api_roster_student').json()['data']['object']
        self.assertEqual(data['organizations'], [])
        self.client.force_login(self.student)
        data = self.client.get('/api/v2/user/api_roster_student').json()['data']['object']
        self.assertEqual(data['organizations'], [self.course.id])
