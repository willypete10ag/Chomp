"""Barcode normalization: UPC-A, EAN-8, EAN-13 and GTIN-14 to a 14-digit GTIN.

Storage uses the 14-digit form (left-padded with zeros). Upstream sources index the same
product under different lengths, so lookups try every equivalent form.
"""

from __future__ import annotations

import re

VALID_LENGTHS = (8, 12, 13, 14)


class InvalidBarcode(ValueError):
    pass


def check_digit(body: str) -> int:
    """GS1 check digit for the digits before it (weights 3,1,3,... from the right)."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def is_valid(digits: str) -> bool:
    return (
        digits.isdigit()
        and len(digits) in VALID_LENGTHS
        and check_digit(digits[:-1]) == int(digits[-1])
    )


def normalize(code: str) -> str:
    """Return the 14-digit GTIN for a scanned or typed barcode.

    Spaces and dashes are ignored. Raises InvalidBarcode for the wrong length or a bad
    check digit (usually a misread digit).
    """
    digits = re.sub(r"[\s-]", "", code or "")
    if not digits.isdigit():
        raise InvalidBarcode(f"barcode must be digits only, got {code!r}")
    if len(digits) not in VALID_LENGTHS:
        raise InvalidBarcode(
            f"barcode must have 8, 12, 13 or 14 digits, got {len(digits)} ({digits})"
        )
    if not is_valid(digits):
        raise InvalidBarcode(f"check digit does not match for {digits}; a digit may be misread")
    return digits.zfill(14)


def variants(gtin14: str) -> list[str]:
    """Equivalent forms to try upstream, most common first: EAN-13, UPC-A, GTIN-14, EAN-8."""
    stripped = gtin14.lstrip("0")
    out = []
    for n in (13, 12, 14, 8):
        if len(stripped) <= n:
            out.append(gtin14[-n:])
    return list(dict.fromkeys(out))


def lenient(code: str | None) -> str | None:
    """Normalize an upstream barcode field without rejecting it.

    FDC's gtinUpc is sometimes padded oddly or missing its check digit; this returns the
    14-digit GTIN when the value is valid and None otherwise, so bad upstream data never
    blocks caching a food.
    """
    if not code:
        return None
    digits = re.sub(r"\D", "", code)
    if len(digits) > 14:
        digits = digits.lstrip("0")
    if digits and len(digits) <= 14:
        for candidate in (digits, digits.zfill(12), digits.zfill(13)):
            if is_valid(candidate):
                return candidate.zfill(14)
    return None


def same_product(gtin14: str, upstream: str | None) -> bool:
    """Whether an upstream barcode field refers to `gtin14`, tolerating a dropped check digit."""
    if not upstream:
        return False
    digits = re.sub(r"\D", "", upstream).lstrip("0")
    target = gtin14.lstrip("0")
    return bool(digits) and (digits == target or digits == target[:-1])
