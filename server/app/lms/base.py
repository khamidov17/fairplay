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
import socket
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpcore
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
PRIVATE_DETAIL = "That LMS address points to a private network"


def blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Anything that is not a public internet address (audit L-01)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global)


def literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host string means *as an IP*, including the old forms browsers and
    libc still accept: 127.1, 2130706433, 0x7f000001, 0177.0.0.1 (audit L-01)."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


async def resolve_public(host: str, port: int) -> str:
    """Resolve once, refuse if ANY address is private, return the address to connect to.
    Connecting to exactly this address (not resolving again) stops DNS rebinding."""
    ip = literal_ip(host)
    if ip is not None:
        if blocked_ip(ip):
            raise LmsError(PRIVATE_DETAIL)
        return str(ip)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise LmsError("The LMS address could not be found. Check the spelling.") from None
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise LmsError("The LMS address could not be found. Check the spelling.")
    for a in addrs:
        if blocked_ip(ipaddress.ip_address(a.split("%", 1)[0])):
            raise LmsError(PRIVATE_DETAIL)
    return addrs[0]


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects only to checked public addresses. TLS still
    uses the LMS hostname (SNI + certificate check): httpcore takes it from the URL."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ip = await resolve_public(host, port)
        return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise LmsError(PRIVATE_DETAIL)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_only_transport() -> httpx.AsyncHTTPTransport:
    t = httpx.AsyncHTTPTransport()
    pool = getattr(t, "_pool", None)
    if not isinstance(pool, httpcore.AsyncConnectionPool):   # httpx internals changed
        raise RuntimeError("httpx version not supported by the LMS address guard")
    t._pool = httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(),
                                           network_backend=_PublicOnlyBackend())
    return t


def normalize_base_url(raw: str, allow_private: bool = False) -> str:
    """https://school.instructure.com/ -> https://school.instructure.com (no path suffixes
    like /api/v1). Rejects non-http(s) URLs and, unless allow_private, private addresses in
    any IP notation. Hostnames are checked again at connect time (public_only_transport)."""
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
        ip = literal_ip(host)
        if host == "localhost" or host.endswith(".localhost") or (ip is not None and blocked_ip(ip)):
            raise LmsError(PRIVATE_DETAIL)
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

    def __init__(self, base_url: str, credential: str, allow_private: bool = False):
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        transport = _transport_factory(self.info.id, self.base_url) if _transport_factory else None
        if transport is None and not allow_private:
            transport = public_only_transport()     # audit L-01: no private addresses, ever
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


def json_as(resp: httpx.Response, what: str, kind: type) -> Any:
    """Audit L-16: JSON of the expected shape (dict / list) or a clear LmsError, never a
    500 from .get() on a list."""
    value = LmsProvider.json_or_error(resp, what)
    if not isinstance(value, kind):
        raise LmsError(f"The LMS sent an unexpected answer while {what} "
                       f"(HTTP {resp.status_code}). Check the web address.")
    return value


def dicts(items: Any) -> list[dict]:
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


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
