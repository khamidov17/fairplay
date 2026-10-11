"""Split a mixed stack of pages (many students, no QR codes) into one set per student.

1. `read_header` (vision, PROMPT_VERSION) reads each page's top: name, student ID, whether it
   looks like a first page, a written page number ("2/3").
2. `group_pages` (deterministic) starts a new student at a page with a name/ID that differs
   from the current one or at a first page; other pages continue the current student. Sets
   matched to the same student are merged. Without any names it can fall back to "every N
   pages = one student".
3. `match_roster` fuzzy-matches the name / ID against the class roster (difflib + token and
   initials matching, accents folded). The result carries a confidence and the top
   candidates; low confidence -> the teacher confirms.

Group record:
{"pages": [page indexes in reading order], "name": "as written", "student_id_text": "S123",
 "student_id": 7 | None, "confidence": 0.92, "candidates": [{"student_id","name","username","score"}],
 "needs_check": bool, "reasons": ["no_name", "weak_match", "ambiguous", ...]}
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .. import ai

PROMPT_VERSION = "fp-page-header-v1"
MATCH_MIN = 0.72        # below: no student is picked
MATCH_SURE = 0.88       # at or above (with a clear margin): no teacher check needed
MARGIN = 0.08

SYSTEM = (
    "You look at one photographed or scanned page of a student's handwritten exam or homework. "
    "Read what identifies the student and the page: the student's name and student ID if written "
    "(usually at the top), whether this looks like the FIRST page of a student's work (name line, "
    "title, question 1 starting), and a page number if the student wrote one. Reply with ONE JSON object only."
)
SCHEMA = ('{"name": "name as written or empty", "student_id": "ID as written or empty", '
          '"first_page": true|false, "page_number": number or null, "confidence": 0-1}')


# ---------------------------------------------------------------- header reading
def parse_header(out: Any) -> dict:
    if not isinstance(out, dict):
        out = {}
    name = " ".join(str(out.get("name") or "").split())[:120]
    sid = " ".join(str(out.get("student_id") or out.get("id") or "").split())[:60]
    if name.lower() in ("none", "n/a", "unknown", "empty", "-"):
        name = ""
    if sid.lower() in ("none", "n/a", "unknown", "empty", "-"):
        sid = ""
    pn = out.get("page_number")
    try:
        pn = int(pn) if pn is not None and not isinstance(pn, bool) and float(pn).is_integer() else None
    except (TypeError, ValueError):
        pn = None
    if pn is not None and not 1 <= pn <= 99:
        pn = None
    try:
        conf = float(out.get("confidence"))
        conf = conf / 100 if 1 < conf <= 100 else conf
        conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = 0.5
    first = out.get("first_page")
    first = first if isinstance(first, bool) else str(first).strip().lower() in ("true", "yes", "1")
    return {"name": name, "student_id": sid, "first_page": bool(first), "page_number": pn,
            "confidence": round(conf, 2)}


async def read_header(image: bytes) -> dict:
    """What identifies this page. Never raises (AI off/failing -> empty header, confidence 0)."""
    empty = {"name": "", "student_id": "", "first_page": False, "page_number": None, "confidence": 0.0}
    if not ai.enabled() or not image:
        return dict(empty, error="ai_off" if not ai.enabled() else "no_image")
    try:
        out = await ai.complete_json(SYSTEM, f"Prompt version: {PROMPT_VERSION}\nReturn JSON exactly like: {SCHEMA}",
                                     images=[image], max_tokens=200)
    except ai.AIUnavailable as e:
        return dict(empty, error=str(e)[:120])
    return parse_header(out)


async def read_headers(images: list[bytes], concurrency: int = 4) -> list[dict]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(img: bytes) -> dict:
        async with sem:
            return await read_header(img)

    return list(await asyncio.gather(*(one(i) for i in images)))


# ---------------------------------------------------------------- fuzzy names
_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ",
                ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                 "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "o'", "q", "g'", "h"]))


def norm_name(s: str) -> str:
    """Lower-case, accents folded, Cyrillic transliterated, only letters/digits/spaces."""
    s = (s or "").lower()
    s = "".join(_CYR.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return " ".join(s.split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def name_score(read: str, full_name: str, username: str = "") -> float:
    """0..1 similarity between a handwritten name (as read) and a roster entry."""
    r, n, u = norm_name(read), norm_name(full_name), norm_name(username)
    if not r:
        return 0.0
    if u and r.replace(" ", "") == u.replace(" ", ""):
        return 1.0
    if not n:
        return _ratio(r, u)
    if r == n:
        return 1.0
    rt, nt = r.split(), n.split()
    whole = _ratio(r, n)
    sort = _ratio(" ".join(sorted(rt)), " ".join(sorted(nt)))
    # token matching with initials ("A. Smith" ~ "Alice Smith")
    scores = []
    for t in rt:
        best = 0.0
        for m in nt:
            if len(t) == 1 and m.startswith(t):
                best = max(best, 0.85)
            else:
                best = max(best, _ratio(t, m))
        scores.append(best)
    tokens = sum(scores) / len(scores) if scores else 0.0
    # reading only the given name or only the family name is weaker evidence
    coverage = min(1.0, len(rt) / max(1, len(nt)))
    tokens *= 0.85 + 0.15 * coverage
    return round(max(whole, sort, tokens), 3)


def id_score(read_id: str, student: dict) -> float:
    rid = re.sub(r"[^a-z0-9]", "", (read_id or "").lower())
    if not rid:
        return 0.0
    for key in ("username", "student_number", "email"):
        v = str(student.get(key) or "").lower()
        v = v.split("@")[0] if key == "email" else v
        v = re.sub(r"[^a-z0-9]", "", v)
        if v and v == rid:
            return 1.0
    v = re.sub(r"[^a-z0-9]", "", str(student.get("username") or "").lower())
    return 0.9 * _ratio(rid, v) if len(rid) >= 4 and _ratio(rid, v) >= 0.85 else 0.0


def match_roster(name: str, student_id_text: str, roster: list[dict], top: int = 3) -> dict:
    """{"student_id": id | None, "confidence", "candidates": [...], "reasons": [...]}
    roster: [{"id", "name", "username"}]."""
    scored = []
    for s in roster:
        sc = max(name_score(name, s.get("name") or "", s.get("username") or ""), id_score(student_id_text, s))
        scored.append({"student_id": s["id"], "name": s.get("name") or "", "username": s.get("username") or "",
                       "score": round(sc, 3)})
    scored.sort(key=lambda x: -x["score"])
    cands = [c for c in scored[:top] if c["score"] > 0]
    reasons: list[str] = []
    if not name and not student_id_text:
        return {"student_id": None, "confidence": 0.0, "candidates": cands, "reasons": ["no_name"]}
    best = cands[0] if cands else None
    second = cands[1]["score"] if len(cands) > 1 else 0.0
    if best is None or best["score"] < MATCH_MIN:
        return {"student_id": None, "confidence": round(best["score"] if best else 0.0, 3), "candidates": cands,
                "reasons": ["no_match"]}
    conf = best["score"]
    if best["score"] - second < MARGIN:
        reasons.append("ambiguous")
        conf = min(conf, MATCH_SURE - 0.1)
    elif conf < MATCH_SURE:
        reasons.append("weak_match")
    return {"student_id": best["student_id"], "confidence": round(conf, 3), "candidates": cands, "reasons": reasons}


# ---------------------------------------------------------------- grouping
def _same_person(a: dict, b: dict) -> bool:
    if a.get("student_id") and b.get("student_id"):
        return re.sub(r"\W", "", a["student_id"].lower()) == re.sub(r"\W", "", b["student_id"].lower())
    if a.get("name") and b.get("name"):
        return _ratio(norm_name(a["name"]), norm_name(b["name"])) >= 0.8
    return False


def group_pages(headers: list[dict], roster: list[dict], pages_per_student: int | None = None) -> list[dict]:
    """Pages (in upload order, one header per page) -> one group per student."""
    if not headers:
        return []
    named = any(h.get("name") or h.get("student_id") for h in headers)
    raw: list[dict] = []
    if not named and pages_per_student and pages_per_student > 1:
        for i in range(0, len(headers), pages_per_student):
            raw.append({"pages": list(range(i, min(len(headers), i + pages_per_student))), "header": {}})
    else:
        for i, h in enumerate(headers):
            ident = bool(h.get("name") or h.get("student_id"))
            cur = raw[-1] if raw else None
            starts = cur is None
            if cur is not None:
                if ident and cur["header"] and not _same_person(h, cur["header"]):
                    starts = True
                elif ident and not cur["header"] and cur["pages"]:
                    # name on a later page of an unnamed run: a new student unless it says page >= 2
                    starts = not (h.get("page_number") or 1) > 1
                elif not ident and h.get("first_page") and (h.get("page_number") in (None, 1)) and cur["pages"]:
                    starts = True
                elif not ident and h.get("page_number") == 1 and cur["pages"]:
                    starts = True
            if starts:
                raw.append({"pages": [i], "header": h if ident else {}})
            else:
                cur["pages"].append(i)
                if ident and not cur["header"]:
                    cur["header"] = h
    groups: list[dict] = []
    for g in raw:
        h = g["header"]
        m = match_roster(h.get("name", ""), h.get("student_id", ""), roster)
        read_conf = float(h.get("confidence") or 0) if h else 0.0
        conf = min(m["confidence"], max(read_conf, 0.5)) if m["student_id"] else m["confidence"]
        groups.append({"pages": g["pages"], "name": h.get("name", ""), "student_id_text": h.get("student_id", ""),
                       "student_id": m["student_id"], "confidence": round(conf, 3), "candidates": m["candidates"],
                       "reasons": list(m["reasons"])})
    # the same student twice (pages shuffled, or a name on every page): merge strong matches,
    # otherwise keep the better one and send the other to the teacher
    by_student: dict[int, dict] = {}
    out: list[dict] = []
    for g in groups:
        sid = g["student_id"]
        if sid is None:
            out.append(g)
            continue
        prev = by_student.get(sid)
        if prev is None:
            by_student[sid] = g
            out.append(g)
        elif min(prev["confidence"], g["confidence"]) >= MATCH_MIN:
            prev["pages"] = prev["pages"] + g["pages"]
            prev["confidence"] = min(prev["confidence"], g["confidence"])
            if "merged" not in prev["reasons"]:
                prev["reasons"].append("merged")
        else:
            loser = g if g["confidence"] <= prev["confidence"] else prev
            loser["student_id"] = None
            loser["reasons"].append("duplicate_student")
            if loser is prev:
                by_student[sid] = g
            out.append(g)
    for g in out:
        # pages of one student in the written page-number order when given
        if all(headers[p].get("page_number") for p in g["pages"]) and len(g["pages"]) > 1:
            g["pages"].sort(key=lambda p: (headers[p]["page_number"], p))
        g["needs_check"] = g["student_id"] is None or g["confidence"] < MATCH_SURE or bool(
            set(g["reasons"]) & {"ambiguous", "weak_match", "no_name", "no_match", "duplicate_student", "merged"})
    return out
