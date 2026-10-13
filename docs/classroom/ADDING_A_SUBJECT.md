# Adding a subject to DKU OJ

Every subject is declared in one file: `classroom/judge/subjects/definitions.py`. Adding a subject takes one `register_subject(...)` call there. If the subject needs its own tools, you also add one `register_pack(...)` call and, optionally, one section template. You do not need a migration, a form change or a model change:

- the course form and the admin read their subject list from the registry;
- `Organization.subject` is a plain `CharField`, checked by a validator against the registry.

The DKU subjects that exist and why they have the packs they have are listed in `SUBJECTS.md`.

## The pieces

| Piece | Where | What it is |
|---|---|---|
| Capability | `Cap` in `judge/subjects/base.py` | One switchable feature, e.g. `ide`, `question_programming`. |
| Feature pack | `register_pack(FeaturePack(...))` in `definitions.py` | A key, a label, a set of capabilities, and optional dashboard sections and menu items. |
| Subject | `register_subject(Subject(...))` in `definitions.py` | A key (max 32 characters: lowercase letters, digits, `_`), a label, its packs (`core` is always added), the DKU course-code prefixes, a colour and a sort order. |
| Dashboard section | `DashboardSection(...)` inside a pack | A template rendered on the course page (`places=('course',)`), on the teacher home (`'teacher_home'`), or both. Set `teacher_only=True` to hide it from students. |
| Menu item | `MenuItem(...)` inside a pack | A link on the course card on the teacher home. With `course_param=True` it gets `?course=<id>`, so the problem editor opens in that course's context. |

The API that views and templates use is in `judge/subjects/__init__.py`:

```python
from judge.subjects import subject_for, features_for, has_feature, ensure_question_type_allowed

subject_for(course)                       # -> Subject (course = Organization or Class)
features_for(user, course=None)           # -> frozenset of pack keys + capabilities
has_feature(user, 'code', course)         # a pack key or a capability
ensure_question_type_allowed(user, 'programming', [course, ...])  # raises PermissionError
```

```jinja
{% if has_feature('code') %} ... {% endif %}          {# uses the view's `subject_course`, else site-wide #}
{% if has_feature('ide', contest) %} ... {% endif %}  {# an Organization, Class, Contest, Problem or a list #}
{{ course_subject(organization).label }}
{% for section in subject_sections('course', organization) %}{% include section.template %}{% endfor %}
```

## Worked example: Chemistry

This is exactly what is in the repository; look at the bottom of `definitions.py`.

**1. Register the pack** (a placeholder with one course-page section):

```python
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
```

**2. Register the subject.** CHEM is the DKU prefix, seen in the DNAS four-year plans (CHEM 110):

```python
register_subject(Subject(
    key='chem', label=_('Chemistry'), order=40, color='#2f8fb3',
    packs=('chem', 'math', 'lab'),
    course_prefixes=('CHEM',),
))
```

**3. Add the section template** `classroom/templates/subjects/sections/chemistry.html`. It receives `section`, `course`, `subject` and `is_teacher`, and it can call `has_feature(...)` and `url(...)`:

```jinja
<p class="muted">{{ _('Chemistry tools (formula and equation input, structure drawing, lab safety checklists) are planned.') }}</p>
{% if is_teacher %}
    <ul class="subject-links">
        <li><a href="{{ url('problem_create_v2') }}?course={{ course.id }}"><i class="fa fa-flask"></i> {{ _('New chemistry question') }}</a></li>
    </ul>
{% endif %}
```

That is all. Results:

- "Chemistry" appears in the Subject dropdown of the course form and the admin.
- A new course with the code `CHEM 301` and "Detect from course code" selected becomes `chem`.
- The course page shows the Chemistry badge, the core section and the Chemistry section.
- The course has no IDE, programming type, MOSS or rankings, because `chem` does not list the `code` pack.

The tests that check this: `judge.subjects.tests.SubjectRegistryTestCase.test_chemistry_worked_example_is_registered`, `judge.views.tests.test_subjects.CoursePageSubjectTestCase.test_chemistry_course_page_renders_its_pack_section` and `...CourseFormSubjectTestCase.test_create_infers_subject_from_course_code`.

## Other common changes

- **Give a subject an existing pack.** Add the pack key to its `packs`. For example, `packs=('code', 'math')` gives Statistics the judge and the IDE for R or Python homework.
- **Map another DKU prefix.** Add it to `course_prefixes`. A prefix may belong to only one subject; registration fails at start-up if two subjects claim the same prefix. Courses that already exist keep their subject. Change them in the admin (the list can be filtered by subject).
- **Fill a placeholder pack.** Add a capability constant to `Cap`, add it to the pack's `capabilities`, and check it where the feature lives: `has_feature(user, Cap.X, course)` in Python, or `has_feature('x')` in templates. For a new question type, also map it in `CREATE_V2_TYPE_CAPS` / `SUBMISSION_TYPE_CAPS` (`base.py`). The create page, the problem list filters and the server-side refusal then follow automatically.
- **Hide a navigation entry.** Navigation entries are `NavigationBar` rows that admins edit. Map the entry's key or path to a capability in `NAV_KEY_CAPS` / `NAV_PATH_CAPS` (`judge/subjects/access.py`).
- **Remove a subject.** Delete its `register_subject` call. Courses that still hold its key fall back to `other` (core only) until someone reassigns them. Nothing breaks.

## Checklist

1. Edit `definitions.py`, and add a section template if the pack has a section.
2. If you added a new prefix, cite the DKU page for it in `SUBJECTS.md`.
3. Run `bash classroom/scripts/dev-test.sh judge.subjects judge.views.tests.test_subjects`. The registry tests check that every subject uses registered packs, that only CS has the code pack (change that test on purpose if you give a non-CS subject the judge), and that prefixes map as expected.
