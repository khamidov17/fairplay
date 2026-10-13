import unittest

from judge.utils.unique_ids import is_series_id, next_prefixed_id, wants_auto_series


class NextPrefixedIdTestCase(unittest.TestCase):
    def test_first_unused_is_zero(self):
        self.assertEqual(next_prefixed_id([], 'c'), 'c000000')
        self.assertEqual(next_prefixed_id([], 'p'), 'p000000')

    def test_fills_the_first_hole(self):
        self.assertEqual(next_prefixed_id(['p000000', 'p000002'], 'p'), 'p000001')
        self.assertEqual(next_prefixed_id({'c000000'}, 'c'), 'c000001')

    def test_is_series_id(self):
        self.assertTrue(is_series_id('p000000', 'p'))
        self.assertTrue(is_series_id('c000123', 'c'))
        self.assertFalse(is_series_id('p00000', 'p'))
        self.assertFalse(is_series_id('P000000', 'p'))
        self.assertFalse(is_series_id('homework', 'c'))
        self.assertFalse(is_series_id('', 'c'))

    def test_wants_auto_series_flag(self):
        self.assertTrue(wants_auto_series(None, 'c000001', 'c'))
        self.assertFalse(wants_auto_series(None, 'homework', 'c'))
        self.assertTrue(wants_auto_series(True, 'homework', 'c'))
        self.assertFalse(wants_auto_series(False, 'c000000', 'c'))
        self.assertTrue(wants_auto_series('1', 'homework', 'c'))
        self.assertFalse(wants_auto_series('0', 'c000000', 'c'))
        self.assertFalse(wants_auto_series('false', 'c000000', 'c'))
