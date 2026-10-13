"""Tests for the contest analytics anomaly ranking (AI-5, AI-6)."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import ContestParticipation, Language, Submission
from judge.models.tests.util import create_contest, create_contest_problem, create_problem, create_user
from judge.utils.contest_analytics import _compute_anomaly_scores, get_contest_analytics


def _student(uid, *, first_try=40.0, attempts=3.0, no_prog=20.0, burst=0, spacing=1500.0,
             comments=5.0, length=400.0, perfect=30.0, subs=10):
    return {
        'username': 'u%d' % uid, 'user_id': uid, 'score': 0, 'problems_solved': 1,
        'total_submissions': subs,
        'attempt': {'first_try_ac_rate': first_try, 'avg_attempts_before_ac': attempts,
                    'no_progression_rate': no_prog},
        'timing': {'burst_count': burst, 'spacing_variance': spacing},
        'code': {'avg_comment_density': comments, 'avg_source_length': length},
        'score_patterns': {'perfect_score_rate': perfect},
    }


class AnomalyPercentileTestCase(TestCase):
    def _class_of_20_with_2_outliers(self):
        metrics = {}
        for uid in range(18):
            # Ordinary students: small, ordinary spread on every metric.
            wiggle = (uid % 3) - 1
            metrics[uid] = _student(uid, first_try=40 + wiggle, attempts=3 + wiggle * 0.1,
                                    spacing=1500 + wiggle * 10, length=400 + wiggle * 5,
                                    perfect=30 + wiggle)
        metrics[100] = _student(100, first_try=100, attempts=1, perfect=100, length=900)
        metrics[101] = _student(101, first_try=100, attempts=1, perfect=100, length=1200, burst=3)
        return metrics

    def test_twenty_students_two_outliers(self):
        metrics = self._class_of_20_with_2_outliers()
        _compute_anomaly_scores(metrics)
        for uid in range(18):
            with self.subTest(student=uid):
                self.assertEqual(metrics[uid]['anomaly_score'], 0)
                self.assertEqual(metrics[uid]['anomaly_percentile'], 0)
                self.assertEqual(metrics[uid]['anomaly_level'], 'low')
        self.assertGreater(metrics[101]['anomaly_score'], metrics[100]['anomaly_score'])
        self.assertGreater(metrics[100]['anomaly_score'], 0)
        # Strictly-lower rank: 18 of 20 below the first outlier, 19 below the second.
        self.assertEqual(metrics[100]['anomaly_percentile'], 90)
        self.assertEqual(metrics[101]['anomaly_percentile'], 95)
        self.assertEqual(metrics[100]['anomaly_level'], 'high')
        self.assertEqual(metrics[101]['anomaly_level'], 'high')

    def test_all_tied_at_zero_is_not_high(self):
        metrics = {uid: _student(uid) for uid in range(10)}
        _compute_anomaly_scores(metrics)
        for m in metrics.values():
            self.assertEqual(m['anomaly_percentile'], 0)
            self.assertEqual(m['anomaly_level'], 'low')

    def test_few_submissions_marked_not_enough_data(self):
        metrics = self._class_of_20_with_2_outliers()
        # Old producers reported 0 for "lower is suspicious" metrics with no data.
        metrics[200] = _student(200, attempts=0, spacing=0, first_try=0, perfect=0, length=0, subs=0)
        metrics[201] = _student(201, attempts=1, spacing=0, subs=1)
        _compute_anomaly_scores(metrics)
        for uid in (200, 201):
            with self.subTest(student=uid):
                self.assertTrue(metrics[uid]['insufficient_data'])
                self.assertIsNone(metrics[uid]['anomaly_score'])
                self.assertIsNone(metrics[uid]['anomaly_percentile'])
                self.assertEqual(metrics[uid]['outlier_metrics'], [])
        # Leaving them out of the class statistics keeps the real ranking intact.
        self.assertEqual(metrics[100]['anomaly_percentile'], 90)
        self.assertEqual(metrics[0]['anomaly_level'], 'low')

    @override_settings(DMOJ_ANALYTICS_MIN_SUBMISSIONS=12)
    def test_minimum_is_a_setting(self):
        metrics = self._class_of_20_with_2_outliers()
        _compute_anomaly_scores(metrics)
        self.assertTrue(all(m['insufficient_data'] for m in metrics.values()))


class AnalyticsNoSubmissionStudentsTestCase(TestCase):
    """AI-6 end to end: absent students do not top the anomaly list."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.contest = create_contest(key='anacontest', start_time=now - timedelta(hours=5),
                                     end_time=now + timedelta(hours=5))
        cls.problems = [create_problem(code='anaprob%d' % i) for i in range(2)]
        for order, problem in enumerate(cls.problems):
            create_contest_problem(contest=cls.contest, problem=problem, order=order)
        language = Language.objects.get(key='PY3')
        cls.active = []
        for i in range(6):
            user = create_user(username='anaactive%d' % i)
            ContestParticipation.objects.create(contest=cls.contest, user=user.profile)
            cls.active.append(user)
            for j in range(4):
                problem = cls.problems[j % 2]
                sub = Submission.objects.create(
                    user=user.profile, problem=problem, language=language, status='D',
                    result='AC' if j >= 2 else 'WA', contest_object=cls.contest,
                    case_points=1 if j >= 2 else 0, case_total=1,
                )
                Submission.objects.filter(id=sub.id).update(
                    date=now - timedelta(hours=4) + timedelta(minutes=10 * j + 7 * i * (j % 2 + 1)))
        cls.absent = create_user(username='anaabsent')
        ContestParticipation.objects.create(contest=cls.contest, user=cls.absent.profile)
        cls.once = create_user(username='anaonce')
        ContestParticipation.objects.create(contest=cls.contest, user=cls.once.profile)
        Submission.objects.create(
            user=cls.once.profile, problem=cls.problems[0], language=language, status='D',
            result='WA', contest_object=cls.contest, case_points=0, case_total=1,
        )

    def test_absent_and_single_submission_students_are_not_ranked(self):
        students = get_contest_analytics(self.contest)['students']
        for user in (self.absent, self.once):
            m = students[user.profile.id]
            with self.subTest(user=user.username):
                self.assertTrue(m['insufficient_data'])
                self.assertIsNone(m['anomaly_score'])
                self.assertEqual(m['outlier_metrics'], [])
        absent = students[self.absent.profile.id]
        self.assertIsNone(absent['attempt']['avg_attempts_before_ac'])
        self.assertIsNone(absent['timing']['spacing_variance'])
        for user in self.active:
            self.assertFalse(students[user.profile.id]['insufficient_data'])

    def test_analytics_page_lists_absent_students_last(self):
        editor = create_user(username='anaeditor', user_permissions=('edit_all_contest',))
        self.client.force_login(editor)
        response = self.client.get('/contest/%s/analytics' % self.contest.key)
        self.assertEqual(response.status_code, 200)
        names = [s['username'] for s in response.context['students_list']]
        self.assertEqual(set(names[-2:]), {'anaabsent', 'anaonce'})
        self.assertContains(response, 'Not enough data')
