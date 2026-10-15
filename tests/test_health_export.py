"""Apple Health's Export All Health Data (#94): past weigh-ins and daily activity from
export.zip, read by `chomp import-health`."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
import test_web
from test_web import ORIGIN, sign_in
from test_web_edits import follow

from chomp import cli, health_export, logic, store

public_settings = test_web.public_settings
http = test_web.http

TZ = "America/New_York"
HEAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [
<!-- HealthKit Export Version: 14 -->
<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout)*)>
<!ATTLIST HealthData locale CDATA #REQUIRED>
<!ATTLIST Record type CDATA #REQUIRED unit CDATA #IMPLIED>
<!ATTLIST Record type CDATA #REQUIRED>
]>
<HealthData locale="en_US">
 <ExportDate value="2026-10-14 23:10:00 -0400"/>
 <Me HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>
"""


def record(kind, value, start, unit="count", source="Will's iPhone"):
    return (
        f' <Record type="HKQuantityTypeIdentifier{kind}" sourceName="{source}" unit="{unit}"'
        f' creationDate="{start}" startDate="{start}" endDate="{start}" value="{value}">\n'
        '  <MetadataEntry key="HKWasUserEntered" value="0"/>\n'
        " </Record>\n"
    )


def export_xml(*records: str) -> bytes:
    return HEAD + "".join(records).encode() + b"</HealthData>\n"


XML = export_xml(
    record("BodyMass", "166.3", "2026-10-06 19:19:00 -0400", "lb", "VeSync"),
    record("BodyMass", "75", "2026-09-01 07:00:00 -0400", "kg", "Health"),
    # The same walk counted by the iPhone and the Watch: the day keeps the larger
    record("StepCount", "4000", "2026-10-12 08:00:00 -0400"),
    record("StepCount", "3000", "2026-10-12 18:00:00 -0400"),
    record("StepCount", "7500", "2026-10-12 08:00:00 -0400", source="Will's Apple Watch"),
    # Late evening in New York is the next day in UTC, but stays on its local day
    record("StepCount", "400", "2026-10-12 23:30:00 -0400"),
    record("ActiveEnergyBurned", "412.5", "2026-10-12 19:00:00 -0400", "Cal"),
    record("DistanceWalkingRunning", "2", "2026-10-12 08:00:00 -0400", "mi"),
    record("AppleExerciseTime", "34", "2026-10-11 08:00:00 -0400", "min"),
    record("FlightsClimbed", "7", "2026-10-11 08:00:00 -0400"),
    record("HeartRate", "61", "2026-10-11 08:00:00 -0400", "count/min"),
    record("StepCount", "lots", "2026-10-10 08:00:00 -0400"),
    record("StepCount", "10", "2026-10-10 08:00:00 -0400", "furlongs"),
    # A correlation repeats records already listed at the top level
    ' <Correlation type="HKCorrelationTypeIdentifierFood" startDate="2026-10-12 12:00:00 -0400">'
    + record("StepCount", "99999", "2026-10-12 12:00:00 -0400")
    + " </Correlation>\n",
)


def day(d: str) -> str:
    return logic.local_day_bounds(date.fromisoformat(d), TZ)[0]


def zipped(xml: bytes = XML) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("apple_health_export/export_cda.xml", b"<ClinicalDocument/>")
        z.writestr("apple_health_export/export.xml", xml)
    return out.getvalue()


def test_reads_weigh_ins_and_daily_totals():
    parsed = health_export.read_bytes(zipped(), TZ)
    assert parsed.weights == {
        "2026-10-06T23:19:00Z": pytest.approx(166.3 * logic.KG_PER_LB),
        "2026-09-01T11:00:00Z": 75,
    }
    assert parsed.days == {
        ("steps", day("2026-10-12")): 7500,  # the Watch's 7500 over the iPhone's 4000+3000+400
        ("active_kcal", day("2026-10-12")): 412.5,
        ("distance_m", day("2026-10-12")): pytest.approx(2 * logic.M_PER_MILE),
        ("exercise_min", day("2026-10-11")): 34,
        ("flights", day("2026-10-11")): 7,
    }
    assert parsed.rejected == 2 and parsed.problems == [
        "steps: could not convert string to float: 'lots'",
        "steps: unknown unit 'furlongs'",
    ]
    # The plain XML reads the same as the zip
    assert health_export.read_bytes(XML, TZ).days == parsed.days


def test_rejects_what_is_not_a_health_export():
    with pytest.raises(health_export.ExportError, match="no export"):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("workout_data.csv", "title\n")
        health_export.read_bytes(out.getvalue(), TZ)
    with pytest.raises(health_export.ExportError, match="expected <HealthData>"):
        health_export.read_bytes(b"<plist/>", TZ)
    with pytest.raises(health_export.ExportError, match="not a readable"):
        health_export.read_bytes(b"<HealthData><Record", TZ)
    huge = export_xml(record("StepCount", "300000", "2026-10-12 08:00:00 -0400"))
    assert health_export.read_bytes(huge, TZ).days == {}


def test_import_keeps_days_the_phone_sent(conn):
    store.add_activity(conn, [("steps", "day", day("2026-10-12"), 9963)])
    store.add_activity(conn, [("active_kcal", "part", "2026-10-12T22:00:00Z", 300)])
    sent = store.last_activity_sync(conn)
    parsed = health_export.read_bytes(zipped(), TZ)
    days = health_export.day_rows(parsed, TZ)
    counts = store.import_activity_days(conn, days)
    assert (counts.added, counts.unchanged) == (3, 2)
    rows = {(r["metric"], r["period"]): r["qty"] for r in store.activity_between(conn, "0", "9")}
    assert rows[("steps", "day")] == 9963 and rows[("active_kcal", "part")] == 300
    assert rows[("flights", "day")] == 7
    # Importing old days doesn't move the phone's last send
    assert store.last_activity_sync(conn) == sent
    assert store.import_activity_days(conn, days).added == 0


def test_cli_import(settings, conn, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("chomp.config.Settings.from_env", lambda: settings)
    path = tmp_path / "export.zip"
    path.write_bytes(zipped())
    assert cli.main(["import-health", str(path)]) == 0
    out = capsys.readouterr().out
    assert "2 weigh-ins added, 0 updated, 0 already there; 5 days of activity added" in out
    assert "(from 2026-10-11); 2 samples skipped" in out
    stdin = io.TextIOWrapper(io.BytesIO(zipped()))
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["import-health", "-"]) == 0
    assert "0 days of activity added, 5 kept" in capsys.readouterr().out
    assert len(store.weights_between(conn, "0", "9")) == 2
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04 not really")
    assert cli.main(["import-health", str(bad)]) == 1


def upload(http, data: bytes, name="export.zip"):
    return http.post(
        "/app/activity/import",
        files={"export": (name, data, "application/zip")},
        headers=ORIGIN,
    )


def test_dashboard_import(http, conn, monkeypatch):
    sign_in(http)
    page = http.get("/app/activity").text
    assert '<details class="fold" data-fold="health" open>' in page  # nothing yet: open
    store.add_activity(conn, [("steps", "day", day("2026-10-12"), 9963)])
    page = follow(http, upload(http, zipped()))
    assert (
        "Imported 4 days of activity and 2 weigh-ins (1 day kept as your phone sent,"
        " 2 unreadable samples skipped)." in page
    )
    assert '<details class="fold" data-fold="health">' in page
    assert len(store.weights_between(conn, "0", "9")) == 2
    page = follow(http, upload(http, b"<plist/>", "export.xml"))
    assert "That file isn&#x27;t Apple Health&#x27;s export." in page
    monkeypatch.setattr("chomp.web.HEALTH_MAX", 100)
    assert "over 95 MB" in follow(http, upload(http, zipped()))
    no_origin = http.post("/app/activity/import", files={"export": ("e.zip", zipped())})
    assert no_origin.status_code == 403
