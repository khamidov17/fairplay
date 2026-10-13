# P2: Subjects (feature-pack registry)

Branch `p2-subjects`. DKU OJ now knows which subject each course belongs to and shows only the tools that subject uses. CS courses keep everything they have today. A new subject is one `register_subject(...)` call in one file.

## What changed

**Registry**, all in `classroom/judge/subjects/`:

- `base.py`: capabilities (`Cap`), `FeaturePack`, `Subject`, `DashboardSection`, `MenuItem`, the registration functions, prefix inference and the field validator. It does not import any models, so `models`, migrations and forms can import it.
- `definitions.py`: the seven packs (`core`, `code`, `math`, `lab`, `writing`, `language`, `chem`) and the twelve subjects (`cs`, `math`, `phys`, `chem`, `bio`, `envsci`, `econ`, `socsci`, `humanities`, `lang`, `general`, `other`), with the DKU course prefixes I could verify. This is the only file you edit to add a subject.
- `access.py`: who sees what. `subject_for`, `features_for`, `has_feature`, `resolve_courses` (accepts an Organization, Class, Contest, Problem or a list), `question_type_refusal` / `ensure_question_type_allowed`, `filter_question_types`, `sections_for`, `menu_items_for`, `teaching_courses`, `nav_item_visible`.
- `jinja.py`: the Jinja globals `has_feature`, `course_subject`, `subject_sections`, `subject_menu_items` and `nav_item_visible`. They are registered from `judge/jinja2/__init__.py`. A view can set `subject_course` in its context to give `has_feature()` a default course.

**Model.** `Organization.subject` is a `CharField(max_length=32, default='cs', db_index=True)` with a registry validator. It has no `choices` on purpose, so adding a subject needs no migration. Migration `0222_organization_subject` sets existing rows to `cs`.

**Forms and admin:**
- Course create and edit (`OrganizationSiteForm`): a Subject dropdown filled from the registry.
  - On create, the default is "Detect from course code". The server infers the subject from the course-code prefix, and a hint ("Detected: Chemistry") updates as the teacher types.
  - On edit, a post without a subject keeps the current one.
- The inline course creation on the problem page also infers the subject.
- Admin: a Subject field, a list column and a list filter.

**Hidden for courses without the code pack, and refused on the server:**

- **Problem create v2:**
  - The Programming type is not offered in a non-code course context. The course context is `?course=<id>`, which also preselects the course, or the problem's courses when editing.
  - Without a course context, the page offers the union of the teacher's courses' features. The page knows each course's capabilities, so it strikes through types the selected courses do not allow and blocks the save with a message.
  - Server: saving a programming problem attached to a non-code course returns 403. Without a course, the user's own features decide. An existing problem keeps its type when its course's subject changes later; only newly added courses are checked.
- **Excel import:** creating a code problem attached to a non-code course fails with a 400 and is rolled back.
- **Problem list:** the "Programming" format filter and the add menu follow the user's features. The workspace format filter does too.
- **IDE:** `/ide/` returns 404 and IDE runs return 403 for users whose courses all lack the code pack.
- **Navigation bar:** entries for `/ide`, `/status`, `/runtimes`, `/stats/language` and `/users` (rankings) are hidden when the user lacks the matching capability.
- **Assignments:** the Rankings tab is hidden for students of non-code assignments (teachers keep it). The MOSS tab is hidden and the MOSS page returns "No such contest" (404). In the assignment editor, "Hide IDE / Run Test output" and "Run pretests only" are hidden unless they are already on.

**Dashboards:**
- **Course page:** shows a subject badge, plus the sections of the course's packs: core "Questions and assignments" (teachers), code "Programming" (IDE and tools) and chem "Chemistry".
- **Home page:** has a "Your courses" panel for teachers. Each course card shows its subject and its packs' menu items (new question / question bank with `?course=`, new assignment, IDE). Below the cards, each pack's teacher-home section appears once.
- **Adding a section:** one template under `templates/subjects/sections/` plus one `DashboardSection(...)` line.

**Docs:**
- `docs/classroom/SUBJECTS.md`: the DKU research, with sources.
- `docs/classroom/ADDING_A_SUBJECT.md`: the worked example. Chemistry is that example, and it really is added, at the bottom of `definitions.py`.

## Tests run

- New tests: `judge/subjects/tests.py` (23: registry and access) and `judge/views/tests/test_subjects.py` (35: views, server-side refusal, mixed teacher, forms, admin, import, IDE, nav, contest tabs, MOSS, editor). Command: `bash classroom/scripts/dev-test.sh judge.subjects judge.views.tests.test_subjects`. Result: **58 tests, OK**.
- Full suite: `bash classroom/scripts/dev-test.sh judge --parallel 2`. Result: **1884 tests, OK** (1826 before plus the 58 new). No existing test changed.
- `flake8 --extend-exclude node_modules`: clean.
- `manage.py check`: no issues.
- `check_migration_graph judge`: single leaf `0222_organization_subject`.
- `makemigrations --check`: no changes.
- Smoke render on the dev database (`classroom_subjects`, migrated by `dev-server.sh`), done through a Django test client in `manage.py shell`, not in a browser: create v2 (with and without `?course=`), the home page, CS and Bio course pages, the new-course form and the new-assignment page all return 200. The inline scripts of each page pass `node --check`.

**Not done:** I did not click through the pages in a real browser, so the client-side strike-through of type pills and the "Detected:" hint are checked only for syntax and for the data the page receives.

## Decisions

1. **CS is `code` + `math` plus core, not literally every pack.** This gives every capability that exists today. Giving CS the placeholder packs would also put, for example, the Chemistry section on CS course pages. When `math` gets real features, CS gets them too.
2. **Users with no course get CS features.** This covers anonymous visitors and new accounts, so DKU OJ's public pages (public CS problems, IDE, rankings) are unchanged until a non-CS course exists.
3. **"Staff see everything" means superusers and `judge.edit_all_organization`, not `is_staff`.** `grant_site_teacher_role` gives every approved teacher `is_staff`, so with `is_staff` a biology teacher would see the IDE.
4. **The programming-in-a-non-code-course refusal applies to site editors too.** It protects the course. To allow it, change the course subject.
5. **An unrecognised course prefix becomes `cs`**, which matches today. The course form says so. An unknown key stored in the database falls back to `other` (core only).
6. **Rankings, judge status and language statistics are only hidden, not refused.** These are read-only pages. The rankings tab stays for teachers, who use the scoreboard.
7. **Only verified DKU prefixes are mapped.** Unverified ones are listed in `SUBJECTS.md` for the team to confirm.
8. **The course context for the problem editor is `?course=<id>`.** Links from the course page and the teacher home pass it. Without it the editor uses the union of the teacher's courses, and the page checks each selected course before saving.

## Open questions for the DKU OJ team

- Should STATS (or MATH) courses get the `code` pack for R and Python homework? Today they do not. It is one line in `definitions.py`, or a teacher can set the course to Computer science.
- Please confirm the unverified prefixes in `SUBJECTS.md` (POLSCI, SOCIOL, ANTHRO, PUBPOL, GLHLTH, NEUROSCI, MATSCI, ARTS, MEDIA, HIST, LIT, PHIL, RELIG, WOC). Also, where should INTGSCI, CAPSTONE and DKU 101 go?
- Should rankings and the scoreboard be refused on the server for non-code assignments, or only hidden from students as now?
- Should a subject be per class (section) rather than per course? The design canvas mentions per-teacher and per-class exceptions ("Just for this teacher"). That would be a second layer on top of `features_for`; it is not built.
- Should the public pages for anonymous visitors (`/users`, `/ide` navigation) stay CS by default, or follow a site setting?
- Problem bank sharing: a CS problem that is already attached to a course whose subject changed is kept and stays editable. Should such problems be flagged in the admin?

## Left undone

- The assignment editor's problem picker ("From library") still lists programming problems for non-code assignments. Adding one is not refused on the server. The create path is guarded; the attach path is not.
- The placeholder packs (`math`, `lab`, `writing`, `language`, `chem`) add no question types yet. That is by design for this phase.
- The teacher home shows at most 12 courses, sorted by name.
