"""Tests for the AI-hint window quota: reservation, refunds, and cold-cache seeding."""
from django.core.cache import cache
from django.test import TestCase

from judge import ai_hints
from judge.models import Language, Submission
from judge.models.ai_hints import AIHintLog
from judge.models.tests.util import create_problem, create_user


class AIHintQuotaTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.profile = create_user(username='hintuser').profile

    def setUp(self):
        cache.clear()

    def test_fresh_user_has_full_quota(self):
        self.assertEqual(ai_hints.get_remaining(self.profile.id), 5)

    def test_reserve_consumes_and_stops_at_limit(self):
        for _ in range(5):
            self.assertTrue(ai_hints.try_reserve(self.profile.id))
        self.assertFalse(ai_hints.try_reserve(self.profile.id))
        self.assertEqual(ai_hints.get_remaining(self.profile.id), 0)
        # A failed reservation must not push the counter past the limit.
        self.assertFalse(ai_hints.try_reserve(self.profile.id))
        ai_hints.refund(self.profile.id)
        self.assertEqual(ai_hints.get_remaining(self.profile.id), 1)

    def test_refund_restores_slot(self):
        self.assertTrue(ai_hints.try_reserve(self.profile.id))
        self.assertEqual(ai_hints.get_remaining(self.profile.id), 4)
        ai_hints.refund(self.profile.id)
        self.assertEqual(ai_hints.get_remaining(self.profile.id), 5)

    def test_cold_cache_seeds_from_db(self):
        problem = create_problem(code='hintseed')
        submission = Submission.objects.create(user=self.profile, problem=problem,
                                               language=Language.get_default_language())
        AIHintLog.objects.create(user=self.profile, submission=submission, problem=problem, success=True)
        AIHintLog.objects.create(user=self.profile, submission=submission, problem=problem, success=True)
        AIHintLog.objects.create(user=self.profile, submission=submission, problem=problem, success=False)
        cache.clear()
        # Two successful hints in the current window; the failed one does not count.
        self.assertEqual(ai_hints.get_remaining(self.profile.id), 3)
