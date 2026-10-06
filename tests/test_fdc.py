from __future__ import annotations

import pytest
from conftest import fixture_json

from chomp.sources import SourceError
from chomp.sources import fdc as fdcmod
from chomp.sources.fdc import FDCClient

SEARCH = "/fdc/v1/foods/search"
GENERIC = {"dataType": "Foundation,SR Legacy"}
BRANDED = {"dataType": "Branded"}


def test_parse_search_hit_nutrients():
    food = fixture_json("fdc/search_egg_white_generic.json")["foods"][0]
    rec = fdcmod.to_record(food)
    assert rec.source == "fdc" and rec.source_id == "172183"
    assert rec.nutrients == {
        "kcal": 52,
        "protein_g": 10.9,
        "carbs_g": 0.73,
        "fat_g": 0.17,
        "fiber_g": 0,
        "sugar_g": 0.71,
        "sodium_mg": 166,
    }


def test_foundation_fallback_energy_and_carbs():
    food = fixture_json("fdc/search_egg_white_generic.json")["foods"][1]
    rec = fdcmod.to_record(food)
    assert rec.nutrients["kcal"] == 55  # Atwater general (2047) when 1008 is absent
    assert rec.nutrients["carbs_g"] == 2.36  # by summation (1050)


def test_energy_from_kj_only():
    n = fdcmod.parse_nutrients(
        [
            {"nutrientId": 1062, "unitName": "kJ", "value": 418.4},
            {"nutrientId": 1003, "unitName": "G", "value": 1},
            {"nutrientId": 1005, "unitName": "G", "value": 1},
            {"nutrientId": 1004, "unitName": "G", "value": 1},
        ]
    )
    assert n["kcal"] == pytest.approx(100)


def test_food_missing_core_nutrient_is_skipped():
    food = fixture_json("fdc/search_egg_white_generic.json")["foods"][2]
    assert fdcmod.to_record(food) is None


def test_detail_format_parses():
    n = fdcmod.parse_nutrients(
        [
            {"nutrient": {"id": 1008, "unitName": "kcal"}, "amount": 120},
            {"nutrient": {"id": 1003, "unitName": "g"}, "amount": 3},
            {"nutrient": {"id": 1005, "unitName": "g"}, "amount": 20},
            {"nutrient": {"id": 1004, "unitName": "g"}, "amount": 2.5},
        ]
    )
    assert n["kcal"] == 120 and n["fat_g"] == 2.5 and n["sodium_mg"] is None


def test_branded_record_fields():
    food = fixture_json("fdc/search_egg_white_branded.json")["foods"][0]
    rec = fdcmod.to_record(food)
    assert rec.name == "100% Liquid Egg Whites"
    assert rec.brand == "HILL COUNTRY FARE"
    assert rec.gtin == "00041220576463"
    assert rec.serving_g == 46
    assert rec.serving_desc == "3 Tbsp (46 g)"


def test_serving_in_ml_has_no_grams():
    rec = fdcmod.to_record(
        {
            **fixture_json("fdc/search_egg_white_branded.json")["foods"][0],
            "servingSize": 240,
            "servingSizeUnit": "MLT",
            "householdServingFullText": "1 cup",
        }
    )
    assert rec.serving_g is None
    assert rec.serving_desc == "1 cup (240 mlt)"


@pytest.mark.anyio
async def test_search_ranked_generic_before_branded(upstream, fdc):
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_generic.json"), match=GENERIC)
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_branded.json"), match=BRANDED)
    recs = await fdc.search_ranked("egg white", limit=5)
    assert [(r.data_type, r.source_id) for r in recs] == [
        ("Foundation", "747997"),
        ("SR Legacy", "172183"),
        ("Branded", "2091234"),
    ]


@pytest.mark.anyio
async def test_search_skips_branded_when_generic_fills_limit(upstream, fdc):
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_generic.json"), match=GENERIC)
    recs = await fdc.search_ranked("egg white", limit=2)
    assert len(recs) == 2
    assert all(r.url.params["dataType"] != "Branded" for r in upstream.requests)


@pytest.mark.anyio
async def test_key_sent_as_header_not_in_url(upstream, fdc):
    upstream.add(SEARCH, {"foods": []})
    await fdc.search("x", ["Branded"])
    req = upstream.requests[0]
    assert req.headers["X-Api-Key"] == "test-key"
    assert "test-key" not in str(req.url)


@pytest.mark.anyio
async def test_responses_are_cached(upstream, fdc):
    upstream.add(SEARCH, fixture_json("fdc/search_egg_white_generic.json"))
    await fdc.search("egg white", ["SR Legacy"])
    await fdc.search("egg white", ["SR Legacy"])
    assert len(upstream.requests) == 1


@pytest.mark.anyio
async def test_retries_on_429_then_succeeds(upstream, fdc):
    upstream.add(SEARCH, [(429, {}), (200, {"foods": []})])
    assert await fdc.search("x", ["Branded"]) == []
    assert len(upstream.requests) == 2


@pytest.mark.anyio
async def test_gives_up_after_retries(upstream, fdc):
    upstream.add(SEARCH, {}, status=503)
    with pytest.raises(SourceError, match="unavailable"):
        await fdc.search("x", ["Branded"])
    assert len(upstream.requests) == 3


@pytest.mark.anyio
async def test_client_error_is_not_retried(upstream, fdc):
    upstream.add(SEARCH, {}, status=403)
    with pytest.raises(SourceError, match="HTTP 403"):
        await fdc.search("x", ["Branded"])
    assert len(upstream.requests) == 1


@pytest.mark.anyio
async def test_transport_error_retried(fdc):
    import httpx

    calls = []

    def boom(request):
        calls.append(request)
        raise httpx.ConnectTimeout("slow")

    client = FDCClient(
        "k", http=httpx.AsyncClient(transport=httpx.MockTransport(boom)), backoff_s=0
    )
    with pytest.raises(SourceError, match="unreachable"):
        await client.search("x", ["Branded"])
    assert len(calls) == 3


@pytest.mark.anyio
async def test_disabled_without_key():
    client = FDCClient("")
    assert not client.enabled
    with pytest.raises(SourceError, match="not configured"):
        await client.search("x", ["Branded"])
