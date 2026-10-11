"""Canvas LMS (Instructure) — REST API v1 with a personal access token.

Endpoints used (https://canvas.instructure.com/doc/api/):
  GET  /api/v1/users/self
  GET  /api/v1/courses?enrollment_type=teacher&per_page=100          (Link-header paging)
  GET  /api/v1/courses/:id/users?enrollment_type[]=student&include[]=email&per_page=100
  GET  /api/v1/courses/:id/assignments/:aid                           (does it still exist?)
  POST /api/v1/courses/:id/assignments                                 (create, JSON body)
  POST /api/v1/courses/:id/assignments/:aid/submissions/update_grades (form-encoded)
  GET  /api/v1/progress/:id                                            (async job status)

Verified only against the fake Canvas in tests/test_lms.py, not a live Canvas instance.
"""
from __future__ import annotations

import re

import httpx

from . import base
from .base import (GradeError, GradeRow, LmsAuthError, LmsCourse, LmsError, LmsIdentity,
                   LmsProvider, LmsStudent, ProviderInfo, short)

_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?([^";]+)"?')
GRADE_CHUNK = 50                # students per update_grades call
PROGRESS_POLLS = 10             # how long we wait for Canvas' background grading job
PROGRESS_DELAY_S = 1.0


def next_link(resp: httpx.Response) -> str | None:
    for url, rel in _LINK_RE.findall(resp.headers.get("link", "")):
        if "next" in rel.split():
            return url
    return None


class CanvasProvider(LmsProvider):
    info = ProviderInfo(
        id="canvas", name="Canvas", auth="token", status="ready",
        base_url_hint="https://yourschool.instructure.com",
        token_help=("In Canvas open Account → Settings, scroll to “Approved "
                    "integrations” and press “+ New access token”. Give it a "
                    "purpose (FairPlay) and copy the token. Your school's Canvas admin can later "
                    "replace personal tokens with a developer key or LTI 1.3."))

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}"}

    def _check(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 401:
            raise LmsAuthError("Canvas did not accept the access token (401). Make a new token "
                               "in Canvas → Account → Settings and paste it again.", 401)
        if resp.status_code == 403:
            raise LmsAuthError(f"Canvas says you are not allowed to {what} (403).", 403)
        if resp.status_code == 404:
            raise LmsError(f"Canvas could not find that while {what} (404). "
                           "Check the Canvas web address.", 404)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                errs = body.get("errors") if isinstance(body, dict) else None
                if isinstance(errs, list) and errs:
                    detail = errs[0].get("message", "") if isinstance(errs[0], dict) else str(errs[0])
                elif isinstance(errs, dict):
                    detail = "; ".join(f"{k}: {v}" for k, v in errs.items())
                elif isinstance(body, dict):
                    detail = body.get("message", "")
            except ValueError:
                pass
            raise LmsError(f"Canvas error while {what} (HTTP {resp.status_code})"
                           + (f": {short(detail, 200)}" if detail else ""), resp.status_code)

    async def _get_all(self, path: str, params: dict | list, what: str) -> list:
        out: list = []
        url: str | None = path
        first = True
        for _ in range(base.MAX_PAGES):
            if url is None:
                break
            resp = await self.request("GET", url, params=params if first else None)
            first = False
            self._check(resp, what)
            page = self.json_or_error(resp, what)
            if not isinstance(page, list):
                raise LmsError(f"Canvas sent an unexpected answer while {what}")
            out.extend(page)
            url = next_link(resp)
        return out

    async def whoami(self) -> LmsIdentity:
        resp = await self.request("GET", "/api/v1/users/self")
        self._check(resp, "checking the token")
        me = self.json_or_error(resp, "checking the token")
        if not isinstance(me, dict) or "id" not in me:
            raise LmsError("That address does not look like Canvas (no user returned)")
        return LmsIdentity(id=str(me["id"]), name=me.get("name") or me.get("short_name") or "",
                           login=me.get("login_id") or me.get("primary_email") or "")

    async def list_courses(self) -> list[LmsCourse]:
        rows = await self._get_all("/api/v1/courses",
                                   {"enrollment_type": "teacher", "per_page": 100},
                                   "listing your courses")
        out = []
        for c in rows:
            if not isinstance(c, dict) or "id" not in c or c.get("access_restricted_by_date"):
                continue
            out.append(LmsCourse(id=str(c["id"]), name=c.get("name") or f"Course {c['id']}",
                                 code=c.get("course_code") or ""))
        return out

    async def list_students(self, course_id: str) -> list[LmsStudent]:
        rows = await self._get_all(
            f"/api/v1/courses/{course_id}/users",
            [("enrollment_type[]", "student"), ("include[]", "email"), ("per_page", 100)],
            "reading the class list")
        out, seen = [], set()
        for u in rows:
            if not isinstance(u, dict) or "id" not in u or str(u["id"]) in seen:
                continue
            seen.add(str(u["id"]))
            out.append(LmsStudent(lms_user_id=str(u["id"]),
                                  name=u.get("name") or u.get("sortable_name") or f"Student {u['id']}",
                                  login_id=u.get("login_id") or "",
                                  email=u.get("email") or ""))
        return out

    async def ensure_assignment(self, course_id: str, name: str, points: float,
                                existing_id: str | None = None) -> str:
        if existing_id:
            resp = await self.request("GET", f"/api/v1/courses/{course_id}/assignments/{existing_id}")
            if resp.status_code == 200:
                return existing_id
            if resp.status_code != 404:
                self._check(resp, "checking the assignment")
            # deleted in Canvas -> make a new one
        body = {"assignment": {
            "name": name[:255], "points_possible": points, "grading_type": "points",
            "submission_types": ["none"], "published": True,
            "description": "Exam held in FairPlay. Scores are sent from FairPlay."}}
        resp = await self.request("POST", f"/api/v1/courses/{course_id}/assignments", json=body)
        self._check(resp, "creating the assignment")
        a = self.json_or_error(resp, "creating the assignment")
        if not isinstance(a, dict) or "id" not in a:
            raise LmsError("Canvas did not return the new assignment")
        return str(a["id"])

    async def post_grades(self, course_id: str, assignment_id: str,
                          rows: list[GradeRow]) -> list[GradeError]:
        errors: list[GradeError] = []
        for i in range(0, len(rows), GRADE_CHUNK):
            chunk = rows[i:i + GRADE_CHUNK]
            form: list[tuple[str, str]] = []
            for r in chunk:
                form.append((f"grade_data[{r.lms_user_id}][posted_grade]", _fmt(r.score)))
                if r.comment:
                    form.append((f"grade_data[{r.lms_user_id}][text_comment]", r.comment))
            try:
                resp = await self.request(
                    "POST",
                    f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/update_grades",
                    data=form)
                self._check(resp, "sending grades")
                progress = self.json_or_error(resp, "sending grades")
                failed = await self._wait_progress(progress)
            except LmsAuthError:
                raise
            except LmsError as exc:
                failed = exc.message
            if failed:
                errors.extend(GradeError(r.lms_user_id, failed) for r in chunk)
        return errors

    async def _wait_progress(self, progress: object) -> str | None:
        """update_grades runs as a background job; returns an error text if it failed.
        If it is still running after ~10 s we report success-pending (no error)."""
        if not isinstance(progress, dict):
            return None
        for _ in range(PROGRESS_POLLS):
            state = progress.get("workflow_state")
            if state == "completed":
                return None
            if state == "failed":
                return short(progress.get("message") or "Canvas could not save the grades")
            pid = progress.get("id")
            url = progress.get("url") or (f"/api/v1/progress/{pid}" if pid is not None else None)
            if not url or not self.same_host(self.url(url)):
                return None
            await base._sleep(PROGRESS_DELAY_S)
            resp = await self.request("GET", url)
            if resp.status_code != 200:
                return None
            progress = self.json_or_error(resp, "checking the grade upload")
            if not isinstance(progress, dict):
                return None
        return None


def _fmt(score: float) -> str:
    return str(int(score)) if float(score).is_integer() else f"{score:.4f}".rstrip("0").rstrip(".")
