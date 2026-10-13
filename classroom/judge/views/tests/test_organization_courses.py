from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from judge.models import Class, ClassMaterial, InstructorVerificationRequest, Organization, OrganizationRequest
from judge.models.tests.util import CommonDataMixin, create_user
from judge.views.organization import safe_redirect_url


@override_settings(ALLOWED_HOSTS=['testserver'])
class SafeRedirectUrlTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_allows_same_host_relative_path(self):
        request = self.factory.get('/organizations/')
        self.assertEqual(
            safe_redirect_url(request, '/organization/1-open/', '/fallback/'),
            '/organization/1-open/',
        )

    def test_rejects_open_redirect(self):
        request = self.factory.get('/organizations/')
        self.assertEqual(
            safe_redirect_url(request, 'https://evil.example/phish', '/fallback/'),
            '/fallback/',
        )

    def test_empty_candidate_uses_fallback(self):
        request = self.factory.get('/organizations/')
        self.assertEqual(safe_redirect_url(request, '', '/fallback/'), '/fallback/')
        self.assertEqual(safe_redirect_url(request, None, '/fallback/'), '/fallback/')


class InstructorJoinRequestActionsTest(CommonDataMixin, TestCase):
    """Approve / reopen must respect grants_organization (class-only revoke)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = cls.organizations['open']
        cls.org.is_open = False
        cls.org.save()
        cls.student = create_user(username='join_student')
        cls.student_profile = cls.student.profile
        cls.klass = Class.objects.create(
            organization=cls.org,
            name='Join Class A',
            slug='join-class-a',
        )
        cls.klass.admins.add(cls.users['staff_organization_admin'].profile)

    def _post_action(self, action, request_ids, user='staff_organization_admin'):
        self.client.force_login(self.users[user])
        return self.client.post(
            reverse('organization_join_requests'),
            {'action': action, 'request_ids': [str(i) for i in request_ids]},
        )

    def test_reopen_class_only_request_keeps_course_membership(self):
        self.student_profile.organizations.add(self.org)
        req = OrganizationRequest.objects.create(
            user=self.student_profile,
            organization=self.org,
            request_class=self.klass,
            state='A',
            reason='already in course',
            grants_organization=False,
        )
        self.student_profile.classes.add(self.klass)

        response = self._post_action('reopen', [req.id])
        self.assertEqual(response.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.state, 'P')
        self.assertTrue(self.student_profile.organizations.filter(id=self.org.id).exists())
        self.assertFalse(self.student_profile.classes.filter(id=self.klass.id).exists())

    def test_reopen_course_request_removes_course_membership(self):
        req = OrganizationRequest.objects.create(
            user=self.student_profile,
            organization=self.org,
            request_class=self.klass,
            state='A',
            reason='join course',
            grants_organization=True,
        )
        self.student_profile.organizations.add(self.org)
        self.student_profile.classes.add(self.klass)

        response = self._post_action('reopen', [req.id])
        self.assertEqual(response.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.state, 'P')
        self.assertFalse(self.student_profile.organizations.filter(id=self.org.id).exists())
        self.assertFalse(self.student_profile.classes.filter(id=self.klass.id).exists())

    def test_approve_pending_request(self):
        req = OrganizationRequest.objects.create(
            user=self.student_profile,
            organization=self.org,
            request_class=None,
            state='P',
            reason='please',
            grants_organization=True,
        )
        response = self._post_action('approve', [req.id])
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.state, 'A')
        self.assertTrue(self.student_profile.organizations.filter(id=self.org.id).exists())

    def test_approve_class_only_adds_course_membership(self):
        """Class approve must not leave the user in-class without course."""
        req = OrganizationRequest.objects.create(
            user=self.student_profile,
            organization=self.org,
            request_class=self.klass,
            state='P',
            reason='class only',
            grants_organization=False,
        )
        self.assertFalse(self.student_profile.organizations.filter(id=self.org.id).exists())

        response = self._post_action('approve', [req.id])
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.state, 'A')
        self.assertTrue(self.student_profile.organizations.filter(id=self.org.id).exists())
        self.assertTrue(self.student_profile.classes.filter(id=self.klass.id).exists())

    def test_reopen_course_request_clears_all_classes_under_course(self):
        other = Class.objects.create(
            organization=self.org,
            name='Join Class B',
            slug='join-class-b',
        )
        req = OrganizationRequest.objects.create(
            user=self.student_profile,
            organization=self.org,
            request_class=self.klass,
            state='A',
            reason='join course',
            grants_organization=True,
        )
        self.student_profile.organizations.add(self.org)
        self.student_profile.classes.add(self.klass, other)

        response = self._post_action('reopen', [req.id])
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.student_profile.organizations.filter(id=self.org.id).exists())
        self.assertFalse(self.student_profile.classes.filter(id=self.klass.id).exists())
        self.assertFalse(self.student_profile.classes.filter(id=other.id).exists())


class ClassAccessAndMaterialsTest(CommonDataMixin, TestCase):
    """Class page gate + material download require membership/admin."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = cls.organizations['open']
        cls.org.is_open = True
        cls.org.save()
        cls.klass = Class.objects.create(
            organization=cls.org,
            name='Access Class',
            slug='access-class',
        )
        cls.admin = cls.users['staff_organization_admin']
        cls.klass.admins.add(cls.admin.profile)
        cls.member = create_user(username='class_member')
        cls.outsider = create_user(username='class_outsider')
        cls.member.profile.organizations.add(cls.org)
        cls.member.profile.classes.add(cls.klass)
        from django.core.files.base import ContentFile
        cls.material = ClassMaterial.objects.create(
            class_obj=cls.klass,
            name='secret.txt',
            is_folder=False,
            uploaded_by=cls.admin.profile,
        )
        cls.material.file.save('secret.txt', ContentFile(b'secret-bytes'), save=True)

    def _class_url(self):
        return reverse('class_home', args=[self.org.id, self.org.slug, self.klass.id, self.klass.slug])

    def _download_url(self):
        return reverse(
            'class_material_download',
            args=[self.org.id, self.org.slug, self.klass.id, self.klass.slug, self.material.id],
        )

    def test_anonymous_class_home_forbidden(self):
        response = self.client.get(self._class_url())
        self.assertEqual(response.status_code, 403)

    def test_outsider_class_home_forbidden(self):
        self.client.force_login(self.outsider)
        response = self.client.get(self._class_url())
        self.assertEqual(response.status_code, 403)

    def test_member_class_home_ok(self):
        self.client.force_login(self.member)
        response = self.client.get(self._class_url())
        self.assertEqual(response.status_code, 200)

    def test_admin_class_home_ok(self):
        self.client.force_login(self.admin)
        response = self.client.get(self._class_url())
        self.assertEqual(response.status_code, 200)

    def test_assignment_actions_live_on_assignments_tab(self):
        from django.contrib.auth.models import Permission

        perm = Permission.objects.get(codename='add_contest', content_type__app_label='judge')
        self.admin.user_permissions.add(perm)
        self.client.force_login(self.admin)
        html = self.client.get(self._class_url()).content.decode()
        self.assertIn('Add Assignment', html)
        self.assertIn(reverse('contest_create'), html)

    def test_member_does_not_see_assignment_actions(self):
        self.client.force_login(self.member)
        html = self.client.get(self._class_url()).content.decode()
        self.assertNotIn('Add Assignment', html)

    def test_class_admin_with_create_permission_sees_add_assignment(self):
        from django.contrib.auth.models import Permission
        from judge.models.notification import ensure_instructors_group

        ensure_instructors_group()
        teacher = create_user(username='class_admin_creator', is_staff=True)
        perm = Permission.objects.get(codename='add_contest', content_type__app_label='judge')
        teacher.user_permissions.add(perm)
        self.klass.admins.add(teacher.profile)
        self.client.force_login(teacher)
        html = self.client.get(self._class_url()).content.decode()
        self.assertIn('Add Assignment', html)
        self.assertIn(reverse('contest_create'), html)

    def test_pending_can_open_class_but_not_download(self):
        pending = create_user(username='class_pending')
        pending.profile.organizations.add(self.org)
        OrganizationRequest.objects.create(
            user=pending.profile,
            organization=self.org,
            request_class=self.klass,
            state='P',
            reason='want in',
            grants_organization=False,
        )
        self.client.force_login(pending)
        response = self.client.get(self._class_url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_view_content'])
        dl = self.client.get(self._download_url())
        self.assertEqual(dl.status_code, 403)

    def test_member_can_download_material(self):
        self.client.force_login(self.member)
        response = self.client.get(self._download_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'secret-bytes')

    def test_outsider_download_forbidden(self):
        self.client.force_login(self.outsider)
        response = self.client.get(self._download_url())
        self.assertEqual(response.status_code, 403)

    def test_cross_class_material_id_not_downloadable(self):
        """Student in class A must not fetch class B's file by swapping material id."""
        other = Class.objects.create(
            organization=self.org, name='Other Class Materials', slug='other-class-mat',
        )
        other.members.add(self.member.profile)
        from django.core.files.base import ContentFile
        foreign = ClassMaterial.objects.create(
            class_obj=other, name='other-secret.txt', is_folder=False,
            uploaded_by=self.admin.profile,
        )
        foreign.file.save('other-secret.txt', ContentFile(b'other-bytes'), save=True)
        # URL claims self.klass but mid belongs to other class → 404
        url = reverse(
            'class_material_download',
            args=[self.org.id, self.org.slug, self.klass.id, self.klass.slug, foreign.id],
        )
        self.client.force_login(self.member)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_delete_folder_removes_nested_oss_files(self):
        from django.core.files.base import ContentFile
        from judge.models import StoredFileRecord

        folder = ClassMaterial.objects.create(
            class_obj=self.klass, name='Notes', is_folder=True, uploaded_by=self.admin.profile,
        )
        nested = ClassMaterial.objects.create(
            class_obj=self.klass, parent=folder, name='nested.txt', is_folder=False,
            uploaded_by=self.admin.profile,
        )
        nested.file.save('nested.txt', ContentFile(b'nested-bytes'), save=True)
        path = nested.file.name
        self.assertTrue(path)
        self.assertTrue(StoredFileRecord.objects.filter(path=path).exists())

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse(
                'class_material_action',
                args=[self.org.id, self.org.slug, self.klass.id, self.klass.slug],
            ),
            {'action': 'delete', 'material_ids': [str(folder.id)]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ClassMaterial.objects.filter(id=folder.id).exists())
        self.assertFalse(ClassMaterial.objects.filter(id=nested.id).exists())
        record = StoredFileRecord.objects.filter(path=path).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, StoredFileRecord.STATUS_DELETED)
        self.assertTrue(path.startswith('class_materials/'))


class LeaveCourseClearsClassesTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = cls.organizations['open']
        cls.klass = Class.objects.create(
            organization=cls.org,
            name='Leave Class',
            slug='leave-class',
        )
        cls.student = create_user(username='leave_student')
        cls.student.profile.organizations.add(cls.org)
        cls.student.profile.classes.add(cls.klass)

    def test_leave_removes_class_membership(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse('leave_organization', args=[self.org.id, self.org.slug]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.student.profile.organizations.filter(id=self.org.id).exists())
        self.assertFalse(self.student.profile.classes.filter(id=self.klass.id).exists())


class KickUserClearsClassesTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = cls.organizations['open']
        cls.klass = Class.objects.create(
            organization=cls.org,
            name='Kick Class',
            slug='kick-class',
        )
        cls.admin = cls.users['staff_organization_admin']
        cls.org.admins.add(cls.admin.profile)
        cls.student = create_user(username='kick_student')
        cls.student.profile.organizations.add(cls.org)
        cls.student.profile.classes.add(cls.klass)

    def test_kick_removes_course_and_class(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('organization_user_kick', args=[self.org.id, self.org.slug]),
            {'user': self.student.profile.id},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.student.profile.organizations.filter(id=self.org.id).exists())
        self.assertFalse(self.student.profile.classes.filter(id=self.klass.id).exists())


class SyncClassMembersToOrgTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = cls.organizations['open']
        cls.klass = Class.objects.create(
            organization=cls.org,
            name='Sync Class',
            slug='sync-class',
        )

    def test_sync_adds_org_membership(self):
        from judge.views.organization import sync_class_members_to_organization
        user = create_user(username='sync_only_class')
        self.klass.members.add(user.profile)
        self.assertFalse(user.profile.organizations.filter(id=self.org.id).exists())
        sync_class_members_to_organization(self.klass)
        self.assertTrue(user.profile.organizations.filter(id=self.org.id).exists())


class ScopedInstructorVerificationTest(CommonDataMixin, TestCase):
    """Site-wide teacher vs class-instructor verification."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = cls.organizations['open']
        cls.klass = Class.objects.create(
            organization=cls.org,
            name='CS101 Spring',
            slug='cs101-spring',
            term_session='Spring',
            academic_year='2026',
        )
        cls.other = Class.objects.create(
            organization=cls.org,
            name='CS101 Fall',
            slug='cs101-fall',
            term_session='Fall',
            academic_year='2026',
        )
        cls.applicant = create_user(username='instructor_applicant')

    def test_teacher_submit_and_approve_grants_instructors_group(self):
        from django.contrib.auth.models import Group
        from judge.models.notification import INSTRUCTORS_GROUP, ensure_instructors_group

        ensure_instructors_group()
        self.client.force_login(self.applicant)
        response = self.client.post(reverse('organization_instructor_verification'), {
            'kind': 'teacher',
            'reason': 'I teach at DKU',
        })
        self.assertEqual(response.status_code, 302)
        req = InstructorVerificationRequest.objects.get(user=self.applicant.profile, state='P')
        self.assertEqual(req.kind, InstructorVerificationRequest.KIND_TEACHER)
        self.assertIsNone(req.request_class_id)

        self.client.force_login(self.users['superuser'])
        response = self.client.post(
            reverse('organization_join_requests'),
            {'action': 'approve', 'request_ids': ['v:%d' % req.id]},
        )
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.state, 'A')
        self.assertTrue(req.granted_instructors_group)
        instructors = Group.objects.get(name=INSTRUCTORS_GROUP)
        self.assertTrue(instructors.user_set.filter(id=self.applicant.id).exists())
        self.applicant.refresh_from_db()
        self.assertTrue(self.applicant.is_staff)
        # Not automatically a class or course admin.
        self.assertFalse(self.klass.admins.filter(id=self.applicant.profile.id).exists())
        self.assertFalse(self.org.admins.filter(id=self.applicant.profile.id).exists())

    def test_ensure_instructors_group_merges_singular_alias(self):
        from django.contrib.auth.models import Group, Permission
        from judge.models.notification import INSTRUCTORS_GROUP, INSTRUCTOR_PERMISSION_CODENAMES, \
            ensure_instructors_group, user_in_instructors_group

        alias = Group.objects.create(name='Instructor')
        alias.user_set.add(self.applicant)
        leftover = Permission.objects.filter(content_type__app_label='judge', codename='add_problem').first()
        if leftover is not None:
            alias.permissions.add(leftover)

        group = ensure_instructors_group()
        self.assertEqual(group.name, INSTRUCTORS_GROUP)
        self.assertFalse(Group.objects.filter(name='Instructor').exists())
        self.assertTrue(group.user_set.filter(id=self.applicant.id).exists())
        self.assertTrue(user_in_instructors_group(self.applicant))
        attached = set(group.permissions.filter(content_type__app_label='judge')
                       .values_list('codename', flat=True))
        self.assertTrue(set(INSTRUCTOR_PERMISSION_CODENAMES).issubset(attached))
        self.assertNotIn('add_problem', attached)

    def test_site_teacher_joining_class_becomes_instructor(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import (
            grant_site_teacher_role, maybe_add_course_instructor, profile_can_access_class,
            profile_manages_class,
            profile_can_view_class_content,
        )

        ensure_instructors_group()
        grant_site_teacher_role(self.applicant.profile)
        self.org.members.add(self.applicant.profile)
        maybe_add_course_instructor(self.applicant.profile, self.org)
        # Course teacher may open/view every class read-only, but not edit.
        self.assertTrue(profile_can_access_class(self.applicant.profile, self.klass, self.applicant))
        self.assertTrue(profile_can_access_class(self.applicant.profile, self.other, self.applicant))
        self.assertTrue(profile_can_view_class_content(self.applicant.profile, self.klass, self.applicant))
        self.assertFalse(profile_manages_class(self.applicant.profile, self.klass, self.applicant))
        self.client.force_login(self.applicant)
        response = self.client.get(self.klass.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Apply as class instructor')
        # Sibling class also openable.
        self.assertEqual(self.client.get(self.other.get_absolute_url()).status_code, 200)
        # Students still cannot open classes they did not join.
        student = self.users['normal']
        self.org.members.add(student.profile)
        self.assertFalse(profile_can_access_class(student.profile, self.klass, student))

    def test_site_teacher_course_join_adds_course_instructor(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role, maybe_add_course_instructor

        ensure_instructors_group()
        grant_site_teacher_role(self.applicant.profile)
        self.org.members.add(self.applicant.profile)
        maybe_add_course_instructor(self.applicant.profile, self.org)
        self.assertTrue(self.org.instructors.filter(id=self.applicant.profile.id).exists())
        # Students without teacher status are not added.
        student = self.users['normal']
        maybe_add_course_instructor(student.profile, self.org)
        self.assertFalse(self.org.instructors.filter(id=student.profile.id).exists())

    def test_teacher_member_can_create_class(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role

        ensure_instructors_group()
        grant_site_teacher_role(self.applicant.profile)
        self.org.members.add(self.applicant.profile)
        self.client.force_login(self.applicant)
        url = reverse('organization_create_class', args=[self.org.id, self.org.slug])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class CourseJoinRequestRoleTest(CommonDataMixin, TestCase):
    """Students pick a class; teachers may leave class empty or choose one to co-teach."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.org = Organization.objects.create(
            name='Private Course', slug='priv-course', short_name='PRIV',
            about='closed', is_open=False, class_required=True,
        )
        cls.org.admins.add(cls.users['staff_organization_admin'].profile)
        cls.klass = Class.objects.create(
            organization=cls.org, name='PRIV Spring', slug='priv-spring', is_active=True,
        )
        cls.student = create_user(username='joinrole_student')
        cls.teacher = create_user(username='joinrole_teacher')

    def test_student_must_select_class(self):
        self.client.force_login(self.student)
        url = reverse('request_organization', args=[self.org.id, self.org.slug])
        response = self.client.post(url, {
            'reason': 'please',
            'next': reverse('organization_list'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrganizationRequest.objects.filter(user=self.student.profile).exists())

    def test_teacher_can_request_without_class(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role

        ensure_instructors_group()
        grant_site_teacher_role(self.teacher.profile)
        self.client.force_login(self.teacher)
        url = reverse('request_organization', args=[self.org.id, self.org.slug])
        response = self.client.post(url, {
            'next': reverse('organization_list'),
        })
        self.assertEqual(response.status_code, 302)
        req = OrganizationRequest.objects.get(user=self.teacher.profile, organization=self.org)
        self.assertIsNone(req.request_class_id)
        self.assertEqual(req.state, 'P')
        self.assertTrue(req.grants_organization)

    def test_teacher_can_request_a_class_to_co_teach(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role

        ensure_instructors_group()
        grant_site_teacher_role(self.teacher.profile)
        self.client.force_login(self.teacher)
        url = reverse('request_organization', args=[self.org.id, self.org.slug])
        response = self.client.post(url, {
            'class_': str(self.klass.id),
            'next': reverse('organization_list'),
        })
        self.assertEqual(response.status_code, 302)
        req = OrganizationRequest.objects.get(user=self.teacher.profile, organization=self.org)
        self.assertEqual(req.request_class_id, self.klass.id)

    def test_approving_teacher_course_request_adds_instructor(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role

        ensure_instructors_group()
        grant_site_teacher_role(self.teacher.profile)
        req = OrganizationRequest.objects.create(
            user=self.teacher.profile,
            organization=self.org,
            request_class=None,
            state='P',
            reason='instructor',
            grants_organization=True,
        )
        self.client.force_login(self.users['staff_organization_admin'])
        response = self.client.post(
            reverse('organization_join_requests'),
            {'action': 'approve', 'request_ids': [str(req.id)]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.org.members.filter(id=self.teacher.profile.id).exists())
        self.assertTrue(self.org.instructors.filter(id=self.teacher.profile.id).exists())
        self.assertFalse(self.klass.admins.filter(id=self.teacher.profile.id).exists())

    def test_approving_teacher_class_request_grants_class_instructor(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role

        ensure_instructors_group()
        grant_site_teacher_role(self.teacher.profile)
        req = OrganizationRequest.objects.create(
            user=self.teacher.profile,
            organization=self.org,
            request_class=self.klass,
            state='P',
            reason='co-teach',
            grants_organization=True,
        )
        self.client.force_login(self.users['staff_organization_admin'])
        response = self.client.post(
            reverse('organization_join_requests'),
            {'action': 'approve', 'request_ids': [str(req.id)]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.org.instructors.filter(id=self.teacher.profile.id).exists())
        self.assertTrue(self.klass.admins.filter(id=self.teacher.profile.id).exists())

    def test_list_shows_join_modal_trigger(self):
        self.client.force_login(self.student)
        html = self.client.get(reverse('organization_list')).content.decode()
        self.assertIn('js-open-join-modal', html)
        self.assertIn('join-request-modal', html)
        self.assertIn('join-classes-json', html)

    def test_course_home_does_not_offer_a_second_request(self):
        self.client.force_login(self.student)
        html = self.client.get(self.org.get_absolute_url()).content.decode()
        self.assertNotIn('js-open-join-modal', html)
        self.assertNotIn('join-request-modal', html)
        self.assertIn('Request from course list', html)
        self.assertIn('join=%s' % self.org.id, html)


class ClassSiteFormTests(CommonDataMixin, TestCase):
    def test_instructor_required_and_name_autogenerated(self):
        from judge.forms import ClassSiteForm, suggested_class_name

        org = self.organizations['open']
        form = ClassSiteForm(data={
            'academic_year': '2026-2027',
            'term_session': 'Spring - Session 1',
            'is_active': 'on',
        }, organization=org)
        self.assertFalse(form.is_valid())
        self.assertIn('instructor_names', form.errors)

        form = ClassSiteForm(data={
            'academic_year': '2026-2027',
            'term_session': 'Spring - Session 1',
            'instructor_names': 'Alice Smith',
            'is_active': 'on',
        }, organization=org)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['name'], '2026-2027 Spring - Session 1 Alice Smith')
        self.assertEqual(
            suggested_class_name('2027-2028', 'Fall Session', 'Prof. Huang'),
            '2027-2028 Fall Session Prof. Huang',
        )

        # JS-filled legacy names (course + year + session) are rewritten to include the instructor.
        legacy_name = ' '.join(
            part for part in [(org.short_name or org.name), '2026-2027', 'Spring - Session 1'] if part
        )
        form = ClassSiteForm(data={
            'name': legacy_name,
            'academic_year': '2026-2027',
            'term_session': 'Spring - Session 1',
            'instructor_names': 'Prof. Huang',
            'is_active': 'on',
        }, organization=org)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['name'], '2026-2027 Spring - Session 1 Prof. Huang')
