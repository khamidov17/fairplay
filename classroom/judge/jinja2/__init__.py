import itertools
import json
from urllib.parse import quote

from jinja2.ext import Extension
from mptt.utils import get_cached_trees
from statici18n.templatetags.statici18n import inlinei18n

from judge.highlight_code import highlight_code
from judge.impersonation import can_impersonate_user
from judge.subjects import jinja as subjects_jinja  # noqa: F401  (registers has_feature() and friends)
from judge.user_translations import gettext
from judge.utils.assignment_harvest import assignment_harvest_enabled
from judge.utils.problems import participation_submit_progress
from judge.utils.user_search import profile_choice_label
from . import (camo, datetime, filesize, format, gravatar, language, markdown, rating, reference, render, social,
               spaceless, submission, timedelta)
from . import registry

registry.function('str', str)
registry.filter('str', str)
registry.filter('json', json.dumps)
registry.filter('highlight', highlight_code)
registry.filter('urlquote', quote)
registry.filter('roundfloat', round)
registry.function('inlinei18n', inlinei18n)
registry.function('mptt_tree', get_cached_trees)
registry.function('user_trans', gettext)
registry.function('can_impersonate_user', can_impersonate_user)
registry.function('participation_submit_progress', participation_submit_progress)
registry.function('assignment_harvest_enabled', assignment_harvest_enabled)
registry.function('profile_choice_label', profile_choice_label)


@registry.function
def counter(start=1):
    return itertools.count(start).__next__


class DMOJExtension(Extension):
    def __init__(self, env):
        super(DMOJExtension, self).__init__(env)
        env.globals.update(registry.globals)
        env.filters.update(registry.filters)
        env.tests.update(registry.tests)
