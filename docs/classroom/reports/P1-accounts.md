# P1 report: accounts, courses/classes, API, settings (branch `p1-accounts`)

Scope: CA-1 to CA-11, SEB-2, and the "course teacher" breadth risk (REVIEW.md section 5).
Every finding has at least one regression test. Each new test was run against a copy of the
unfixed code (the same tree with the source files reset to the base commit): all of them failed
there (33 failures and 2 errors, plus 13 more failures in `test_deploy_security` once its helper
module was present). All of them pass on this branch.

Test totals and lint are at the end.

## Findings

### CA-1 (Critical): CSV import overwrote any account's e-mail, name and active flag. Fixed.
- `judge/utils/import_export.py`: new `import_may_update_existing_user()`. `_upsert_user` only
  changes name, e-mail or `is_active` of an existing account when `update_existing` is set **and**
  the account is a plain member: not staff, not superuser, not a course admin, course instructor
  or class admin, and already a member of the target class (or course, if no class is given).
  With no course or class there is no update at all. Every other existing account is only linked.
- `judge/views/organization.py`: both class roster paths (`apply_class_member_csv`, used by
  create/edit class, and `ClassImportMembers`) now pass `update_existing=False`. That is the
  safest option: they only create new users and link existing ones. The course-level
  `OrganizationImportUsers` keeps its "Update existing users" checkbox, guarded as above.
- Not changed: the operator CLI `manage.py import_users_csv` (shell access only).
- Tests: `judge.utils.tests.test_import_export.UserImportExistingAccountGuardTest` (5 tests);
  `judge.views.tests.test_organization_courses.P1AccountsCourseSecurityTest.test_ca1_*` (3 tests,
  including a superuser e-mail rewrite through `class_import_members`). I updated the existing
  `test_existing_user_skipped_unless_update_requested`: the user is now a course member, because
  an update without a course is no longer allowed.

### CA-2 (High): any site teacher could delete any class. Fixed.
- `DeleteClass`: removed the site-wide `has_perm('judge.delete_class')` branch. The allowed
  roles are now course admin, class admin, `edit_all_organization` and superuser.
- Tests: `test_ca2_site_teacher_cannot_delete_other_courses_class`,
  `test_ca2_class_admin_can_still_delete_own_class`.

### CA-3 (High): "Add from library" linked any contest. Fixed.
- `judge/forms.py` `ClassAssignmentsForm`:
  - It now takes `user`.
  - The queryset is the contests the user can edit (`edit_all_contest`, or listed as author or
    curator), plus contests already linked to this class. The second part means saving the form
    does not drop links someone else made.
  - `clean_contests()` rejects any newly added contest that fails `is_editable_by` (the
    server-side check).
- `ClassLinkAssignmentsView` passes `request.user`.
- Tests: `test_ca3_cannot_link_contest_the_user_cannot_edit`,
  `test_ca3_can_link_own_contest_and_keep_existing_links`,
  `test_ca3_form_queryset_only_offers_editable_contests`.

### CA-4 (Medium): select2 user search leaked names and NetIDs. Fixed.
`judge/views/select2.py` now has one helper, `scoped_user_queryset`, used by `/judge-select2/profile/`
and `/widgets/select2/user_search`. The ticket and contest pickers use the same login,
minimum-length and name rules.
- **Login:** anonymous callers get an empty result.
- **Minimum length:** a term shorter than 2 characters (`USER_SEARCH_MIN_TERM_LENGTH`) returns
  nothing, so an empty term no longer lists everyone.
- **Who is searchable:** inactive users are excluded. `is_unlisted` users are excluded for
  everyone except superusers and the caller themself.
- **Scope:**
  - "Site-wide" callers search everybody listed: superuser, `is_staff`, `edit_all_organization`,
    `add_organization`, or the Instructors group.
  - Everyone else only finds people who share a course or class with them, as member or admin.
- **Real names:** only site-wide callers and course or class admins see real names and can search
  by name. Students match by username only, and their labels show only the username, so a name
  can't be looked up to find a NetID.
- **Product decision:** before, teacher form pickers (`profile_select2`) deliberately returned
  unlisted users. They no longer do (superusers still see them). The old test was rewritten to
  `test_profile_select2_unlisted_users_only_for_superusers`.
- Tests: `judge.views.tests.test_select2.UserSearchSelect2ScopeTest` (4 tests). The existing
  `UserSelect2NameSearchTest` now logs in as a teacher.

### CA-5 (Medium): impersonation left no record. Fixed.
- `dmoj/settings.py`: `IMPERSONATE['DISABLE_LOGGING'] = False`. django-impersonate then writes an
  `impersonate.ImpersonationLog` row (impersonator, target, start, end) on every start and stop.
  The row is read-only in the Django admin.
- Not done (out of scope / product decision): blocking impersonation during a live participation.
- Tests: `judge.views.tests.test_impersonate.ImpersonatePermissionTestCase.test_impersonation_start_and_stop_are_logged`,
  `judge.utils.tests.test_deploy_security.InsecureDefaultsTest.test_impersonation_logging_enabled`.

### CA-6 (Medium): the API ignored scoreboard visibility and exposed rosters. Fixed.
- `judge/views/api/api_v2.py`: new `scoreboard_visible_contests(user, now)`. Ended contests are
  visible except those set to `SCOREBOARD_HIDDEN`. Editors with `edit_own_contest`,
  `view_contest_scoreboard` users and `see_private_contest`/`edit_all_contest` see everything, as
  before. Both `/api/v2/participations` and the `contests` history of `/api/v2/user/<u>` use it.
- `/api/v2/users?organization=` returns 403 unless the caller manages every course requested
  (course admin, `edit_all_organization` or superuser).
- `/api/v2/user/<u>` `organizations`: full list for the user themself and site editors. Other
  callers only see the courses they administer.
- Decision: a student's own hidden-scoreboard score is not in their API history either. This is
  the same rule as for everyone else.
- Tests: `judge.views.tests.test_api_v2_visibility` (6 tests).

### CA-7 (High/Med): NetID SSO. Fixed.
`judge/views/user.py`:
- **TLS check:** the token check uses `requests.get(..., params={'token': token}, verify=NETID_SSO_VERIFY,
  timeout=NETID_SSO_TIMEOUT)`. `NETID_SSO_VERIFY` defaults to True and can be a CA bundle path.
  A False value is coerced to True.
- **Binding:** new `netid_may_bind_existing_user()`. An SSO login only lands in an existing
  account that:
  - has no usable password (it was created by SSO or by roster import), or
  - is active and has an e-mail equal to the SSO `eppn`. A local registration stays inactive until
    its activation mail is clicked, so an active account's e-mail is verified.

  In every other case the login is refused with a message.
- **Disabled accounts:** they can no longer log in through SSO. `login()` never checked
  `is_active`.

`judge/views/register.py` and `dmoj/settings.py`:
- **Reserved usernames:** new setting `SSO_RESERVED_USERNAME_REGEX`, default
  `^[A-Za-z]{2,8}[0-9]{1,6}$` (NetID-shaped). Local registration refuses matching usernames. Set it
  to `''` to turn this off. **Product decision:** the pattern is a guess at the NetID format. Check
  it against real NetIDs.
- **Turning registration off:** the existing `REGISTRATION_OPEN` setting already does this
  (django-registration). It is now documented, and a test confirms it closes `/accounts/register/`.
  It stays True by default.
- Tests: `judge.views.tests.test_netid_sso` (9 tests: TLS and param encoding, first login, the
  squatted local account refused, imported account bound, verified e-mail bound, disabled account
  refused, reserved username, ordinary username, registration switched off).

### CA-8 (Medium): a class instructor's reject/reopen removed the user from the whole course. Fixed.
- `InstructorJoinRequestsView.post` `revoke_membership`: the user is removed from the course (and
  all its classes) only when the reviewer is a course admin of that course or a site-wide reviewer.
  A class instructor's reject/reopen only removes membership and admin rights for `request_class`.
- Tests: `test_ca8_class_instructor_reject_only_removes_own_class`,
  `test_ca8_class_instructor_reopen_keeps_coteacher_admin_elsewhere`. The existing course-admin
  tests (full removal) still pass.

### CA-9 (Medium): DEBUG on by default, which switched off the default-key check. Fixed.
- `dmoj/settings.py`: `DEBUG = False` by default.
- The refusal of the default `SECRET_KEY` (and the default `EVENT_DAEMON_SUBMISSION_KEY` when
  `EVENT_DAEMON_USE`) now runs **regardless of DEBUG**. Only the test runner
  (`manage.py test`) and an explicit `CLASSROOM_ALLOW_INSECURE_DEV=1` are exempt. The rule lives in
  the new `dmoj/insecure_defaults.py` so it can be unit-tested.
- Local dev keeps working: `local_settings.example.py` already generates a per-checkout key and
  turns on DEBUG through `CLASSROOM_DEBUG` (default 1). No change was needed there.
- `.ci.settings.py` now sets a random throwaway `SECRET_KEY`, because CI runs `compilejsi18n` and
  `check_migration_graph` outside the test runner.
- Tests: `judge.utils.tests.test_deploy_security.InsecureDefaultsTest`. Besides the unit tests of
  the rule, one test imports `dmoj.settings` in a child process with a `local_settings` that sets
  only `DEBUG = True` and expects `ImproperlyConfigured`.

### CA-10 (Medium): CI exposed tokens to branch pipelines and used unpinned images. Fixed in the file. Needs GitLab settings too.
`.gitlab-ci.yml`:
- **Pinned images:**
  - `python:3.11.9-bookworm` and `python:3.12.4-bookworm`
  - `node:20.15.1-bookworm`
  - `mariadb:11.4.2`
  - `gcr.io/kaniko-project/executor:v1.23.2-debug`
  - `pragent/pr-agent:0.29`

  **Uncertain:** these tags came from memory. Docker Hub can't be reached from this sandbox, so I
  could not check them. Check each tag exists (`pr-agent:0.29` especially), then replace it with
  its `@sha256` digest.
- **Protected refs only:** every job that receives credentials or uses the `dku-deploy` runner
  (build-web, build-wsevent, deploy:dev, backup:prod, deploy:prod, pr_agent) only runs when
  `$CI_COMMIT_REF_PROTECTED == "true"`. As a result, pr_agent no longer runs for MRs from
  unprotected feature branches.
- **Header comments:** they now say the PAT and the LLM key must be **protected** variables (and
  should be a project access token), and that the `dku-deploy` runner must be set to "protected
  branches only". A `rules:` clause alone can't stop a branch that edits the CI file. Those GitLab
  settings are outside the repo, so the OJ team has to change them.
- Not changed: `.github/workflows/*` (actions pinned to major tags; no PAT).
- Tests: `judge.utils.tests.test_deploy_security.GitLabCITest` (images pinned, secret jobs gated on
  protected refs). P2 may delete this CI file. Delete the test with it.

### CA-11 (Low): anyone could apply as class instructor, and approval granted `is_staff`. Fixed.
- `InstructorVerificationSubmitView`: class-instructor applications now require
  `profile_is_site_teacher` on the server.
- `apply_instructor_verification_action`: refuses to approve a class-instructor request from a
  non-teacher. Such requests stay pending; this covers legacy pending requests.
- `grant_class_instructor_role`: no longer sets `is_staff` (and returns `granted_staff=False`).
  Class rights come from `Class.admins`.
- This changes how site-teacher join requests that pick a class are approved too
  (`apply_approved_join_request`).
- **Left unchanged:** site-wide teacher approval (superuser-only, explicit) still grants `is_staff`.
  Problem creation (`problem_create_v2`, `question_types/code.py`) checks `is_staff` together with
  `add_problem`.
- Tests: `test_ca11_student_cannot_apply_as_class_instructor`,
  `test_ca11_class_instructor_approval_does_not_grant_staff`,
  `test_ca11_approval_refused_for_non_teacher_applicant`.

### SEB-2 (Low-Med): spectators could live-join and skip SEB. Fixed.
- `Contest.is_live_joinable_by`: spectators are treated like editors and testers (no live join).
- `Contest.is_spectatable_by`: explicit spectators may always spectate, as editors and testers
  can.
- `judge/views/contests.py`: the two list-view `editor_or_tester` annotations include spectators,
  so list pages agree with the model.
- Tests: `judge.models.tests.test_contest.P1ContestAccessTestCase.test_spectator_cannot_live_join`
  and `test_spectator_cannot_live_join_through_view` (POST `/contest/<key>/join` creates no LIVE
  participation).

### Course-teacher breadth (REVIEW.md section 5, minimum). Fixed.
- `Contest.access_check`: the course-teacher shortcut (`is_viewable_by_course_teacher`) now only
  applies to **published** (`is_visible=True`) org-private assignments. An unpublished one is open
  only to:
  - its editors, testers and spectators (unchanged, earlier in the check),
  - `see_private_contest` and `edit_all_contest` holders,
  - course admins of a linked course (`organizations`, or the courses of its linked classes), via
    the new `is_viewable_by_linked_course_admin`.
- `Contest.get_visible_contests`: changed the same way. The teaching-org clauses require
  `is_visible=True`, and course admins of class-linked courses are added.
- Not changed: course teachers still skip assignment rules (`user_bypasses_assignment_rules`) and
  the sandbox-output hiding for published assignments. Class admins can still add anyone as a class
  admin. Those are design decisions for later.
- Tests: `test_unpublished_assignment_hidden_from_course_teachers` (a joined site teacher and a
  class instructor) and `test_unpublished_assignment_visible_to_editors_testers_and_course_admin`.
- I updated two existing tests. `judge.models.tests.test_exam_contest.CourseTeacherClassScopedContestAccessTest`
  (`test_course_instructor_sees_class_scoped_contest` and
  `test_class_instructor_sees_sibling_class_contest`) used an unpublished fixture contest and
  asserted the old broad access. They now assert that access is denied while the contest is hidden
  and granted once it is published.

## Touched outside my area
- `judge/models/contest.py` (`access_check`, `is_live_joinable_by`, `is_spectatable_by`,
  `get_visible_contests`, new `is_viewable_by_linked_course_admin`) and `judge/views/contests.py`
  (the two `editor_or_tester` annotations). These were needed for SEB-2 and the course-teacher
  item. Each change is small. Merge them carefully with the authoring and grading branches.

## Results
- Full suite (`bash classroom/scripts/dev-test.sh judge`, serial, locmem): **1687 tests, OK,
  1 expected failure** (the known `test_nan_score_does_not_crash`). The baseline was 1637; this
  branch adds 50 tests.
- `flake8` is clean on every changed or new file.
