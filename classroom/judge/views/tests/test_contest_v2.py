import json
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import Contest, ContestSubmission, ContestTag, Language, Organization, Submission
from judge.models.profile import Class
from judge.models.tests.util import (
    CommonDataMixin,
    create_contest,
    create_contest_participation,
    create_contest_problem,
    create_problem,
    create_user,
)


class ContestListV2ViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users.update({
            'staff_contest_edit_all': create_user(
                username='staff_contest_edit_all',
                is_staff=True,
                user_permissions=('edit_own_contest', 'edit_all_contest'),
            ),
            'normal_user': create_user(username='normal_user'),
        })

        _now = timezone.now()

        cls.past_contest = create_contest(
            key='past_contest',
            name='Past Contest',
            start_time=_now - timedelta(days=10),
            end_time=_now - timedelta(days=5),
            is_visible=True,
            authors=('superuser',),
        )

        cls.ongoing_contest = create_contest(
            key='ongoing_contest',
            name='Ongoing Contest',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True,
            authors=('superuser',),
        )

        cls.future_contest = create_contest(
            key='future_contest',
            name='Future Contest',
            start_time=_now + timedelta(days=5),
            end_time=_now + timedelta(days=10),
            is_visible=True,
            authors=('superuser',),
        )

        cls.tag1 = ContestTag.objects.create(name='tag1', color='#ff0000')
        cls.tag2 = ContestTag.objects.create(name='tag2', color='#00ff00')
        cls.past_contest.tags.add(cls.tag1)
        cls.ongoing_contest.tags.add(cls.tag2)

    def setUp(self):
        self.client = Client()

    def test_status_code(self):
        response = self.client.get(reverse('contest_list'))
        self.assertEqual(response.status_code, 200)

    def test_hidden_badge_shows_for_any_time_status(self):
        now = timezone.now()
        hidden_ongoing = create_contest(
            key='hidden_ongoing',
            name='Hidden Ongoing',
            start_time=now - timedelta(days=1),
            end_time=now + timedelta(days=1),
            is_visible=False,
            authors=('superuser',),
        )
        hidden_future = create_contest(
            key='hidden_future',
            name='Hidden Future',
            start_time=now + timedelta(days=2),
            end_time=now + timedelta(days=3),
            is_visible=False,
            authors=('superuser',),
        )
        self.client.force_login(self.users['superuser'])
        html = self.client.get(reverse('contest_list')).content.decode()

        def row(key):
            start = html.index('data-key="%s"' % key)
            end = html.index('</article>', start)
            return html[start:end]

        self.assertIn('fa-eye-slash', row(hidden_ongoing.key))
        self.assertIn('fa-eye-slash', row(hidden_future.key))
        self.assertNotIn('fa-eye-slash', row(self.ongoing_contest.key))

    def test_template(self):
        response = self.client.get(reverse('contest_list'))
        self.assertTemplateUsed(response, 'contest/list-v2.html')

    def test_context(self):
        response = self.client.get(reverse('contest_list'))
        context = response.context
        self.assertIn('past_contests', context)
        self.assertIn('current_contests', context)
        self.assertIn('future_contests', context)
        self.assertIn('organizations', context)
        self.assertIn('tags', context)
        self.assertIn('total_count', context)

    def test_search(self):
        response = self.client.get(reverse('contest_list') + '?search=past')
        self.assertEqual(response.status_code, 200)

    def test_search_box_preserves_full_query(self):
        """Committed keywords are listed in JS state, not stuffed into the live input."""
        response = self.client.get(reverse('contest_list') + '?search=question')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "keywords: ['question']", html=False)
        self.assertContains(response, 'dkuojBindKeywordEnter', html=False)
        self.assertContains(response, 'Type a keyword, then press Enter')

    def test_selected_row_rebuilds_filter_chips_in_js(self):
        response = self.client.get(reverse('contest_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="active-filters"', html)
        self.assertIn('No filters applied', html)
        self.assertIn('renderActiveFilters', html)
        self.assertIn('placeholder.hidden', html)
        self.assertNotIn('placeholder.style.display', html)

    def test_sort(self):
        response = self.client.get(reverse('contest_list') + '?sort=name')
        self.assertEqual(response.status_code, 200)

    def test_course_group_head_renders(self):
        """By-course grouping (the default view) renders a course header
        labelled with the org name and places the assignment in that section."""
        create_contest(
            key='org_course_contest', name='Org Course Contest',
            organizations=('open',), is_visible=True,
        )
        self.client.force_login(self.users['normal_user'])
        response = self.client.get(reverse('contest_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<div class="cl-group-head">', html)
        # The course section header carries the org name...
        self.assertIn('<i class="fa fa-book"></i> open</span>', html)
        # ...and the assignment card is present inside that section.
        self.assertIn('Org Course Contest', html)

    def test_solved_progress_not_shown_on_rows(self):
        """Assignment-list rows no longer show a per-assignment Solved x/y."""
        self.client.force_login(self.users['normal_user'])
        response = self.client.get(reverse('contest_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('class="cl-solved-col"', html)
        self.assertNotIn('class="cl-solved"', html)
        self.assertNotIn('Solved 0/', html)
        self.assertIn('class="cl-trailing"', html)
        self.assertLess(html.index('class="cl-users"'), html.index('class="cl-actions"'))

    def test_class_checkbox_includes_data_org(self):
        """Class filter options carry data-org so the course cascade can hide them."""
        org = self.organizations['open']
        klass = Class.objects.create(organization=org, name='Open Class', slug='open-class')
        contest = create_contest(
            key='class_org_contest', name='Class Org Contest',
            organizations=('open',), is_visible=True,
        )
        contest.classes.add(klass)
        self.client.force_login(self.users['normal_user'])
        response = self.client.get(reverse('contest_list'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('data-org="open"', html)
        self.assertIn('value="Open Class"', html)
        classes = list(response.context['classes'])
        self.assertTrue(any(c.pk == klass.pk and c.organization_id == org.id for c in classes))

    def test_teacher_sees_assignment_author_filter(self):
        """Teachers get an author filter built from visible contest authors."""
        author = self.users['superuser']
        author.first_name = '张三'
        author.save(update_fields=['first_name'])
        self.client.force_login(self.users['staff_problem_edit_own'])
        response = self.client.get(reverse('contest_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_see_problem_authors'])
        authors = list(response.context['contest_authors'])
        self.assertTrue(any(a.user.username == 'superuser' for a in authors))
        html = response.content.decode()
        self.assertIn('id="filter-author"', html)
        self.assertIn('data-authors="%s"' % author.profile.id, html)
        self.assertIn('张三 (superuser)', html)
        self.assertIn('cl-authors', html)
        self.assertNotIn('id="author-multi-select"', html)

    def test_teacher_selected_author_keeps_real_name(self):
        """A selected author option/chip must keep the real name, not just the id."""
        author = self.users['superuser']
        author.first_name = '张三'
        author.save(update_fields=['first_name'])
        self.client.force_login(self.users['staff_problem_edit_own'])
        response = self.client.get(reverse('contest_list'), {'author': str(author.profile.id)})
        self.assertEqual(response.status_code, 200)
        profiles = list(response.context['selected_author_profiles'])
        self.assertEqual([profile.id for profile in profiles], [author.profile.id])
        html = response.content.decode()
        self.assertRegex(
            html,
            r'<option value="%s" selected>\s*张三 \(superuser\)\s*</option>' % author.profile.id,
        )

    def test_student_hides_assignment_author_filter(self):
        """Students do not see the author filter; a URL author param is ignored."""
        self.client.force_login(self.users['normal_user'])
        response = self.client.get(reverse('contest_list') + '?author=1')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_see_problem_authors'])
        self.assertEqual(list(response.context['contest_authors']), [])
        self.assertEqual(list(response.context['selected_author_profiles']), [])
        html = response.content.decode()
        self.assertNotIn('id="filter-author"', html)
        self.assertNotIn('id="author-multi-select"', html)
        self.assertNotIn('data-authors=', html)
        self.assertNotIn('cl-authors', html)


class ContestCreateViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users.update({
            # Full contest editor: edit_all_contest plus the granular permissions
            # the Django admin gates behind (visibility, access codes,
            # ratings, org scoping...). Without them, ContestCreate._restricted_fields
            # correctly refuses to write those fields — so tests asserting
            # is_visible / organizations are saved must grant them.
            'staff_contest_edit_all': create_user(
                username='staff_contest_edit_all',
                is_staff=True,
                user_permissions=(
                    'add_contest', 'edit_own_contest', 'edit_all_contest', 'contest_rating',
                    'lock_contest', 'contest_access_code',
                    'create_private_contest', 'change_contest_visibility',
                    'contest_problem_label', 'override_performance_ceiling',
                ),
            ),
            'normal_user': create_user(username='normal_user'),
            # Holds only create_private_contest: Admin add is add_contest, so
            # this user must NOT get the creator entry.
            'contest_creator': create_user(
                username='contest_creator',
                user_permissions=('create_private_contest',),
            ),
            'add_contest_only': create_user(
                username='add_contest_only',
                user_permissions=('add_contest',),
            ),
            # edit_own_contest but NOT add_contest: must NOT see the create entry.
            'edit_own_only': create_user(
                username='edit_own_only',
                user_permissions=('edit_own_contest',),
            ),
        })

        cls.organization = Organization.objects.create(name='Test Org', slug='test-org')
        cls.tag = ContestTag.objects.create(name='test-tag', color='#0000ff')

    def setUp(self):
        self.client = Client()

    def test_get_status_code(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 200)

    def test_get_template(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        response = self.client.get(reverse('contest_create'))
        self.assertTemplateUsed(response, 'contest/create.html')

    def test_get_unauthorized(self):
        self.client.force_login(self.users['normal_user'])
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 403)

    def test_create_private_contest_does_not_grant_add(self):
        """``create_private_contest`` alone matches Admin field access, not Add."""
        self.client.force_login(self.users['contest_creator'])
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 403)

    def test_add_contest_grants_access(self):
        """Frontend Add matches ContestAdmin.has_add_permission (``add_contest``)."""
        self.client.force_login(self.users['add_contest_only'])
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 200)

    def test_edit_own_contest_without_add_is_denied(self):
        """``edit_own_contest`` alone must NOT grant the creator entry."""
        self.client.force_login(self.users['edit_own_only'])
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 403)

    def test_add_assignment_tab_gated_on_add_contest(self):
        """The 'Add Assignment' tab renders for add_contest holders and is absent otherwise."""
        self.client.force_login(self.users['add_contest_only'])
        html = self.client.get(reverse('contest_list')).content.decode()
        self.assertIn('/contests/create/', html)
        self.assertIn('Add Assignment', html)

        self.client.force_login(self.users['edit_own_only'])
        html = self.client.get(reverse('contest_list')).content.decode()
        self.assertNotIn('/contests/create/', html)

        self.client.force_login(self.users['contest_creator'])
        html = self.client.get(reverse('contest_list')).content.decode()
        self.assertNotIn('/contests/create/', html)

    def test_instructors_group_can_create_assignment(self):
        """Instructors include add_contest, matching Admin add and live Instructor."""
        from django.contrib.auth.models import Group
        from judge.models.notification import INSTRUCTORS_GROUP, ensure_instructors_group

        ensure_instructors_group()
        teacher = create_user(username='site_teacher_assignment')
        Group.objects.get(name=INSTRUCTORS_GROUP).user_set.add(teacher)
        self.client.force_login(teacher)
        self.assertEqual(self.client.get(reverse('contest_create')).status_code, 200)
        html = self.client.get(reverse('contest_list')).content.decode()
        self.assertIn('/contests/create/', html)
        self.assertIn('Add Assignment', html)

    def test_post_success(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'New Contest',
            'key': 'newcontest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
            'description': 'A test contest',
            'is_visible': True,
            'is_rated': False,
            'format_name': 'default',
            'scoreboard_visibility': 'V',
            'use_clarifications': True,
            'allow_virtual_participation': True,
            'points_precision': 3,
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['ok'])
        self.assertIn('redirect', result)

        contest = Contest.objects.get(key='newcontest')
        self.assertEqual(contest.name, 'New Contest')
        self.assertTrue(contest.is_visible)
        self.assertEqual(contest.authors.count(), 1)

    def test_post_missing_fields(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        data = {'name': 'Incomplete Contest'}

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_post_invalid_key(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'Invalid Key Contest',
            'key': 'INVALID-KEY!',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('lowercase', response.json()['error'])

    def test_post_duplicate_key(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        create_contest(
            key='existingcontest',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('superuser',),
        )

        now = timezone.now()
        data = {
            'name': 'Duplicate',
            'key': 'existingcontest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('already exists', response.json()['error'])

    def test_post_invalid_dates(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'Bad Dates',
            'key': 'baddates',
            'start_time': (now + timedelta(hours=3)).isoformat(),
            'end_time': (now + timedelta(hours=1)).isoformat(),
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('before end time', response.json()['error'])

    def test_post_unauthorized(self):
        self.client.force_login(self.users['normal_user'])
        now = timezone.now()
        data = {
            'name': 'Unauthorized',
            'key': 'unauthorized',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_post_with_duration(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'Duration Contest',
            'key': 'durationcontest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
            'duration_hours': 2,
            'duration_minutes': 30,
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='durationcontest')
        self.assertEqual(contest.time_limit, timedelta(hours=2, minutes=30))

    def test_post_with_organizations(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'Org Contest',
            'key': 'orgcontest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
            'organizations': [self.organization.id],
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='orgcontest')
        self.assertIn(self.organization, contest.organizations.all())

    def test_post_with_tags(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'Tagged Contest',
            'key': 'taggedcontest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
            'tags': [self.tag.id],
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='taggedcontest')
        self.assertIn(self.tag, contest.tags.all())

    def test_post_author_set(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        now = timezone.now()
        data = {
            'name': 'Author Test',
            'key': 'authortest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
        }

        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(data),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='authortest')
        self.assertEqual(contest.authors.first(), self.users['staff_contest_edit_all'].profile)


class ContestDetailSolvedStatusTestCase(TestCase):
    """Detail page solved/attempted context + that the status column is removed.

    The solved/attempted *context* sets are still computed (server-side gating for
    exam-style hidden results), but the assignment page no longer renders them to
    students — the "Status" column was removed so students cannot directly observe
    a per-problem status that would bypass the hidden-results permission design.

    Rendered with social + comments disabled so the page only goes through the
    Jinja2 engine — on Python 3.14 + Django 4.2 the Django-DTL error path crashes
    the test client's template-context copy (pre-existing env issue), which would
    mask the real assertion below.
    """

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        _now = timezone.now()
        cls.contest = create_contest(
            key='statcontest', name='Stats',
            start_time=_now - timedelta(days=1), end_time=_now + timedelta(days=1),
            is_visible=True, description='',
        )
        cls.solved_problem = create_problem('solved-problem')
        cls.attempted_problem = create_problem('attempted-problem')
        from judge.models import ContestProblem, Language
        cls.cp_solved = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.solved_problem, points=100, order=1)
        cls.cp_attempted = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.attempted_problem, points=100, order=2)
        cls.user = create_user(username='status_user')
        # AC (full points) → solved; WA → attempted only.
        cls.ac = Submission.objects.create(
            user=cls.user.profile, problem=cls.solved_problem,
            language=Language.objects.get(key='PY3'),
            result='AC', status='D', contest_object=cls.contest,
            case_points=100, case_total=100,
        )
        cls.wa = Submission.objects.create(
            user=cls.user.profile, problem=cls.attempted_problem,
            language=Language.objects.get(key='PY3'),
            result='WA', status='D', contest_object=cls.contest,
            case_points=0, case_total=100,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _get_detail(self):
        return self.client.get(reverse('contest_view', args=['statcontest']))

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_solved_and_attempted_sets(self):
        response = self._get_detail()
        self.assertEqual(response.status_code, 200)
        solved = response.context['contest_solved_problem_ids']
        attempted = response.context['contest_attempted_problem_ids']
        self.assertIn(self.solved_problem.id, solved)
        self.assertNotIn(self.attempted_problem.id, solved)
        self.assertIn(self.solved_problem.id, attempted)
        self.assertIn(self.attempted_problem.id, attempted)

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_status_column_removed(self):
        """The assignment page no longer renders solved/attempted status to
        students — it must not bypass the hidden-results permission design."""
        response = self._get_detail()
        html = response.content.decode()
        self.assertNotIn('solved-problem-color fa fa-check-circle', html)
        self.assertNotIn('attempted-problem-color fa fa-minus-circle', html)
        self.assertNotIn('col-status', html)

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_empty_description_renders_placeholder(self):
        """A contest without a statement must show a placeholder, not an empty box."""
        response = self._get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertIn('No description.', response.content.decode())

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_anonymous_gets_no_solved_state(self):
        client = Client()
        response = client.get(reverse('contest_view', args=['statcontest']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['contest_solved_problem_ids'], set())
        self.assertEqual(response.context['contest_attempted_problem_ids'], set())


class ContestDetailExamHiddenResultsTestCase(TestCase):
    """Exam-style contests must not leak verdicts through the checkmark before release."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        _now = timezone.now()
        cls.contest = create_contest(
            key='examcontest', name='Exam',
            start_time=_now - timedelta(days=1), end_time=_now + timedelta(days=1),
            is_visible=True, format_name='exam',
        )
        cls.problem = create_problem('exam-problem')
        from judge.models import ContestProblem, Language
        cls.cp = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.problem, points=100, order=1,
            hide_results=True,
        )
        cls.user = create_user(username='exam_user')
        cls.sub = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem,
            language=Language.objects.get(key='PY3'),
            result='AC', status='D', contest_object=cls.contest,
            case_points=100, case_total=100,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _get_detail(self):
        return self.client.get(reverse('contest_view', args=['examcontest']))

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_solved_hidden_before_finalize(self):
        """Before release the checkmark must be withheld (exam integrity)."""
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.results_finalized)
        response = self._get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['contest_solved_problem_ids'], set())

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_solved_revealed_after_finalize(self):
        self.contest.results_finalized = True
        self.contest.save(update_fields=['results_finalized'])
        response = self._get_detail()
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.problem.id, response.context['contest_solved_problem_ids'])


def _assignment_row_html(html, key):
    marker = 'data-key="%s"' % key
    start = html.index(marker)
    article_start = html.rfind('<article', 0, start)
    article_end = html.index('</article>', start) + len('</article>')
    return html[article_start:article_end]


class ContestListV2ActionsTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users.update({
            'staff_contest_edit_all': create_user(
                username='list_actions_editor',
                is_staff=True,
                user_permissions=('edit_own_contest', 'edit_all_contest', 'clone_contest'),
            ),
            'staff_can_delete': create_user(
                username='list_actions_deleter',
                is_staff=True,
                user_permissions=('edit_own_contest', 'edit_all_contest', 'delete_contest', 'clone_contest'),
            ),
            'normal_user': create_user(username='list_actions_student'),
        })
        _now = timezone.now()
        cls.ongoing = create_contest(
            key='actions_ongoing',
            name='Actions Ongoing',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True,
            authors=('superuser',),
        )
        cls.upcoming = create_contest(
            key='actions_upcoming',
            name='Actions Upcoming',
            start_time=_now + timedelta(days=5),
            end_time=_now + timedelta(days=10),
            is_visible=True,
            authors=('superuser',),
        )
        cls.past = create_contest(
            key='actions_past',
            name='Actions Past',
            start_time=_now - timedelta(days=10),
            end_time=_now - timedelta(days=5),
            is_visible=True,
            authors=('superuser',),
            allow_virtual_participation=True,
        )

    def setUp(self):
        self.client = Client()

    def test_join_actions_wrappers_always_rendered(self):
        self.client.force_login(self.users['normal_user'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        self.assertIn('class="cl-join-actions"', row)
        self.assertIn('class="cl-manage-actions"', row)

    def test_student_sees_join_not_manage_actions(self):
        self.client.force_login(self.users['normal_user'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        self.assertIn('>Join</button>', row)
        self.assertNotIn('fa-pencil', row)
        self.assertNotIn('fa-copy', row)
        self.assertNotIn('js-del-assignment"', row)
        self.assertNotIn('fa-check-square-o', row)
        self.assertNotIn('>Grade<', row)
        self.assertNotIn('class="assignment-select"', row)
        self.assertNotIn(reverse('contest_edit', args=[self.ongoing.key]), row)
        self.assertNotIn('id="bulk-delete-btn"', html)
        self.assertNotIn('cl-list--selectable', html)

    def test_manage_comes_before_join_actions(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        self.assertLess(row.index('class="cl-manage-actions"'), row.index('class="cl-join-actions"'))
        join_idx = row.find('>Join</button>')
        if join_idx < 0:
            join_idx = row.find('Spectate')
        self.assertGreater(join_idx, -1)
        self.assertLess(row.index('fa-pencil'), join_idx)

    def test_editor_sees_icon_only_edit_clone_and_grade(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        self.assertIn('fa-pencil', row)
        self.assertIn(reverse('contest_edit', args=[self.ongoing.key]), row)
        self.assertNotIn('fa-edit', row)
        self.assertIn('title="Edit"', row)
        self.assertIn('fa-copy', row)
        self.assertIn(reverse('contest_clone', args=[self.ongoing.key]), row)
        self.assertNotIn('/admin/judge/contest/', row)
        self.assertNotIn('js-del-assignment', row)
        self.assertNotIn('>Grade<', row)
        self.assertIn('fa-check-square-o', row)
        self.assertIn('title="Grade"', row)
        self.assertIn('aria-label="Grade"', row)
        self.assertIn(reverse('contest_grade', args=[self.ongoing.key]), row)
        manage = row[row.index('class="cl-manage-actions"'):row.index('class="cl-join-actions"')]
        self.assertLess(manage.index('fa-pencil'), manage.index('fa-copy'))
        self.assertLess(manage.index('fa-copy'), manage.index('fa-check-square-o'))

    def test_author_without_edit_own_contest_sees_grade_not_edit(self):
        """Grade follows is_editable_by; Edit matches Admin (needs edit_own_contest)."""
        author = create_user(username='list_author_no_edit_own')
        self.ongoing.authors.add(author.profile)
        self.client.force_login(author)
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        self.assertIn('fa-check-square-o', row)
        self.assertIn(reverse('contest_grade', args=[self.ongoing.key]), row)
        self.assertNotIn('fa-pencil', row)
        self.assertNotIn(reverse('contest_edit', args=[self.ongoing.key]), row)
        self.assertNotIn('fa-copy', row)
        self.assertNotIn('js-del-assignment', row)

    def test_delete_icon_requires_delete_contest(self):
        self.client.force_login(self.users['staff_can_delete'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        self.assertIn('js-del-assignment', row)
        self.assertIn('fa-trash', row)
        self.assertNotIn('assignment-select', row)
        self.assertNotIn('id="bulk-delete-btn"', html)
        self.assertNotIn('select-all-assignments', html)
        self.assertNotIn('cl-list--selectable', html)
        self.assertIn(reverse('contest_bulk_delete'), html)
        manage = row[row.index('class="cl-manage-actions"'):row.index('class="cl-join-actions"')]
        self.assertLess(manage.index('fa-trash'), manage.index('fa-pencil'))
        self.assertLess(manage.index('fa-pencil'), manage.index('fa-copy'))
        self.assertLess(manage.index('fa-copy'), manage.index('fa-check-square-o'))

    def test_upcoming_shows_remind_in_join_slot(self):
        self.client.force_login(self.users['normal_user'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.upcoming.key)
        self.assertIn('>Remind<', row)

    def test_ongoing_spectate_path_is_labelled_spectate_not_virtual(self):
        """Editors may spectate, but cannot join as live participants."""
        self.client.force_login(self.users['superuser'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.ongoing.key)
        join_actions = row[row.index('class="cl-join-actions"'):]
        self.assertIn('>Spectate</button>', join_actions)
        self.assertNotIn('>Virtual</button>', join_actions)

    def test_past_virtual_join_stays_in_join_slot(self):
        self.client.force_login(self.users['normal_user'])
        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.past.key)
        self.assertIn('>Virtual<', row)
        self.assertLess(row.index('class="cl-join-actions"'), row.index('Virtual'))
        self.assertLess(row.index('class="cl-manage-actions"'), row.index('class="cl-join-actions"'))

    def test_clone_post_redirects_to_frontend_edit_not_admin(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        resp = self.client.post(reverse('contest_clone', args=[self.ongoing.key]), {'key': 'clonedactions'})
        self.assertRedirects(resp, reverse('contest_edit', args=['clonedactions']))
        self.assertFalse(resp.url.startswith('/admin/'))

    def test_clone_keeps_hide_sandbox_output(self):
        self.ongoing.hide_sandbox_output = True
        self.ongoing.save(update_fields=['hide_sandbox_output'])
        self.client.force_login(self.users['staff_contest_edit_all'])
        resp = self.client.post(reverse('contest_clone', args=[self.ongoing.key]), {'key': 'clonedhideout'})
        self.assertRedirects(resp, reverse('contest_edit', args=['clonedhideout']))
        cloned = Contest.objects.get(key='clonedhideout')
        self.assertTrue(cloned.hide_sandbox_output)

    def test_clone_get_prefills_next_assignment_id(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        occupied = Contest.generate_unique_key()
        create_contest(
            key=occupied, name='Occupied clone key',
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            authors=('superuser',),
        )
        response = self.client.get(reverse('contest_clone', args=[self.ongoing.key]))
        self.assertEqual(response.status_code, 200)
        key = response.context['form'].initial['key']
        self.assertRegex(key, r'^c\d{6}$')
        self.assertNotEqual(key, occupied)
        html = response.content.decode()
        self.assertIn('?suggest=1', html)
        self.assertIn('generateUniqueContestKey', html)

    def test_clone_occupied_series_key_uses_next(self):
        self.client.force_login(self.users['staff_contest_edit_all'])
        occupied = Contest.generate_unique_key()
        create_contest(
            key=occupied, name='Occupied clone series',
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            authors=('superuser',),
        )
        resp = self.client.post(reverse('contest_clone', args=[self.ongoing.key]), {'key': occupied})
        self.assertEqual(resp.status_code, 302)
        cloned = Contest.objects.exclude(pk=self.ongoing.pk).get(name=self.ongoing.name)
        self.assertNotEqual(cloned.key, occupied)
        self.assertRedirects(resp, reverse('contest_edit', args=[cloned.key]),
                             fetch_redirect_response=False)
        notice = self.client.get(resp.url)
        self.assertContains(notice, 'already in use')
        self.assertContains(notice, occupied)
        self.assertContains(notice, cloned.key)


class ContestDeleteViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.deleter = create_user(
            username='contest_deleter',
            is_staff=True,
            user_permissions=('edit_own_contest', 'edit_all_contest', 'delete_contest'),
        )
        cls.editor = create_user(
            username='contest_editor_no_delete',
            is_staff=True,
            user_permissions=('edit_own_contest', 'edit_all_contest'),
        )
        cls.student = create_user(username='contest_delete_student')
        _now = timezone.now()
        cls.contest = create_contest(
            key='delete_me',
            name='Delete Me',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True,
            authors=('contest_deleter',),
        )

    def setUp(self):
        self.client = Client()

    def test_get_not_allowed(self):
        self.client.force_login(self.deleter)
        resp = self.client.get(reverse('contest_delete', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 405)

    def test_delete_succeeds_for_admin_equivalent_permission(self):
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_delete', args=[self.contest.key]), {
            'next': reverse('contest_list'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertFalse(Contest.objects.filter(key='delete_me').exists())

    def test_delete_denied_without_delete_contest(self):
        keep = create_contest(
            key='keep_me', name='Keep Me',
            is_visible=True, authors=('contest_editor_no_delete',),
        )
        self.client.force_login(self.editor)
        resp = self.client.post(reverse('contest_delete', args=[keep.key]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Contest.objects.filter(key='keep_me').exists())

    def test_delete_denied_for_student(self):
        self.client.force_login(self.student)
        resp = self.client.post(reverse('contest_delete', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Contest.objects.filter(key='delete_me').exists())

    def test_rejects_external_next_redirect(self):
        target = create_contest(
            key='delete_redirect', name='Delete Redirect',
            is_visible=True, authors=('contest_deleter',),
        )
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_delete', args=[target.key]), {
            'next': 'https://evil.example/phish',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertFalse(Contest.objects.filter(key='delete_redirect').exists())


class ContestBulkDeleteViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.deleter = create_user(
            username='bulk_contest_deleter',
            is_staff=True,
            user_permissions=('edit_own_contest', 'delete_contest'),
        )
        cls.editor = create_user(
            username='bulk_contest_editor',
            is_staff=True,
            user_permissions=('edit_own_contest', 'edit_all_contest'),
        )
        cls.student = create_user(username='bulk_contest_student')
        _now = timezone.now()
        cls.own_a = create_contest(
            key='bulk_own_a', name='Bulk Own A',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True, authors=('bulk_contest_deleter',),
        )
        cls.own_b = create_contest(
            key='bulk_own_b', name='Bulk Own B',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True, authors=('bulk_contest_deleter',),
        )
        cls.other = create_contest(
            key='bulk_other', name='Bulk Other',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True, authors=('bulk_contest_editor',),
        )

    def setUp(self):
        self.client = Client()

    def test_get_not_allowed(self):
        self.client.force_login(self.deleter)
        resp = self.client.get(reverse('contest_bulk_delete'))
        self.assertEqual(resp.status_code, 405)

    def test_bulk_delete_succeeds_for_owned_contests(self):
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_bulk_delete'), {
            'keys': [self.own_a.key, self.own_b.key],
            'next': reverse('contest_list'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertFalse(Contest.objects.filter(key='bulk_own_a').exists())
        self.assertFalse(Contest.objects.filter(key='bulk_own_b').exists())

    def test_bulk_delete_skips_contests_user_cannot_delete(self):
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_bulk_delete'), {
            'keys': [self.own_a.key, self.other.key],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Contest.objects.filter(key='bulk_own_a').exists())
        self.assertTrue(Contest.objects.filter(key='bulk_other').exists())

    def test_bulk_delete_denied_for_student(self):
        self.client.force_login(self.student)
        resp = self.client.post(reverse('contest_bulk_delete'), {
            'keys': [self.own_a.key, self.own_b.key],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Contest.objects.filter(key='bulk_own_a').exists())
        self.assertTrue(Contest.objects.filter(key='bulk_own_b').exists())

    def test_bulk_delete_denied_without_delete_contest(self):
        self.client.force_login(self.editor)
        resp = self.client.post(reverse('contest_bulk_delete'), {
            'keys': [self.other.key],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Contest.objects.filter(key='bulk_other').exists())

    def test_student_list_hides_delete_controls(self):
        self.client.force_login(self.student)
        html = self.client.get(reverse('contest_list')).content.decode()
        self.assertNotIn('js-del-assignment"', html)
        self.assertNotIn('class="assignment-select"', html)
        self.assertNotIn('id="bulk-delete-btn"', html)
        self.assertNotIn('cl-list--selectable', html)

    def test_rejects_external_next_redirect(self):
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_bulk_delete'), {
            'keys': [self.own_b.key],
            'next': 'https://evil.example/phish',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertFalse(Contest.objects.filter(key='bulk_own_b').exists())

    def test_empty_selection_redirects_without_deleting(self):
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_bulk_delete'), {
            'next': reverse('contest_list'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertTrue(Contest.objects.filter(key='bulk_own_a').exists())


class ContestDetailAssignmentStatsTestCase(TestCase):
    """Assignment detail Users / AC Rate must use this assignment's submissions."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='detail-student')
        cls.classmate = create_user(username='detail-classmate')
        cls.outsider = create_user(username='detail-outsider')
        cls.virtual_user = create_user(username='detail-virtual')
        cls.language = Language.objects.get(key='PY3')
        cls.problem = create_problem(
            code='detail-p1', name='Hello World', is_public=True, ac_rate=99.0, user_count=151,
        )
        cls.untouched = create_problem(
            code='detail-p2', name='A+B Problem', is_public=True, ac_rate=58.0, user_count=87,
        )
        cls.private = create_problem(
            code='detail-p3', name='Secret Quiz', is_public=False, ac_rate=10.0, user_count=9,
        )
        cls.contest = create_contest(
            key='detailstats', name='Class Homework',
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=2),
            is_visible=True,
        )
        cls.contest_problem = create_contest_problem(
            contest=cls.contest, problem=cls.problem, points=100, order=1,
        )
        create_contest_problem(contest=cls.contest, problem=cls.untouched, points=100, order=2)
        create_contest_problem(contest=cls.contest, problem=cls.private, points=100, order=3)

        student_part = create_contest_participation(contest=cls.contest, user=cls.student.profile)
        classmate_part = create_contest_participation(contest=cls.contest, user=cls.classmate.profile)

        ac = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=cls.language,
            result='AC', status='D', points=100, contest_object=cls.contest,
        )
        ContestSubmission.objects.create(
            submission=ac, problem=cls.contest_problem, participation=student_part, points=100,
        )
        wa = Submission.objects.create(
            user=cls.classmate.profile, problem=cls.problem, language=cls.language,
            result='WA', status='D', points=0, contest_object=cls.contest,
        )
        ContestSubmission.objects.create(
            submission=wa, problem=cls.contest_problem, participation=classmate_part, points=0,
        )
        Submission.objects.create(
            user=cls.outsider.profile, problem=cls.problem, language=cls.language,
            result='AC', status='D', points=100, contest_object=None,
        )
        virtual_part = create_contest_participation(
            contest=cls.contest, user=cls.virtual_user.profile, virtual=1,
        )
        virtual_ac = Submission.objects.create(
            user=cls.virtual_user.profile, problem=cls.problem, language=cls.language,
            result='AC', status='D', points=100, contest_object=cls.contest,
        )
        ContestSubmission.objects.create(
            submission=virtual_ac, problem=cls.contest_problem, participation=virtual_part, points=100,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_users_and_ac_rate_use_live_assignment_submissions(self):
        response = self.client.get(reverse('contest_view', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        problems = {p.code: p for p in response.context['contest_problems']}

        attempted = problems['detail-p1']
        # Live 1 AC + 1 WA; practice and virtual AC must not count.
        self.assertEqual(attempted.user_count, 2)
        self.assertEqual(attempted.ac_rate, 50.0)

        untouched = problems['detail-p2']
        self.assertEqual(untouched.user_count, 0)
        self.assertEqual(untouched.ac_rate, 0.0)

        self.assertContains(response, '50.0%')
        self.assertNotContains(response, '99.0%')
        self.assertNotContains(response, '58.0%')
        self.assertNotContains(response, '>151<')
        self.assertNotContains(response, '>87<')

        ranked = reverse('contest_ranked_submissions', args=[self.contest.key, self.problem.code])
        self.assertContains(response, ranked)
        self.assertNotContains(response, reverse('ranked_submissions', args=[self.problem.code]))

    @override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
    def test_users_link_hides_private_problem_code_from_strangers(self):
        ranked_private = reverse(
            'contest_ranked_submissions', args=[self.contest.key, self.private.code],
        )
        self.assertContains(self.client.get(reverse('contest_view', args=[self.contest.key])), ranked_private)

        stranger = Client()
        stranger.force_login(self.outsider)
        response = stranger.get(reverse('contest_view', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ranked_private)


class ContestJoinLeaveRejoinTestCase(CommonDataMixin, TestCase):
    """A student who joins then leaves an in-progress (time-limited) assignment
    must be able to get back in: the list offers "Join" (not a dead "Enter"
    link), and rejoining re-attaches the original live participation without
    restarting the timer."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.student = create_user(username='joinleave_student')
        _now = timezone.now()
        cls.contest = create_contest(
            key='joinleave',
            name='Join Leave Rejoin',
            start_time=_now - timedelta(hours=1),
            end_time=_now + timedelta(hours=3),
            time_limit=timedelta(hours=2),
            is_visible=True,
            authors=('superuser',),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)

    def test_list_shows_join_not_enter_after_leave(self):
        # Join, then leave: the participation stays live (window not expired),
        # so the row must still offer "Join" (a re-attach), never a plain
        # "Enter" link that only opens the detail page without re-joining.
        self.client.post(reverse('contest_join', args=[self.contest.key]))
        self.client.post(reverse('contest_leave', args=[self.contest.key]))
        self.student.profile.refresh_from_db()
        self.assertIsNone(self.student.profile.current_contest)

        html = self.client.get(reverse('contest_list')).content.decode()
        row = _assignment_row_html(html, self.contest.key)
        self.assertIn('data-status="active"', row)
        self.assertIn('>Join</button>', row)
        self.assertIn(reverse('contest_join', args=[self.contest.key]), row)
        self.assertNotIn('>Enter<', row)

    def test_rejoin_after_leave_reattaches_same_participation(self):
        from judge.models import ContestParticipation

        response = self.client.post(reverse('contest_join', args=[self.contest.key]))
        self.assertEqual(response.status_code, 302)
        self.student.profile.refresh_from_db()
        participation = self.student.profile.current_contest
        self.assertIsNotNone(participation)
        self.assertTrue(participation.live)
        self.assertFalse(participation.spectate)
        original_real_start = participation.real_start

        response = self.client.post(reverse('contest_leave', args=[self.contest.key]))
        self.assertEqual(response.status_code, 302)
        self.student.profile.refresh_from_db()
        self.assertIsNone(self.student.profile.current_contest)

        # Rejoin must re-attach the SAME live participation: timer not reset,
        # still live (not demoted to spectator), and no duplicate row created.
        response = self.client.post(reverse('contest_join', args=[self.contest.key]))
        self.assertEqual(response.status_code, 302)
        self.student.profile.refresh_from_db()
        reattached = self.student.profile.current_contest
        self.assertIsNotNone(reattached)
        self.assertEqual(reattached.id, participation.id)
        self.assertTrue(reattached.live)
        self.assertFalse(reattached.spectate)
        self.assertEqual(reattached.real_start, original_real_start)
        self.assertEqual(
            ContestParticipation.objects.filter(
                contest=self.contest, user=self.student.profile,
                virtual=ContestParticipation.LIVE,
            ).count(),
            1,
        )
