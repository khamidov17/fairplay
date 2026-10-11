"""Any-paper answer finding: OpenCV candidates, model-output validation, roster fuzzy
matching, page grouping, and the scan pipeline with a fake AI (no QR codes anywhere)."""
import base64
import json
import re
import time

import cv2
import httpx
import numpy as np
import pymupdf
import pytest

from app import ai
from app.scan import grouping, segment

from .test_scan import _wait

W = 827


@pytest.fixture(autouse=True)
def ai_reset():
    yield
    ai.set_transport(None)


# ---------------------------------------------------------------- OpenCV candidates
def _lined_page(h=1169, rule=200):
    img = np.full((h, W, 3), 250, np.uint8)
    for y in range(80, h, 34):
        cv2.line(img, (0, y), (W, y), (rule, rule, rule), 1)
    cv2.line(img, (60, 0), (60, h), (rule, rule, 230), 1)  # margin line
    return img


def _write(img, y, text, x=70, scale=0.9):
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (30, 30, 70), 2)
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    h, w = img.shape[:2]
    return [x / w, (y - th) / h, (x + tw) / w, y / h]  # the text's own box


def _covers(cand, box, tol=0.01):
    return cand[0] <= box[0] + tol and cand[1] <= box[1] + tol and cand[2] >= box[2] - tol and cand[3] >= box[3] - tol


@pytest.mark.parametrize("rule", [200, 120])  # light and dark notebook rules
def test_candidates_cover_text_blocks(rule):
    img = _lined_page(rule=rule)
    blocks = [
        [_write(img, 110, "Name: Alice Chen")],
        [_write(img, 300, "1) x^2 + 3x = 10"), _write(img, 334, "x = 2 or x = -5")],
        [_write(img, 600, "2b) F = ma = 20 N"), _write(img, 634, "a = 4 m/s2"), _write(img, 668, "so F = 5*4")],
        [_write(img, 950, "3) v = 12 m/s", x=300)],
    ]
    cands = segment.candidate_regions(img)
    assert len(cands) == len(blocks), cands
    for cand, lines in zip(cands, blocks):  # top to bottom, one candidate per block
        for line in lines:
            assert _covers(cand, line), (cand, line)
    # nothing from the rules / margin line leaks in
    assert all(c[0] > 0.06 for c in cands)


def test_candidates_blank_and_tiny_pages():
    assert segment.candidate_regions(np.full((1169, W), 255, np.uint8)) == []
    assert segment.candidate_regions(np.full((10, 10), 255, np.uint8)) == []
    # many lines far apart: capped
    img = np.full((3000, W), 255, np.uint8)
    for i in range(60):
        cv2.putText(img, f"line {i}", (80, 40 + i * 48), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 0, 2)
    assert 1 <= len(segment.candidate_regions(img, max_candidates=10)) <= 10


# ---------------------------------------------------------------- parsing / validation
QS = [{"id": "qa", "type": "numeric", "prompt": "2+2?"},
      {"id": "qb", "type": "short", "prompt": "Capital of France?"},
      {"id": "qc", "type": "essay", "prompt": "2b) Explain why."}]
PAGES = [{"index": 0, "candidates": [{"n": 1, "box": [0.1, 0.05, 0.5, 0.1]}, {"n": 2, "box": [0.1, 0.2, 0.8, 0.3]},
                                     {"n": 3, "box": [0.1, 0.4, 0.8, 0.5]}, {"n": 4, "box": [0.1, 0.7, 0.8, 0.8]}]},
         {"index": 1, "candidates": [{"n": 5, "box": [0.1, 0.1, 0.8, 0.2]}, {"n": 6, "box": [0.1, 0.5, 0.9, 0.6]}]}]


def test_resolve_question_labels():
    r = segment.resolve_question
    assert r("qa", QS) == "qa" and r("QB", QS) == "qb"
    assert r("1", QS) == "qa" and r(2, QS) == "qb" and r("Q3", QS) == "qc"
    assert r("3)", QS) == "qc" and r("#2", QS) == "qb" and r("question 1", QS) == "qa"
    assert r("2b", QS) == "qc"  # prompt of qc starts with "2b)"
    assert r("1a", QS) == "qa"  # no sub-question 1a: question 1
    assert r("7", QS) is None and r("zz", QS) is None and r(None, QS) is None and r(True, QS) is None


def test_parse_segmentation_validates():
    out = {
        "name": {"text": "Alice  Chen", "student_id": "", "candidates": [1], "confidence": 0.9},
        "answers": [
            {"question": "1)", "label": "1)", "candidates": [2], "confidence": 0.95},
            # continues on the next page; percent confidence
            {"question": "qc", "label": "2b", "candidates": [3, 5], "confidence": 90},
            # raw box in percent, snapped to candidate 6
            {"question": "Q2", "boxes": [{"page": 2, "box": [12, 52, 50, 58]}], "confidence": 0.5},
            {"question": "q-unknown", "candidates": [4]},          # dropped
            {"question": "qa", "candidates": [999]},               # unknown candidate: nothing added
            "junk",
        ],
        "crossed_out": [{"question": "2", "candidates": [4]}, 77],
    }
    res = segment.parse_segmentation(out, QS, PAGES)
    by_q = {}
    for r in res["regions"]:
        by_q.setdefault((r["question_id"], r["crossed_out"]), []).append(r)
    assert [r["box"] for r in by_q[("qa", False)]] == [[0.1, 0.2, 0.8, 0.3]]
    assert [(r["page"], r["confidence"]) for r in by_q[("qc", False)]] == [(0, 0.9), (1, 0.9)]
    qb = by_q[("qb", False)][0]
    assert qb["page"] == 1 and qb["box"] == [0.1, 0.5, 0.9, 0.6] and qb["needs_check"]
    assert by_q[("qb", True)][0]["box"] == [0.1, 0.7, 0.8, 0.8]
    assert res["missing"] == [] and res["name"]["text"] == "Alice Chen" and res["name"]["box"] == [0.1, 0.05, 0.5, 0.1]
    assert res["needs_check"] and res["reasons"] == ["low_confidence"] and res["confidence"] == 0.5
    assert [r["id"] for r in res["regions"]] == [f"r{i + 1}" for i in range(len(res["regions"]))]
    assert res["prompt_version"] == segment.PROMPT_VERSION


def test_parse_segmentation_junk_and_clamps():
    res = segment.parse_segmentation("nonsense", QS, PAGES)
    assert res["regions"] == [] and res["missing"] == ["qa", "qb", "qc"] and res["needs_check"]
    res = segment.parse_segmentation({"answers": {"qa": {"boxes": [{"page": 1, "box": [-0.2, 0.15, 1.7, 0.35]}],
                                                         "confidence": "high"}}}, QS, PAGES)
    assert res["regions"][0]["box"] == [0.0, 0.15, 1.0, 0.35] and res["regions"][0]["confidence"] == 0.5
    assert res["missing"] == ["qb", "qc"] and "missing" in res["reasons"]
    # two questions claim the same box: the more confident one keeps it, flagged
    res = segment.parse_segmentation({"answers": [{"question": "qa", "candidates": [2], "confidence": 0.9},
                                                  {"question": "qb", "candidates": [2], "confidence": 0.8}]}, QS, PAGES)
    assert [r["question_id"] for r in res["regions"]] == ["qa"] and res["regions"][0]["needs_check"]
    assert segment.clamp_box([0.5, 0.5, 0.5, 0.9]) is None and segment.clamp_box("x") is None
    assert segment.clamp_box([0.5, 0.5, 0.502, 0.9])[2] - segment.clamp_box([0.5, 0.5, 0.502, 0.9])[0] >= 0.0099
    assert segment.clamp_box([float("nan"), 0, 1, 1]) is None


def test_segment_pages_ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    img = _lined_page()
    _write(img, 300, "1) 4")
    import asyncio
    res = asyncio.run(segment.segment_pages(QS, [img]))
    assert res["source"] == "fallback" and res["reasons"] == ["ai_off"] and res["missing"] == ["qa", "qb", "qc"]
    assert len(res["pages"][0]["candidates"]) == 1


def test_crop_answer_stitches_pages():
    a = np.full((1000, 800), 255, np.uint8)
    b = np.full((1200, 800), 255, np.uint8)
    out = segment.crop_answer([a, b], [(0, [0.1, 0.5, 0.9, 0.6]), (1, [0.1, 0.0, 0.5, 0.1])], pad=0)
    assert out.shape == (100 + 6 + 120, 640)
    assert segment.crop_answer([a], [(3, [0, 0, 1, 1])]) is None


# ---------------------------------------------------------------- roster matching + grouping
ROSTER = [{"id": 1, "name": "Alice Chen", "username": "alice"},
          {"id": 2, "name": "Bob Karimov", "username": "bob"},
          {"id": 3, "name": "Carol Wang", "username": "carol"},
          {"id": 4, "name": "Avazbek Toshmatov", "username": "s2024017"}]


def test_name_matching():
    s = grouping.name_score
    assert s("Alice Chen", "Alice Chen") == 1.0
    assert s("alice", "Alice Chen", "alice") == 1.0
    assert s("Chen, Alice", "Alice Chen") > 0.9
    assert s("A. Chen", "Alice Chen") > 0.85
    assert s("Bob Karimow", "Bob Karimov") > 0.88
    assert s("Авазбек Тошматов", "Avazbek Toshmatov") > 0.85   # Cyrillic handwriting
    assert s("Alicé Chén", "Alice Chen") == 1.0
    assert s("Carol Wang", "Alice Chen") < 0.6
    m = grouping.match_roster("Bob Karimow", "", ROSTER)
    assert m["student_id"] == 2 and m["confidence"] > 0.88 and m["candidates"][0]["username"] == "bob"
    m = grouping.match_roster("", "S2024-017", ROSTER)
    assert m["student_id"] == 4 and m["confidence"] == 1.0
    assert grouping.match_roster("", "", ROSTER) == {"student_id": None, "confidence": 0.0, "candidates": [],
                                                    "reasons": ["no_name"]}
    m = grouping.match_roster("Zed Zulu", "", ROSTER)
    assert m["student_id"] is None and m["reasons"] == ["no_match"]
    twins = ROSTER + [{"id": 5, "name": "Alice Chan", "username": "alice2"}]
    m = grouping.match_roster("Alice Chen", "", twins)
    assert m["student_id"] == 1  # exact beats near
    m = grouping.match_roster("Alice Ch", "", twins)
    assert "ambiguous" in m["reasons"] and m["confidence"] < grouping.MATCH_SURE


def _h(name="", sid="", first=False, pn=None, conf=0.9):
    return {"name": name, "student_id": sid, "first_page": first, "page_number": pn, "confidence": conf}


def test_group_pages():
    heads = [_h("Alice Chen", first=True), _h(pn=2), _h("Bob Karimov", first=True), _h(), _h("Carol Wang"),
             _h("Carol Wang", pn=2)]  # carol wrote her name on every page
    g = grouping.group_pages(heads, ROSTER)
    assert [x["pages"] for x in g] == [[0, 1], [2, 3], [4, 5]]
    assert [x["student_id"] for x in g] == [1, 2, 3] and not any(x["needs_check"] for x in g)
    # shuffled stack: alice's page 2 (named) comes after bob's -> merged back, page order kept
    heads = [_h("Alice Chen", pn=1), _h("Bob Karimov"), _h("Alice Chen", pn=2)]
    g = grouping.group_pages(heads, ROSTER)
    assert [x["pages"] for x in g] == [[0, 2], [1]] and "merged" in g[0]["reasons"] and g[0]["needs_check"]
    # unknown name -> needs the teacher
    g = grouping.group_pages([_h("Zed Zulu", first=True), _h()], ROSTER)
    assert g[0]["student_id"] is None and g[0]["needs_check"] and g[0]["pages"] == [0, 1]
    # no names at all: fall back to pages per student
    g = grouping.group_pages([_h(), _h(), _h(), _h()], ROSTER, pages_per_student=2)
    assert [x["pages"] for x in g] == [[0, 1], [2, 3]] and all(x["needs_check"] for x in g)
    # first-page markers without names split students
    g = grouping.group_pages([_h(first=True), _h(), _h(first=True, pn=1)], ROSTER)
    assert [x["pages"] for x in g] == [[0, 1], [2]]
    assert grouping.group_pages([], ROSTER) == []


def test_parse_header():
    p = grouping.parse_header({"name": "N/A", "student_id": " 12 3 ", "first_page": "yes", "page_number": "2",
                               "confidence": 80})
    assert p == {"name": "", "student_id": "12 3", "first_page": True, "page_number": 2, "confidence": 0.8}
    assert grouping.parse_header(None)["confidence"] == 0.5


# ---------------------------------------------------------------- pipeline with a fake AI
EXAM = {
    "title": "Any paper quiz", "duration_min": 30, "course": "PHYS 1", "delivery": "paper",
    "questions": [
        {"type": "numeric", "prompt": "What is 2+2?", "points": 2, "answer_number": 4, "topics": ["arithmetic"]},
        {"type": "short", "prompt": "Capital of France?", "points": 2, "accepted_answers": ["Paris"],
         "topics": ["geography"]},
        {"type": "essay", "prompt": "Explain Newton's second law with an example.", "points": 4,
         "topics": ["mechanics"]},
    ],
}

# page id -> (height, header, [(y px, text, kind, question number)])
PAGE_SPECS = {
    "A1": (1100, {"name": "Alice Chen", "first_page": True},
           [(90, "Name: Alice Chen", "name", None), (300, "1) 2+2 = 4", "answer", 1),
            (520, "3) F = m a, push a cart", "answer", 3), (760, "2) Lyon", "crossed", 2)]),
    "A2": (1180, {"page_number": 2},
           [(150, "so a = F / m", "answer", 3), (560, "2) Paris", "answer", 2)]),
    "B1": (1260, {"name": "Bob Karimow", "first_page": True},
           [(100, "Bob Karimov", "name", None), (380, "Q2: Paris", "answer", 2), (700, "1) 5", "answer", 1)]),
}
CONF = {("A", 1): 0.95, ("A", 2): 0.6, ("A", 3): 0.9, ("B", 1): 0.9, ("B", 2): 0.92}


def _page(spec):
    h, _, items = spec
    img = np.full((h, W, 3), 252, np.uint8)
    for y, text, kind, _ in items:
        _write(img, y, text, scale=1.0)
        if kind == "crossed":
            cv2.line(img, (60, y - 12), (300, y - 12), (30, 30, 70), 3)
    return img


def _pdf(names):
    doc = pymupdf.open()
    for n in names:
        img = _page(PAGE_SPECS[n])
        ok, buf = cv2.imencode(".png", img)
        page = doc.new_page(width=595, height=595 * img.shape[0] / W)
        page.insert_image(page.rect, stream=buf.tobytes())
    return doc.tobytes()


def _which(img_b64: str) -> str:
    data = base64.b64decode(img_b64.split(",", 1)[1])
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    ratio = img.shape[0] / img.shape[1]
    return min(PAGE_SPECS, key=lambda k: abs(PAGE_SPECS[k][0] / W - ratio))


class FakeModel:
    def __init__(self):
        self.calls = {"header": 0, "segment": 0, "grade": 0}
        self.grade_images = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        content = body["messages"][1]["content"]
        text = "".join(c.get("text", "") for c in content if c["type"] == "text")
        imgs = [c["image_url"]["url"] for c in content if c["type"] == "image_url"]
        if "fp-page-header-v1" in text:
            self.calls["header"] += 1
            head = dict(PAGE_SPECS[_which(imgs[0])][1])
            out = {"name": head.get("name", ""), "student_id": "", "first_page": head.get("first_page", False),
                   "page_number": head.get("page_number"), "confidence": 0.9}
        elif "fp-segment-v1" in text:
            self.calls["segment"] += 1
            out = self.segment(text, [_which(u) for u in imgs])
        else:
            self.calls["grade"] += 1
            data = base64.b64decode(imgs[0].split(",", 1)[1])
            self.grade_images.append(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE).shape)
            out = {"legibility": "clear", "transcript": "x", "steps": [{"text": "x", "status": "correct"}],
                   "points": 1, "explanation": "ok", "confidence": 0.8}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})

    @staticmethod
    def segment(text, pages):
        cands = {}
        for m in re.finditer(r"^Page (\d+): (.*)$", text, re.M):
            for c in re.finditer(r"(\d+): \[([^\]]+)\]", m.group(2)):
                cands.setdefault(int(m.group(1)) - 1, []).append((int(c.group(1)), [float(v) for v in c.group(2).split(",")]))

        def find(pi, y):
            for n, b in cands.get(pi, []):
                if b[1] - 0.01 <= y <= b[3] + 0.01:
                    return n
            return None

        out = {"answers": [], "crossed_out": [], "not_found": []}
        per_q = {}
        student = pages[0][0]
        for pi, pid in enumerate(pages):
            h, _, items = PAGE_SPECS[pid]
            for y, txt, kind, qn in items:
                n = find(pi, (y - 10) / h)
                if kind == "name":
                    out["name"] = {"text": txt.replace("Name: ", ""), "candidates": [n], "confidence": 0.9}
                elif kind == "crossed":
                    out["crossed_out"].append({"question": str(qn), "candidates": [n]})
                else:
                    per_q.setdefault(qn, []).append(n)
        labels = {1: "1)", 2: "Q2", 3: "3)"}
        for qn, ns in per_q.items():
            out["answers"].append({"question": labels[qn], "label": labels[qn], "candidates": ns,
                                   "confidence": CONF.get((student, qn), 0.9)})
        return out


def _ai_on(monkeypatch):
    fake = FakeModel()
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "openai")
    monkeypatch.setenv("FAIRPLAY_AI_KEY", "k")
    ai.set_transport(httpx.MockTransport(fake))
    return fake


def _wait_drafts(client, teacher, eid, n, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        q = client.get(f"/api/exams/{eid}/scan/queue", headers=teacher).json()
        items = [i for i in q["items"] if i["draft"]]
        if len(items) >= n:
            return q
        time.sleep(0.15)
    raise AssertionError("drafts did not appear")


def _setup(client, teacher, monkeypatch):
    fake = _ai_on(monkeypatch)
    e = client.post("/api/exams", json=EXAM, headers=teacher).json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob", "carol"]}, headers=teacher)
    r = client.post(f"/api/exams/{e['id']}/scans?unprinted=1", headers=teacher,
                    files=[("files", ("stack.pdf", _pdf(["A1", "A2", "B1"]), "application/pdf"))])
    assert r.status_code == 202, r.text
    st = _wait(client, teacher, e["id"])
    assert st["batches"][0]["status"] == "done", st
    return fake, e, [q["id"] for q in e["questions"]]


def test_pipeline_groups_finds_regions_and_drafts(client, teacher, monkeypatch):
    fake, e, q = _setup(client, teacher, monkeypatch)
    eid = e["id"]
    assert fake.calls["header"] == 3 and fake.calls["segment"] == 2
    sets = client.get(f"/api/exams/{eid}/scans/sets", headers=teacher).json()
    assert len(sets["sets"]) == 2 and sets["loose_pages"] == []
    a, b = sets["sets"]
    assert a["student"]["username"] == "alice" and len(a["pages"]) == 2 and a["name_read"] == "Alice Chen"
    assert b["student"]["username"] == "bob" and b["name_read"] == "Bob Karimow" and b["match"]["confidence"] > 0.88
    # alice: q3 continues onto page 2; crossed-out "Lyon" kept but not an answer; q2 is low confidence
    q3 = [r for r in a["regions"] if r["question_id"] == q[2] and not r["crossed_out"]]
    assert [r["page_id"] for r in q3] == [p["page_id"] for p in a["pages"]]
    crossed = [r for r in a["regions"] if r["crossed_out"]]
    assert len(crossed) == 1 and crossed[0]["question_id"] == q[1]
    assert a["missing"] == [] and a["needs_check"] and "low_confidence" in a["reasons"]
    assert all(len(p["candidates"]) >= 2 for p in a["pages"])
    # bob answered out of order and skipped q3
    assert b["missing"] == [q[2]] and "missing" in b["reasons"]
    assert sets["needs_check"] == 2

    queue = _wait_drafts(client, teacher, eid, 6)
    items = {(i["username"], i["question_id"]): i for i in queue["items"]}
    assert len(items) == 6
    assert items[("bob", q[2])]["draft"]["source"] == "blank" and items[("bob", q[2])]["blank"]
    assert items[("alice", q[0])]["draft"]["source"] in ("ai", "key")
    # the review screen gets the exact answer crop, not the whole page
    page_h = {p["page_id"]: p for p in a["pages"]}
    assert items[("alice", q[0])]["image"] not in {p["image"] for p in page_h.values()}
    assert fake.calls["grade"] == 5  # bob's unanswered q3 needs no AI
    small = [h for h, w in fake.grade_images]
    assert max(small) < 500, small  # crops (q3 = two parts stitched), never a ~1700px page
    # pages are assigned to the students (Map pages / unmatched lists see them as done)
    m = client.get(f"/api/exams/{eid}/scans/map", headers=teacher).json()
    assert {pg["status"] for pg in m["pages"]} == {"assigned"}
    assert client.get(f"/api/exams/{eid}/scans/unmatched", headers=teacher).json() == []
    # alice's q2 is on her 2nd page; its answer value records the region it came from
    a_q2 = items[("alice", q[1])]
    assert a_q2["page"] == 2 and a_q2["page_image"] is not None


def test_teacher_fixes_regions_students_and_pages(client, teacher, monkeypatch):
    fake, e, q = _setup(client, teacher, monkeypatch)
    eid = e["id"]
    _wait_drafts(client, teacher, eid, 6)
    sets = client.get(f"/api/exams/{eid}/scans/sets", headers=teacher).json()
    a, b = sets["sets"]
    url = f"/api/exams/{eid}/scans/sets/{a['set_id']}"
    regions = [dict(r) for r in a["regions"]]

    # resize alice's q2 box: only q2 is re-cropped and re-drafted
    before = fake.calls["grade"]
    for r in regions:
        if r["question_id"] == q[1] and not r["crossed_out"]:
            r["box"] = [r["box"][0], r["box"][1] - 0.02, min(1, r["box"][2] + 0.1), r["box"][3]]
    r = client.put(url, headers=teacher, json={"regions": regions})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["queued"] == 1
    moved = next(x for x in out["set"]["regions"] if x["question_id"] == q[1] and not x["crossed_out"])
    assert moved["source"] == "teacher" and moved["confidence"] == 1.0 and not moved["needs_check"]
    assert "low_confidence" not in out["set"]["reasons"]
    end = time.time() + 20
    while time.time() < end and fake.calls["grade"] < before + 1:
        time.sleep(0.1)
    assert fake.calls["grade"] == before + 1

    # mark q1 "not answered" (remove its box) -> blank answer, flagged missing
    regions = [x for x in out["set"]["regions"] if x["question_id"] != q[0]]
    out = client.put(url, headers=teacher, json={"regions": regions}).json()
    assert out["set"]["missing"] == [q[0]] and out["queued"] == 1
    queue = _wait_drafts(client, teacher, eid, 6)
    item = next(i for i in queue["items"] if i["username"] == "alice" and i["question_id"] == q[0])
    assert item["blank"] and item["draft"]["source"] == "blank"

    # validation
    bad = [dict(regions[0], question_id="nope")]
    assert client.put(url, headers=teacher, json={"regions": bad}).status_code == 422
    bad = [dict(regions[0], page_id=b["pages"][0]["page_id"])]
    assert client.put(url, headers=teacher, json={"regions": bad}).status_code == 422
    assert client.put(f"/api/exams/{eid}/scans/sets/nope", headers=teacher, json={}).status_code == 404
    users = {s["username"]: s["id"] for s in out["students"]}
    assert client.put(url, headers=teacher, json={"student_id": users["bob"]}).status_code == 409

    # confirm
    out = client.put(url, headers=teacher, json={"confirm": True}).json()
    assert out["set"]["status"] == "confirmed" and not out["set"]["needs_check"]

    # bob's pages were really carol's: pick carol -> bob's paper attempt disappears
    burl = f"/api/exams/{eid}/scans/sets/{b['set_id']}"
    out = client.put(burl, headers=teacher, json={"student_id": users["carol"]}).json()
    assert out["set"]["student"]["username"] == "carol" and out["queued"] == 3
    queue = _wait_drafts(client, teacher, eid, 6)
    names = {i["username"] for i in queue["items"]}
    assert names == {"alice", "carol"}

    # move alice's page 2 to carol: alice's q2/q3 parts there go, carol's set rebuilds
    a_p2 = a["pages"][1]["page_id"]
    out = client.put(burl, headers=teacher, json={"page_ids": [b["pages"][0]["page_id"], a_p2]}).json()
    sa = next(s for s in out["sets"] if s["set_id"] == a["set_id"])
    sb = next(s for s in out["sets"] if s["set_id"] == b["set_id"])
    assert [p["page_id"] for p in sa["pages"]] == [a["pages"][0]["page_id"]]
    assert q[1] in sa["missing"] and sa["status"] == "needs_check"
    assert len(sb["pages"]) == 2

    # split the page off again into a new student; the AI finds its answers
    seg_before = fake.calls["segment"]
    r = client.post(f"/api/exams/{eid}/scans/sets", headers=teacher, json={"page_ids": [a_p2]})
    assert r.status_code == 201, r.text
    new = r.json()["set"]
    assert new["student"] is None and "no_student" in new["reasons"] and fake.calls["segment"] == seg_before + 1
    assert {x["question_id"] for x in new["regions"]} >= {q[1], q[2]}
    assert client.post(f"/api/exams/{eid}/scans/sets", headers=teacher,
                       json={"page_ids": [a_p2], "student_id": users["alice"]}).status_code == 409
    # give the loose set to alice by merging the page back
    out = client.put(url, headers=teacher, json={"page_ids": [a["pages"][0]["page_id"], a_p2]}).json()
    assert len(out["sets"]) == 2  # the emptied set is gone

    # find again with AI
    r = client.post(f"/api/exams/{eid}/scans/sets/{a['set_id']}/resegment", headers=teacher)
    assert r.status_code == 200 and r.json()["set"]["missing"] == []


def test_ai_off_keeps_manual_mapping(client, teacher, monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    e = client.post("/api/exams", json=EXAM, headers=teacher).json()
    r = client.post(f"/api/exams/{e['id']}/scans?unprinted=1", headers=teacher,
                    files=[("files", ("stack.pdf", _pdf(["A1", "B1"]), "application/pdf"))])
    assert r.status_code == 202
    st = _wait(client, teacher, e["id"])
    assert st["batches"][0]["unmatched"] == 2
    sets = client.get(f"/api/exams/{e['id']}/scans/sets", headers=teacher).json()
    assert sets["sets"] == [] and len(sets["loose_pages"]) == 2 and not sets["ai_enabled"]
    assert client.post(f"/api/exams/{e['id']}/scans/sets/x/resegment", headers=teacher).status_code == 409
    # teacher groups pages by hand into a set for alice (no AI: no regions, every question "not found")
    users = {s["username"]: s["id"] for s in sets["students"]}
    r = client.post(f"/api/exams/{e['id']}/scans/sets", headers=teacher,
                    json={"page_ids": [sets["loose_pages"][0]["page_id"]], "student_id": users["alice"]})
    assert r.status_code == 201
    s = r.json()["set"]
    assert s["missing"] == [q["id"] for q in e["questions"]] and "not_found_by_ai" in s["reasons"]
    # draw a box by hand
    pid = s["pages"][0]["page_id"]
    out = client.put(f"/api/exams/{e['id']}/scans/sets/{s['set_id']}", headers=teacher, json={"regions": [
        {"question_id": e["questions"][0]["id"], "page_id": pid, "box": [0.05, 0.2, 0.6, 0.32]}]}).json()
    assert out["set"]["missing"] == [q["id"] for q in e["questions"][1:]]
    queue = client.get(f"/api/exams/{e['id']}/scan/queue", headers=teacher).json()
    assert len(queue["items"]) == 3 and sum(1 for i in queue["items"] if i["blank"]) == 2
    found = {i["question_id"]: i["found"] for i in queue["items"]}
    assert found[e["questions"][0]["id"]]["by"] == "teacher" and found[e["questions"][1]["id"]]["not_found"]


def test_find_answers_for_stored_pages(client, monkeypatch):
    """The helper homework mode calls: stored page uploads -> regions -> answer values."""
    import asyncio
    from app.scan.storage import save_file
    fake = _ai_on(monkeypatch)
    urls = [save_file(cv2.imencode(".png", _page(PAGE_SPECS[k]))[1].tobytes()) for k in ("A1", "A2")]
    qs = [{"id": f"q{i}", "type": t, "prompt": "p"} for i, t in enumerate(["numeric", "short", "essay"])]
    seg, answers = asyncio.run(segment.find_answers(qs, urls, {"source_upload": "hw"}))
    assert fake.calls["segment"] == 1 and seg["source"] == "ai" and seg["missing"] == []
    assert answers["q2"]["page"] == 1 and [r["page"] for r in answers["q2"]["regions"]] == [1, 2]
    assert answers["q1"]["page"] == 2 and answers["q0"]["source_upload"] == "hw"
    assert all(a["image"].startswith("/api/uploads/") and a["image"] not in urls for a in answers.values())
