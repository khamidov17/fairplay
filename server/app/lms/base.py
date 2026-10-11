"""LMS provider interface + shared HTTP plumbing (httpx async, timeouts, retries).

Every provider implements the same five calls:
    whoami()                                   -> LmsIdentity   (verifies the credential)
    list_courses()                             -> [LmsCourse]   (courses the user teaches)
    list_students(course_id)                   -> [LmsStudent]
    ensure_assignment(course_id, name, points, existing_id=None) -> assignment id (str)
    post_grades(course_id, assignment_id, [(lms_user_id, score, comment)]) -> [GradeError]

Tests (and only tests) inject a fake LMS through `set_transport_factory`, which hands
each provider an httpx transport (e.g. httpx.MockTransport) instead of the network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx

log = logging.getLogger("proctora.lms")

TIMEOUT_S = 15.0
MAX_ATTEMPTS = 4                 # 1 try + 3 retries on 429 / 5xx / network errors
BACKOFF_BASE_S = 0.5             # 0.5 s, 1 s, 2 s (Retry-After honoured, capped)
MAX_RETRY_AFTER_S = 10.0
MAX_PAGES = 50                   # safety cap on pagination loops


class LmsError(Exception):
    """A failure talking to the LMS, with a message a teacher can act on."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class LmsAuthError(LmsError):
    """The LMS rejected the credential (401/403 or an invalid-token error)."""


@dataclass
class LmsIdentity:
    id: str
    name: str
    login: str = ""


@dataclass
class LmsCourse:
    id: str
    name: str
    code: str = ""

    def as_dict(self) -> dict:
        return {"lms_course_id": self.id, "name": self.name, "code": self.code}


@dataclass
class LmsStudent:
    lms_user_id: str
    name: str
    login_id: str = ""
    email: str = ""


@dataclass
class GradeError:
    lms_user_id: str
    detail: str
    sent: bool = False      # True: the score did reach the LMS, only something extra failed


@dataclass
class GradeRow:
    lms_user_id: str
    score: float
    comment: str = ""


@dataclass
class ProviderInfo:
    id: str
    name: str
    auth: str
    status: str            # "ready" (tested against a fake LMS) | "beta"
    token_help: str = ""
    base_url_hint: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "auth": self.auth, "status": self.status,
                "token_help": self.token_help, "base_url_hint": self.base_url_hint}


# ---------------------------------------------------------------- transport hook (tests)
TransportFactory = Callable[[str, str], httpx.AsyncBaseTransport | None]
_transport_factory: TransportFactory | None = None
# Tests set this to 0 so retries do not sleep.
_sleep: Callable[[float], Any] = asyncio.sleep


def set_transport_factory(factory: TransportFactory | None) -> None:
    """Route provider HTTP through factory(provider_id, base_url) -> transport (tests)."""
    global _transport_factory
    _transport_factory = factory


def set_sleep(fn: Callable[[float], Any] | None) -> None:
    global _sleep
    _sleep = fn or asyncio.sleep


# ---------------------------------------------------------------- URL checks
def normalize_base_url(raw: str, allow_private: bool = False) -> str:
    """https://school.instructure.com/ -> https://school.instructure.com (no path suffixes
    like /api/v1). Rejects non-http(s) URLs and, unless allow_private, IP literals in
    private/loopback ranges. (Hostnames resolving to private IPs are NOT checked.)"""
    url = (raw or "").strip()
    if not url:
        raise LmsError("Enter your LMS web address, for example https://school.instructure.com")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LmsError("The LMS address must start with https://")
    if parts.scheme == "http" and not allow_private:
        raise LmsError("The LMS address must use https://")
    if parts.username or parts.password:
        raise LmsError("Do not put a user name or password in the LMS address")
    host = parts.hostname
    if not allow_private:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if host == "localhost" or (ip is not None and (ip.is_private or ip.is_loopback
                                                       or ip.is_link_local or ip.is_reserved)):
            raise LmsError("That LMS address points to a private network")
    path = parts.path.rstrip("/")
    for suffix in ("/api/v1", "/webservice/rest/server.php", "/learn/api/public/v1", "/d2l/home",
                   "/login"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
    netloc = parts.netloc.lower()
    return f"{parts.scheme}://{netloc}{path}"


# ---------------------------------------------------------------- provider base
class LmsProvider:
    info: ProviderInfo

    def __init__(self, base_url: str, credential: str):
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        transport = _transport_factory(self.info.id, self.base_url) if _transport_factory else None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_S), transport=transport, follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "FairPlay-LMS-Sync/1.0"})
        self._host = urlsplit(self.base_url).netloc.lower()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # Subclasses
    def auth_headers(self) -> dict[str, str]:
        return {}

    async def whoami(self) -> LmsIdentity:
        raise NotImplementedError

    async def list_courses(self) -> list[LmsCourse]:
        raise NotImplementedError

    async def list_students(self, course_id: str) -> list[LmsStudent]:
        raise NotImplementedError

    async def ensure_assignment(self, course_id: str, name: str, points: float,
                                existing_id: str | None = None) -> str:
        raise NotImplementedError

    async def post_grades(self, course_id: str, assignment_id: str,
                          rows: list[GradeRow]) -> list[GradeError]:
        raise NotImplementedError

    # Shared HTTP
    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def same_host(self, url: str) -> bool:
        """Never follow a pagination link (and send the token) to another host."""
        return urlsplit(url).netloc.lower() == self._host

    async def request(self, method: str, path: str, *, params: Any = None, json: Any = None,
                      data: list[tuple[str, str]] | None = None, auth: bool = True) -> httpx.Response:
        """data = form fields as (name, value) pairs (repeated names allowed)."""
        url = self.url(path)
        if not self.same_host(url):
            raise LmsError("The LMS sent a link to a different website; stopped for safety")
        headers = self.auth_headers() if auth else {}
        content = None
        if data is not None:
            content = urlencode(data).encode()
            headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await self._client.request(method, url, params=params, json=json,
                                                  content=content, headers=headers)
            except httpx.TimeoutException as exc:
                last_exc = exc
                log.warning("%s %s timed out (try %d)", method, url, attempt + 1)
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("%s %s failed: %s (try %d)", method, url, exc, attempt + 1)
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = None
                    if attempt + 1 < MAX_ATTEMPTS:
                        await _sleep(self._retry_delay(resp, attempt))
                        continue
                    raise LmsError(f"The LMS is busy or failing (HTTP {resp.status_code}). "
                                   "Try again in a few minutes.", resp.status_code)
                return resp
            if attempt + 1 < MAX_ATTEMPTS:
                await _sleep(BACKOFF_BASE_S * (2 ** attempt))
        if isinstance(last_exc, httpx.TimeoutException):
            raise LmsError("The LMS did not answer within 15 seconds")
        raise LmsError("Could not reach the LMS. Check the web address"
                       f" ({type(last_exc).__name__ if last_exc else 'error'}).")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(float(ra), MAX_RETRY_AFTER_S)
            except ValueError:
                pass
        return BACKOFF_BASE_S * (2 ** attempt)

    @staticmethod
    def json_or_error(resp: httpx.Response, what: str) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise LmsError(f"The LMS sent an unexpected answer while {what} "
                           f"(HTTP {resp.status_code}, not JSON). Check the web address.")


def short(text: Any, n: int = 300) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def username_from(login_id: str, email: str, fallback: str) -> str:
    """LMS login id, else the e-mail local part, else fallback; lowercase, max 64."""
    for cand in (login_id, email.split("@", 1)[0] if email else ""):
        c = (cand or "").strip().lower()
        if "@" in c:
            c = c.split("@", 1)[0]
        c = "".join(ch for ch in c if ch.isalnum() or ch in "._-")
        if c:
            return c[:64]
    return fallback[:64]
