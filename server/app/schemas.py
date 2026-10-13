from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QID_RE = re.compile(r"^q_[A-Za-z0-9_-]{1,40}$")
# slice 3, tightened by the 2026-10-12 audit (#10): an allowed-website entry is an EXACT
# host ("canvas.dku.edu"), "*.host" for the host and all its subdomains, optionally
# followed by a path prefix ("docs.google.com/forms/d/e/1FAIpQL/").
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
MAX_ALLOWED_DOMAINS = 30
MAX_EXTERNAL_URL = 2000
MAX_FINISH_CONTAINS = 200


def normalize_domain(v: str) -> str:
    """Normalize/validate one allowed-website entry ([*.]host[/path]); raises ValueError.
    The host is lowercased, the path keeps its case (form ids are case-sensitive)."""
    raw = v.strip()
    s = raw
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        s = s.split("://", 1)[1]
    s = s.split("?", 1)[0].split("#", 1)[0]
    host, slash, path = s.partition("/")
    wildcard = host.startswith("*.")
    if wildcard:
        host = host[2:]
    host = host.lower().rstrip(".")
    if ":" in host:
        host = host.split(":", 1)[0]
    if not host:
        raise ValueError("allowed website must not be empty")
    if len(host) > 253 or not DOMAIN_RE.match(host) or ("." not in host and host != "localhost"):
        raise ValueError(f"'{raw}' is not a website name like canvas.dku.edu")
    path = ("/" + path) if slash else ""
    if path == "/":
        path = ""
    if path and (len(path) > 500 or not PATH_RE.match(path)):
        raise ValueError(f"'{raw}' has an unusable path")
    return ("*." if wildcard else "") + host + path


def external_host(url: str | None) -> str | None:
    """Lowercase hostname of an http(s) URL, or None."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return host.lower()


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


MAX_ANSWER_CHARS = 50_000


def _whole(x: float) -> int | float:
    return int(x) if float(x).is_integer() else x


class RubricItemIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str = Field(min_length=1, max_length=500)
    points: float = Field(ge=0, le=1000)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("rubric label must not be empty")
        return v

    @field_validator("points")
    @classmethod
    def _points(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("rubric points must be finite")
        return v


# ---------------------------------------------------------------- slice 6: images + topics
MAX_QUESTION_IMAGES = 6
MAX_TOPICS = 5
MAX_TOPIC_CHARS = 60
UPLOAD_URL_RE = re.compile(r"^/api/uploads/[A-Za-z0-9_-]{16,64}$")
TOPIC_REQUIRED_DETAIL = "Each question needs at least one topic"


class ImageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = Field(min_length=1, max_length=2000)
    alt: str = Field(default="", max_length=300)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if UPLOAD_URL_RE.match(v):
            return v
        parts = urlsplit(v) if v.lower().startswith("https://") else None
        if parts is None or not parts.hostname or any(c.isspace() for c in v):
            raise ValueError("image url must be an uploaded image (/api/uploads/...) or an https:// address")
        return v

    @field_validator("alt")
    @classmethod
    def _alt(cls, v: str) -> str:
        return v.strip()


def normalize_topics(v: list | None) -> list[str]:
    """Trim, lower-case, drop blanks, case-insensitive dedupe; 1-60 chars each, max 5."""
    out: list[str] = []
    for t in v or []:
        if not isinstance(t, str):
            raise ValueError("topics must be text")
        t = " ".join(t.split()).lower()
        if not t:
            continue
        if len(t) > MAX_TOPIC_CHARS:
            raise ValueError(f"a topic can have at most {MAX_TOPIC_CHARS} characters")
        if t not in out:
            out.append(t)
    if len(out) > MAX_TOPICS:
        raise ValueError(f"a question can have at most {MAX_TOPICS} topics")
    return out


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    # slice 6: pictures shown above the prompt, and topic tags (required on POST/PUT exam,
    # checked in ExamIn so stored exams from before slice 6 stay valid)
    images: list[ImageIn] = Field(default_factory=list, max_length=MAX_QUESTION_IMAGES)
    topics: list[str] = Field(default_factory=list, max_length=50)
    type: Literal["mcq", "numeric", "short", "essay"]
    prompt: str = Field(min_length=1, max_length=10_000)
    points: int = Field(default=1, ge=1, le=1000)
    options: list[str] | None = None
    correct_index: int | None = None
    answer_number: float | None = None
    tolerance: float | None = None
    accepted_answers: list[str] | None = None
    rubric: list[RubricItemIn] | None = Field(default=None, max_length=50)
    min_words: int | None = Field(default=None, ge=1, le=100_000)
    max_words: int | None = Field(default=None, ge=1, le=100_000)
    # slice 6 (Scan & Grade): lines of answer space on the printed paper (null = default)
    answer_lines: int | None = Field(default=None, ge=1, le=40)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must not be empty")
        return v

    @field_validator("topics")
    @classmethod
    def _topics(cls, v: list[str]) -> list[str]:
        return normalize_topics(v)

    @model_validator(mode="after")
    def _by_type(self) -> "QuestionIn":
        if self.type == "essay":
            rubric = self.rubric or []
            if rubric and abs(sum(r.points for r in rubric) - self.points) > 1e-9:
                raise ValueError("rubric points must sum to the question points")
            if (self.min_words is not None and self.max_words is not None
                    and self.min_words > self.max_words):
                raise ValueError("min_words must be <= max_words")
            self.rubric = rubric
            self.options, self.correct_index, self.accepted_answers = [], None, []
            self.answer_number = self.tolerance = None
            return self
        # rubric / word limits only apply to essays
        self.rubric, self.min_words, self.max_words = [], None, None
        if self.type == "mcq":
            opts = self.options or []
            if not 2 <= len(opts) <= 8:
                raise ValueError("mcq needs 2-8 options")
            if any(not isinstance(o, str) or not o.strip() for o in opts):
                raise ValueError("mcq options must be non-empty strings")
            if self.correct_index is None or not 0 <= self.correct_index < len(opts):
                raise ValueError("mcq correct_index must point at an option")
            self.answer_number = self.tolerance = None
            self.accepted_answers = []
        elif self.type == "numeric":
            if self.answer_number is None or not math.isfinite(self.answer_number):
                raise ValueError("numeric question needs answer_number")
            tol = 0.0 if self.tolerance is None else self.tolerance
            if not math.isfinite(tol) or tol < 0:
                raise ValueError("tolerance must be >= 0")
            self.tolerance = tol
            self.options, self.correct_index, self.accepted_answers = [], None, []
        else:  # short
            acc = [a for a in (self.accepted_answers or []) if isinstance(a, str) and a.strip()]
            if not acc:
                raise ValueError("short question needs at least one accepted answer")
            self.accepted_answers = acc
            self.options, self.correct_index = [], None
            self.answer_number = self.tolerance = None
        return self

    def to_dict(self) -> dict:
        d = self.model_dump(exclude={"id", "rubric"})
        d["rubric"] = [{"label": r.label, "points": _whole(r.points)} for r in self.rubric or []]
        return d


class ExamIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    duration_min: int = Field(ge=1, le=1440)
    # slice 3: may be [] for external exams; "questions" mode still needs >= 1 (checked below)
    questions: list[QuestionIn] = Field(default_factory=list, max_length=500)
    # slice 2, all optional (on PUT an omitted field keeps the stored value)
    course: str = Field(default="", max_length=64)
    room: str = Field(default="", max_length=64)
    scheduled_at: datetime | None = None
    shuffle_questions: bool = False
    # slice 3: external-website exams
    mode: Literal["questions", "external"] = "questions"
    external_url: str | None = Field(default=None, max_length=MAX_EXTERNAL_URL)
    allowed_domains: list[str] = Field(default_factory=list, max_length=MAX_ALLOWED_DOMAINS)
    finish_url_contains: str | None = Field(default=None, max_length=MAX_FINISH_CONTAINS)
    # slice 4: points for an external exam (the teacher enters each student's score)
    external_points: float = Field(default=100, gt=0, le=10000)
    # slice 6: how the exam is delivered. Omitted -> derived from mode. "website" <=> mode
    # "external"; sending only delivery sets the mode, sending both must agree (422).
    delivery: Literal["computer", "paper", "website"] | None = None
    # Audit S-03: the class that takes this exam (one of the teacher's classes). Required
    # before the exam can be opened, unless it is linked to an LMS course.
    class_id: int | None = Field(default=None, ge=1)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        return v

    @field_validator("course", "room")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("scheduled_at")
    @classmethod
    def _naive_utc(cls, v: datetime | None) -> datetime | None:
        # Audit S-17: a time without a zone ("2026-10-12T09:00") is ambiguous -- a teacher in
        # China meant 09:00 there, the server would store 09:00 UTC (8 hours off). Refuse it.
        if v is not None and v.tzinfo is None:
            raise ValueError("scheduled_at needs a time zone, e.g. 2026-10-12T09:00:00+08:00 or ...Z")
        if v is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for d in v:
            if not isinstance(d, str) or not d.strip():
                continue  # blank lines from a textarea are ignored
            d = normalize_domain(d)
            if d not in out:
                out.append(d)
        return out

    @model_validator(mode="after")
    def _by_mode(self) -> "ExamIn":
        # slice 6: delivery and mode must agree
        if self.delivery is None:
            self.delivery = "website" if self.mode == "external" else "computer"
        elif "mode" not in self.model_fields_set:
            self.mode = "external" if self.delivery == "website" else "questions"
        elif (self.delivery == "website") != (self.mode == "external"):
            raise ValueError("delivery \"website\" goes with mode \"external\" (and only with it)")
        # 2026-10-13: topics are optional (a question without one gets the course, or
        # "general", when saved -- see routes_teacher._apply).
        if self.mode == "external":
            url = (self.external_url or "").strip()
            if not url:
                raise ValueError("external_url is required for an exam on another website")
            if external_host(url) is None:
                raise ValueError("external_url must be a full http:// or https:// address")
            if self.questions:
                raise ValueError("an exam on another website cannot have questions")
            self.external_url = url
            fin = (self.finish_url_contains or "").strip()
            self.finish_url_contains = fin or None
        else:
            if not self.questions:
                raise ValueError("add at least one question")
            self.external_url = None
            self.allowed_domains = []
            self.finish_url_contains = None
        return self


AnswerMap = dict[str, Any]


class ClientProofIn(BaseModel):
    """Audit #1: proof that the FairPlay app (not a browser) starts the attempt.
    signature = hex HMAC-SHA256(app key, "start|<nonce>|<device_id>"); the nonce comes
    from GET /api/student/exams/{id}/challenge and is single-use."""
    model_config = ConfigDict(extra="ignore")
    device_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    nonce: str = Field(min_length=8, max_length=128)
    signature: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    version: str = Field(default="", max_length=32)
    build: str = Field(default="", max_length=16)


class StartIn(BaseModel):
    start_code: str = Field(default="", max_length=16)
    client: ClientProofIn | None = None
    # slice 6: the student began from the offline package before the server knew; the
    # server accepts up to 30 minutes in the past (older is clamped) and sets the deadline
    # from it. Ignored when resuming an existing attempt.
    offline_started_at: datetime | None = None

    @field_validator("offline_started_at")
    @classmethod
    def _offline_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


MAX_ANSWER_INT = 10 ** 300


class AnswersIn(BaseModel):
    answers: AnswerMap = Field(default_factory=dict)
    client_seq: int = Field(default=0, ge=0, le=2 ** 62)   # audit S-14: fits BIGINT

    @field_validator("answers")
    @classmethod
    def _limit(cls, v: AnswerMap) -> AnswerMap:
        if len(v) > 1000:
            raise ValueError("too many answers")
        for k, val in v.items():
            if len(k) > 64:
                raise ValueError("bad question id")
            if isinstance(val, str) and len(val) > MAX_ANSWER_CHARS:
                raise ValueError("answer too long")
            if isinstance(val, (dict, list)):
                raise ValueError("answer values must be numbers, strings or null")
            # Audit S-04: no inf/NaN, no integers a float can't hold.
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError("answer numbers must be finite")
            if isinstance(val, int) and not isinstance(val, bool) and abs(val) > MAX_ANSWER_INT:
                raise ValueError("answer number too large")
        return v


class HeartbeatIn(BaseModel):
    current_question: int = Field(default=0, ge=0, le=10_000)
    # Ignored since the audit (#2): lock state only counts from signed app beats.
    locked: bool = False


class AppBeatIn(BaseModel):
    """Sent by the Mac app itself every 5 s (audit #2, #6). signature = hex
    HMAC-SHA256(app key, "beat|<attempt>|<session>|<device_id>|<ts>|<locked 0/1>|<checks>")."""
    model_config = ConfigDict(extra="ignore")
    session: str = Field(min_length=8, max_length=64)
    device_id: str = Field(min_length=8, max_length=64)
    ts: int = Field(ge=0)                       # client ms, strictly increasing per attempt
    locked: bool = False
    checks: str = Field(default="", max_length=200)   # e.g. "vm=0,displays=1,tap=1,input=1"
    signature: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class ExitCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    # Audit 2026-10-13: the app's nonce; the reply then carries proofs over
    # "exit|<attempt>|<session>|ok|<nonce>" (app_client.exit_message).
    nonce: str | None = Field(default=None, min_length=8, max_length=128,
                              pattern=r"^[A-Za-z0-9_-]+$")


class OfflineKeyIn(BaseModel):
    """The app asks for its attempt's offline exit key. signature = hex
    HMAC-SHA256(app key, "offkey|<attempt>|<session>|<device_id>|<ts>")."""
    model_config = ConfigDict(extra="ignore")
    session: str = Field(min_length=8, max_length=64)
    device_id: str = Field(min_length=8, max_length=64)
    ts: int = Field(ge=0, le=2 ** 62)
    signature: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class OfflineExitIn(BaseModel):
    mac_code: str = Field(min_length=1, max_length=20)


class EventIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str = Field(min_length=1, max_length=64)
    detail: str = ""
    client_time: str | None = None


class EventsIn(BaseModel):
    events: list[EventIn] = Field(default_factory=list, max_length=500)


# ---------------------------------------------------------------- slice 2 teacher bodies
class ExtendIn(BaseModel):
    minutes: int = Field(ge=1, le=120)


class ReviewIn(BaseModel):
    status: Literal["dismissed", "noted"]
    note: str | None = Field(default=None, max_length=500)


class GradeIn(BaseModel):
    points: float
    comment: str = Field(default="", max_length=5000)
    rubric_scores: list[float] | None = Field(default=None, max_length=50)

    @field_validator("points")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("points must be a finite number")
        return v

    @field_validator("rubric_scores")
    @classmethod
    def _finite_list(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and not all(math.isfinite(x) for x in v):
            raise ValueError("rubric scores must be finite numbers")
        return v


class CommentIn(BaseModel):
    comment: str = Field(default="", max_length=2000)


class ReleaseIn(BaseModel):
    force: bool = False
