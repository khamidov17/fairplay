import tempfile
from unittest import mock

from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import EditorAttachment, StoredFileRecord
from judge.models.tests.util import CommonDataMixin, create_user
from judge.storage import StorageUnavailableError
from judge.storage.resilient import ResilientStorage, generate_editor_attachment_key


def _pdf(name='notes.pdf', content=b'%PDF-1.4 hello'):
    return SimpleUploadedFile(name, content, content_type='application/pdf')


def _png(name='fig.png'):
    # 1x1 PNG
    payload = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
        b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
        b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    return SimpleUploadedFile(name, payload, content_type='image/png')


class EditorAttachmentTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users.update({
            'creator': create_user(
                username='creator',
                user_permissions=('create_private_problem', 'edit_own_problem'),
            ),
        })

    def setUp(self):
        super().setUp()
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        storage = ResilientStorage(primary=FileSystemStorage(location=self._tmp.name), backup=None)
        self._patch = mock.patch.object(EditorAttachment._meta.get_field('file'), 'storage', storage)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()
        super().tearDown()

    def test_staff_can_upload_pdf_and_download(self):
        self.client.force_login(self.users['superuser'])
        response = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _pdf()},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 200)
        self.assertEqual(body['name'], 'notes.pdf')
        self.assertFalse(body['is_image'])
        self.assertIn('/widgets/files/', body['link'])

        attachment = EditorAttachment.objects.get()
        self.assertTrue(attachment.file.name.startswith('editor_attachments/'))
        self.assertTrue(StoredFileRecord.objects.filter(path=attachment.file.name).exists())

        download = self.client.get(reverse('editor_file_download', args=[attachment.uuid]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Type'], 'application/octet-stream')
        self.assertIn('attachment', download['Content-Disposition'])
        self.assertEqual(b''.join(download.streaming_content), b'%PDF-1.4 hello')

    def test_problem_author_can_upload_and_anonymous_can_download_image_inline(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png()},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['is_image'])
        attachment = EditorAttachment.objects.get()

        self.client.logout()
        download = self.client.get(reverse('editor_file_download', args=[attachment.uuid]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Type'], 'image/png')
        self.assertNotIn('attachment', download.get('Content-Disposition', '').lower())

    def test_normal_user_can_upload_image(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png()},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 200)
        self.assertTrue(body['is_image'])
        self.assertTrue(EditorAttachment.objects.exists())

    def test_normal_user_cannot_upload_pdf(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _pdf()},
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(EditorAttachment.objects.exists())

    @override_settings(
        CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
        DMOJ_EDITOR_ATTACHMENT_DAILY_LIMIT=1,
        DMOJ_EDITOR_ATTACHMENT_RATE_LIMIT=10,
        DMOJ_EDITOR_ATTACHMENT_RATE_WINDOW_SECONDS=60,
    )
    def test_normal_user_hits_daily_quota(self):
        self.client.force_login(self.users['normal'])
        first = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png('one.png')},
        )
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png('two.png')},
        )
        self.assertEqual(second.status_code, 429, second.content)
        self.assertEqual(EditorAttachment.objects.count(), 1)

    @override_settings(
        CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
        DMOJ_EDITOR_ATTACHMENT_DAILY_LIMIT=40,
        DMOJ_EDITOR_ATTACHMENT_RATE_LIMIT=1,
        DMOJ_EDITOR_ATTACHMENT_RATE_WINDOW_SECONDS=60,
    )
    def test_normal_user_hits_rate_limit(self):
        self.client.force_login(self.users['normal'])
        first = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png('one.png')},
        )
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png('two.png')},
        )
        self.assertEqual(second.status_code, 429, second.content)

    @override_settings(
        CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
        DMOJ_EDITOR_ATTACHMENT_DAILY_LIMIT=1,
        DMOJ_EDITOR_ATTACHMENT_RATE_LIMIT=1,
        DMOJ_EDITOR_ATTACHMENT_RATE_WINDOW_SECONDS=60,
    )
    def test_failed_store_does_not_consume_student_quota(self):
        self.client.force_login(self.users['normal'])
        storage = EditorAttachment._meta.get_field('file').storage
        with mock.patch.object(storage, 'save', side_effect=StorageUnavailableError):
            failed = self.client.post(
                reverse('editor_file_uploader'),
                {'markdown-file-upload': _png('fail.png')},
            )
        self.assertEqual(failed.status_code, 503, failed.content)
        self.assertFalse(EditorAttachment.objects.exists())

        retry = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _png('ok.png')},
        )
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(EditorAttachment.objects.count(), 1)

    def test_instructor_quota_does_not_apply_to_staff(self):
        self.client.force_login(self.users['superuser'])
        with override_settings(
            DMOJ_EDITOR_ATTACHMENT_DAILY_LIMIT=1,
            DMOJ_EDITOR_ATTACHMENT_RATE_LIMIT=1,
            DMOJ_EDITOR_ATTACHMENT_RATE_WINDOW_SECONDS=60,
        ):
            for name in ('a.png', 'b.png'):
                response = self.client.post(
                    reverse('editor_file_uploader'),
                    {'markdown-file-upload': _png(name)},
                )
                self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(EditorAttachment.objects.count(), 2)

    def test_anonymous_upload_redirects(self):
        response = self.client.post(
            reverse('editor_file_uploader'),
            {'markdown-file-upload': _pdf()},
        )
        self.assertEqual(response.status_code, 302)

    def test_rejects_oversize_file(self):
        self.client.force_login(self.users['superuser'])
        with mock.patch('judge.views.editor_attachment.MAX_EDITOR_ATTACHMENT_BYTES', 10):
            response = self.client.post(
                reverse('editor_file_uploader'),
                {'markdown-file-upload': _pdf(content=b'x' * 11)},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EditorAttachment.objects.exists())

    def test_normal_user_cannot_upload_martor_image(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(
            reverse('martor_image_uploader'),
            {'markdown-image-upload': _png()},
        )
        self.assertEqual(response.status_code, 404)

    def test_create_v2_exposes_file_upload_url(self):
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_create_v2'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('editor_file_uploader'))
        self.assertContains(response, 'fileUploadUrl')

    def test_storage_key_shape(self):
        now = timezone.datetime(2026, 8, 18, tzinfo=timezone.utc)
        key = generate_editor_attachment_key(9, 'Handout (final).PDF', now=now)
        self.assertRegex(key, r'^editor_attachments/2026/08/9/[0-9a-f]{32}\.pdf$')
