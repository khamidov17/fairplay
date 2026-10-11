"""D2L Brightspace — Valence REST API (status: beta, NOT tested against a real Brightspace).

Credential: an OAuth 2.0 access token (Bearer) issued for a Brightspace OAuth app that an
admin registered (scopes roughly: core:*:*, enrollment:orgunit:read, grades:gradeobjects:write,
grades:gradevalues:write). FairPlay does not run the OAuth flow yet; the token is pasted.

Endpoints used (docs.valence.desire2learn.com), API versions below are assumptions:
  GET  /d2l/api/lp/{LP}/users/whoami
  GET  /d2l/api/lp/{LP}/enrollments/myenrollments/?orgUnitTypeId=3   (PagingInfo.Bookmark)
  GET  /d2l/api/le/{LE}/{orgUnitId}/classlist/
  GET  /d2l/api/le/{LE}/{orgUnitId}/grades/{gradeObjectId}
  POST /d2l/api/le/{LE}/{orgUnitId}/grades/
  PUT  /d2l/api/le/{LE}/{orgUnitId}/grades/{gradeObjectId}/values/{userId}

Assumptions: orgUnitTypeId 3 = "Course Offering" (default in most tenants); class list
entries whose ClasslistRoleDisplayName mentions "student" or "learner" are students (role
names are configurable per school); myenrollments does not tell us whether the user teaches
the course, so every course offering is listed.
"""
from __future__ import annotations

import httpx

from . import base
from .base import (GradeError, GradeRow, LmsAuthError, LmsCourse, LmsError, LmsIdentity,
                   LmsProvider, LmsStudent, ProviderInfo, short)

LP = "1.43"
LE = "1.74"
COURSE_OFFERING = 3


class BrightspaceProvider(LmsProvider):
    info = ProviderInfo(
        id="brightspace", name="D2L Brightspace", auth="oauth_token", status="beta",
        base_url_hint="https://yourschool.brightspace.com",
        token_help=("Brightspace uses OAuth 2.0. Your Brightspace admin registers FairPlay under "
                    "Admin Tools → Manage Extensibility → OAuth 2.0 and gives you an "
                    "access token. Beta: not yet tested with a real Brightspace."))

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}

    def _check(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 401:
            raise LmsAuthError("Brightspace did not accept the access token (401)", 401)
        if resp.status_code == 403:
            raise LmsAuthError(f"Brightspace says you are not allowed to {what} (403)", 403)
        if resp.status_code >= 400:
            raise LmsError(f"Brightspace error while {what} (HTTP {resp.status_code})"
                           + (f": {short(resp.text, 200)}" if resp.text else ""), resp.status_code)

    async def whoami(self) -> LmsIdentity:
        resp = await self.request("GET", f"/d2l/api/lp/{LP}/users/whoami")
        self._check(resp, "checking the token")
        me = self.json_or_error(resp, "checking the token")
        if not isinstance(me, dict) or "Identifier" not in me:
            raise LmsError("That address does not look like Brightspace")
        return LmsIdentity(id=str(me["Identifier"]),
                           name=f"{me.get('FirstName', '')} {me.get('LastName', '')}".strip(),
                           login=me.get("UniqueName") or "")

    async def list_courses(self) -> list[LmsCourse]:
        out: list[LmsCourse] = []
        bookmark = None
        for _ in range(base.MAX_PAGES):
            params = {"orgUnitTypeId": COURSE_OFFERING}
            if bookmark:
                params["bookmark"] = bookmark
            resp = await self.request("GET", f"/d2l/api/lp/{LP}/enrollments/myenrollments/",
                                      params=params)
            self._check(resp, "listing your courses")
            body = self.json_or_error(resp, "listing your courses")
            for item in body.get("Items") or []:
                ou = item.get("OrgUnit") or {}
                if "Id" in ou:
                    out.append(LmsCourse(id=str(ou["Id"]), name=ou.get("Name") or "",
                                         code=ou.get("Code") or ""))
            paging = body.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                break
            bookmark = paging.get("Bookmark")
        return out

    async def list_students(self, course_id: str) -> list[LmsStudent]:
        resp = await self.request("GET", f"/d2l/api/le/{LE}/{course_id}/classlist/")
        self._check(resp, "reading the class list")
        rows = self.json_or_error(resp, "reading the class list")
        out = []
        for u in rows or []:
            role = str(u.get("ClasslistRoleDisplayName") or "").lower()
            if "student" not in role and "learner" not in role:
                continue
            out.append(LmsStudent(lms_user_id=str(u.get("Identifier")),
                                  name=u.get("DisplayName") or "Student",
                                  login_id=u.get("Username") or u.get("OrgDefinedId") or "",
                                  email=u.get("Email") or ""))
        return out

    async def ensure_assignment(self, course_id: str, name: str, points: float,
                                existing_id: str | None = None) -> str:
        grades = f"/d2l/api/le/{LE}/{course_id}/grades/"
        if existing_id:
            resp = await self.request("GET", f"{grades}{existing_id}")
            if resp.status_code == 200:
                return existing_id
            if resp.status_code != 404:
                self._check(resp, "checking the grade item")
        resp = await self.request("POST", grades, json={
            "MaxPoints": points, "CanExceedMaxPoints": False, "IsBonus": False,
            "ExcludeFromFinalGradeCalculation": False, "GradeSchemeId": None,
            "Name": name[:128], "ShortName": "", "GradeType": "Numeric", "CategoryId": 0,
            "Description": {"Content": "Exam held in FairPlay", "Type": "Text"},
            "AssociatedTool": None, "IsHidden": False})
        self._check(resp, "creating the grade item")
        obj = self.json_or_error(resp, "creating the grade item")
        if not isinstance(obj, dict) or "Id" not in obj:
            raise LmsError("Brightspace did not return the new grade item")
        return str(obj["Id"])

    async def post_grades(self, course_id: str, assignment_id: str,
                          rows: list[GradeRow]) -> list[GradeError]:
        errors = []
        for r in rows:
            body = {"GradeObjectType": 1, "PointsNumerator": r.score,
                    "Comments": {"Content": r.comment or "", "Type": "Text"},
                    "PrivateComments": {"Content": "", "Type": "Text"}}
            try:
                resp = await self.request(
                    "PUT", f"/d2l/api/le/{LE}/{course_id}/grades/{assignment_id}/values/{r.lms_user_id}",
                    json=body)
                self._check(resp, "sending a grade")
            except LmsAuthError:
                raise
            except LmsError as exc:
                errors.append(GradeError(r.lms_user_id, exc.message))
        return errors
