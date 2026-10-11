"""CSV (+ images zip) import of benchmark items. Pure parsing; storing images is done by
the caller through app.uploads. Student-identifying columns are dropped, never stored.

Columns (header row, case-insensitive; aliases in brackets):
  question_prompt [prompt, question]   required
  question_type [type]                 mcq|numeric|short|essay|work (default essay)
  subject                              default: the set's subject
  max_points [points, max]             required, > 0
  rubric                               JSON list [{"label","points"}] or "label:points; label:points"
  answer_key [key, correct_answer]
  answer_text [answer, transcript]
  image [answer_image, file]           file name inside the zip
  human_mark [human_marks, mark, marks, score]   required: "3" or several "3;4" / "3|4"
  human_comment [comment, feedback]
"""
from __future__ import annotations

import csv
import io
import json
import re

MAX_ROWS = 2000
QUESTION_TYPES = ("mcq", "numeric", "short", "essay", "work", "external")
PII_COLUMNS = {"student", "student_name", "name", "first_name", "last_name", "full_name", "username",
               "email", "student_id", "student_email", "id_number", "netid"}

ALIASES = {
    "question_prompt": ("question_prompt", "prompt", "question"),
    "question_type": ("question_type", "type"),
    "subject": ("subject",),
    "max_points": ("max_points", "points", "max"),
    "rubric": ("rubric",),
    "answer_key": ("answer_key", "key", "correct_answer"),
    "answer_text": ("answer_text", "answer", "transcript"),
    "image": ("image", "answer_image", "file"),
    "human_marks": ("human_mark", "human_marks", "mark", "marks", "score"),
    "human_comment": ("human_comment", "comment", "feedback"),
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def parse_rubric(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        out = []
        for r in data:
            out.append({"label": str(r["label"])[:300], "points": float(r["points"])})
        return out
    out = []
    for part in re.split(r"[;\n]", text):
        if not part.strip():
            continue
        label, _, pts = part.rpartition(":")
        if not label.strip():
            raise ValueError(f"Rubric part '{part.strip()}' needs 'label: points'")
        out.append({"label": label.strip()[:300], "points": float(pts)})
    return out


def parse_marks(text: str) -> list[float]:
    """"3" -> [3.0]; "3;4" / "3|4" -> [3.0, 4.0]; a decimal comma is accepted ("2,5")."""
    parts = [p.strip() for p in re.split(r"[;|/]", text or "") if p.strip()]
    return [float(p.replace(",", ".")) for p in parts]


def parse_csv(data: bytes) -> tuple[list[dict], list[dict], list[str]]:
    """(rows, errors [{row, error}], ignored columns). Row numbers count the header as 1."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], [{"row": 1, "error": "The file is empty"}], []
    cols = [_norm(h) for h in header]
    index: dict[str, int] = {}
    for field, names in ALIASES.items():
        for i, c in enumerate(cols):
            if c in names and field not in index:
                index[field] = i
    used = set(index.values())
    ignored = [header[i] for i, c in enumerate(cols) if i not in used]
    missing = [f for f in ("question_prompt", "max_points", "human_marks") if f not in index]
    if missing:
        return [], [{"row": 1, "error": "Missing column(s): " + ", ".join(missing)}], ignored
    rows, errors = [], []
    for n, raw in enumerate(reader, start=2):
        if not any(c.strip() for c in raw):
            continue
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append({"row": n, "error": f"At most {MAX_ROWS} rows per import"})
            break

        def get(field: str) -> str:
            i = index.get(field)
            return raw[i].strip() if i is not None and i < len(raw) else ""
        try:
            prompt = get("question_prompt")
            if not prompt:
                raise ValueError("question_prompt is empty")
            qtype = (get("question_type") or "essay").lower()
            if qtype not in QUESTION_TYPES:
                raise ValueError(f"Unknown question_type '{qtype}'")
            max_points = float(get("max_points").replace(",", "."))
            if not 0 < max_points <= 1000:
                raise ValueError("max_points must be between 0 and 1000")
            marks = parse_marks(get("human_marks"))
            if not marks:
                raise ValueError("human_mark is empty")
            if any(not 0 <= m <= max_points for m in marks):
                raise ValueError(f"Marks must be between 0 and {max_points:g}")
            rubric = parse_rubric(get("rubric"))
            if rubric and abs(sum(r["points"] for r in rubric) - max_points) > 1e-6:
                raise ValueError("Rubric points must add up to max_points")
            answer_text, image = get("answer_text"), get("image")
            if not answer_text and not image:
                raise ValueError("Give answer_text or an image")
            rows.append({"row": n, "question_prompt": prompt[:20000], "question_type": qtype,
                         "subject": get("subject")[:100], "max_points": max_points, "rubric": rubric,
                         "answer_key": get("answer_key")[:2000], "answer_text": answer_text[:50000],
                         "image": image, "human_marks": marks[:5], "human_comment": get("human_comment")[:5000]})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            errors.append({"row": n, "error": str(e) or "Invalid row"})
    return rows, errors, ignored
