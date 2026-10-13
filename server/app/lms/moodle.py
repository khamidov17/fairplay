"""Moodle — Web services REST protocol with a web service token (status: beta).

All calls are POST {base}/webservice/rest/server.php with form fields
wstoken, wsfunction, moodlewsrestformat=json. Errors come back as HTTP 200 with
{"exception","errorcode","message"}.

Functions used (the site's external service must allow them):
  core_webservice_get_site_info       who am I (userid, username, fullname)
  core_enrol_get_users_courses        courses the user is enrolled in
  core_enrol_get_enrolled_users       class list (we keep users with role shortname "student")
  mod_assign_get_assignments          find the assignment by name
  mod_assign_save_grades              write grades + feedback comments

Assumptions (unverified against a live Moodle; only tested against a fake):
  * core_enrol_get_users_courses returns every course the user is enrolled in, whatever
    the role; Moodle has no web service filter for "teacher" here, so the list may include
    courses where the user is a student.
  * Moodle web services cannot create an assignment activity. The teacher must create an
    Assignment named exactly like the FairPlay exam (Maximum grade = exam points) in the
    Moodle course; we find it by name with mod_assign_get_assignments. If it is missing we
    stop with a clear message instead of guessing.
  * Grades go through mod_assign_save_grades (attemptnumber -1, addattempt 0) with the
    comment in plugindata[assignfeedbackcomments_editor]. That needs the "Feedback comments"
    feedback type enabled on the assignment; we retry without a comment if Moodle rejects it.
    core_grades_update_grades is not used: it only writes to grade items of existing
    activities and is restricted on many sites.
  * Usernames: needs moodle/user:viewalldetails for "username"; otherwise the e-mail local part.
"""
from __future__ import annotations

from typing import Any

from .base import (GradeError, GradeRow, LmsAuthError, LmsCourse, LmsError, LmsIdentity, dicts, json_as,
                   LmsProvider, LmsStudent, ProviderInfo, short)

ENDPOINT = "/webservice/rest/server.php"
AUTH_ERRORS = {"invalidtoken", "accessexception", "webservicesnotenabled", "servicenotavailable",
               "invalidlogin", "nopermissions", "usernotfullysetup"}


def _flatten(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    """Moodle's REST form encoding: arrays as name[0][field]=..."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]" if prefix else str(k), v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out.append((prefix, "1" if value else "0"))
    elif value is not None:
        out.append((prefix, str(value)))


class MoodleProvider(LmsProvider):
    info = ProviderInfo(
        id="moodle", name="Moodle", auth="token", status="beta",
        base_url_hint="https://moodle.yourschool.edu",
        token_help=("In Moodle open your profile menu → Preferences → Security keys and "
                    "copy the key for the web service your admin enabled for FairPlay. If you see "
                    "no key, ask your Moodle admin to enable web services (REST) and create one. "
                    "To send grades, first create an Assignment in Moodle with the same name as "
                    "the FairPlay exam."))

    async def call(self, function: str, **params: Any) -> Any:
        form: list[tuple[str, str]] = [("wstoken", self.credential), ("wsfunction", function),
                                       ("moodlewsrestformat", "json")]
        _flatten("", params, form)
        resp = await self.request("POST", ENDPOINT, data=form, auth=False)
        if resp.status_code == 404:
            raise LmsError("Moodle web services were not found at that address (404). "
                           "Check the Moodle web address.", 404)
        if resp.status_code in (401, 403):
            raise LmsAuthError(f"Moodle refused the request (HTTP {resp.status_code})",
                               resp.status_code)
        if resp.status_code >= 400:
            raise LmsError(f"Moodle error (HTTP {resp.status_code})", resp.status_code)
        if not resp.content.strip():  # some write functions answer with an empty body
            return None
        body = self.json_or_error(resp, f"calling {function}")
        if isinstance(body, dict) and body.get("exception"):
            code = body.get("errorcode") or ""
            msg = short(body.get("message") or code, 240)
            if code in AUTH_ERRORS:
                raise LmsAuthError(f"Moodle did not accept the token: {msg}")
            raise LmsError(f"Moodle error in {function}: {msg}")
        return body

    async def whoami(self) -> LmsIdentity:
        info = await self.call("core_webservice_get_site_info")
        if not isinstance(info, dict) or "userid" not in info:
            raise LmsError("That address does not look like Moodle web services")
        self._userid = int(info["userid"])
        return LmsIdentity(id=str(info["userid"]), name=info.get("fullname") or "",
                           login=info.get("username") or "")

    async def list_courses(self) -> list[LmsCourse]:
        userid = getattr(self, "_userid", None)
        if userid is None:
            userid = int((await self.whoami()).id)
        rows = await self.call("core_enrol_get_users_courses", userid=userid)
        return [LmsCourse(id=str(c["id"]), name=c.get("fullname") or c.get("shortname") or "",
                          code=c.get("shortname") or "")
                for c in rows or [] if isinstance(c, dict) and "id" in c]

    async def list_students(self, course_id: str) -> list[LmsStudent]:
        rows = await self.call("core_enrol_get_enrolled_users", courseid=int(course_id))
        out = []
        for u in rows or []:
            if not isinstance(u, dict) or "id" not in u:
                continue
            roles = {r.get("shortname") for r in u.get("roles") or [] if isinstance(r, dict)}
            if "student" not in roles:
                continue
            out.append(LmsStudent(lms_user_id=str(u["id"]),
                                  name=u.get("fullname") or f"Student {u['id']}",
                                  login_id=u.get("username") or "", email=u.get("email") or ""))
        return out

    async def ensure_assignment(self, course_id: str, name: str, points: float,
                                existing_id: str | None = None) -> str:
        data = await self.call("mod_assign_get_assignments", courseids=[int(course_id)])
        assignments = []
        for c in dicts((data if isinstance(data, dict) else {}).get("courses")):
            assignments.extend(dicts(c.get("assignments")))
        if existing_id and any(str(a.get("id")) == str(existing_id) for a in assignments):
            return str(existing_id)
        wanted = name.strip().lower()
        for a in assignments:
            if str(a.get("name", "")).strip().lower() == wanted:
                return str(a["id"])
        raise LmsError(f"Create an Assignment called “{name}” (maximum grade "
                       f"{points:g}) in this Moodle course, then send the grades again. "
                       "Moodle does not let other apps create assignments.")

    async def post_grades(self, course_id: str, assignment_id: str,
                          rows: list[GradeRow]) -> list[GradeError]:
        errors: list[GradeError] = []
        for r in rows:
            grade: dict[str, Any] = {"userid": int(r.lms_user_id), "grade": r.score,
                                     "attemptnumber": -1, "addattempt": 0, "workflowstate": ""}
            if r.comment:
                grade["plugindata"] = {"assignfeedbackcomments_editor":
                                       {"text": r.comment, "format": 1}}
            try:
                await self.call("mod_assign_save_grades", assignmentid=int(assignment_id),
                                applytoall=0, grades=[grade])
            except LmsAuthError:
                raise
            except LmsError as exc:
                if r.comment:  # feedback comments plugin may be off: retry score only
                    grade.pop("plugindata", None)
                    try:
                        await self.call("mod_assign_save_grades", assignmentid=int(assignment_id),
                                        applytoall=0, grades=[grade])
                        errors.append(GradeError(r.lms_user_id, "Score sent; Moodle did not take "
                                                 "the comment (turn on feedback comments)",
                                                 sent=True))
                        continue
                    except LmsError as exc2:
                        exc = exc2
                errors.append(GradeError(r.lms_user_id, exc.message))
        return errors
