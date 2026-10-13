"""Problem-type registry package.

Importing this package registers all built-in question types. Add a new type by creating a
module here that ``@register``s a ``QuestionType`` subclass and importing it below.
"""
# Imported for their registration side effects.
from judge.question_types import code, file_upload, fill_blank, mcq, short_answer  # noqa: F401
from judge.question_types.registry import all_types, creatable_types, get_grader, register

__all__ = ['all_types', 'creatable_types', 'get_grader', 'register']
