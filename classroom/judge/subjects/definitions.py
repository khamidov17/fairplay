"""The feature packs and subjects DKU OJ ships with. THE place to add a subject or a pack.

Course-code prefixes come from DKU's own pages (see ``docs/classroom/SUBJECTS.md`` for the
sources). Only prefixes we could verify are listed; a course whose code matches none of them is
created as ``DEFAULT_SUBJECT`` and the teacher can change it on the course form.

To add a subject: append one ``register_subject(Subject(...))`` below (and, if it needs its own
tools, one ``register_pack(FeaturePack(...))`` plus an optional section template).
``docs/classroom/ADDING_A_SUBJECT.md`` walks through the Chemistry example at the bottom.
"""
from django.utils.translation import gettext_lazy as _

from judge.subjects.base import (Cap, DashboardSection, FeaturePack, MenuItem, Subject, register_pack,
                                 register_subject)


def can_open_problem_creator(user):
    from judge.views.problem_create_v2 import _can_create_any
    return user.is_authenticated and _can_create_any(user)


# Existing courses (migration default) and courses whose code prefix is not recognised.
DEFAULT_SUBJECT = 'cs'
# Used when a course row holds a key that is no longer registered (e.g. a subject was removed).
FALLBACK_SUBJECT = 'other'


# ----------------------------------------------------------------------------- feature packs

register_pack(FeaturePack(
    key='core',
    label=_('Core'),
    description=_('Choice, true/false, fill in the blank, short answer, file upload and composite '
                  'questions; assignments, grading and analytics. Every subject has it.'),
    capabilities=frozenset({
        Cap.QUESTION_CHOICE, Cap.QUESTION_TRUEFALSE, Cap.QUESTION_FILL_BLANK, Cap.QUESTION_SHORT_ANSWER,
        Cap.QUESTION_FILE_UPLOAD, Cap.QUESTION_COMPOSITE, Cap.QUESTION_OTHER, Cap.EXCEL_IMPORT,
    }),
    sections=(
        DashboardSection(key='core', title=_('Questions and assignments'), template='subjects/sections/core.html',
                         places=('course',), order=10, teacher_only=True),
    ),
    menu_items=(
        # The problem creator's own rule (a class TA without it got a 404).
        MenuItem(key='new_question', label=_('New question'), url_name='problem_create_v2', icon='fa-plus',
                 course_param=True, requires=can_open_problem_creator),
        MenuItem(key='question_bank', label=_('Question bank'), url_name='problem_list', icon='fa-list',
                 course_param=True),
        MenuItem(key='new_assignment', label=_('New assignment'), url_name='contest_create', icon='fa-calendar-plus-o',
                 requires='judge.add_contest'),
    ),
))

register_pack(FeaturePack(
    key='code',
    label=_('Code and judge'),
    description=_('Programming problems, the judge, the IDE and Run tests, test data, code harness, MOSS, '
                  'rankings and submissions-by-language statistics.'),
    capabilities=frozenset({
        Cap.QUESTION_PROGRAMMING, Cap.JUDGE, Cap.IDE, Cap.TEST_DATA, Cap.HARNESS, Cap.MOSS, Cap.RANKINGS,
        Cap.LANGUAGE_STATS,
    }),
    sections=(
        DashboardSection(key='code', title=_('Programming'), template='subjects/sections/code.html',
                         places=('course', 'teacher_home'), order=20),
    ),
    menu_items=(
        MenuItem(key='ide', label=_('IDE'), url_name='ide', icon='fa-terminal', teacher_only=False),
    ),
))

register_pack(FeaturePack(
    key='math',
    label=_('Math and formulas'),
    description=_('Planned: formula input, numeric answers with tolerance and units, math keyboard.'),
    placeholder=True,
))

register_pack(FeaturePack(
    key='lab',
    label=_('Lab and data'),
    description=_('Planned: lab reports, data tables and plots, units and significant figures.'),
    placeholder=True,
))

register_pack(FeaturePack(
    key='writing',
    label=_('Writing'),
    description=_('Planned: essays with rubrics, drafts and feedback rounds, citation checks.'),
    placeholder=True,
))

register_pack(FeaturePack(
    key='language',
    label=_('Language practice'),
    description=_('Planned: listening and speaking (audio answers), character and pinyin input, vocabulary drills.'),
    placeholder=True,
))


# ----------------------------------------------------------------------------- subjects
# ``packs`` never needs 'core': it is always added. Order = order in the course form.

register_subject(Subject(
    key='cs', label=_('Computer science'), order=10, color='#2f6fb3',
    packs=('code', 'math'),
    course_prefixes=('COMPSCI', 'CS', 'INFOSCI', 'COMPDSGN'),
    description=_('Everything DKU OJ has today: programming problems, judge, IDE, MOSS, rankings.'),
))

register_subject(Subject(
    key='math', label=_('Mathematics and statistics'), order=20, color='#7a4fb3',
    packs=('math',),
    course_prefixes=('MATH', 'STATS', 'STAT'),
))

register_subject(Subject(
    key='phys', label=_('Physics'), order=30, color='#b35a2f',
    packs=('math', 'lab'),
    course_prefixes=('PHYS',),
))

register_subject(Subject(
    key='bio', label=_('Biology and life sciences'), order=50, color='#3a8f4f',
    packs=('lab',),
    course_prefixes=('BIOL',),
))

register_subject(Subject(
    key='envsci', label=_('Environmental science'), order=55, color='#4f8f7a',
    packs=('lab', 'math'),
    course_prefixes=('ENVIR',),
))

register_subject(Subject(
    key='econ', label=_('Economics'), order=60, color='#8f7a3a',
    packs=('math',),
    course_prefixes=('ECON',),
))

register_subject(Subject(
    key='socsci', label=_('Social and behavioral sciences'), order=65, color='#8f3a5c',
    packs=('writing',),
    course_prefixes=('PSYCH', 'SOSC'),
))

register_subject(Subject(
    key='humanities', label=_('Arts and humanities'), order=70, color='#6b5c4f',
    packs=('writing',),
    course_prefixes=(),  # no DKU prefix verified yet, see SUBJECTS.md
))

register_subject(Subject(
    key='lang', label=_('Languages (Chinese, EAP)'), order=75, color='#b33a3a',
    packs=('language', 'writing'),
    course_prefixes=('CHIN', 'EAP'),
))

register_subject(Subject(
    key='general', label=_('Common Core and general education'), order=80, color='#4f5c8f',
    packs=('writing',),
    course_prefixes=('CCORE', 'GCHINA', 'GLOCHALL', 'ETHLDR', 'CHSC'),
))

register_subject(Subject(
    key='other', label=_('Other'), order=1000,
    packs=(),
))


# ----------------------------------------------------------------------------- worked example
# Chemistry, exactly as docs/classroom/ADDING_A_SUBJECT.md adds it: one pack (a placeholder with one
# course-page section) and one subject line.

register_pack(FeaturePack(
    key='chem',
    label=_('Chemistry tools'),
    description=_('Planned: chemical formulas and equations, structure drawing, lab safety checklists.'),
    sections=(
        DashboardSection(key='chem', title=_('Chemistry'), template='subjects/sections/chemistry.html',
                         places=('course',), order=40),
    ),
    placeholder=True,
))

register_subject(Subject(
    key='chem', label=_('Chemistry'), order=40, color='#2f8fb3',
    packs=('chem', 'math', 'lab'),
    course_prefixes=('CHEM',),
))
