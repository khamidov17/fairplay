"""Audit 2026-10-12 L-03 / L-05: hostile scan uploads."""
import io
import struct
import zlib

import pymupdf
import pytest
from PIL import Image

from app.scan import pipeline

from .test_scan import PAPER_EXAM, _wait, ai_off  # noqa: F401


def _png_header_bomb(w: int, h: int) -> bytes:
    """A tiny PNG whose header claims w x h pixels (decoding would need w*h*3 bytes)."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = zlib.compress(b"\x00" * 64)          # far too little data: never decoded anyway
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def test_image_bomb_is_refused_from_the_header(tmp_path):
    p = tmp_path / "bomb.png"
    p.write_bytes(_png_header_bomb(30000, 30000))
    with pytest.raises(pipeline.SourceError, match="too large"):
        pipeline.source_page_count(str(p))
    res = pipeline.process_source_page(str(p), 0, "e_x", {"pages": []})
    assert res["status"] == "unmatched" and "Could not read" in res["reason"]


def test_big_but_real_photo_is_decoded_reduced(tmp_path):
    p = tmp_path / "photo.jpg"
    Image.new("RGB", (12000, 6000), (240, 240, 240)).save(p, quality=50)   # 72 MP
    img = pipeline.load_page(str(p), 0)
    assert img.shape[0] * img.shape[1] <= pipeline.MAX_IMAGE_PIXELS


def test_pdf_limits(tmp_path, monkeypatch):
    enc = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(enc), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    with pytest.raises(pipeline.SourceError, match="password"):
        pipeline.source_page_count(str(enc))
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\n1 0 obj <<")
    with pytest.raises(pipeline.SourceError):
        pipeline.source_page_count(str(broken))
    monkeypatch.setattr(pipeline, "MAX_PDF_PAGES", 3)
    many = tmp_path / "many.pdf"
    d = pymupdf.open()
    for _ in range(4):
        d.new_page()
    d.save(str(many))
    with pytest.raises(pipeline.SourceError, match="at most 3"):
        pipeline.source_page_count(str(many))


def test_one_bad_file_does_not_fail_the_batch(client, teacher, ai_off):  # noqa: F811
    import os
    from app.scan.storage import sources_dir
    r = client.post("/api/exams", json=PAPER_EXAM, headers=teacher)
    e = r.json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice"]}, headers=teacher)
    assert client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher).status_code == 200
    good = pymupdf.open()
    good.new_page()
    buf = io.BytesIO()
    good.save(buf)
    r = client.post(f"/api/exams/{e['id']}/scans", headers=teacher, files=[
        ("files", ("good.pdf", buf.getvalue(), "application/pdf")),
        ("files", ("bad.pdf", b"%PDF-1.7\n garbage", "application/pdf")),
        ("files", ("bomb.png", _png_header_bomb(30000, 30000), "image/png"))])
    assert r.status_code == 202, r.text
    body = _wait(client, teacher, e["id"])
    b = body["batches"][0]
    assert b["status"] == "done", b
    um = client.get(f"/api/exams/{e['id']}/scans/unmatched", headers=teacher).json()
    reasons = " ".join(pg.get("reason") or "" for pg in um)
    assert "bad.pdf" in reasons and "bomb.png" in reasons, um
    assert not [f for f in os.listdir(sources_dir()) if f.startswith(b["batch_id"])]  # sources removed


# ---------------------------------------------------------------- audit L-18
def test_scan_images_need_an_expiring_signature(client, teacher):
    import io as _io
    import re
    import time
    from PIL import Image as _Image
    from app import uploads
    from app.scan.storage import save_file
    buf = _io.BytesIO()
    _Image.new("RGB", (4, 4), (0, 0, 0)).save(buf, format="PNG")
    url = save_file(buf.getvalue())
    uid = url.rsplit("/", 1)[1]
    assert uid.startswith("p") and len(uid) == 33
    assert client.get(url).status_code == 403                      # no signature: refused
    secret = "test-secret-0123456789abcdef"
    signed = uploads.sign_url(secret, uid)
    r = client.get(signed)
    assert r.status_code == 200 and r.headers["cache-control"].startswith("private")
    expired = uploads.sign_url(secret, uid, now=time.time() - 3 * 3600)
    assert client.get(expired).status_code == 403
    forged = re.sub(r"sig=[0-9a-f]+", "sig=" + "0" * 32, signed)
    assert client.get(forged).status_code == 403
    # ordinary question images stay public and long-cached
    pub = client.post("/api/uploads", headers=teacher, files={"file": ("a.png", buf.getvalue(), "image/png")}).json()
    r = client.get(pub["url"])
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]


def test_api_answers_get_signed_links():
    import asyncio
    import json as _json
    from types import SimpleNamespace
    from app.http_guard import SignPrivateUploads
    uid = "p" + "a" * 32

    async def app(scope, receive, send):
        body = _json.dumps({"crop": f"/api/uploads/{uid}", "pub": "/api/uploads/" + "b" * 32}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
    out = []

    async def send(msg):
        out.append(msg)

    scope = {"type": "http", "path": "/api/exams/x/scan/queue",
             "app": SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(secret="s" * 20)))}
    asyncio.run(SignPrivateUploads(app)(scope, None, send))
    data = _json.loads(out[1]["body"])
    assert data["crop"].startswith(f"/api/uploads/{uid}?exp=") and "&sig=" in data["crop"]
    assert data["pub"] == "/api/uploads/" + "b" * 32
    assert dict(out[0]["headers"])[b"content-length"] == str(len(out[1]["body"])).encode()


# ---------------------------------------------------------------- audit L-06
def _queued_batch(client, teacher, monkeypatch, pages=2):
    """Upload scans but keep the batch from starting (as if the server died right away)."""
    from app import routes_scan
    r = client.post("/api/exams", json=PAPER_EXAM, headers=teacher)
    e = r.json()
    client.put(f"/api/exams/{e['id']}/roster", json={"usernames": ["alice"]}, headers=teacher)
    assert client.get(f"/api/exams/{e['id']}/print.pdf", headers=teacher).status_code == 200
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    buf = io.BytesIO()
    doc.save(buf)
    started = []
    monkeypatch.setattr(routes_scan, "_spawn", lambda coro: (started.append(1), coro.close()))
    r = client.post(f"/api/exams/{e['id']}/scans", headers=teacher,
                    files=[("files", ("scan.pdf", buf.getvalue(), "application/pdf"))])
    assert r.status_code == 202 and started
    monkeypatch.undo()
    return e, r.json()["batch_id"]


def _db(client, fn):
    from app.db import get_database

    async def go():
        async with get_database().sessionmaker() as s:
            out = await fn(s)
            await s.commit()
            return out
    return client.portal.call(go)


def test_a_batch_left_behind_by_a_restart_is_resumed(client, teacher, ai_off, monkeypatch):  # noqa: F811
    from datetime import timedelta
    from app import routes_scan
    from app.scan.models import ScanBatch, ScanPage
    from app.timeutil import utcnow
    e, bid = _queued_batch(client, teacher, monkeypatch)

    async def age(s):
        b = await s.get(ScanBatch, bid)
        b.status, b.source_counts = "reading", [2]
        b.created_at = utcnow() - timedelta(minutes=10)
        b.heartbeat_at = utcnow() - timedelta(minutes=5)       # its worker died
        # page 0 was already stored before the crash
        s.add(ScanPage(batch_id=bid, exam_id=e["id"], seq=0, status="ignored", reason="kept",
                       qr_found=0, rotation=0, crops={}, created_at=utcnow()))
    _db(client, age)
    listed = client.get(f"/api/exams/{e['id']}/scans", headers=teacher).json()["batches"][0]
    assert listed["status"] == "stalled"
    assert client.portal.call(routes_scan.sweep_batches) == 1
    b = _wait(client, teacher, e["id"])["batches"][0]
    assert b["status"] == "done", b

    async def pages(s):
        from sqlalchemy import select
        return [(p.seq, p.status) for p in (await s.execute(
            select(ScanPage).where(ScanPage.batch_id == bid).order_by(ScanPage.seq))).scalars()]
    got = _db(client, pages)
    assert [sq for sq, _ in got] == [0, 1] and got[0][1] == "ignored"   # page 0 not read twice


def test_a_live_batch_is_not_taken_twice(client, teacher, ai_off, monkeypatch):  # noqa: F811
    from datetime import timedelta
    from app import routes_scan
    from app.scan.models import ScanBatch
    from app.timeutil import utcnow
    e, bid = _queued_batch(client, teacher, monkeypatch)

    async def busy(s):
        b = await s.get(ScanBatch, bid)
        b.status, b.created_at, b.heartbeat_at = "reading", utcnow() - timedelta(minutes=10), utcnow()
    _db(client, busy)
    assert client.portal.call(routes_scan.sweep_batches) == 0
    assert client.portal.call(routes_scan._claim, bid) is False
    r = client.post(f"/api/exams/{e['id']}/scans/{bid}/rerun-ai", headers=teacher)
    assert r.status_code == 409


def test_a_lost_source_file_is_reported_not_crashed(client, teacher, ai_off, monkeypatch):  # noqa: F811
    import os
    from datetime import timedelta
    from app import routes_scan
    from app.scan.models import ScanBatch
    from app.timeutil import utcnow
    e, bid = _queued_batch(client, teacher, monkeypatch, pages=1)

    async def lose(s):
        b = await s.get(ScanBatch, bid)
        for pth in b.sources:
            os.remove(pth)
        b.created_at = utcnow() - timedelta(minutes=10)
    _db(client, lose)
    assert client.portal.call(routes_scan.sweep_batches) == 1
    b = _wait(client, teacher, e["id"])["batches"][0]
    assert b["status"] == "done", b
    um = client.get(f"/api/exams/{e['id']}/scans/unmatched", headers=teacher).json()
    assert any("upload it again" in (pg.get("reason") or "") for pg in um), um
