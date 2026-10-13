from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from judge.models.tests.util import CommonDataMixin, create_problem, create_problem_type
from judge.views.problem_create_v2 import classify_problem_difficulty


class ClassifyProblemDifficultyTests(TestCase):
    def test_heuristic_when_llm_unconfigured(self):
        with mock.patch('judge.llm.is_configured', return_value=False):
            self.assertEqual(classify_problem_difficulty('short'), 'easy')
            self.assertEqual(classify_problem_difficulty('x' * 120), 'medium')
            self.assertEqual(classify_problem_difficulty('x' * 300), 'hard')


class TagMissingDifficultiesCommandTests(CommonDataMixin, TestCase):
    def test_apply_tags_only_untagged_codes(self):
        create_problem_type(name='easy', full_name='Easy')
        create_problem_type(name='type')
        untagged = create_problem(
            code='nodiff1', name='No difficulty', types=('type',), is_public=True,
        )
        untagged.description = 'A short statement.'
        untagged.save(update_fields=['description'])
        tagged = create_problem(
            code='hasdiff1', name='Has difficulty', types=('easy', 'type'), is_public=True,
        )
        tagged.description = 'Already tagged.'
        tagged.save(update_fields=['description'])

        with mock.patch(
            'judge.management.commands.tag_missing_difficulties.classify_text',
            return_value='easy',
        ) as classify:
            call_command(
                'tag_missing_difficulties', '--apply', '--sleep', '0',
                '--code', 'nodiff1', '--code', 'hasdiff1',
                stdout=StringIO(),
            )
            classify.assert_called_once()

        untagged.refresh_from_db()
        tagged.refresh_from_db()
        untagged.__dict__.pop('difficulty', None)
        tagged.__dict__.pop('difficulty', None)
        self.assertEqual(untagged.difficulty, 'easy')
        self.assertEqual(tagged.difficulty, 'easy')
