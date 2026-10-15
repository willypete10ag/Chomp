"""The Workouts page (#94): importing a Hevy export, the list, a workout's sets, deleting."""

from __future__ import annotations

import test_web
from conftest import FIXTURES
from test_web import ORIGIN, sign_in
from test_web_edits import follow

from chomp import hevy, store

public_settings = test_web.public_settings
http = test_web.http
CSV = (FIXTURES / "hevy" / "workouts.csv").read_bytes()


def upload(http, data: bytes):
    return http.post(
        "/app/workouts/import",
        files={"csv": ("workout_data.csv", data, "text/csv")},
        headers=ORIGIN,
    )


def test_empty_page_points_to_the_import(http):
    sign_in(http)
    page = http.get("/app/progress/training").text
    assert 'href="/app/progress/training" aria-current=page>Training<' in page
    assert "No workouts yet." in page and 'href="/app/you/integrations#hevy"' in page
    page = http.get("/app/you/integrations").text
    assert 'id="hevy"' in page and 'action="/app/workouts/import"' in page
    assert 'enctype="multipart/form-data"' in page


def test_import_list_and_workout_page(http, conn):
    sign_in(http)
    r = upload(http, CSV)
    assert r.headers["location"].startswith("/app/you/integrations?hevy=")
    page = follow(http, r)
    assert "Imported 2 new workouts." in page
    assert 'href="/app/progress/training?days=365">See it' in page
    page = http.get("/app/progress/training?days=365").text.replace("\xa0", " ")
    assert "Push Day" in page and "Legs" in page
    assert "1 h 12 min · 3 exercises · 3,155 lb lifted" in page
    page = follow(http, upload(http, CSV))
    assert "Imported 0 new workouts (2 already here)." in page

    push = next(w for w in store.workouts_between(conn, "2026", "2027") if w["title"] == "Push Day")
    page = http.get(f"/app/workouts/{push['id']}").text.replace("\xa0", " ")
    assert "<h1>Push Day</h1>" in page and "Felt strong" in page
    assert "Monday, October 12, 6:30 PM" in page
    assert '<span class="setno warmup" title="warmup">W</span> 95 lb &times; 10' in page
    assert '<span class="setno normal" title="normal">1</span> 185 lb &times; 6' in page
    assert '<span class="setno failure" title="failure">F</span>' in page
    assert "RPE 8.5" in page and "Rope" in page and "20 reps" in page
    legs = next(w for w in store.workouts_between(conn, "2026", "2027") if w["title"] == "Legs")
    assert "1.25 mi · 20:15" in http.get(f"/app/workouts/{legs['id']}").text.replace("\xa0", " ")
    assert http.get("/app/workouts/9999").status_code == 404

    r = http.post(f"/app/workouts/{push['id']}/delete", headers=ORIGIN)
    assert r.headers["location"] == "/app/progress/training?removed=1"
    assert "Workout deleted." in follow(http, r)
    assert [w["title"] for w in store.workouts_between(conn, "2026", "2027")] == ["Legs"]


def test_bad_uploads_are_refused(http, conn, monkeypatch):
    sign_in(http)
    page = follow(http, upload(http, b"date,weight\n2026-10-01,180\n"))
    assert "a Hevy workout export. In Hevy: Profile &gt; Settings" in page
    monkeypatch.setattr("chomp.web.HEVY_MAX", 100)
    page = follow(http, upload(http, CSV))
    assert "too big" in page
    monkeypatch.setattr("chomp.web.HEVY_MAX", 20 * 1024 * 1024)
    page = follow(http, upload(http, CSV.replace(b'"normal",225,5', b'"normal",x,5')))
    assert "Imported 1 new workout (1 skipped because a row couldn" in page
    assert [w["title"] for w in store.workouts_between(conn, "2026", "2027")] == ["Push Day"]
    conn.execute("DELETE FROM workouts")
    no_origin = http.post("/app/workouts/import", files={"csv": ("w.csv", CSV, "text/csv")})
    assert no_origin.status_code == 403
    assert store.workouts_between(conn, "2026", "2027") == []
    assert hevy.parse(CSV, "America/New_York").workouts  # the fixture itself is fine
