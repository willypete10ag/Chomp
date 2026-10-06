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
