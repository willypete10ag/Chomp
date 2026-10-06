from __future__ import annotations

import pytest

from chomp.sources import gtin

# Real-format codes with valid check digits
UPC_A = "041220576463"
EAN_13 = "3017624010701"
EAN_8 = "96385074"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (UPC_A, "00041220576463"),
        ("0" + UPC_A, "00041220576463"),  # same product as EAN-13
        ("00" + UPC_A, "00041220576463"),  # GTIN-14
        (EAN_13, "03017624010701"),
        (EAN_8, "00000096385074"),
        ("0 41220-57646 3", "00041220576463"),
    ],
)
def test_normalize(code, expected):
    assert gtin.normalize(code) == expected


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("041220576464", "check digit"),
        ("12345", "8, 12, 13 or 14 digits"),
        ("04122057646X", "digits only"),
        ("", "digits only"),
    ],
)
def test_normalize_rejects(code, message):
    with pytest.raises(gtin.InvalidBarcode, match=message):
        gtin.normalize(code)


def test_variants():
    assert gtin.variants("00041220576463") == ["0041220576463", "041220576463", "00041220576463"]
    assert gtin.variants("03017624010701") == ["3017624010701", "03017624010701"]
    assert gtin.variants("00000096385074") == [
        "0000096385074",
        "000096385074",
        "00000096385074",
        "96385074",
    ]


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        ("041220576463", "00041220576463"),
        ("0041220576463", "00041220576463"),
        ("41220576463", "00041220576463"),  # leading zero dropped
        ("4122057646", None),  # check digit dropped: can't validate
        ("garbage", None),
        (None, None),
    ],
)
def test_lenient(upstream, expected):
    assert gtin.lenient(upstream) == expected


def test_same_product_tolerates_padding_and_missing_check_digit():
    g = "00041220576463"
    assert gtin.same_product(g, "041220576463")
    assert gtin.same_product(g, "00041220576463")
    assert gtin.same_product(g, "04122057646")
    assert not gtin.same_product(g, "041220576470")
    assert not gtin.same_product(g, None)
