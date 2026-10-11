"""Phase 2 (e): phone photos — page detection, perspective flattening, QR matching on photos,
HEIC handling, and the phone capture link (token expiry, scope, wrong exam)."""
from datetime import timedelta

import cv2
import numpy as np
import pytest

from app import ai
from app.scan import photo
from app.scan.print import build_layout

from .test_scan import PAPER_EXAM, _boxes, _fill_bubble, _render, _wait, _write


@pytest.fixture(autouse=True)
def ai_off(monkeypatch):
    monkeypatch.setenv("FAIRPLAY_AI_PROVIDER", "none")
    monkeypatch.delenv("FAIRPLAY_AI_KEY", raising=False)
    yield
    ai.set_transport(None)


def fake_photo(page: np.ndarray, angle: float = 9.0, seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Put a page on a dark desk with perspective, rotation and uneven light.
    Returns (photo, true corners TL TR BR BL)."""
    rng = np.random.default_rng(seed)
    h, w = page.shape[:2]
    W, H = 2200, 2900
    desk = np.full((H, W, 3), (60, 52, 45), np.uint8)
    desk = cv2.add(desk, rng.integers(0, 25, desk.shape, dtype=np.uint8))
    cx, cy = W / 2, H / 2
    sw, sh = w * 0.95, h * 0.95
    base = np.array([[-sw / 2, -sh / 2], [sw / 2, -sh / 2 + 60], [sw / 2 - 40, sh / 2], [-sw / 2 + 70, sh / 2 - 30]],
                    np.float32)  # trapezoid = perspective
    a = np.deg2rad(angle)
    rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]], np.float32)
    dst = (base @ rot.T + [cx, cy]).astype(np.float32)
    src = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(page, M, (W, H), borderValue=(0, 0, 0))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (W, H))
    out = desk.copy()
    out[mask > 0] = warped[mask > 0]
    # lighting gradient (darker bottom-right corner)
    yy, xx = np.mgrid[0:H, 0:W]
    shade = (1.0 - 0.25 * (xx / W + yy / H) / 2).astype(np.float32)
    out = np.clip(out.astype(np.float32) * shade[..., None], 0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0)
    return out, dst


def test_find_and_flatten_page_on_synthetic_photo():
    page = np.full((1600, 1131, 3), 255, np.uint8)
    cv2.rectangle(page, (100, 100), (1031, 300), (0, 0, 0), 4)
    cv2.putText(page, "TOP", (480, 220), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 0), 8)
    img, truth = fake_photo(page, angle=12)
    quad = photo.find_page_quad(img)
    assert quad is not None
    err = np.abs(quad - photo.order_corners(truth)).max()
    assert err < 25, (quad, truth)
    flat = photo.rectify(img)
    h, w = flat.shape[:2]
    assert abs(h / w - photo.A4_RATIO) < 0.02  # snapped to A4 portrait
    gray = cv2.cvtColor(flat, cv2.COLOR_BGR2GRAY)
    # the heavy rectangle is near the top, upright: dark rows in the top fifth, white bottom
    top, bottom = gray[: h // 5], gray[h // 2:]
    assert (top < 100).mean() > 0.01 and (bottom < 100).mean() < 0.002
    assert gray[h // 2:, :].mean() > 225  # shadows removed: paper is white everywhere
    # an image that is already just the page (a flatbed scan) is left alone
    assert photo.find_page_quad(page) is None and photo.rectify(page) is None
    # not a page at all
    assert photo.rectify(np.full((800, 600, 3), 40, np.uint8)) is None


def test_heic_sniff():
    assert photo.is_heic(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00")
    assert photo.is_heic(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00")
    assert not photo.is_heic(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01")


@pytest.fixture
def paper(client, teacher):
    e = client.post("/api/exams", json=PAPER_EXAM, headers=teacher).json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice", "bob"]}, headers=teacher)
    return e


def _photo_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


def test_phone_capture_flow_matches_qr_on_photos(client, teacher, paper, bob):
    e = paper
    q = [x["id"] for x in e["questions"]]
    # capture needs the printed layout first (like uploads)
    assert client.post(f"/api/exams/{e['id']}/capture", json={}, headers=teacher).status_code == 409
    pdf = client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher).content
    layout, _ = build_layout(e["id"], e["questions"])
    pages = _render(pdf)
    p1 = _boxes(layout, 1)
    alice1 = pages[0].copy()
    _fill_bubble(alice1, p1[q[0]], 1)
    _write(alice1, p1[q[1]], "3.14")

    r = client.post(f"/api/exams/{e['id']}/capture", json={}, headers=teacher)
    assert r.status_code == 200, r.text
    link = r.json()
    token = link["token"]
    assert link["path"] == f"/teacher/capture/{token}" and link["url"].endswith(link["path"])
    assert link["qr_svg"].lstrip().startswith("<") and "svg" in link["qr_svg"][:200]
    # the phone needs no login
    info = client.get(f"/api/capture/{token}")
    assert info.status_code == 200, info.text
    assert info.json()["exam_title"] == "Paper Midterm" and info.json()["photos"] == 0
    assert client.get("/api/capture/not-a-real-token-123456").status_code == 404

    photo1, _ = fake_photo(alice1, angle=7)
    r = client.post(f"/api/capture/{token}/photos",
                    files=[("files", ("IMG_0001.jpg", _photo_bytes(photo1), "image/jpeg"))])
    assert r.status_code == 202, r.text
    st = _wait(client, teacher, e["id"])
    b = st["batches"][0]
    assert b["status"] == "done" and b["matched"] == 1, b
    res = client.get(f"/api/exams/{e['id']}/results", headers=teacher).json()
    al = next(a for a in res["attempts"] if a["username"] == "alice")
    ans = {a["question_id"]: a for a in al["answers"]}
    assert ans[q[0]]["given"] == 1 and ans[q[0]]["points_awarded"] == 2  # bubble read on a photo
    assert client.get(f"/api/capture/{token}").json()["photos"] == 1

    # not images -> 422; too many files -> 422
    r = client.post(f"/api/capture/{token}/photos", files=[("files", ("x.pdf", b"%PDF-1.4 x", "application/pdf"))])
    assert r.status_code == 422
    # HEIC without pillow-heif: a clear error (or accepted when the server has it)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    r = client.post(f"/api/capture/{token}/photos", files=[("files", ("IMG.HEIC", heic, "image/heic"))])
    if photo.heic_supported():
        assert r.status_code in (202, 422)
    else:
        assert r.status_code == 422 and "HEIC" in r.json()["detail"]

    # scope: a token only works for its exam; other teachers' exams are 404; students cannot create links
    other = client.post("/api/exams", json=dict(PAPER_EXAM, title="Other"), headers=teacher).json()
    assert client.post(f"/api/exams/{other['id']}/capture", json={}, headers=bob).status_code == 403
    r = client.post(f"/api/capture/{token}/photos?exam_id={other['id']}",
                    files=[("files", ("a.jpg", _photo_bytes(photo1), "image/jpeg"))])
    assert r.status_code == 202
    _wait(client, teacher, e["id"])
    assert client.get(f"/api/exams/{other['id']}/scans", headers=teacher).json()["batches"] == []

    # expiry
    from app.scan import capture
    capture.set_clock(lambda: capture.real_now() + timedelta(minutes=31))
    try:
        assert client.get(f"/api/capture/{token}").status_code == 410
        r = client.post(f"/api/capture/{token}/photos", files=[("files", ("a.jpg", _photo_bytes(photo1), "image/jpeg"))])
        assert r.status_code == 410
    finally:
        capture.set_clock(None)
    # revoke
    assert client.post(f"/api/exams/{e['id']}/capture/revoke", headers=teacher).json() == {"revoked": 1}
    assert client.get(f"/api/capture/{token}").status_code == 410


def test_upload_photo_of_booklet_via_normal_upload(client, teacher, paper):
    """A teacher uploads phone photos (JPEG) through the normal upload: flattened + matched."""
    e = paper
    pdf = client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher).content
    pages = _render(pdf)
    shots = [_photo_bytes(fake_photo(pg, angle=a, seed=i)[0]) for i, (pg, a) in enumerate(zip(pages, (5, -8, 11, 3)))]
    r = client.post(f"/api/exams/{e['id']}/scans", headers=teacher,
                    files=[("files", (f"p{i}.jpg", b, "image/jpeg")) for i, b in enumerate(shots)])
    assert r.status_code == 202, r.text
    st = _wait(client, teacher, e["id"])
    assert st["batches"][0]["matched"] == 4, st
    assert st["students_scanned"] == 2
