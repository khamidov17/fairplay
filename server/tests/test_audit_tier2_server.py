"""Audit 2026-10-12 tier 2 (server): S-06 events, S-07 start/close race, S-08 Redis,
S-09 single-use proof, S-12 exit-code input, S-13 beats after release, S-14 bounds."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import routes_student, routes_teacher
from app.config import Settings
from app.main import create_app
from app.services import exam_key
from app.store import MemoryStore, get_store

from .conftest import SAMPLE_EXAM, login
from .test_app_session import DEV_A, DEV_B, KEY, app_client, beat, exam, proof, start  # noqa: F401


def _start_browser(client, h, e):
    return client.post(f"/api/student/exams/{e['id']}/start", headers=h,
                       json={"start_code": e["start_code"]}).json()


def _post_events(client, h, aid, types):
    return client.post(f"/api/student/attempts/{aid}/events", headers=h,
                       json={"events": [{"type": t, "detail": "d", "client_time": "t"} for t in types]})


def _event_types(client, teacher, exam_id):
    res = client.get(f"/api/exams/{exam_id}/results", headers=teacher).json()
    return [ev["type"] for a in res["attempts"] for ev in a["events"]]


# ---------------------------------------------------------------- S-06
def test_client_cannot_post_server_event_types(client, teacher, alice, live_exam):
    a = _start_browser(client, alice, live_exam)
    _post_events(client, alice, a["attempt_id"], ["emergency_exit", "device_bound", "focus_lost", "made_up"])
    types = _event_types(client, teacher, live_exam["id"])
    assert "emergency_exit" not in types and "device_bound" not in types
    assert {"client_emergency_exit", "client_device_bound", "focus_lost", "client_made_up"} <= set(types)


def test_event_requests_are_rate_limited(client, alice, live_exam, monkeypatch):
    monkeypatch.setattr(routes_student, "EVENT_REQUESTS_PER_MIN", 3)
    a = _start_browser(client, alice, live_exam)
    codes = [_post_events(client, alice, a["attempt_id"], ["focus_lost"]).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_events_per_attempt_are_capped(client, teacher, alice, live_exam, monkeypatch):
    monkeypatch.setattr(routes_student, "MAX_EVENTS_PER_ATTEMPT", 5)
    a = _start_browser(client, alice, live_exam)
    assert _post_events(client, alice, a["attempt_id"], ["focus_lost"] * 3).json()["stored"] == 3
    assert _post_events(client, alice, a["attempt_id"], ["focus_lost"] * 3).json()["stored"] == 2
    assert _post_events(client, alice, a["attempt_id"], ["focus_lost"]).json()["stored"] == 0
    types = _event_types(client, teacher, live_exam["id"])
    assert types.count("focus_lost") == 5 and "event_limit_reached" in types


def test_events_closed_after_hand_in(client, alice, live_exam, monkeypatch):
    a = _start_browser(client, alice, live_exam)
    client.post(f"/api/student/attempts/{a['attempt_id']}/submit", headers=alice,
                json={"answers": {}, "client_seq": 1})
    assert _post_events(client, alice, a["attempt_id"], ["focus_lost"]).status_code == 200  # within 10 min
    monkeypatch.setattr(routes_student, "EVENTS_AFTER_SUBMIT", timedelta(seconds=-1))
    assert _post_events(client, alice, a["attempt_id"], ["focus_lost"]).status_code == 409


def test_results_list_newest_events_and_paginate(client, teacher, alice, live_exam, monkeypatch):
    monkeypatch.setattr(routes_teacher, "RESULTS_EVENTS_PER_ATTEMPT", 2)
    a = _start_browser(client, alice, live_exam)
    _post_events(client, alice, a["attempt_id"], ["focus_lost", "paste", "copy", "blocked_shortcut"])
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    att = res["attempts"][0]
    assert att["events_total"] == 4 and [e["type"] for e in att["events"]] == ["copy", "blocked_shortcut"]
    assert att["flag_count"] == 4
    url = f"/api/exams/{live_exam['id']}/attempts/{a['attempt_id']}/events"
    page = client.get(url, params={"offset": 1, "limit": 2}, headers=teacher).json()
    assert page["total"] == 4 and [e["type"] for e in page["events"]] == ["paste", "copy"]
    bob = login(client, "bob")
    assert client.get(url, headers=bob).status_code in (401, 403)


# ---------------------------------------------------------------- S-07
def test_start_refused_when_cache_still_says_live(client, teacher, alice, bob, live_exam):
    _start_browser(client, alice, live_exam)          # warms the exam cache
    cached = client.portal.call(get_store().get, exam_key(live_exam["id"]))
    client.post(f"/api/exams/{live_exam['id']}/close", headers=teacher)
    # a worker with a stale cache: the exam still looks live there
    client.portal.call(get_store().set, exam_key(live_exam["id"]), cached, 120)
    assert json.loads(cached)["status"] == "live"
    r = client.post(f"/api/student/exams/{live_exam['id']}/start", headers=bob,
                    json={"start_code": live_exam["start_code"]})
    assert r.status_code == 409
    res = client.get(f"/api/exams/{live_exam['id']}/results", headers=teacher).json()
    assert all(x["status"] == "submitted" for x in res["attempts"])


# ---------------------------------------------------------------- S-08
def _plain(tmp_path, static_dir, **kw):
    return Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}", redis_url=None,
                    secret="test-secret-0123456789abcdef", pbkdf2_iterations=1000,
                    require_app=False, app_keys=["k"], dev=False, static_dir=str(static_dir),
                    uploads_dir=str(tmp_path / "up"), **kw)


@pytest.mark.parametrize("kw", [{}, {"single_process": True, "workers": 2}])
def test_real_server_needs_redis(tmp_path, static_dir, kw):
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        with TestClient(create_app(_plain(tmp_path, static_dir, **kw))):
            pass


def test_single_process_without_redis_is_allowed(tmp_path, static_dir):
    with TestClient(create_app(_plain(tmp_path, static_dir, single_process=True))) as c:
        assert c.get("/api/health").status_code == 200


# ---------------------------------------------------------------- S-09
def test_memory_getdel():
    import asyncio
    s = MemoryStore()

    async def go():
        await s.set("k", "v", 10)
        return await s.getdel("k"), await s.getdel("k")
    assert asyncio.run(go()) == ("v", None)


def test_one_proof_starts_once(app_client, exam):  # noqa: F811
    h = login(app_client, "alice")
    p = proof(app_client, h, exam["id"], DEV_A)
    body = {"start_code": exam["start_code"], "client": p}
    url = f"/api/student/exams/{exam['id']}/start"
    assert app_client.post(url, headers=h, json=body).status_code == 200
    assert app_client.post(url, headers=h, json=body).status_code == 403


# ---------------------------------------------------------------- S-12
def test_exit_code_non_ascii_is_a_wrong_code(app_client, exam):  # noqa: F811
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    r = app_client.post(f"/api/student/attempts/{a['attempt_id']}/exit-code",
                        headers={**h, "X-FairPlay-Session": a["session"]}, json={"code": "äöü"})
    assert r.status_code == 403


# ---------------------------------------------------------------- S-13
def test_new_mac_with_slower_clock_is_not_rejected(app_client, exam):  # noqa: F811
    t = login(app_client, "teacher")
    h = login(app_client, "alice")
    a = start(app_client, h, exam).json()
    assert beat(app_client, h, a["attempt_id"], a["session"], 10_000_000).status_code == 200
    app_client.post(f"/api/exams/{exam['id']}/attempts/{a['attempt_id']}/release-device", headers=t)
    b = start(app_client, h, exam, device=DEV_B).json()
    # the replacement Mac's clock is 3 minutes behind the old one
    assert beat(app_client, h, b["attempt_id"], b["session"], 10_000_000 - 180_000,
                device=DEV_B).status_code == 200
    # still single-use within a session
    assert beat(app_client, h, b["attempt_id"], b["session"], 10_000_000 - 180_000,
                device=DEV_B).status_code == 409


# ---------------------------------------------------------------- S-14
@pytest.mark.parametrize("seq", [2 ** 63, -5])
def test_client_seq_bounds(client, alice, live_exam, seq):
    a = _start_browser(client, alice, live_exam)
    r = client.put(f"/api/student/attempts/{a['attempt_id']}/answers", headers=alice,
                   json={"answers": {}, "client_seq": seq})
    assert r.status_code == 422


def test_current_question_bounds(client, alice, live_exam):
    a = _start_browser(client, alice, live_exam)
    r = client.post(f"/api/student/attempts/{a['attempt_id']}/heartbeat", headers=alice,
                    json={"current_question": -1})
    assert r.status_code == 422


def test_start_after_extend_uses_the_new_duration_even_with_a_stale_cache(client, teacher, alice, bob, live_exam):
    """T-02: extend vs start -- a worker whose exam cache predates "Add 10 min"."""
    from datetime import datetime
    _start_browser(client, alice, live_exam)                        # warms the cache (30 min)
    cached = client.portal.call(get_store().get, exam_key(live_exam["id"]))
    client.post(f"/api/exams/{live_exam['id']}/extend", headers=teacher, json={"minutes": 10})
    client.portal.call(get_store().set, exam_key(live_exam["id"]), cached, 120)   # stale worker
    b = _start_browser(client, bob, live_exam)
    mins = (datetime.fromisoformat(b["deadline_at"].replace("Z", "")) -
            datetime.fromisoformat(b["started_at"].replace("Z", ""))).total_seconds() / 60
    assert round(mins) == 40
