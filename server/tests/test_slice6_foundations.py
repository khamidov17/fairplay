"""Slice 6 A/B/C + delivery: offline packages, question images, topic tags."""
import base64
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from PIL import Image

from app.offline import decrypt_package, encrypt_package, inline_images
from app.routes_topics import count_topics
from app.services import exam_delivery

from .conftest import SAMPLE_EXAM, login


def png_bytes(w=3, h=2, fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


def upload(client, headers, data: bytes, name="pic.png", ctype="image/png"):
    return client.post("/api/uploads", headers=headers, files={"file": (name, data, ctype)})


def iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def create(client, teacher, body, open_=False):
    r = client.post("/api/exams", json=body, headers=teacher)
    assert r.status_code == 201, r.text
    e = r.json()
    if open_:
        e = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    return e


WEBSITE = {"title": "Web", "duration_min": 20, "questions": [],
           "external_url": "https://forms.example.org/q"}


# ---------------------------------------------------------------- delivery
def test_delivery_defaults_and_derivation(client, teacher):
    e = create(client, teacher, SAMPLE_EXAM)
    assert e["delivery"] == "computer" and e["mode"] == "questions"
    w = create(client, teacher, {**WEBSITE, "mode": "external"})
    assert w["delivery"] == "website"
    # delivery alone sets the mode
    w2 = create(client, teacher, {**WEBSITE, "delivery": "website"})
    assert w2["mode"] == "external" and w2["delivery"] == "website"
    p = create(client, teacher, {**SAMPLE_EXAM, "delivery": "paper"})
    assert p["delivery"] == "paper" and p["mode"] == "questions"
    assert client.get(f"/api/exams/{p['id']}", headers=teacher).json()["delivery"] == "paper"
    # PUT switches it
    r = client.put(f"/api/exams/{p['id']}", json={**SAMPLE_EXAM, "delivery": "computer"},
                   headers=teacher)
    assert r.status_code == 200 and r.json()["delivery"] == "computer"


@pytest.mark.parametrize("body", [
    {**WEBSITE, "mode": "external", "delivery": "computer"},
    {**WEBSITE, "mode": "external", "delivery": "paper"},
    {**SAMPLE_EXAM, "mode": "questions", "delivery": "website"},
    {**SAMPLE_EXAM, "delivery": "tablet"},
])
def test_delivery_must_agree_with_mode(client, teacher, body):
    assert client.post("/api/exams", json=body, headers=teacher).status_code == 422


def test_old_rows_derive_delivery():
    assert exam_delivery(SimpleNamespace(delivery=None, mode="external")) == "website"
    assert exam_delivery(SimpleNamespace(delivery=None, mode="questions")) == "computer"
    assert exam_delivery(SimpleNamespace(delivery="paper", mode="questions")) == "paper"


def test_paper_exam_never_reaches_the_student_app(client, teacher, alice):
    p = create(client, teacher, {**SAMPLE_EXAM, "delivery": "paper"})
    r = client.post(f"/api/exams/{p['id']}/open", headers=teacher)
    assert r.status_code == 409
    assert all(x["id"] != p["id"] for x in client.get("/api/student/exams", headers=alice).json())
    r = client.post(f"/api/student/exams/{p['id']}/start", json={"start_code": "1234"}, headers=alice)
    assert r.status_code == 404
    assert client.get(f"/api/student/exams/{p['id']}/package", headers=alice).status_code == 404
    # closed and released like any other exam
    r = client.post(f"/api/exams/{p['id']}/close", headers=teacher)
    assert r.status_code == 200 and r.json()["status"] == "closed"
    assert client.post(f"/api/exams/{p['id']}/release", json={}, headers=teacher).status_code == 200
    assert all(x["id"] != p["id"] for x in client.get("/api/student/exams", headers=alice).json())


def test_student_list_has_delivery(client, alice, live_exam):
    item = next(x for x in client.get("/api/student/exams", headers=alice).json()
                if x["id"] == live_exam["id"])
    assert item["delivery"] == "computer"


# ---------------------------------------------------------------- topics
def test_topics_required_and_normalised(client, teacher):
    body = {**SAMPLE_EXAM, "questions": [dict(q) for q in SAMPLE_EXAM["questions"]]}
    body["questions"][1] = {k: v for k, v in body["questions"][1].items() if k != "topics"}
    r = client.post("/api/exams", json=body, headers=teacher)
    assert r.status_code == 422 and "Each question needs at least one topic" in r.text
    body["questions"][1]["topics"] = ["  ", ""]
    assert client.post("/api/exams", json=body, headers=teacher).status_code == 422
    body["questions"][1]["topics"] = ["  Integration  by Parts ", "integration by parts", "Limits"]
    e = create(client, teacher, body)
    assert e["questions"][1]["topics"] == ["integration by parts", "limits"]
    # PUT needs them too
    body["questions"][0] = {k: v for k, v in body["questions"][0].items() if k != "topics"}
    assert client.put(f"/api/exams/{e['id']}", json=body, headers=teacher).status_code == 422


@pytest.mark.parametrize("topics", [["a", "b", "c", "d", "e", "f"], ["x" * 61], [3]])
def test_topic_limits(client, teacher, topics):
    body = {**SAMPLE_EXAM, "questions": [dict(SAMPLE_EXAM["questions"][0], topics=topics)]}
    assert client.post("/api/exams", json=body, headers=teacher).status_code == 422


def test_website_item_gets_course_topic(client, teacher):
    w = create(client, teacher, {**WEBSITE, "delivery": "website", "course": "MATH 201"})
    assert w["questions"][0]["topics"] == ["math 201"]
    g = create(client, teacher, {**WEBSITE, "delivery": "website"})
    assert g["questions"][0]["topics"] == ["general"]


def test_topics_endpoint(client, teacher, alice):
    create(client, teacher, SAMPLE_EXAM)
    body = {**SAMPLE_EXAM, "questions": [dict(q, topics=["Arithmetic", "algebra"])
                                         for q in SAMPLE_EXAM["questions"][:2]]}
    create(client, teacher, body)
    create(client, teacher, {**WEBSITE, "delivery": "website"})
    r = client.get("/api/topics", headers=teacher)
    assert r.status_code == 200
    assert r.json() == [{"topic": "arithmetic", "question_count": 3},
                        {"topic": "algebra", "question_count": 2},
                        {"topic": "constants", "question_count": 1},
                        {"topic": "geography", "question_count": 1}]
    assert client.get("/api/topics", headers=alice).status_code == 403
    assert count_topics([[{"type": "mcq"}], None]) == []


def test_students_do_not_see_topics_while_writing(client, alice, live_exam):
    a = client.post(f"/api/student/exams/{live_exam['id']}/start",
                    json={"start_code": live_exam["start_code"]}, headers=alice).json()
    assert all("topics" not in q and q["images"] == [] for q in a["questions"])


# ---------------------------------------------------------------- uploads + images
def test_upload_and_serve(client, teacher):
    data = png_bytes(5, 4)
    r = upload(client, teacher, data)
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["url"] == f"/api/uploads/{up['id']}" and (up["width"], up["height"]) == (5, 4)
    assert len(up["id"]) == 32
    g = client.get(up["url"])  # no auth needed
    assert g.status_code == 200 and g.content == data
    assert g.headers["content-type"] == "image/png"
    assert "max-age=31536000" in g.headers["cache-control"]
    for fmt, media in (("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")):
        r = upload(client, teacher, png_bytes(fmt=fmt), name="x.bin", ctype="application/octet-stream")
        assert r.status_code == 201, (fmt, r.text)
        assert client.get(r.json()["url"]).headers["content-type"] == media
    assert client.get("/api/uploads/" + "0" * 32).status_code == 404
    assert client.get("/api/uploads/..%2Fsecret").status_code == 404


def test_upload_rejects_bad_files(client, teacher, alice):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert upload(client, teacher, svg, "x.svg", "image/svg+xml").status_code == 422
    assert upload(client, teacher, b"%PDF-1.4 ...", "x.png", "image/png").status_code == 422
    assert upload(client, teacher, b"\x89PNG\r\n\x1a\nbroken", "x.png").status_code == 422
    assert upload(client, teacher, b"").status_code == 422
    big = png_bytes() + b"\0" * (5 * 1024 * 1024)
    assert upload(client, teacher, big).status_code == 413
    assert upload(client, alice, png_bytes()).status_code == 403
    assert client.post("/api/uploads", files={"file": ("a.png", png_bytes(), "image/png")}).status_code == 401


def test_question_images_validated_and_shown(client, teacher, alice):
    url = upload(client, teacher, png_bytes()).json()["url"]
    q0 = dict(SAMPLE_EXAM["questions"][0], images=[
        {"url": url, "alt": " A triangle "}, {"url": "https://cdn.example.org/a.png"}])
    body = {**SAMPLE_EXAM, "questions": [q0] + SAMPLE_EXAM["questions"][1:]}
    e = create(client, teacher, body, open_=True)
    assert e["questions"][0]["images"] == [{"url": url, "alt": "A triangle"},
                                           {"url": "https://cdn.example.org/a.png", "alt": ""}]
    a = client.post(f"/api/student/exams/{e['id']}/start", json={"start_code": e["start_code"]},
                    headers=alice).json()
    assert a["questions"][0]["images"][0]["url"] == url
    gq = client.get(f"/api/exams/{e['id']}/grading", headers=teacher).json()
    assert gq["questions"][0]["images"][0]["alt"] == "A triangle"
    client.post(f"/api/student/attempts/{a['attempt_id']}/submit",
                json={"answers": {}, "client_seq": 1}, headers=alice)
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    client.post(f"/api/exams/{e['id']}/release", json={"force": True}, headers=teacher)
    fb = client.get(f"/api/student/grades/{e['id']}", headers=alice).json()
    assert fb["questions"][0]["images"][0]["url"] == url


@pytest.mark.parametrize("images", [
    [{"url": "http://insecure.example.org/a.png"}],
    [{"url": "javascript:alert(1)"}],
    [{"url": "/api/uploads/../../etc/passwd"}],
    [{"url": "data:image/png;base64,AAAA"}],
    [{"url": "https://x.org/a.png"}] * 7,
    [{"url": "https://x.org/a.png", "alt": "x" * 301}],
])
def test_bad_question_images(client, teacher, images):
    q0 = dict(SAMPLE_EXAM["questions"][0], images=images)
    body = {**SAMPLE_EXAM, "questions": [q0]}
    assert client.post("/api/exams", json=body, headers=teacher).status_code == 422


# ---------------------------------------------------------------- offline packages
def test_package_crypto_roundtrip():
    plain = {"exam_id": "e_1", "questions": [{"id": "q_1", "prompt": "Ünïcode ✓"}]}
    pkg, rel = encrypt_package(plain, "4821", "e_1", iterations=1000)
    assert pkg["alg"] == "AES-GCM" and pkg["exam_id"] == "e_1" and pkg["version"] == 2
    assert set(pkg) == {"exam_id", "version", "alg", "iv", "ciphertext"}  # no salt: no code in it
    assert len(base64.b64decode(pkg["iv"])) == 12
    assert len(base64.b64decode(rel["salt"])) == 16 and rel["iterations"] == 1000
    assert decrypt_package(pkg, rel, "4821") == plain
    with pytest.raises(InvalidTag):
        decrypt_package(pkg, rel, "4822")
    # the key is bound to the exam id (AAD + KDF input)
    with pytest.raises(InvalidTag):
        decrypt_package({**pkg, "exam_id": "e_2"}, {**rel, "exam_id": "e_2"}, "4821")


def test_package_without_key_reveals_nothing_for_any_code():
    """phase 1a: the package alone cannot be brute-forced with the 10 000 start codes."""
    plain = {"exam_id": "e_1", "questions": []}
    pkg, _ = encrypt_package(plain, "4821", "e_1", iterations=1000)
    from app.offline import decrypt_content, derive_kek
    for code in ("4821", "0000", "1234"):
        with pytest.raises(InvalidTag):
            decrypt_content(pkg, derive_kek(code, "e_1", b"\0" * 16, 1000))


def test_package_endpoint(client, teacher, alice):
    url = upload(client, teacher, png_bytes()).json()["url"]
    q0 = dict(SAMPLE_EXAM["questions"][0], images=[{"url": url, "alt": "pic"}])
    draft = create(client, teacher, {**SAMPLE_EXAM, "questions": [q0] + SAMPLE_EXAM["questions"][1:]})
    assert client.get(f"/api/student/exams/{draft['id']}/package", headers=alice).status_code == 409
    assert client.get(f"/api/student/exams/{draft['id']}/package-key", headers=alice).status_code == 409
    e = client.post(f"/api/exams/{draft['id']}/open", headers=teacher).json()
    r = client.get(f"/api/student/exams/{e['id']}/package", headers=alice)
    assert r.status_code == 200, r.text
    pkg = r.json()
    assert set(pkg) == {"exam_id", "version", "alg", "iv", "ciphertext"}
    rel = client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).json()
    assert set(rel) == {"exam_id", "version", "alg", "iterations", "salt", "iv", "wrapped_key",
                        "exit_check"}
    assert set(rel["exit_check"]) == {"alg", "iterations", "salt", "hash"}
    plain = decrypt_package(pkg, rel, e["start_code"])
    assert plain["title"] == "Midterm" and plain["duration_min"] == 30
    assert [q["id"] for q in plain["questions"]] == [q["id"] for q in e["questions"]]
    for q in plain["questions"]:  # no answer keys, no topics
        assert not {"correct_index", "answer_number", "accepted_answers", "topics"} & set(q)
    img = plain["questions"][0]["images"][0]
    assert img["url"].startswith("data:image/png;base64,") and img["alt"] == "pic"
    assert base64.b64decode(img["url"].split(",", 1)[1]) == png_bytes()
    with pytest.raises(InvalidTag):
        decrypt_package(pkg, rel, "0000" if e["start_code"] != "0000" else "1111")
    # cached: same package and key release for the next student
    bob = login(client, "bob")
    assert client.get(f"/api/student/exams/{e['id']}/package", headers=bob).json() == pkg
    assert client.get(f"/api/student/exams/{e['id']}/package-key", headers=bob).json() == rel
    assert client.get(f"/api/student/exams/{e['id']}/package", headers=teacher).status_code == 403
    assert client.get(f"/api/student/exams/{e['id']}/package-key", headers=teacher).status_code == 403
    client.post(f"/api/exams/{e['id']}/close", headers=teacher)
    assert client.get(f"/api/student/exams/{e['id']}/package", headers=alice).status_code == 409
    assert client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).status_code == 409
    # reopening uses a new key: the old package no longer opens with the new release
    e2 = client.post(f"/api/exams/{e['id']}/open", headers=teacher).json()
    rel2 = client.get(f"/api/student/exams/{e['id']}/package-key", headers=alice).json()
    with pytest.raises(InvalidTag):
        decrypt_package(pkg, rel2, e2["start_code"])
    pkg2 = client.get(f"/api/student/exams/{e['id']}/package", headers=alice).json()
    assert decrypt_package(pkg2, rel2, e2["start_code"])["title"] == "Midterm"


def test_package_early_but_key_only_when_live(client, teacher, alice):
    """A package can be fetched before a scheduled exam opens; the key only once it is live."""
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    d = create(client, teacher, {**SAMPLE_EXAM, "scheduled_at": soon})
    r = client.get(f"/api/student/exams/{d['id']}/package", headers=alice)
    assert r.status_code == 200, r.text
    assert client.get(f"/api/student/exams/{d['id']}/package-key", headers=alice).status_code == 409
    e = client.post(f"/api/exams/{d['id']}/open", headers=teacher).json()
    rel = client.get(f"/api/student/exams/{d['id']}/package-key", headers=alice).json()
    assert decrypt_package(r.json(), rel, e["start_code"])["title"] == "Midterm"


def test_package_not_for_website_exams(client, teacher, alice):
    w = create(client, teacher, {**WEBSITE, "delivery": "website"}, open_=True)
    assert client.get(f"/api/student/exams/{w['id']}/package", headers=alice).status_code == 409


def test_inline_images_size_limit(client, teacher):
    url = upload(client, teacher, png_bytes()).json()["url"]
    qs = [{"id": "q_1", "images": [{"url": url, "alt": ""}, {"url": "https://x.org/a.png", "alt": ""}]}]
    small = inline_images(qs)
    assert small[0]["images"][0]["url"].startswith("data:image/png")
    assert small[0]["images"][1]["url"] == "https://x.org/a.png"
    big = inline_images(qs, limit=10)  # over the limit: keep URLs
    assert big[0]["images"][0]["url"] == url
    assert qs[0]["images"][0]["url"] == url  # input untouched


def _start(client, h, e, **extra):
    return client.post(f"/api/student/exams/{e['id']}/start",
                       json={"start_code": e["start_code"], **extra}, headers=h)


def test_offline_started_at(client, teacher, alice, bob, live_exam):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    t = now - timedelta(minutes=10)
    a = _start(client, alice, live_exam, offline_started_at=iso_z(t)).json()
    assert abs((parse(a["started_at"]) - t).total_seconds()) < 2
    assert abs((parse(a["deadline_at"]) - (t + timedelta(minutes=30))).total_seconds()) < 2
    # resume ignores a new offline time
    again = _start(client, alice, live_exam, offline_started_at=iso_z(now - timedelta(minutes=20))).json()
    assert again["started_at"] == a["started_at"]
    # older than 30 minutes is clamped to 30 minutes
    b = _start(client, bob, live_exam, offline_started_at=iso_z(now - timedelta(hours=2))).json()
    assert abs((parse(b["started_at"]) - (now - timedelta(minutes=30))).total_seconds()) < 5
    # answers sync after an offline start
    qid = live_exam["questions"][0]["id"]
    r = client.put(f"/api/student/attempts/{a['attempt_id']}/answers",
                   json={"answers": {qid: 1}, "client_seq": 3}, headers=alice)
    assert r.json()["saved"] is True
    # bob's clamped 30 minutes are used up: saving is closed, the submit still lands (late)
    r = client.put(f"/api/student/attempts/{b['attempt_id']}/answers",
                   json={"answers": {qid: 1}, "client_seq": 3}, headers=bob)
    assert r.status_code == 409
    r = client.post(f"/api/student/attempts/{b['attempt_id']}/submit",
                    json={"answers": {qid: 1}, "client_seq": 3}, headers=bob)
    assert r.status_code == 200 and r.json()["receipt"]
    carol = login(client, "carol")
    c = _start(client, carol, live_exam,
               offline_started_at=iso_z(now + timedelta(minutes=5))).json()  # future -> now
    assert abs((parse(c["started_at"]) - now).total_seconds()) < 5
    # wrong code still rejected
    r = client.post(f"/api/student/exams/{live_exam['id']}/start",
                    json={"start_code": "x", "offline_started_at": iso_z(now)}, headers=carol)
    assert r.status_code == 403
