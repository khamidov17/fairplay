# DKU OJ review: courses/classes, users, requests, notifications, API, settings and deployment

Reviewer area: `judge/views/organization.py`, `judge/utils/organization_membership.py`, `templates/organization/*`, `judge/models/profile.py` (DKU changes), `judge/models/notification.py`, `judge/utils/admin_notify.py`, `judge/views/user.py` / `register.py` (DKU changes), `judge/impersonation.py` + `IMPERSONATE`, `judge/social_auth.py`, `judge/middleware.py`, `judge/views/api/api_v2.py`, `judge/views/select2.py`, `judge/utils/assignment_{draft,harvest,schedule}.py`, `judge/views/assignment_draft.py`, `dmoj/settings.py`, `dmoj/urls.py`, `Dockerfile`, `docker-compose.yml`, `docker/*`, `.gitlab-ci.yml`, `.ci.settings.py`, `websocket/*`.

All of this comes from reading the code. Nothing was run. Line numbers refer to `/home/claude/dkuoj`.

---

## Findings (most severe first)

### CA-1: A class instructor's CSV import can rewrite the e-mail address of any account (superusers included), which is enough to take that account over
- **Severity:** Critical. **Confidence:** Confirmed by reading. Whether a given admin account falls also depends on whether it uses a local password and has 2FA.
- **Where:** `judge/views/organization.py:2156-2180` (`ClassImportMembers`), `:2018-2029` (`apply_class_member_csv`, called from `CreateClass`/`EditClass`), `:2441-2484` (`OrganizationImportUsers`). The import code is in `judge/utils/import_export.py:509-536` (`_upsert_user`).
- **What is wrong:** The class import paths always call `process_user_csv(..., update_existing=True, activate=True)`. `_upsert_user` finds the user by `username`. It does not check whether that user is a member of the class or course, or is staff or a superuser. It then overwrites `email`, `first_name` and `last_name`, and sets `is_active=True`:
  ```python
  user = User.objects.get(username=username)
  if update_existing:
      ...
      if email and user.email != email:
          user.email = email
      if activate and not user.is_active:
          user.is_active = True
  ```
  The gate is `org.admins ∋ me or class.admins ∋ me or edit_all_organization`. Every class administrator passes it. That includes approved class instructors, and any site teacher who created a class in a course they joined (`profile_can_create_class`, line 189).
- **Scenario:** Teacher T instructs one class. T uploads `username,first_name,last_name,email` with a row `admin,x,y,t@evil.com`. The superuser `admin` now has T's e-mail address. T then uses `/password/reset/`, the stock Django `PasswordResetForm`, which sends the link to every active user with that e-mail and a usable password. Superusers cannot use NetID (`user.py` blocks them), so they have local passwords. T resets the password and logs in as the superuser. TOTP stops this only where the admin has enabled it. The same upload also re-activates banned or deactivated accounts and renames any user, which allows identity spoofing in rosters. It also adds the victim to T's class.
- **Fix:** For existing users, only link them to the class. Never change email, name or `is_active` from a class or course import. At the very least, refuse to touch staff/superusers and users who are not already in this course.

### CA-2: Any site teacher can delete any class in any course
- **Severity:** High. **Confidence:** Confirmed by reading.
- **Where:** `judge/views/organization.py:2183-2196`. The permission comes from `judge/models/notification.py:30`.
  ```python
  if not (org.admins.filter(...).exists() or class_obj.admins.filter(...).exists() or
          request.user.has_perm('judge.delete_class') or
          user_can_edit_all_organizations(request.user)):
  ```
  `delete_class` is one of the `INSTRUCTOR_PERMISSION_CODENAMES` that `ensure_instructors_group()` gives the whole Instructors group. So the `has_perm` branch is a site-wide bypass. The URL slug is never compared, because only `cpk` is used.
- **Scenario:** Teacher T in course A POSTs `/organization/<any>-<any>/class/<cpk>/-x/delete` with a class id from course B. The class, its roster, its `ClassMaterial` rows and the OSS files (removed by the `pre_delete` receiver) are destroyed. The class's contest links (`Contest.classes`) are dropped too. As a side effect, a contest that was scoped only to that class becomes "org-private with no classes". `Contest.get_visible_contests` and `access_check` then open it to every member of the course (`contest.py:603-621`).
- **Fix:** Drop the `has_perm('judge.delete_class')` branch, or require the perm and class/course admin together.

### CA-3: "Add from library" lets a class instructor attach any contest on the site, including another course's exam, to their own class
- **Severity:** High. **Confidence:** Confirmed by reading.
- **Where:** `judge/forms.py:596` (`ClassAssignmentsForm`: `queryset = Contest.objects.order_by('-start_time')`) and `judge/views/organization.py:2400-2408` (`contest.classes.add(self.object)`). The select2 widget filters only what the dropdown shows. The POSTed ids are validated against every contest on the site.
- **What it causes:**
  1. Instructor of class A, course C1, POSTs `contests=<id of exam X in course C2>`. Now `X.classes` contains A, and `linked_organization_ids()` contains C1.
  2. `Contest.is_viewable_by_course_teacher` (`contest.py:726`) then returns True for the instructor, and for every admin and instructor of C1. `access_check` returns at line 586, before the `is_visible` check. So they can open X even while it is hidden or has not started.
  3. Students of class A pass `in_class` (`contest.py:604`) and can join X when it is not `is_private`.
  4. If X was course-wide (`classes` empty), it now has a class list. Members of C2 who are not in A lose access, because "if classes are specified, check class membership". This is a denial of service on another course's exam.
- **Fix:** Limit the queryset to contests the user can edit (`is_editable_by`), or to contests already linked to this class's course.

### CA-4: Anyone, even without logging in, can list every user's real name next to their username (select2 user search)
- **Severity:** High (student PII). **Confidence:** Confirmed by reading.
- **Where:** `judge/views/select2.py:52-57` (`UserSelect2View`, `/judge-select2/profile/`) and `:169-202` (`UserSearchSelect2View`, `/widgets/select2/user_search`). Neither view requires login (`dmoj/urls.py:428, 498`).
- **What changed vs upstream:** DKU made `profile_user_search_q` match `first_name`/`last_name` and made the label `"First Last (username)"`. An empty `term` gives `Q()`, which matches every profile, 20 per page, with `?page=N`. `UserSelect2View` also skips the `is_unlisted` filter and returns inactive users. `UserSearchSelect2View` also returns a gravatar URL, which is an md5 of the e-mail address.
- **Scenario:** `curl '/judge-select2/profile/?term=&page=1..N'` dumps the full name and NetID of every student and teacher. Searching `?term=Zhang` works as a name-to-NetID lookup.
- **Fix:** Require login. Return real names only to teachers or staff. Require a minimum term length. Apply `is_unlisted` and `is_active` filters.

### CA-5: Impersonation leaves no record in the deployed setup
- **Severity:** High (exam integrity / accountability). **Confidence:** Confirmed by reading.
- **Where:** `dmoj/settings.py:348-352` sets `'DISABLE_LOGGING': True`, so django-impersonate writes no `ImpersonationLog`. The only other trace is `judge/middleware.py:72-80`, which calls `uwsgi.set_logvar(...)` and does that only if `uwsgi` imports. The Docker image runs gunicorn (`docker/entrypoint.sh`), so `uwsgi is None`.
- **Scenario:** A course admin who holds `judge.impersonate_org` impersonates student S during an assignment and submits answers, or deletes S's drafts. The submissions are attributed to S. No table or log records who really acted. This combines with CA-1: CSV import adds any existing non-staff user to the admin's course or class, so `impersonate_org` / `impersonate_class` reaches any non-staff account on the site, not just the admin's real students.
- **Fix:** Set `DISABLE_LOGGING: False`, or write your own audit row in the start/stop signals. Block impersonating a user who has a live participation. Restrict the scope to members who joined through approval, not through CSV import.

### CA-6: The API lets students read other students' assignment scores, including where the scoreboard is hidden, and lets anyone list a course's roster
- **Severity:** High in DKU's use, where contests are graded assignments. **Confidence:** Confirmed by reading. The code is upstream, but DKU's course model turns it into a grade leak.
- **Where:** `judge/views/api/api_v2.py:337-395` (`/api/v2/participations`), `:516-568` (`/api/v2/user/<u>`), `:487-513` (`/api/v2/users?organization=<id>`).
  - `APIContestParticipationList` filters contests only by `end_time < now` (or editor). It ignores `scoreboard_visibility` (`SCOREBOARD_HIDDEN` / `AFTER_PARTICIPATION`), and it never calls `can_see_full_scoreboard`.
  - `APIUserDetail` returns `contests: [{key, score, cumulative_time, ...}]` for every ended contest the *viewer* can see, plus `organizations` (the list of course ids).
  - `APIUserList` is anonymous and filters by `organization`.
- **Scenario:** A classmate calls `GET /api/v2/participations?contest=<assignment_key>` after the deadline and gets every student's score, even though the instructor set the scoreboard to hidden. An anonymous caller uses `GET /api/v2/users?organization=12` to get course 12's roster (usernames, which are NetIDs).
- **Fix:** In the participations and user-detail endpoints, filter contests by `can_see_full_scoreboard(user)`, or by the viewer's own participation. Make `/api/v2/users` and `/user/<u>` require login and hide `organizations` from non-staff.

### CA-7: NetID SSO verifies the login token over HTTPS with certificate checks off, and treats the IdP `uid` as the local username
- **Severity:** Medium. **Confidence:** Confirmed by reading. The impact depends on network position and on whether `REGISTRATION_OPEN` is left on, which needs a runtime check.
- **Where:** `judge/views/user.py:159-249` (`CustomNetidLoginView`).
  ```python
  response = requests.get(f'{auth_host}/verify_token.php?token={token}', verify=False)
  ...
  user = User.objects.get(username=uid)   # or create it
  login(request, user, backend='django.contrib.auth.backends.ModelBackend')
  ```
  - `verify=False` means anyone on the path between the OJ and the auth host can answer `{"success":true,"user_info":{"uid":"<any teacher>"...}}` and log in as any non-superuser, staff and instructors included. The token is also inserted into the query string without being URL-encoded, which allows parameter injection.
  - The match is by `username` alone. `REGISTRATION_OPEN = True` is the default (`settings.py:642`). If it is still on, someone can register a local account named after a student's NetID before that student first uses SSO. The student's SSO login then lands in an account whose password the squatter knows.
  - The login is a GET that carries an attacker-chosen `token`, so login CSRF is possible: the victim gets logged into the attacker's account.
- **Fix:** Use `verify=True` (or the DKU CA bundle) and `params={'token': token}`. Link SSO identities through a dedicated field (or `social_django`), not username equality. Disable local registration, or reserve NetID-shaped usernames.

### CA-8: A class instructor's "reject" or "reopen" removes the user from the whole course and every class in it
- **Severity:** Medium. **Confidence:** Confirmed by reading.
- **Where:** `judge/views/organization.py:1539-1548` (`revoke_membership`) and `:1587-1608`. Also `remove_profile_from_organization` at `:94-103`.
- **What is wrong:** A class admin can act on any request whose `request_class` is their class (`_reviewable_queryset`). Requests made from the course page (`RequestJoinOrganization`) carry `grants_organization=True` plus a class. Rejecting an already-approved one of these calls `remove_profile_from_organization(user, org)`. That removes the user from the course, from `org.instructors`, from **every** class in the course, and from `admins` of every class in the course.
- **Scenario:** Instructor of class X in course C rejects a student's old approved request. The student loses membership in classes Y and Z, which belong to other instructors, and loses access to their assignments. If the target was a co-teacher who joined through class X, they also lose their admin role on unrelated classes.
- **Fix:** When the reviewer is not a course admin, revoke only the class membership or admin role for `request_class`.

### CA-9: `DEBUG = True` is the shipped default, it turns off the secret-key safety check, and it is running in production
- **Severity:** Medium. **Confidence:** Confirmed by reading. Whether the dev keys are still in use needs a runtime check.
- **Where:** `dmoj/settings.py:30-34, 593-595, 750-761`, and `dmoj/urls.py:546-548`.
  - The `if not DEBUG:` block is the only thing that refuses the public `SECRET_KEY` and `EVENT_DAEMON_SUBMISSION_KEY` values. The live site runs with DEBUG=True, so the check never runs. Nothing in the repo or deploy files (`docker-compose.yml`, `entrypoint.sh`, `.gitlab-ci.yml`) sets DEBUG. It all depends on the untracked `/srv/dkuoj/local_settings.py`.
  - Default `SECRET_KEY`: anyone can forge sessions (`cached_db` sessions are server-side, but signed cookies, password-reset tokens, `Submission.get_id_secret` and API token HMACs all derive from it) and CSRF tokens.
  - Default `EVENT_DAEMON_SUBMISSION_KEY`: anyone can work out the websocket channel name for any submission and watch its live verdict.
  - DEBUG=True on its own: anyone gets Django's technical 500 page (traceback, local variables, SQL, partly-cleansed settings) and the technical 404 page (the full URL map), because the custom `handler404/500` are ignored. It also enables the `/media/` fallback (`urls.py:547`), which serves all of `MEDIA_ROOT` unauthenticated. That includes martor uploads, DOCX figures, and legacy `.seb` files that migration `0196` deliberately left in `MEDIA_ROOT/seb_configs/`.
- **Fix:** Default `DEBUG` to False and `ALLOWED_HOSTS` from env. Run the secret checks no matter what DEBUG is (or under `not DEBUG or not ALLOWED_HOSTS-local`). Set DEBUG=False in prod and rotate both keys if they were ever the defaults.

### CA-10: The CI exposes a GitLab personal access token and an LLM key to every branch pipeline, and runs them inside an unpinned image
- **Severity:** Medium. **Confidence:** Likely. Variable protection and runner settings need a check in GitLab.
- **Where:** `.gitlab-ci.yml:262-293` (`pr_agent`).
  - The comment says `GITLAB_PERSONAL_ACCESS_TOKEN` and `LLM_API_KEY` are "masked (NOT protected)" so that unprotected feature-branch pipelines can use them. Any developer who can push a branch can add a job that sends `$GITLAB_PERSONAL_ACCESS_TOKEN` somewhere. Masking hides the value in logs only, not from code that runs in the job.
  - `pragent/pr-agent:latest` is unpinned and receives that PAT.
  - Related: the `dku-deploy` shell runner holds the SSH key to prod (`deploy:prod`). If that runner is not restricted to protected branches, a branch pipeline that adds `tags: [dku-deploy]` gets a shell on the dev host and `ssh prod`.
- **Fix:** Use a project access token with the narrowest scope (api on this project only). Pin the pr-agent image by digest. Mark the deploy runner "protected" and protect `develop` and `main`.

### CA-11 (Low): Any logged-in user, students included, can submit a class-instructor application, and approving one silently grants `is_staff`
- **Where:** `judge/views/organization.py:2528-2612`. The only "must be a site teacher" check is in the UI (`can_apply_class_instructor`, line 1840). `grant_class_instructor_role` (`:434-453`) sets `user.is_staff = True` for every approved instructor. The same happens when a class instructor approves a join request from a site teacher who picked a class (`apply_approved_join_request`, `:477-479`). The inbox shows that as an ordinary "class" join.
- **Fix:** Check `profile_is_site_teacher` on the server. Show "grants instructor rights" in the inbox row. Do not set `is_staff` unless the admin site is really needed.

### Checked and found OK in this area (so others don't re-check)
- **CSRF:** the only `csrf_exempt` is upstream `mailgun.py`. Every DKU state change in this area is POST. `APIMiddleware` sets `csrf_processing_done` only for valid Bearer tokens (upstream).
- **Open redirects:** `safe_redirect_url` (`url_has_allowed_host_and_scheme`) guards `next`/`Referer` in join and verification flows. The NetID redirect goes to a fixed host.
- **Class content:** `ClassHome` and `ClassMaterialDownloadView` are gated by `profile_can_view_class_content` (class members, managers, course teachers). Downloads are forced to `application/octet-stream` as attachments. Material delete and folder lookups are scoped to `class_obj`. Being pending gives the page shell only, no content.
- **Join requests:** the course-join form limits `class_` to that course's active classes. Class join requires prior course membership. Class `access_code` is stored but no site view accepts it (there is no code-based join). Open courses (`is_open`) can be joined without approval, which is by design. Org-private contests with no classes are visible to every course member.
- **Instructor verification:** site-wide teacher applications are reviewable only by superusers (`user_can_review_site_teacher_verifications`). Class-instructor applications are reviewable by that course's admins only (`instructor_verifications_for_reviewer` filters `organization_id__in=admin_org_ids`). The fallback when a course has no admins is notifying superusers.
- **Impersonation scoping:** `CUSTOM_ALLOW`/`CUSTOM_USER_QUERYSET` are read correctly from the `IMPERSONATE` dict. Staff, superusers, course admins and class admins are excluded as targets. The perms are not in the Instructors group, so they are admin-granted.
- **API v2 submission endpoints:** they mask verdicts through `results_visible_to` (DKU addition). Submission detail requires login plus `can_see_detail`.
- **Assignment drafts:** writes are bound to `request.profile.current_contest`. They are refused for spectators, bypass users and ended windows, and the problem must be in the contest. Harvest uses a compare-and-set claim and respects `max_submissions` and disqualification. `ASSIGNMENT_HARVEST_ENABLED = False` by default. Draft URLs are inside the contest, so the SEB middleware applies to them.
- **Docker:** `local_settings.py` is excluded from the image (`.dockerignore`). Web, wsevent and the bridge are bound to 127.0.0.1 or docker0 only, and the event "post" port is not published. Containers run as root (`Dockerfile` has no `USER`). That is hardening, not a bug.

### Design risks worth knowing (by intent, but they matter for exam integrity)
- **What a "course teacher" can see.** `Organization.profile_teaches_course` counts course admins, `Organization.instructors` (auto-filled with *any* site teacher who joins the course, `maybe_add_course_instructor`), and admins of *any* class in the course. `Contest.access_check` (`contest.py:586`) lets all of them into every org-private contest of the course *before* the `is_visible` check. So a site teacher who joins an open course, or who creates one class in it, can read unpublished exams of other instructors and all class rosters and materials. `user_bypasses_assignment_rules` also treats them as staff.
- **Class admins can add anyone as a class admin or member through `ClassSiteForm`** (`admins`, `members`). No verification is involved. A student added as a class "admin" gets the course-teacher powers above.
- **`OrganizationUsers`** (`/organization/<id>-<slug>/users`, `organization.py:1046`) is upstream. It is anonymous and lists course members. DKU uses organizations as courses, so this is a public enrollment list.

---

## How it works (for integrators)

**Models** (`judge/models/profile.py`, `judge/models/notification.py`):
- `Organization` = **course**. Fields: `admins` (course admins), `instructors` (DKU), `members` (via `Profile.organizations`), `is_open`, `class_required`, `slots`, `instructor_names` (display only).
- `Class` = **section/session** under one course. Fields: `admins` (class instructors), `members` (`Profile.classes`, related_query_name `class`), DKU meta (`academic_year`, `term_session`, `credits`, `location`, `start_date`/`end_date`, `instructor_names`, `ta_names`), `access_code` (unused on site).
- `ClassMaterial`: a file or folder tree per class, stored in `submission_storage` (S3/Garage + Box mirror) under key `class_materials/YYYY/MM/<class_id>/<uuid>.<ext>`.
- `OrganizationRequest`: a join request. `request_class` is optional. `grants_organization` (DKU) distinguishes course+class requests from class-only ones. States are `P/A/R`.
- `InstructorVerificationRequest`: `kind = teacher` (site-wide, which means the `Instructors` group, reviewed by superusers) or `class_instructor` (reviewed by course admins). The `granted_*` flags record what an approval changed so that revoking it can undo exactly that.
- `AdminNotification`: inbox rows, created by `judge/utils/admin_notify.py`.
- **Assignments are `Contest`s.** The relevant fields are `Contest.organizations`, `Contest.classes` (M2M), `is_organization_private` and `is_private`. Visibility is computed in `Contest.get_visible_contests` / `access_check` (`judge/models/contest.py:555, 811`).

**Roles:**
- **Site teacher:** member of the `Instructors` group. Its perms are in `INSTRUCTOR_PERMISSION_CODENAMES` (add/change org, add/change/delete class, add_contest, create_private_contest, contest_seb_requirement, ...).
- **Course admin:** `org.admins`.
- **Class instructor:** `class.admins`.
- **Course teacher (read-only across the course):** `profile_teaches_course`.
- **Student:** `class.members` (and `org.members`).
- Permission helpers live at the top of `judge/views/organization.py` (`profile_manages_class`, `profile_can_view_class_content`, ...).

**Flows:**
- Course join: open course → `JoinOrganization`. Closed course → `RequestJoinOrganization`, then approval in `InstructorJoinRequestsView` (`/organizations/requests/`), then `apply_approved_join_request`.
- Class join: `RequestJoinClass`. Requires course membership first.
- Bulk enrol: CSV through `process_user_csv` (see CA-1). New users are created as NetID users with an unusable password.
- Login: Duke NetID through `/login_netid_redirect/` → external host → `/login_netid/?token=` (`CustomNetidLoginView`), or local password. Superusers must use local login.
- Middleware order matters for the exam lock. `APIMiddleware` (Bearer token) runs before CSRF and auth. `ContestMiddleware` (DKU SEB gate: `request_matches_seb`, exempt paths in `_seb_block_exempt`) runs after impersonation.

**Notes for a FairPlay launch token and exam lock:**
- The natural hook is `ContestMiddleware.__call__` (`judge/middleware.py:139-173`). Add a `contest.fairplay_required` branch next to `contest.seb_required`, with the same `_seb_block_exempt` whitelist plus a FairPlay launch route. Copy the existing SEB launch-token pattern (`seb_launch_token` / `seb_launch_config`, `DMOJ_SEB_LAUNCH_TOKEN_EXPIRY_MINUTES`).
- Keep the launch-token route's prefix exempt in the same way. The comment in `_seb_launch_prefix` explains why a root-ish prefix would disable the lock entirely.
- Bypass rules: `contest.user_bypasses_seb(user)` and `user_bypasses_assignment_rules`. Note how broad "course teacher" is (see Design risks). Anyone counted as a course teacher bypasses the lock.
- API Bearer tokens are full-account credentials (they set `2fa_passed` and skip CSRF on *every* non-admin URL), not API-only ones. A student holding one could drive the site from outside the locked app unless the FairPlay check covers token-authenticated requests too.
- `SECRET_KEY` signs everything. If FairPlay tokens are HMAC'd with it, CA-9 (the default key while DEBUG=True) must be fixed first.

---

## Reading courses, rosters, assignments and scores programmatically today

**Authentication:** a per-user API token from `POST /api/token/generate/`, sent as `Authorization: Bearer <48-char token>`. The request then runs with that user's full permissions. Admin URLs are blocked. Without the header, requests are anonymous.

### Existing `/api/v2/` endpoints
Paginated responses use `DMOJ_API_PAGE_SIZE = 1000`.

| Endpoint | Auth | Fields | Visibility / leak notes |
|---|---|---|---|
| `GET contests` (`?key=&tag=&organization=&is_rated=`) | none | key, name, start/end, time_limit, is_rated, rate_all, tags | `get_visible_contests(user)`. Anonymous callers see public contests only. No class filter. |
| `GET contest/<key>` | none | + rating params, scoreboard_visibility, is_organization_private, organizations (ids), is_private, format, problems (when in contest / ended / editor), rankings (when `can_see_full_scoreboard`), each with user, score, per-problem breakdown, ratings | `is_accessible_by`. Rankings are correctly gated. |
| `GET participations` (`?contest=&user=&is_disqualified=&virtual_participation_number=`) | none | user, contest, start/end, score, cumtime, tiebreaker, dq, virtual | **CA-6:** any ended visible contest, scoreboard visibility ignored. |
| `GET problems` / `problem/<code>` | none | code, name, types, group, points, partial, org-private flag, is_public. The detail adds authors, limits and languages. | `get_visible_problems` / `is_accessible_by`. |
| `GET users` (`?id=&username=&organization=`) | none | id, username, points, pp, problem_count, rank, rating | **CA-6:** course roster through `organization`. No real names. |
| `GET user/<username>` | none | above + solved public problems, organizations (ids), contest history (key, score, rating) | **CA-6** |
| `GET submissions` (`?user=&problem=&contest=&id=&language=&result=`) | none | id, problem, user, date, language, time/memory/points/result (masked when hidden), contest {key, points, virtual, time since start} | Visible problems only. IDE submissions excluded. Verdicts masked through `results_visible_to`. |
| `GET submission/<id>` | login | + status, case_points/total, per-case results | `can_see_detail` |
| `GET organizations` (`?id=&is_open=`) | none | id, slug, short_name, is_open, member_count | All courses. |
| `GET languages`, `GET judges` | none | language and judge metadata | — |

### What cannot be read today
- **Classes:** there is no endpoint for classes, class rosters (`Class.members`/`admins`), class-to-assignment links (`Contest.classes`), materials, join requests or instructor roles. These are only available as HTML (`/organization/<id>-<slug>/class/<cpk>-<cslug>` tabs) or in the Django admin.
- **Grades:** there is no endpoint for per-student grades or a manual/AI-graded final score. `participations.score` is the contest-format score. DKU's manual-grade and AI-grade results live in the grade views (`judge/views/grade.py`, another reviewer's area).

### Endpoints to add for FairPlay or an LMS
Each should be token-authenticated and gated by `profile_manages_class` / `profile_can_view_class_content`, not anonymous:
1. `GET /api/v2/courses?mine=1`: the courses the caller administers or teaches (`Organization` filtered by `admins|instructors|class__admins`).
2. `GET /api/v2/course/<id>/classes`: id, name, offering_label, start/end, admins (username + name), member_count.
3. `GET /api/v2/class/<id>/roster`: members with username, first/last name, email; managers only.
4. `GET /api/v2/class/<id>/assignments`: `Contest.objects.filter(classes=cls)` with key, name, start/end, time_limit, format, `seb_required` and a future `fairplay_required`, results_finalized.
5. `GET /api/v2/contest/<key>/grades`: per participation, the final score after manual/AI grading, gated by `contest.is_editable_by(user)`.
6. FairPlay itself: `POST /api/v2/contest/<key>/fairplay-token`, mirroring `SEBLaunchToken`, plus a verify endpoint that FairPlay calls. Read the SEB launch-token code first.

Before any of this is exposed, fix CA-6 and change Bearer tokens to read-only, scoped tokens. Today a token grants everything the account can do.
