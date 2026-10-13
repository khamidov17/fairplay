"""Unit tests for the subject registry (judge/subjects/). View-level tests: judge/views/tests/test_subjects.py."""
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from judge.models import Class, Organization
from judge.models.tests.util import create_contest, create_organization, create_problem, create_user
from judge.subjects import (Cap, DEFAULT_SUBJECT, FeaturePack, Subject, all_features, all_packs, all_subjects,
                            course_code_prefix, ensure_question_type_allowed, features_for, filter_question_types,
                            get_pack, get_subject, has_feature, infer_subject_key, prefix_labels,
                            question_type_refusal, register_pack, register_subject, resolve_courses,
                            sections_for, site_features, subject_choices, subject_features, subject_for,
                            validate_subject_key)
from judge.subjects.base import _packs, _subjects

CODE_CAPS = {Cap.QUESTION_PROGRAMMING, Cap.JUDGE, Cap.IDE, Cap.TEST_DATA, Cap.HARNESS, Cap.MOSS, Cap.RANKINGS,
             Cap.LANGUAGE_STATS}
CORE_CAPS = {Cap.QUESTION_CHOICE, Cap.QUESTION_TRUEFALSE, Cap.QUESTION_FILL_BLANK, Cap.QUESTION_SHORT_ANSWER,
             Cap.QUESTION_FILE_UPLOAD, Cap.QUESTION_COMPOSITE, Cap.EXCEL_IMPORT}


class SubjectRegistryTestCase(SimpleTestCase):
    def test_every_subject_uses_registered_packs_and_gets_core(self):
        pack_keys = {p.key for p in all_packs()}
        self.assertGreaterEqual(len(all_subjects()), 10)
        for subject in all_subjects():
            with self.subTest(subject=subject.key):
                self.assertTrue(set(subject.packs) <= pack_keys)
                features = subject_features(subject)
                self.assertIn('core', features)
                self.assertTrue(CORE_CAPS <= features)

    def test_cs_has_every_existing_capability(self):
        """CS = today's DKU OJ: every capability of every pack that has capabilities."""
        cs = subject_features(get_subject('cs'))
        self.assertTrue(CODE_CAPS <= cs)
        self.assertTrue(CORE_CAPS <= cs)
        self.assertIn('code', cs)
        self.assertEqual(DEFAULT_SUBJECT, 'cs')

    def test_only_cs_has_the_code_pack(self):
        for subject in all_subjects():
            with self.subTest(subject=subject.key):
                features = subject_features(subject)
                if subject.key == 'cs':
                    self.assertIn(Cap.IDE, features)
                else:
                    self.assertTrue(CODE_CAPS.isdisjoint(features), subject.key)
                    self.assertNotIn('code', features)

    def test_course_code_inference_uses_verified_dku_prefixes(self):
        cases = {
            'COMPSCI 201': 'cs', 'CS101': 'cs', 'INFOSCI 102': 'cs', 'COMPDSGN 490': 'cs',
            'math 205': 'math', 'STATS 302': 'math', 'STAT102': 'math',
            'PHYS 121': 'phys', 'CHEM 110': 'chem', 'BIOL 110': 'bio', 'ENVIR 101': 'envsci',
            'ECON 314': 'econ', 'PSYCH 101': 'socsci', 'SOSC 320': 'socsci',
            'CHIN 101A': 'lang', 'EAP 101A': 'lang', 'CCORE 101': 'general', 'GCHINA 101': 'general',
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(infer_subject_key(code), expected)

    def test_unknown_prefix_falls_back_to_default(self):
        self.assertEqual(infer_subject_key('HIST 101'), 'cs')
        self.assertEqual(infer_subject_key(''), 'cs')
        self.assertEqual(infer_subject_key(None, '2026 Fall'), 'cs')
        self.assertEqual(infer_subject_key('XYZ 1', default='other'), 'other')
        # The name is used when the code says nothing.
        self.assertEqual(infer_subject_key('', 'BIOL 205 Genetics'), 'bio')

    def test_course_code_prefix(self):
        self.assertEqual(course_code_prefix('COMPSCI 201'), 'COMPSCI')
        self.assertEqual(course_code_prefix('  math205'), 'MATH')
        self.assertEqual(course_code_prefix('101'), '')

    def test_unknown_subject_key_falls_back_to_other(self):
        self.assertEqual(get_subject('no-such-subject').key, 'other')
        self.assertEqual(get_subject('').key, 'other')

    def test_validator(self):
        validate_subject_key('chem')
        with self.assertRaises(ValidationError):
            validate_subject_key('alchemy')

    def test_registration_refuses_duplicates_and_unknown_packs(self):
        before_packs, before_subjects = dict(_packs), dict(_subjects)
        with self.assertRaises(ValueError):
            register_pack(FeaturePack(key='code', label='dup'))
        with self.assertRaises(ValueError):
            register_subject(Subject(key='cs', label='dup', packs=()))
        with self.assertRaises(ValueError):
            register_subject(Subject(key='astro', label='Astronomy', packs=('telescope',)))
        with self.assertRaises(ValueError):
            register_subject(Subject(key='astro', label='Astronomy', packs=(), course_prefixes=('chem',)))
        with self.assertRaises(ValueError):
            register_subject(Subject(key='Bad Key', label='x', packs=()))
        self.assertEqual(_packs, before_packs)
        self.assertEqual(_subjects, before_subjects)

    def test_chemistry_worked_example_is_registered(self):
        """docs/classroom/ADDING_A_SUBJECT.md adds Chemistry with its own placeholder pack."""
        chem = get_subject('chem')
        self.assertEqual(chem.course_prefixes, ('CHEM',))
        self.assertIn('chem', chem.packs)
        pack = get_pack('chem')
        self.assertTrue(pack.placeholder)
        self.assertEqual([s.template for s in pack.sections], ['subjects/sections/chemistry.html'])
        self.assertIn(('chem', 'Chemistry'), [(k, str(v)) for k, v in subject_choices()])
        self.assertEqual(prefix_labels()['CHEM'], 'Chemistry')

    def test_choices_are_ordered_and_other_is_last(self):
        keys = [k for k, _label in subject_choices()]
        self.assertEqual(keys[0], 'cs')
        self.assertEqual(keys[-1], 'other')

    def test_all_features_is_the_union(self):
        union = frozenset().union(*(subject_features(s) for s in all_subjects()))
        self.assertTrue(union <= all_features())


class SubjectAccessTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cs = create_organization(name='COMPSCI 201', short_name='COMPSCI 201')
        cls.bio = create_organization(name='BIOL 110', short_name='BIOL 110', subject='bio')
        cls.chem = create_organization(name='CHEM 110', short_name='CHEM 110', subject='chem')
        cls.mixed = create_user(username='subj_mixed')
        cls.cs.admins.add(cls.mixed.profile)
        cls.bio.instructors.add(cls.mixed.profile)
        # A site teacher (is_staff, like grant_site_teacher_role) who only teaches biology.
        cls.bio_teacher = create_user(username='subj_bio_teacher', is_staff=True)
        cls.bio.admins.add(cls.bio_teacher.profile)
        cls.bio_student = create_user(username='subj_bio_student')
        cls.bio_student.profile.organizations.add(cls.bio)
        cls.newbie = create_user(username='subj_newbie')
        cls.superuser = create_user(username='subj_super', is_superuser=True, is_staff=True)
        cls.editor = create_user(username='subj_editor', user_permissions=('edit_all_organization',))

    def fresh(self, user):
        # Features are cached on the user object for one request; tests reload to be safe.
        return type(user).objects.get(pk=user.pk)

    def test_new_organizations_default_to_cs(self):
        org = Organization.objects.create(name='Plain', slug='plain', short_name='PLAIN', about='')
        self.assertEqual(org.subject, 'cs')
        self.assertEqual(subject_for(org).key, 'cs')

    def test_migration_adds_subject_with_cs_default(self):
        """Existing rows get the AddField default, so every current course stays CS."""
        from importlib import import_module
        migration = import_module('judge.migrations.0222_organization_subject').Migration
        operation = migration.operations[0]
        self.assertEqual((operation.model_name, operation.name), ('organization', 'subject'))
        self.assertEqual(operation.field.default, 'cs')
        self.assertFalse(operation.field.null)
        self.assertEqual(Organization._meta.get_field('subject').default, 'cs')

    def test_subject_for_class_and_unknown_key(self):
        klass = Class.objects.create(organization=self.bio, name='BIOL 110 Fall', slug='biol-110-fall')
        self.assertEqual(subject_for(klass).key, 'bio')
        Organization.objects.filter(pk=self.chem.pk).update(subject='removed')
        self.assertEqual(subject_for(Organization.objects.get(pk=self.chem.pk)).key, 'other')

    def test_course_context_uses_only_that_course(self):
        user = self.fresh(self.mixed)
        self.assertTrue(has_feature(user, 'code', self.cs))
        self.assertTrue(has_feature(user, Cap.IDE, self.cs))
        self.assertFalse(has_feature(user, 'code', self.bio))
        self.assertFalse(has_feature(user, Cap.QUESTION_PROGRAMMING, self.bio))
        self.assertTrue(has_feature(user, Cap.QUESTION_CHOICE, self.bio))

    def test_site_wide_is_union_of_the_users_courses(self):
        self.assertTrue(has_feature(self.fresh(self.mixed), Cap.IDE))
        self.assertFalse(has_feature(self.fresh(self.bio_teacher), Cap.IDE))
        self.assertFalse(has_feature(self.fresh(self.bio_student), 'code'))
        self.assertTrue(has_feature(self.fresh(self.bio_student), 'lab'))

    def test_users_without_courses_keep_todays_features(self):
        self.assertTrue(has_feature(self.fresh(self.newbie), Cap.IDE))
        self.assertTrue(has_feature(AnonymousUser(), Cap.IDE))
        self.assertEqual(site_features(AnonymousUser()), subject_features(get_subject('cs')))

    def test_site_editors_see_everything_even_in_a_bio_course(self):
        for user in (self.superuser, self.editor):
            with self.subTest(user=user.username):
                self.assertEqual(features_for(self.fresh(user), self.bio), all_features())
        # is_staff alone (site teachers get it) is not enough.
        self.assertFalse(has_feature(self.fresh(self.bio_teacher), 'code', self.bio))

    def test_contest_and_problem_contexts(self):
        now = timezone.now()
        contest = create_contest(key='subjbio', start_time=now, end_time=now + timedelta(days=1))
        contest.organizations.add(self.bio)
        self.assertEqual(resolve_courses(contest), [self.bio])
        self.assertFalse(has_feature(self.fresh(self.mixed), Cap.RANKINGS, contest))
        klass = Class.objects.create(organization=self.cs, name='CS class', slug='cs-class')
        contest.classes.add(klass)
        self.assertEqual({c.pk for c in resolve_courses(contest)}, {self.bio.pk, self.cs.pk})
        self.assertTrue(has_feature(self.fresh(self.mixed), Cap.RANKINGS, contest))

        problem = create_problem(code='subjprob')
        problem.organizations.add(self.chem)
        self.assertFalse(has_feature(self.fresh(self.mixed), Cap.TEST_DATA, problem))

    def test_question_type_refusal(self):
        user = self.fresh(self.mixed)
        self.assertIsNone(question_type_refusal(user, 'programming', [self.cs]))
        self.assertIsNotNone(question_type_refusal(user, 'programming', [self.bio]))
        self.assertIsNotNone(question_type_refusal(user, 'C', [self.cs, self.bio]))
        self.assertIsNone(question_type_refusal(user, 'single', [self.bio]))
        self.assertIsNone(question_type_refusal(user, 'programming', []))  # union has code
        self.assertIsNotNone(question_type_refusal(self.fresh(self.bio_teacher), 'programming', []))
        # The course rule applies to site editors too.
        with self.assertRaises(PermissionError):
            ensure_question_type_allowed(self.fresh(self.superuser), 'programming', [self.bio])

    def test_filter_question_types(self):
        from judge.question_types import all_types
        bio_types = {t.submission_type for t in filter_question_types(self.fresh(self.bio_teacher), all_types())}
        self.assertNotIn('C', bio_types)
        self.assertIn('M', bio_types)
        mixed_types = {t.submission_type for t in filter_question_types(self.fresh(self.mixed), all_types())}
        self.assertIn('C', mixed_types)

    def test_sections(self):
        cs_keys = [s.key for s in sections_for(self.mixed, 'course', self.cs, is_teacher=True)]
        self.assertEqual(cs_keys, ['core', 'code'])
        self.assertEqual([s.key for s in sections_for(self.mixed, 'course', self.bio, is_teacher=True)], ['core'])
        self.assertEqual([s.key for s in sections_for(self.mixed, 'course', self.chem, is_teacher=True)],
                         ['core', 'chem'])
        # Students do not get teacher-only sections.
        self.assertEqual([s.key for s in sections_for(self.bio_student, 'course', self.bio, is_teacher=False)], [])
        # Teacher home: each pack of the teacher's subjects once.
        self.assertEqual([s.key for s in sections_for(self.mixed, 'teacher_home')], ['code'])
        self.assertEqual([s.key for s in sections_for(self.bio_teacher, 'teacher_home')], [])
