from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError
from django.test import SimpleTestCase

from judge.utils.problem_create import generate_problem_code
from judge.views.problem_import import _delete_problem_data_directories, _save_problem_with_unique_code


class ProblemImportCreationTestCase(SimpleTestCase):
    @mock.patch('judge.utils.problem_create.Problem.objects.values_list')
    def test_problem_code_generation_uses_one_existing_code_query(self, values_list):
        values_list.return_value = ['p000000', 'p000002']

        self.assertEqual(generate_problem_code(), 'p000001')
        values_list.assert_called_once_with('code', flat=True)

    @mock.patch('judge.views.problem_import.apply_simple_problem_defaults')
    @mock.patch('judge.views.problem_import.Problem.objects.filter')
    @mock.patch('judge.views.problem_import.transaction.atomic')
    def test_retries_generated_code_collision_before_problem_side_effects(self, atomic, problem_filter, defaults):
        problem = SimpleNamespace(code='p000000')
        problem.save = mock.Mock(side_effect=[IntegrityError(), None])
        atomic.side_effect = lambda: nullcontext()
        problem_filter.return_value.exists.return_value = True

        def assign_next_code(instance):
            instance.code = 'p000001'

        defaults.side_effect = assign_next_code

        _save_problem_with_unique_code(problem)

        self.assertEqual(problem.code, 'p000001')
        self.assertEqual(problem.save.call_count, 2)
        defaults.assert_called_once_with(problem)

    @mock.patch('judge.views.problem_import.shutil.rmtree')
    @mock.patch('judge.views.problem_import.os.path.isdir', return_value=True)
    @mock.patch('judge.views.problem_import.problem_data_storage.path')
    def test_removes_each_failed_import_data_directory_once(self, storage_path, isdir, rmtree):
        storage_path.side_effect = lambda code: '/mnt/problems/{}'.format(code)

        _delete_problem_data_directories(['p000001', 'p000002', 'p000001'])

        self.assertEqual(storage_path.call_count, 2)
        self.assertEqual(isdir.call_count, 2)
        self.assertEqual(rmtree.call_count, 2)
