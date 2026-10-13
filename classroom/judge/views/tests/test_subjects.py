"""Subject registry in the views: CS courses unchanged, non-CS courses hide/refuse code features, mixed teachers.

Registry unit tests are in judge/subjects/tests.py.
"""
import json
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import Language, NavigationBar, Organization, Problem, ProblemImportDraft
from judge.models.tests.util import (CommonDataMixin, create_contest, create_organization,
                                     create_problem_group, create_problem_type, create_user)
from judge.utils.problem_create import default_problem_group
from judge.utils.simple_xlsx import build_xlsx_workbook


class SubjectsDataMixin(CommonDataMixin):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = default_problem_group()
        cls.cs = create_organization(name='COMPSCI 201', short_name='COMPSCI 201', slug='compsci-201', about='CS')
        cls.bio = create_organization(name='BIOL 110', short_name='BIOL 110', slug='biol-110', subject='bio',
                                      about='Bio')
        cls.chem = create_organization(name='CHEM 110', short_name='CHEM 110', slug='chem-110', subject='chem',
                                       about='Chem')
        teacher_perms = ('create_private_problem', 'edit_own_problem', 'add_organization', 'change_organization',
                         'add_contest', 'edit_own_contest')
        # Teaches a CS course and a biology course.
        cls.mixed = create_user(username='mixed_teacher', is_staff=True, user_permissions=teacher_perms)
        cls.cs.admins.add(cls.mixed.profile)
        cls.bio.admins.add(cls.mixed.profile)
        # Teaches biology only (site teachers get is_staff; that must not unlock code features).
        cls.bio_teacher = create_user(username='bio_teacher', is_staff=True, user_permissions=teacher_perms)
        cls.bio.admins.add(cls.bio_teacher.profile)
        cls.chem.admins.add(cls.bio_teacher.profile)
        cls.cs_student = create_user(username='cs_student')
        cls.cs_student.profile.organizations.add(cls.cs)
        cls.bio_student = create_user(username='bio_student')
        cls.bio_student.profile.organizations.add(cls.bio)

    def save_problem(self, user, qtype, organizations, **extra):
        payload = {
            'problemTypes': ['Subjects'],
            'type': qtype,
            'name': 'Subject %s' % qtype,
            'description': 'Statement',
            'points': 10,
            'group_id': self.group.id,
            'organizations': [o.pk for o in organizations],
        }
        if qtype == 'single':
            payload['options'] = [{'text': 'A', 'correct': True}, {'text': 'B', 'correct': False}]
        payload.update(extra)
        self.client.force_login(user)
        return self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                content_type='application/json')


class CoursePageSubjectTestCase(SubjectsDataMixin, TestCase):
    def home(self, org, user):
        self.client.force_login(user)
        return self.client.get(reverse('organization_home', args=(org.id, org.slug)))

    def test_cs_course_page_shows_code_section_and_ide(self):
        response = self.home(self.cs, self.mixed)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-subject="cs"')
        self.assertContains(response, 'data-section="code"')
        self.assertContains(response, 'data-menu-item="ide"')
        self.assertContains(response, 'data-section="core"')
        self.assertContains(response, 'href="%s?course=%d"' % (reverse('problem_create_v2'), self.cs.pk))

    def test_bio_course_page_has_no_code_tools(self):
        response = self.home(self.bio, self.mixed)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-subject="bio"')
        self.assertContains(response, 'Biology and life sciences')
        self.assertContains(response, 'data-section="core"')
        self.assertNotContains(response, 'data-section="code"')
        self.assertNotContains(response, 'data-menu-item="ide"')

    def test_chemistry_course_page_renders_its_pack_section(self):
        response = self.home(self.chem, self.bio_teacher)
        self.assertContains(response, 'data-section="chem"')
        self.assertContains(response, 'data-menu-item="chem_new_question"')
        self.assertNotContains(response, 'data-section="code"')

    def test_students_see_course_tools_but_not_teacher_sections(self):
        response = self.home(self.cs, self.cs_student)
        self.assertContains(response, 'data-section="code"')
        self.assertContains(response, 'data-menu-item="ide"')
        self.assertNotContains(response, 'data-section="core"')
        response = self.home(self.bio, self.bio_student)
        self.assertNotContains(response, 'data-section=')
        self.assertContains(response, 'data-subject="bio"')


class TeacherHomeSubjectTestCase(SubjectsDataMixin, TestCase):
    def test_mixed_teacher_sees_each_course_with_its_own_tools(self):
        self.client.force_login(self.mixed)
        html = self.client.get(reverse('home')).content.decode()
        self.assertIn('data-course="%d" data-subject="cs"' % self.cs.pk, html)
        self.assertIn('data-course="%d" data-subject="bio"' % self.bio.pk, html)
        cs_card = html.split('data-course="%d"' % self.cs.pk)[1].split('teacher-home-course')[0]
        bio_card = html.split('data-course="%d"' % self.bio.pk)[1].split('teacher-home-course')[0]
        self.assertIn('data-menu-item="ide"', cs_card)
        self.assertNotIn('data-menu-item="ide"', bio_card)
        self.assertIn('data-menu-item="new_question"', bio_card)
        self.assertIn('data-section="code"', html)

    def test_bio_teacher_home_has_no_code_section(self):
        self.client.force_login(self.bio_teacher)
        html = self.client.get(reverse('home')).content.decode()
        self.assertIn('data-subject="bio"', html)
        self.assertIn('data-subject="chem"', html)
        self.assertNotIn('data-section="code"', html)
        self.assertNotIn('data-menu-item="ide"', html)

    def test_students_and_anonymous_get_no_teacher_panel(self):
        self.client.force_login(self.cs_student)
        self.assertNotContains(self.client.get(reverse('home')), 'teacher-home')
        self.client.logout()
        self.assertNotContains(self.client.get(reverse('home')), 'teacher-home')


class ProblemCreateSubjectTestCase(SubjectsDataMixin, TestCase):
    def get_create(self, user, course=None):
        self.client.force_login(user)
        url = reverse('problem_create_v2')
        if course is not None:
            url += '?course=%d' % course.pk
        return self.client.get(url)

    def test_cs_context_offers_programming(self):
        response = self.get_create(self.mixed, self.cs)
        self.assertTrue(response.context['can_create_code'])
        self.assertContains(response, 'name="qtype" value="programming"')
        self.assertEqual(response.context['preselect_organization_ids'], [self.cs.pk])

    def test_bio_context_hides_programming(self):
        response = self.get_create(self.mixed, self.bio)
        self.assertFalse(response.context['can_create_code'])
        self.assertNotContains(response, 'name="qtype" value="programming"')
        self.assertContains(response, 'name="qtype" value="single"')
        self.assertEqual(response.context['preselect_organization_ids'], [self.bio.pk])

    def test_without_context_mixed_teacher_gets_union_and_per_course_caps(self):
        response = self.get_create(self.mixed)
        self.assertTrue(response.context['can_create_code'])
        caps = json.loads(response.context['org_question_caps_json'])
        self.assertIn('question_programming', caps[str(self.cs.pk)])
        self.assertNotIn('question_programming', caps[str(self.bio.pk)])

    def test_bio_only_teacher_never_sees_programming(self):
        response = self.get_create(self.bio_teacher)
        self.assertFalse(response.context['can_create_code'])
        self.assertNotContains(response, 'name="qtype" value="programming"')

    def test_course_param_ignores_courses_the_user_does_not_teach(self):
        response = self.get_create(self.bio_teacher, self.cs)
        self.assertEqual(response.context['preselect_organization_ids'], [])

    def test_programming_in_cs_course_still_saves(self):
        response = self.save_problem(self.mixed, 'programming', [self.cs])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Problem.objects.get(code=response.json()['code']).submission_type, 'C')

    def test_programming_in_bio_course_is_refused(self):
        before = Problem.objects.count()
        response = self.save_problem(self.mixed, 'programming', [self.bio])
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn('not available', response.json()['error'])
        response = self.save_problem(self.mixed, 'programming', [self.cs, self.bio])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Problem.objects.count(), before)

    def test_programming_without_course_is_refused_for_bio_only_teacher(self):
        response = self.save_problem(self.bio_teacher, 'programming', [])
        self.assertEqual(response.status_code, 403, response.content)

    def test_core_types_save_in_bio_course(self):
        response = self.save_problem(self.bio_teacher, 'single', [self.bio])
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(list(problem.organizations.all()), [self.bio])

    def test_existing_programming_problem_stays_editable_after_subject_change(self):
        course = create_organization(name='COMPSCI 999', short_name='COMPSCI 999', slug='cs-999', about='x')
        course.admins.add(self.mixed.profile)
        created = self.save_problem(self.mixed, 'programming', [course])
        self.assertEqual(created.status_code, 200, created.content)
        code = created.json()['code']
        Organization.objects.filter(pk=course.pk).update(subject='bio')
        edited = self.save_problem(self.mixed, 'programming', [course], edit_code=code, name='Renamed')
        self.assertEqual(edited.status_code, 200, edited.content)
        self.assertEqual(Problem.objects.get(code=code).name, 'Renamed')
        # Adding a non-code course to it is still refused.
        refused = self.save_problem(self.mixed, 'programming', [course, self.bio], edit_code=code)
        self.assertEqual(refused.status_code, 403)

    def test_site_editor_sees_programming_but_course_rule_still_applies(self):
        editor = create_user(username='subj_site_editor', is_staff=True, user_permissions=(
            'create_private_problem', 'edit_own_problem', 'edit_all_organization'))
        response = self.get_create(editor, self.bio)
        self.assertTrue(response.context['can_create_code'])
        self.assertEqual(self.save_problem(editor, 'programming', [self.bio]).status_code, 403)
        self.assertEqual(self.save_problem(editor, 'programming', [self.cs]).status_code, 200)

    def test_inline_course_creation_infers_subject(self):
        self.client.force_login(self.mixed)
        response = self.client.post(reverse('problem_create_v2_organization'),
                                    data=json.dumps({'name': 'CHEM 201 Organic'}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Organization.objects.get(pk=response.json()['organization']['id']).subject, 'chem')


class ProblemImportSubjectTestCase(SubjectsDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_problem_group(name='import-group')
        create_problem_type(name='import-type')

    def test_code_import_into_bio_course_is_refused(self):
        self.client.force_login(self.mixed)
        language = Language.objects.exclude(key='ANSWER').first()
        workbook = build_xlsx_workbook([('Problems', [
            ['name', 'question_type', 'description', 'organizations', 'allowed_languages'],
            ['Bio code', 'code', 'Print the input.', self.bio.slug, language.key],
        ])])
        upload = self.client.post(reverse('problem_import_upload'), {
            'excel_file': SimpleUploadedFile(
                'questions.xlsx', workbook,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(upload.status_code, 200, upload.content)
        item = ProblemImportDraft.objects.get(owner=self.mixed.profile).items.get()
        self.assertEqual(item.editor_data, {'organizations': [self.bio.pk]})
        response = self.client.post(
            reverse('problem_import_item_action', args=(item.draft_id, item.pk, 'create')),
            {'name': item.name, 'question_type': item.submission_type, 'description': item.description},
        )
        self.assertFalse(Problem.objects.filter(name='Bio code').exists())
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('not available', response.content.decode())


class SiteWideSubjectTestCase(SubjectsDataMixin, TestCase):
    def test_ide_refused_for_bio_only_users(self):
        for user in (self.bio_student, self.bio_teacher):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse('ide')).status_code, 404)
                response = self.client.post(reverse('ide_submit'), {'source': 'x', 'language': 1, 'input': ''})
                self.assertEqual(response.status_code, 403)

    def test_ide_available_to_cs_and_mixed_users(self):
        for user in (self.cs_student, self.mixed):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse('ide')).status_code, 200)

    def test_navigation_hides_code_entries_for_bio_only_users(self):
        NavigationBar.objects.create(key='ide', label='IDE', path='/ide/', regex='^/ide', order=10)
        self.client.force_login(self.bio_student)
        html = self.client.get(reverse('problem_list')).content.decode()
        self.assertNotIn('nav-ide', html)
        self.client.force_login(self.mixed)
        self.assertIn('nav-ide', self.client.get(reverse('problem_list')).content.decode())
        self.client.logout()
        self.assertIn('nav-ide', self.client.get(reverse('problem_list')).content.decode())

    def test_problem_list_format_filter_follows_subjects(self):
        self.client.force_login(self.bio_teacher)
        response = self.client.get(reverse('problem_list'))
        self.assertNotIn('C', {f['code'] for f in response.context['format_types']})
        self.assertIn('M', {f['code'] for f in response.context['format_types']})
        self.client.force_login(self.mixed)
        response = self.client.get(reverse('problem_list'))
        self.assertIn('C', {f['code'] for f in response.context['format_types']})


class ContestSubjectTestCase(SubjectsDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        cls.bio_contest = create_contest(key='bioquiz', name='Bio quiz', start_time=now - timedelta(hours=1),
                                         end_time=now + timedelta(days=1), is_visible=True,
                                         authors=('bio_teacher',))
        cls.bio_contest.organizations.add(cls.bio)
        cls.cs_contest = create_contest(key='cslab', name='CS lab', start_time=now - timedelta(hours=1),
                                        end_time=now + timedelta(days=1), is_visible=True,
                                        authors=('mixed_teacher',))
        cls.cs_contest.organizations.add(cls.cs)

    def test_rankings_tab_hidden_for_students_of_non_code_courses(self):
        self.client.force_login(self.bio_student)
        response = self.client.get(reverse('contest_view', args=(self.bio_contest.key,)))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('contest_ranking', args=(self.bio_contest.key,)))
        self.client.force_login(self.cs_student)
        response = self.client.get(reverse('contest_view', args=(self.cs_contest.key,)))
        self.assertContains(response, reverse('contest_ranking', args=(self.cs_contest.key,)))

    def test_teachers_keep_the_scoreboard(self):
        self.client.force_login(self.bio_teacher)
        response = self.client.get(reverse('contest_view', args=(self.bio_contest.key,)))
        self.assertContains(response, reverse('contest_ranking', args=(self.bio_contest.key,)))

    @override_settings(MOSS_API_KEY='test-key')
    def test_moss_only_for_code_courses(self):
        moss_user = create_user(username='moss_teacher', user_permissions=('moss_contest', 'edit_own_contest'))
        self.bio_contest.authors.add(moss_user.profile)
        self.cs_contest.authors.add(moss_user.profile)
        self.client.force_login(moss_user)
        # ContestMixin turns the Http404 into a "No such contest" page.
        bio = self.client.get(reverse('contest_moss', args=(self.bio_contest.key,)))
        self.assertNotContains(bio, 'contest-moss')
        self.assertContains(bio, 'No such contest', status_code=bio.status_code)
        cs = self.client.get(reverse('contest_moss', args=(self.cs_contest.key,)))
        self.assertContains(cs, 'contest-moss')
        # The tab follows the same rule.
        self.assertNotContains(self.client.get(reverse('contest_view', args=(self.bio_contest.key,))),
                               reverse('contest_moss', args=(self.bio_contest.key,)))
        self.assertContains(self.client.get(reverse('contest_view', args=(self.cs_contest.key,))),
                            reverse('contest_moss', args=(self.cs_contest.key,)))

    def test_editor_hides_ide_output_switch_for_non_code_assignment(self):
        editor = create_user(username='subj_contest_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest'))
        self.client.force_login(editor)
        bio = self.client.get(reverse('contest_edit', args=(self.bio_contest.key,)))
        self.assertEqual(bio.status_code, 200)
        self.assertNotContains(bio, 'id="id_hide_sandbox_output"')
        self.assertNotContains(bio, 'id="id_run_pretests_only"')
        cs = self.client.get(reverse('contest_edit', args=(self.cs_contest.key,)))
        self.assertContains(cs, 'id="id_hide_sandbox_output"')
        self.assertContains(cs, 'id="id_run_pretests_only"')


class CourseFormSubjectTestCase(SubjectsDataMixin, TestCase):
    def post_create(self, **fields):
        data = {'short_name': 'X 1', 'name': 'Course', 'about': 'About', 'is_open': 'on'}
        data.update(fields)
        self.client.force_login(self.mixed)
        return self.client.post(reverse('organization_create'), data)

    def test_create_form_lists_registry_subjects_with_detect_option(self):
        self.client.force_login(self.mixed)
        response = self.client.get(reverse('organization_create'))
        self.assertEqual(response.status_code, 200)
        choices = [k for k, _label in response.context['form'].fields['subject'].choices]
        self.assertEqual(choices[0], '')
        self.assertIn('chem', choices)
        self.assertContains(response, 'id="subject-detected"')

    def test_create_infers_subject_from_course_code(self):
        response = self.post_create(short_name='CHEM 301', name='Physical chemistry')
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and
                         response.context['form'].errors)
        self.assertEqual(Organization.objects.get(short_name='CHEM 301').subject, 'chem')

    def test_create_unknown_prefix_defaults_to_cs(self):
        self.post_create(short_name='HIST 101', name='World history')
        self.assertEqual(Organization.objects.get(short_name='HIST 101').subject, 'cs')

    def test_explicit_subject_wins(self):
        self.post_create(short_name='COMPSCI 1', name='Bio informatics', subject='bio')
        self.assertEqual(Organization.objects.get(short_name='COMPSCI 1').subject, 'bio')

    def test_invalid_subject_rejected(self):
        response = self.post_create(short_name='Z 1', subject='alchemy')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Organization.objects.filter(short_name='Z 1').exists())

    def test_edit_changes_or_keeps_subject(self):
        org = create_organization(name='MATH 205', short_name='MATH 205', slug='math-205', about='Math')
        org.admins.add(self.mixed.profile)
        self.client.force_login(self.mixed)
        url = reverse('edit_organization', args=(org.id, org.slug))
        base = {'short_name': 'MATH 205', 'name': 'MATH 205', 'about': 'Math', 'is_open': 'on',
                'admins': [self.mixed.profile.pk]}
        response = self.client.post(url, base)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Organization.objects.get(pk=org.pk).subject, 'cs')  # kept (created with the default)
        response = self.client.post(url, dict(base, subject='math'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Organization.objects.get(pk=org.pk).subject, 'math')
        edit_page = self.client.get(url)
        self.assertNotIn('', [k for k, _label in edit_page.context['form'].fields['subject'].choices])

    def test_admin_form_offers_registry_subjects(self):
        from judge.admin.organization import OrganizationForm
        self.assertIn('chem', [k for k, _label in OrganizationForm.base_fields['subject'].choices])
