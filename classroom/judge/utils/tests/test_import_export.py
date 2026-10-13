"""Tests for CSV user import (judge.utils.import_export.process_user_csv)."""
import csv
import io

from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import ContestParticipation, Profile
from judge.models.tests.util import (
    create_contest,
    create_contest_participation,
    create_user,
)
from judge.utils.import_export import (
    export_contest_scores_csv,
    export_contest_scores_joined_csv,
    import_contest_scores_from_csv,
    process_user_csv,
)


class UserImportTestCase(TestCase):
    HEADER = 'username,first_name,last_name,email\n'

    def _import(self, rows, **kwargs):
        content = (self.HEADER + '\n'.join(rows)).encode('utf-8')
        return process_user_csv(io.BytesIO(content), **kwargs)

    def test_creates_users_with_unusable_password_for_netid(self):
        result = self._import(['alice,Alice,Liddell,alice@example.com'])
        self.assertEqual(result.created, 1)
        self.assertEqual(result.errors, [])
        user = User.objects.get(username='alice')
        self.assertFalse(user.has_usable_password())
        self.assertTrue(Profile.objects.filter(user=user).exists())

    def test_password_column_in_csv_is_ignored(self):
        """Roster import is SSO-only; a password column must not set a local password."""
        content = (
            'username,first_name,last_name,email,password\n'
            'dave,Dave,One,dave@example.com,secret123\n'
        ).encode('utf-8')
        result = process_user_csv(io.BytesIO(content))
        self.assertEqual(result.created, 1)
        user = User.objects.get(username='dave')
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.check_password('secret123'))

    def test_existing_user_is_linked_even_without_update(self):
        from judge.models.tests.util import create_organization
        org = create_organization(name='Import Course')
        create_user(username='erin')
        content = (
            'username,first_name,last_name,email\n'
            'erin,Erin,New,erin@example.com\n'
        ).encode('utf-8')
        result = process_user_csv(io.BytesIO(content), organization=org, update_existing=False)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.org_linked, 1)
        self.assertEqual(User.objects.get(username='erin').first_name, '')
        self.assertTrue(org.members.filter(user__username='erin').exists())

    def test_duplicate_username_within_csv_reported(self):
        result = self._import([
            'bob,Bob,One,bob1@example.com',
            'bob,Bob,Two,bob2@example.com',
        ])
        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('duplicate username', result.errors[0])
        # First row wins; the second must not overwrite it.
        self.assertEqual(User.objects.get(username='bob').email, 'bob1@example.com')

    def test_existing_user_skipped_unless_update_requested(self):
        from judge.models.tests.util import create_organization
        org = create_organization(name='Carol Course')
        carol_user = create_user(username='carol')
        carol_user.set_unusable_password()  # SSO-only: the import may fill in its e-mail (CA-1)
        carol_user.save()
        org.members.add(carol_user.profile)
        result = self._import(['carol,Carol,New,carol@example.com'], organization=org)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(User.objects.get(username='carol').first_name, '')

        # Updating is only possible for a plain member already in this course (CA-1).
        result = self._import(['carol,Carol,New,carol@example.com'], organization=org, update_existing=True)
        self.assertEqual(result.updated, 1)
        carol = User.objects.get(username='carol')
        self.assertEqual(carol.first_name, 'Carol')
        self.assertEqual(carol.email, 'carol@example.com')

    def test_canvas_roster_headers(self):
        content = (
            'First name,Last Name,Display Name,Duke Unique ID,NetID,netid@duke.edu,Email,Section(s)\n'
            'Qixuan,Yuan,Qixuan Yuan,1234567,qy88,qy88@duke.edu,qy88@duke.edu,CS204\n'
        ).encode('utf-8')
        result = process_user_csv(io.BytesIO(content))
        self.assertEqual(result.errors, [], result.errors)
        self.assertEqual(result.created, 1)
        user = User.objects.get(username='qy88')
        self.assertEqual(user.first_name, 'Qixuan')
        self.assertEqual(user.last_name, 'Yuan')
        self.assertEqual(user.email, 'qy88@duke.edu')

    def test_canvas_prefers_email_over_netid_duke_column(self):
        content = (
            'First name,Last Name,NetID,netid@duke.edu,Email\n'
            'Ada,Lovelace,al100,al100@duke.edu,ada@example.com\n'
        ).encode('utf-8')
        result = process_user_csv(io.BytesIO(content))
        self.assertEqual(result.created, 1)
        self.assertEqual(User.objects.get(username='al100').email, 'ada@example.com')

    def test_canvas_falls_back_to_netid_duke_email(self):
        content = (
            'First name,Last Name,NetID,netid@duke.edu\n'
            'Grace,Hopper,gh200,gh200@duke.edu\n'
        ).encode('utf-8')
        result = process_user_csv(io.BytesIO(content))
        self.assertEqual(result.created, 1)
        self.assertEqual(User.objects.get(username='gh200').email, 'gh200@duke.edu')


class UserImportExistingAccountGuardTest(TestCase):
    """CA-1: an import never rewrites accounts it does not own."""
    HEADER = 'username,first_name,last_name,email\n'

    def setUp(self):
        from judge.models import Class
        from judge.models.tests.util import create_organization
        self.org = create_organization(name='Guard Course')
        self.klass = Class.objects.create(organization=self.org, name='Guard Class', slug='guard-class')

    def _import(self, rows, **kwargs):
        content = (self.HEADER + '\n'.join(rows)).encode('utf-8')
        kwargs.setdefault('organization', self.org)
        kwargs.setdefault('target_class', self.klass)
        return process_user_csv(io.BytesIO(content), update_existing=True, activate=True, **kwargs)

    def test_superuser_and_staff_never_updated(self):
        root = create_user(username='guard_root', email='root@example.com', is_superuser=True, is_staff=True)
        staff = create_user(username='guard_staff', email='staff@example.com', is_staff=True)
        for user in (root, staff):
            self.org.members.add(user.profile)
            self.klass.members.add(user.profile)
        result = self._import(['guard_root,A,B,evil@example.com', 'guard_staff,A,B,evil2@example.com'])
        self.assertEqual(result.updated, 0)
        root.refresh_from_db()
        staff.refresh_from_db()
        self.assertEqual(root.email, 'root@example.com')
        self.assertEqual(staff.email, 'staff@example.com')

    def test_user_outside_the_class_is_only_linked(self):
        outsider = create_user(username='guard_outsider', email='out@example.com', is_active=False)
        result = self._import(['guard_outsider,A,B,evil@example.com'])
        self.assertEqual(result.updated, 0)
        outsider.refresh_from_db()
        self.assertEqual(outsider.email, 'out@example.com')
        self.assertFalse(outsider.is_active)
        self.assertTrue(self.klass.members.filter(id=outsider.profile.id).exists())

    def test_course_manager_is_not_updated(self):
        manager = create_user(username='guard_manager', email='mgr@example.com')
        self.org.members.add(manager.profile)
        self.klass.members.add(manager.profile)
        self.org.admins.add(manager.profile)
        self._import(['guard_manager,A,B,evil@example.com'])
        manager.refresh_from_db()
        self.assertEqual(manager.email, 'mgr@example.com')

    def test_plain_member_of_the_class_may_be_updated(self):
        member = create_user(username='guard_member', email='old@example.com')
        member.set_unusable_password()  # SSO-only: the import may change its e-mail (CA-1)
        member.save()
        self.org.members.add(member.profile)
        self.klass.members.add(member.profile)
        result = self._import(['guard_member,Mem,Ber,new@example.com'])
        self.assertEqual(result.updated, 1)
        member.refresh_from_db()
        self.assertEqual(member.email, 'new@example.com')

    def test_no_scope_means_no_update(self):
        loner = create_user(username='guard_loner', email='loner@example.com')
        content = (self.HEADER + 'guard_loner,A,B,evil@example.com').encode('utf-8')
        process_user_csv(io.BytesIO(content), update_existing=True)
        loner.refresh_from_db()
        self.assertEqual(loner.email, 'loner@example.com')


class ContestScoreImportTestCase(TestCase):
    def _import(self, contest, rows, header='netid,first_name,last_name,score', score_column=None):
        content = (header + '\n' + '\n'.join(rows)).encode('utf-8')
        return import_contest_scores_from_csv(contest, io.BytesIO(content), score_column=score_column)

    def test_import_by_name_without_username(self):
        contest = create_contest(key='import-names')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        bob = create_user(username='bob', first_name='Bob', last_name='Lin')
        create_contest_participation(contest=contest, user=alice.profile)
        create_contest_participation(contest=contest, user=bob.profile)

        result = self._import(
            contest,
            [
                'Alice,Qiu,95',
                'Bob,Lin,88/100',
            ],
            header='first name,last name,score',
        )

        self.assertEqual(result.updated, 2)
        self.assertEqual(result.errors, [])
        self.assertEqual(
            ContestParticipation.objects.get(contest=contest, user=alice.profile).score,
            95.0,
        )
        self.assertEqual(
            ContestParticipation.objects.get(contest=contest, user=bob.profile).score,
            88.0,
        )

    def test_import_matches_name_fails_for_ambiguous_participants(self):
        contest = create_contest(key='import-ambig')
        first = create_user(username='sam-one', first_name='Sam', last_name='Lee')
        second = create_user(username='sam-two', first_name='Sam', last_name='Lee')
        create_contest_participation(contest=contest, user=first.profile)
        create_contest_participation(contest=contest, user=second.profile)

        result = self._import(
            contest,
            [
                'Sam,Lee,100',
            ],
            header='first name,last name,score',
        )

        self.assertEqual(result.updated, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn('Sam Lee', result.errors[1])

    def test_import_fails_when_identifier_is_missing(self):
        contest = create_contest(key='import-miss-id')
        create_user(username='alice', first_name='Alice', last_name='Wang')
        create_contest_participation(contest=contest, user=User.objects.get(username='alice').profile)

        result = self._import(
            contest,
            [
                'Alice,,98',
            ],
            header='first name,last name,score',
        )

        self.assertEqual(result.updated, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn('row is missing identifier information', result.errors[1])

    def test_multiple_score_columns_require_explicit_selection(self):
        contest = create_contest(key='import-score-column')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        participation = create_contest_participation(contest=contest, user=alice.profile)
        header = 'SIS Login ID,first name,last name,Current Score,Final Score'
        rows = ['alice,Alice,Qiu,75,90']

        result = self._import(contest, rows, header=header)

        self.assertFalse(result.applied)
        self.assertEqual(result.updated, 0)
        self.assertIn('Multiple score columns', result.errors[0])
        participation.refresh_from_db()
        self.assertEqual(participation.score, 0)

        result = self._import(contest, rows, header=header, score_column='Final Score')

        self.assertTrue(result.applied)
        self.assertEqual(result.updated, 1)
        participation.refresh_from_db()
        self.assertEqual(participation.score, 90)

    def test_import_is_not_applied_when_same_participant_appears_twice(self):
        contest = create_contest(key='import-duplicate')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        participation = create_contest_participation(contest=contest, user=alice.profile)

        result = self._import(contest, [
            'alice,Alice,Qiu,80',
            ',Alice,Qiu,90',
        ])

        self.assertFalse(result.applied)
        self.assertEqual(result.updated, 0)
        self.assertIn('duplicates a participant', result.errors[-1])
        participation.refresh_from_db()
        self.assertEqual(participation.score, 0)

    def test_import_rejects_malformed_scores_without_partial_updates(self):
        contest = create_contest(key='import-invalid-score')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        participation = create_contest_participation(contest=contest, user=alice.profile)

        result = self._import(contest, ['alice,Alice,Qiu,score: 95'])

        self.assertFalse(result.applied)
        self.assertEqual(result.updated, 0)
        self.assertIn('unable to parse score', result.errors[-1])
        participation.refresh_from_db()
        self.assertEqual(participation.score, 0)

    def test_joined_export_keeps_canvas_rows_only(self):
        contest = create_contest(key='joined-scores')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        test_user = create_user(username='test-user', first_name='Test', last_name='Student')
        create_contest_participation(contest=contest, user=alice.profile, score=87.5)
        create_contest_participation(contest=contest, user=test_user.profile, score=100)
        canvas_csv = (
            'Student,ID,SIS Login ID,Section,Current Score\n'
            ',,,Manual Posting,\n'
            '    Points Possible,,,100,(read only)\n'
            '"Qiu, Alice",101,,CS205,70\n'
            '"Student, Canvas",102,canvas-only,CS205,80\n'
        ).encode('utf-8')

        result = export_contest_scores_joined_csv(contest, io.BytesIO(canvas_csv))
        rows = list(csv.DictReader(io.StringIO(result.csv_content.lstrip('\ufeff'))))

        self.assertEqual(result.errors, [])
        self.assertEqual(result.joined, 1)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['Section'], 'Manual Posting')
        self.assertEqual(rows[1]['Student'], '    Points Possible')
        self.assertEqual(rows[2][contest.name], '87.5')
        self.assertEqual(rows[3][contest.name], '')
        self.assertNotIn('test-user', result.csv_content)

    def test_direct_score_export_uses_canvas_student_columns(self):
        contest = create_contest(key='canvas-export', name='Assignment 1')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        create_contest_participation(contest=contest, user=alice.profile, score=87.5)

        rows = list(csv.DictReader(io.StringIO(export_contest_scores_csv(contest).lstrip('\ufeff'))))

        self.assertEqual(rows[0]['Student'], 'Qiu, Alice')
        self.assertEqual(rows[0]['SIS Login ID'], 'alice')
        self.assertEqual(rows[0]['Assignment 1'], '87.5')

    def test_joined_export_overwrites_selected_canvas_column(self):
        contest = create_contest(key='joined-column')
        alice = create_user(username='alice', first_name='Alice', last_name='Qiu')
        create_contest_participation(contest=contest, user=alice.profile, score=87.5)
        canvas_csv = (
            'Student,SIS Login ID,Current Score\n'
            '"Qiu, Alice",alice,70\n'
        ).encode('utf-8')

        result = export_contest_scores_joined_csv(
            contest, io.BytesIO(canvas_csv), score_column='current score',
        )
        reader = csv.DictReader(io.StringIO(result.csv_content.lstrip('\ufeff')))
        rows = list(reader)

        self.assertEqual(reader.fieldnames, ['Student', 'SIS Login ID', 'Current Score'])
        self.assertEqual(rows[0]['Current Score'], '87.5')
