from django.test import SimpleTestCase

from judge.utils.language_matching import normalize_language_name


class LanguageMatchingTestCase(SimpleTestCase):
    def test_c_plus_plus_display_name_matches_internal_key(self):
        self.assertEqual(normalize_language_name('C++11'), normalize_language_name('CPP11'))
