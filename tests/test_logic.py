from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from chomp import logic

EGG = {"kcal": 143, "protein_g": 12.6, "carbs_g": 0.72, "fat_g": 9.51, "sodium_mg": 142}


def test_per_serving_to_per_100g():
    # Label: 3 tbsp (46 g) = 110 kcal, 26 g protein, 1 g carbs, 0 g fat
    per = {"kcal": 110, "protein_g": 26, "carbs_g": 1, "fat_g": 0, "sodium_mg": 370}
    out = logic.per_serving_to_per_100g(per, 46)
    assert out["kcal"] == pytest.approx(239.13, abs=1e-3)
    assert out["protein_g"] == pytest.approx(56.522, abs=1e-3)
    assert out["fat_g"] == 0
    assert out["fiber_g"] is None
    assert out["sodium_mg"] == pytest.approx(804.348, abs=1e-3)


def test_round_trip_serving_and_scale():
    per = {"kcal": 70, "protein_g": 6, "carbs_g": 0.5, "fat_g": 5}
    back = logic.scale(logic.per_serving_to_per_100g(per, 50), 50)
    for k, v in per.items():
        assert back[k] == pytest.approx(v, abs=1e-3)


def test_scale_and_snapshot():
    out = logic.snapshot(EGG, 220)
    assert out["kcal"] == pytest.approx(314.6)
    assert out["protein_g"] == pytest.approx(27.72)
    assert out["carbs_g"] == pytest.approx(1.584)
    assert out["sugar_g"] is None
    assert out["sodium_mg"] == pytest.approx(312.4)


def test_snapshot_store_precision():
    out = logic.snapshot({"kcal": 1, "protein_g": 1, "carbs_g": 1, "fat_g": 1}, 33.3333)
    assert out["kcal"] == 0.333


@pytest.mark.parametrize("grams", [0, -5])
def test_scale_rejects_non_positive_grams(grams):
    with pytest.raises(ValueError, match="grams"):
        logic.scale(EGG, grams)


@pytest.mark.parametrize("serving", [0, -1])
def test_serving_rejects_non_positive(serving):
    with pytest.raises(ValueError, match="serving_g"):
        logic.per_serving_to_per_100g(EGG, serving)


def test_core_nutrients_required():
    with pytest.raises(ValueError, match="fat_g is required"):
        logic.scale({"kcal": 1, "protein_g": 1, "carbs_g": 1}, 100)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="finite"):
        logic.scale({**EGG, "kcal": bad}, 100)
    with pytest.raises(ValueError, match="finite"):
        logic.scale(EGG, bad)
    with pytest.raises(ValueError, match="finite"):
        logic.per_serving_to_per_100g(EGG, bad)


def test_rejects_negative_and_unknown():
    with pytest.raises(ValueError, match="negative"):
        logic.scale({**EGG, "fat_g": -1}, 100)
    with pytest.raises(ValueError, match="unknown"):
        logic.scale({**EGG, "vitamin_c": 1}, 100)


def test_sum_nutrients_handles_missing_optionals():
    a = logic.snapshot(EGG, 100)
    b = logic.snapshot({"kcal": 52, "protein_g": 10.9, "carbs_g": 0.7, "fat_g": 0.2}, 300)
    total = logic.sum_nutrients([a, b])
    assert total["kcal"] == pytest.approx(143 + 156)
    assert total["protein_g"] == pytest.approx(12.6 + 32.7)
    assert total["sodium_mg"] == pytest.approx(142)
    assert total["fiber_g"] is None


def test_sum_of_nothing_is_zero_core():
    total = logic.sum_nutrients([])
    assert total["kcal"] == 0
    assert total["fiber_g"] is None


def test_round_for_display():
    out = logic.round_for_display(
        {"kcal": 314.6, "protein_g": 27.72, "carbs_g": 1.56, "fat_g": 20.922, "sodium_mg": 312.4}
    )
    assert out == {
        "kcal": 315,
        "protein_g": 27.7,
        "carbs_g": 1.6,
        "fat_g": 20.9,
        "fiber_g": None,
        "sugar_g": None,
        "sodium_mg": 312,
    }


def test_progress_against_targets():
    totals = {"kcal": 1500, "protein_g": 150, "carbs_g": 100, "fat_g": 50}
    out = logic.progress(totals, {"kcal": 2000, "protein_g": 180, "carbs_g": None})
    assert set(out) == {"kcal", "protein_g"}
    assert out["protein_g"] == {"target": 180, "eaten": 150, "remaining": 30, "percent": 83}
    assert out["kcal"]["remaining"] == 500


def test_progress_zero_target():
    assert logic.progress({"kcal": 10}, {"kcal": 0})["kcal"]["percent"] is None


# --- Time -----------------------------------------------------------------------------------

TZ = "America/New_York"


def test_day_bounds_standard_and_daylight():
    assert logic.local_day_bounds(date(2026, 1, 15), TZ) == (
        "2026-01-15T05:00:00Z",
        "2026-01-16T05:00:00Z",
    )
    assert logic.local_day_bounds(date(2026, 7, 4), TZ) == (
        "2026-07-04T04:00:00Z",
        "2026-07-05T04:00:00Z",
    )


def hours(bounds):
    a, b = (logic.parse_utc(x) for x in bounds)
    return (b - a).total_seconds() / 3600


def test_spring_forward_day_is_23_hours():
    bounds = logic.local_day_bounds(date(2026, 3, 8), TZ)
    assert bounds == ("2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z")
    assert hours(bounds) == 23


def test_fall_back_day_is_25_hours():
    bounds = logic.local_day_bounds(date(2026, 11, 1), TZ)
    assert bounds == ("2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z")
    assert hours(bounds) == 25


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2026-01-16T04:59:59Z", date(2026, 1, 15)),  # 11:59:59 pm EST
        ("2026-01-16T05:00:00Z", date(2026, 1, 16)),
        ("2026-07-05T03:30:00Z", date(2026, 7, 4)),  # 11:30 pm EDT; the -4h view agrees
        ("2026-01-16T04:30:00Z", date(2026, 1, 15)),  # 11:30 pm EST; the -4h view says the 16th
    ],
)
def test_local_date(utc, expected):
    assert logic.local_date(utc, TZ) == expected


def test_format_utc_converts_and_rejects_naive():
    ny = datetime(2026, 3, 8, 1, 30).astimezone(UTC)
    assert logic.format_utc(ny).endswith("Z")
    with pytest.raises(ValueError, match="naive"):
        logic.format_utc(datetime(2026, 1, 1))


def test_parse_format_round_trip():
    s = "2026-10-06T01:43:45Z"
    assert logic.format_utc(logic.parse_utc(s)) == s


# --- Meal times and dates -------------------------------------------------------------------

NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)  # 22:00 on the 5th in New York


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("now", "2026-10-06T02:00:00Z"),
        (" NOW ", "2026-10-06T02:00:00Z"),
        ("2026-10-05T08:30", "2026-10-05T12:30:00Z"),  # naive = local (EDT)
        ("2026-01-05T08:30", "2026-01-05T13:30:00Z"),  # EST
        ("2026-10-05T08:30:00Z", "2026-10-05T08:30:00Z"),
        ("2026-10-05T08:30:00-07:00", "2026-10-05T15:30:00Z"),
    ],
)
def test_parse_eaten_at(value, expected):
    assert logic.parse_eaten_at(value, TZ, NOW) == expected


@pytest.mark.parametrize("value", [None, "", "2026-10-05", "breakfast", "10/05/2026 8am"])
def test_parse_eaten_at_rejects(value):
    with pytest.raises(ValueError, match="eaten_at"):
        logic.parse_eaten_at(value, TZ, NOW)


def test_parse_eaten_at_rejects_time_skipped_by_dst():
    with pytest.raises(ValueError, match="does not exist"):
        logic.parse_eaten_at("2026-03-08T02:30", TZ, NOW)
    # the hour after the change is fine, and so is the repeated hour in November
    assert logic.parse_eaten_at("2026-03-08T03:30", TZ, NOW) == "2026-03-08T07:30:00Z"
    assert logic.parse_eaten_at("2026-11-01T01:30", TZ, NOW) == "2026-11-01T05:30:00Z"


def test_parse_local_date_keywords_use_local_today():
    assert logic.parse_local_date(None, TZ, NOW) == date(2026, 10, 5)
    assert logic.parse_local_date("Today", TZ, NOW) == date(2026, 10, 5)
    assert logic.parse_local_date("yesterday", TZ, NOW) == date(2026, 10, 4)
    assert logic.parse_local_date("2026-02-28", TZ, NOW) == date(2026, 2, 28)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        logic.parse_local_date("2026-02-30", TZ, NOW)


def test_local_iso():
    assert logic.local_iso("2026-10-05T12:30:00Z", TZ) == "2026-10-05T08:30:00-04:00"


def test_date_range_and_average():
    days = logic.date_range(date(2026, 10, 1), date(2026, 10, 7))
    assert len(days) == 7 and days[-1] == date(2026, 10, 7)
    with pytest.raises(ValueError):
        logic.date_range(date(2026, 10, 2), date(2026, 10, 1))
    avg = logic.average({"kcal": 14000, "protein_g": 1260, "carbs_g": 0, "fat_g": 7}, 7)
    assert avg["kcal"] == 2000 and avg["protein_g"] == 180 and avg["fiber_g"] is None


# --- Upstream data --------------------------------------------------------------------------


def test_kj_and_sodium_units():
    assert logic.kj_to_kcal(418.4) == pytest.approx(100)
    assert logic.g_to_mg(0.35) == pytest.approx(350)


def test_label_mismatch_over_ten_percent():
    db = {"kcal": 239, "protein_g": 56.5, "carbs_g": 2.2, "fat_g": 0}
    label = {"kcal": 200, "protein_g": 56.5, "carbs_g": 2.2, "fat_g": 0}
    assert logic.label_mismatch(db, label) == ["kcal"]


def test_label_mismatch_ignores_small_absolute_differences():
    # 0.5 g vs 0 g fat is "100%" off, but within label rounding
    db = {"kcal": 52, "protein_g": 10.9, "carbs_g": 0.7, "fat_g": 0.5}
    label = {"kcal": 54, "protein_g": 10.9, "carbs_g": 0, "fat_g": 0}
    assert logic.label_mismatch(db, label) == []


def test_round_nutrients():
    out = logic.round_nutrients({"kcal": 1.23456, "protein_g": 1, "carbs_g": 1, "fat_g": 1})
    assert out["kcal"] == 1.235 and out["sodium_mg"] is None


def test_partial_per_serving_to_per_100g():
    assert logic.partial_per_serving_to_per_100g({"protein_g": 26}, 46) == {
        "protein_g": pytest.approx(56.5217, abs=1e-4)
    }
    with pytest.raises(ValueError, match="at least 0"):
        logic.partial_per_serving_to_per_100g({"fat_g": -1}, 46)
    with pytest.raises(ValueError, match="unknown"):
        logic.partial_per_serving_to_per_100g({"salt": 1}, 46)


def test_totals_by_day_uses_local_dates():
    rows = [
        ("2026-10-05T12:00:00Z", {"kcal": 100.0, "protein_g": 10.0, "carbs_g": 0, "fat_g": 0}),
        # 01:00 UTC on the 6th is 21:00 on the 5th in New York
        ("2026-10-06T01:00:00Z", {"kcal": 50.0, "protein_g": 5.0, "carbs_g": 0, "fat_g": 0}),
        ("2026-10-06T14:00:00Z", {"kcal": 20.0, "protein_g": 1.0, "carbs_g": 0, "fat_g": 0}),
    ]
    out = logic.totals_by_day(rows, "America/New_York")
    assert out[date(2026, 10, 5)]["kcal"] == 150.0
    assert out[date(2026, 10, 5)]["protein_g"] == 15.0
    assert out[date(2026, 10, 6)]["kcal"] == 20.0
    assert logic.totals_by_day([], "America/New_York") == {}


def test_tidy_upstream_zeroes_small_negatives_and_rejects_bad_values():
    base = {"kcal": 120, "protein_g": 25, "carbs_g": -0.104, "fat_g": 1.5}
    assert logic.tidy_upstream(base)["carbs_g"] == 0
    assert logic.tidy_upstream(base)["fiber_g"] is None
    assert logic.tidy_upstream({**base, "carbs_g": -5}) is None
    assert logic.tidy_upstream({**base, "fat_g": float("nan")}) is None
