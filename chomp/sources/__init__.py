"""Upstream nutrition sources. Each client returns `FoodRecord`s with per-100 g nutrients."""

from __future__ import annotations

from dataclasses import dataclass, field

from chomp.logic import Nutrients


class SourceError(RuntimeError):
    """An upstream source could not be reached or returned something unusable."""


@dataclass
class FoodRecord:
    """One upstream food, normalized to per-100 g, ready to cache into `foods`."""

    source: str  # 'fdc' or 'off'
    source_id: str
    name: str
    nutrients: Nutrients  # per 100 g, all seven keys
    brand: str | None = None
    gtin: str | None = None  # normalized 14-digit, when known and valid
    data_type: str | None = None  # FDC dataType, e.g. 'Foundation', 'Branded'
    serving_g: float | None = None
    serving_desc: str | None = None
    flags: list[str] = field(default_factory=list)
    image_url: str | None = None  # product photo (OFF only), for the food's picture (#74)
    same_as: int = 0  # other listings of this product a search grouped into it (#97)
