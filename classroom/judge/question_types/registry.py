"""The problem-type registry: the single source of truth for question types."""

_registry = {}


def register(question_type_cls):
    """Class decorator that instantiates and registers a ``QuestionType``."""
    instance = question_type_cls()
    if instance.submission_type in _registry:
        raise ValueError('Duplicate question type for %r' % instance.submission_type)
    _registry[instance.submission_type] = instance
    return question_type_cls


def get_grader(submission_type):
    """Return the registered ``QuestionType`` for a ``Problem.submission_type`` value."""
    try:
        return _registry[submission_type]
    except KeyError:
        raise NotImplementedError('No grader registered for submission type %r' % submission_type)


def all_types():
    """All registered types, ordered for menu display."""
    return sorted(_registry.values(), key=lambda t: t.order)


def creatable_types(user):
    """Types ``user`` is allowed to create — drives the 'Add new problem' menu."""
    return [t for t in all_types() if t.can_create(user)]
