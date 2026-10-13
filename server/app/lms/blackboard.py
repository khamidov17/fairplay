"""Blackboard Learn — public REST API (status: beta, NOT tested against a real Learn server).

Credential: an OAuth 2.0 *user* access token (Bearer) for a registered REST application.
Blackboard has no "personal access token": an admin registers the FairPlay application key
in Learn (System Admin -> REST API Integrations) and a token is obtained with the three-legged
OAuth flow. FairPlay does not run that flow yet; for now the token must be pasted.

Endpoints used (developer.anthology.com, Learn REST):
  GET   /learn/api/public/v1/users/me
  GET   /learn/api/public/v1/users/{userId}/courses?expand=course     (paging.nextPage)
  GET   /learn/api/public/v1/courses/{courseId}/users?expand=user     (paging.nextPage)
  GET   /learn/api/public/v2/courses/{courseId}/gradebook/columns/{columnId}
  POST  /learn/api/public/v2/courses/{courseId}/gradebook/columns
  PATCH /learn/api/public/v2/courses/{courseId}/gradebook/columns/{columnId}/users/{userId}

Assumptions: role ids "Instructor"/"TeachingAssistant" mean teaching and "Student" means a
student; course ids are the primary ids ("_123_1"); the grade PATCH body {score, feedback}
is accepted for a manual column. Field names follow the public docs; untested.
"""
from __future__ import annotations

import httpx

from . import base
from .base import (GradeError, GradeRow, LmsAuthError, LmsCourse, LmsError, LmsIdentity, dicts, json_as,
                   LmsProvider, LmsStudent, ProviderInfo, short)

API = "/learn/api/public"
TEACHING_ROLES = {"instructor", "teachingassistant", "coursebuilder"}


def _obj(v):
    """Audit L-16: a nested JSON object, or {} when the LMS sent something else."""
    return v if isinstance(v, dict) else {}


class BlackboardProvider(LmsProvider):
    info = ProviderInfo(
        id="blackboard", name="Blackboard Learn", auth="oauth_token", status="beta",
        base_url_hint="https://learn.yourschool.edu",
        token_help=("Blackboard has no personal tokens. Your Blackboard admin registers FairPlay "
                    "under System Admin → REST API Integrations and gives you an access "
                    "token for your account. Beta: not yet tested with a real Blackboard."))

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}

    def _check(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 401:
            raise LmsAuthError("Blackboard did not accept the access token (401)", 401)
        if resp.status_code == 403:
            raise LmsAuthError(f"Blackboard says you are not allowed to {what} (403)", 403)
        if resp.status_code >= 400:
            msg = ""
            try:
                msg = resp.json().get("message", "")
            except (ValueError, AttributeError):
                pass
            raise LmsError(f"Blackboard error while {what} (HTTP {resp.status_code})"
                           + (f": {short(msg, 200)}" if msg else ""), resp.status_code)

    async def _paged(self, path: str, params: dict, what: str) -> list:
        out: list = []
        url: str | None = path
        first = True
        for _ in range(base.MAX_PAGES):
            if not url:
                break
            resp = await self.request("GET", url, params=params if first else None)
            first = False
            self._check(resp, what)
            body = json_as(resp, what, dict)
            out.extend(dicts(body.get("results")))
            paging = body.get("paging")
            url = paging.get("nextPage") if isinstance(paging, dict) else None
        return out

    async def whoami(self) -> LmsIdentity:
        resp = await self.request("GET", f"{API}/v1/users/me")
        self._check(resp, "checking the token")
        me = self.json_or_error(resp, "checking the token")
        if not isinstance(me, dict) or "id" not in me:
            raise LmsError("That address does not look like Blackboard Learn")
        self._uid = me["id"]
        n = _obj(me.get("name"))
        return LmsIdentity(id=str(me["id"]),
                           name=" ".join(x for x in (n.get("given"), n.get("family")) if x),
                           login=me.get("userName") or "")

    async def list_courses(self) -> list[LmsCourse]:
        uid = getattr(self, "_uid", None) or (await self.whoami()).id
        rows = await self._paged(f"{API}/v1/users/{uid}/courses", {"expand": "course"},
                                 "listing your courses")
        out = []
        for m in rows:
            if str(m.get("courseRoleId", "")).lower() not in TEACHING_ROLES:
                continue
            c = _obj(m.get("course"))
            cid = c.get("id") or m.get("courseId")
            if cid:
                out.append(LmsCourse(id=str(cid), name=c.get("name") or str(cid),
                                     code=c.get("courseId") or ""))
        return out

    async def list_students(self, course_id: str) -> list[LmsStudent]:
        rows = await self._paged(f"{API}/v1/courses/{course_id}/users", {"expand": "user"},
                                 "reading the class list")
        out = []
        for m in rows:
            if str(m.get("courseRoleId", "")).lower() != "student":
                continue
            u = _obj(m.get("user"))
            n = _obj(u.get("name"))
            name = " ".join(x for x in (n.get("given"), n.get("family")) if x)
            out.append(LmsStudent(lms_user_id=str(m.get("userId") or u.get("id")),
                                  name=name or u.get("userName") or "Student",
                                  login_id=u.get("userName") or "",
                                  email=(u.get("contact") or {}).get("email") or ""))
        return out

    async def ensure_assignment(self, course_id: str, name: str, points: float,
                                existing_id: str | None = None) -> str:
        cols = f"{API}/v2/courses/{course_id}/gradebook/columns"
        if existing_id:
            resp = await self.request("GET", f"{cols}/{existing_id}")
            if resp.status_code == 200:
                return existing_id
            if resp.status_code != 404:
                self._check(resp, "checking the grade column")
        resp = await self.request("POST", cols, json={
            "name": name[:333], "description": "Exam held in FairPlay",
            "score": {"possible": points}, "availability": {"available": "Yes"},
            "grading": {"type": "Manual"}})
        self._check(resp, "creating the grade column")
        col = self.json_or_error(resp, "creating the grade column")
        if not isinstance(col, dict) or "id" not in col:
            raise LmsError("Blackboard did not return the new grade column")
        return str(col["id"])

    async def post_grades(self, course_id: str, assignment_id: str,
                          rows: list[GradeRow]) -> list[GradeError]:
        errors = []
        for r in rows:
            body: dict = {"score": r.score}
            if r.comment:
                body["feedback"] = r.comment
            try:
                resp = await self.request(
                    "PATCH",
                    f"{API}/v2/courses/{course_id}/gradebook/columns/{assignment_id}/users/{r.lms_user_id}",
                    json=body)
                self._check(resp, "sending a grade")
            except LmsAuthError:
                raise
            except LmsError as exc:
                errors.append(GradeError(r.lms_user_id, exc.message))
        return errors
