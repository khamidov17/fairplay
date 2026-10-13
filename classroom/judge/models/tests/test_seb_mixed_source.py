from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase

from judge.models import SEBConfigPreset
from judge.models.tests.util import CommonDataMixin, create_contest

BEK = 'a' * 64
CFG = 'b' * 64
PRESET_BEK = 'c' * 64
PRESET_CFG = 'd' * 64


class SEBMixedSourceValidationTestCase(CommonDataMixin, TestCase):
    """A .seb from one record with key hashes from another cannot authenticate."""

    def make_preset(self):
        preset = SEBConfigPreset(name='Std', browser_exam_keys=PRESET_BEK, config_keys=PRESET_CFG)
        preset.config_file.save('preset.seb', ContentFile(b'PRESET'), save=False)
        preset.save()
        return preset

    def contest(self, **kwargs):
        return create_contest(key='mix', is_visible=True, seb_required=True, **kwargs)

    def test_contest_file_with_preset_keys_is_rejected(self):
        # Exactly the state that locked out macOS: old upload still served,
        # hashes copied from the newer shared configuration.
        c = self.contest()
        c.seb_preset = self.make_preset()
        c.seb_config_file.save('own.seb', ContentFile(b'OWN'), save=True)
        with self.assertRaises(ValidationError) as raised:
            c.full_clean()
        errors = raised.exception.message_dict
        self.assertIn('seb_browser_exam_keys', errors)
        self.assertIn('seb_config_keys', errors)
        self.assertIn('locks them out', str(errors['seb_config_keys']))

    def test_preset_file_with_contest_keys_is_rejected(self):
        c = self.contest(seb_browser_exam_keys=BEK, seb_config_keys=CFG)
        c.seb_preset = self.make_preset()
        with self.assertRaises(ValidationError) as raised:
            c.full_clean()
        self.assertIn('seb_config_keys', raised.exception.message_dict)

    def test_all_from_the_preset_is_fine(self):
        c = self.contest()
        c.seb_preset = self.make_preset()
        c.full_clean()

    def test_all_from_the_contest_is_fine(self):
        c = self.contest(seb_browser_exam_keys=BEK, seb_config_keys=CFG)
        c.seb_config_file.save('own.seb', ContentFile(b'OWN'), save=True)
        c.full_clean()

    def test_contest_overriding_only_the_quit_url_is_fine(self):
        # Overriding a non-authenticating field does not mix key sources.
        c = self.contest(seb_quit_url='https://example.com/quit')
        c.seb_preset = self.make_preset()
        c.full_clean()
