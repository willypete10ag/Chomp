"""Read a nutrition facts label from a photo (#59): tesseract OCR on the server, then a
parser that pulls the serving size and per-serving values out of the text.

Nothing here guesses: a value is only filled in when the label text has it, and the
dashboard makes the person tick every value against the label before the food counts as
confirmed. OCR runs the `tesseract` command (the Docker image installs it); no model or
LLM runs on the server.
"""

from __future__ import annotations

import io
import logging
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from PIL import Image, ImageOps, UnidentifiedImageError

from chomp import logic

log = logging.getLogger("chomp.label")

MAX_PIXELS = 50_000_000  # refuse bigger images (decompression bombs) before decoding them
LONG_SIDE = 2400  # tesseract reads label text best around this size
OCR_TIMEOUT_S = 40


class LabelError(RuntimeError):
    """The photo couldn't be read; the message is fit to show the person."""


@dataclass
class LabelRead:
    serving_g: float | None = None
    serving_desc: str | None = None
    serving_ml: bool = False  # the serving was printed in mL, so the grams need checking
    values: dict[str, float] = field(default_factory=dict)  # per serving, as printed
    text: str = ""  # the OCR text, for the person to compare against


def available() -> bool:
    return shutil.which("tesseract") is not None


# --- OCR ------------------------------------------------------------------------------------


def _prepare(data: bytes) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))  # reads only the header
        # Pillow only refuses images over twice MAX_IMAGE_PIXELS, so check before decoding
        if img.width * img.height > MAX_PIXELS:
            raise LabelError("That photo is too large. Try a smaller one.")
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise LabelError("That file isn't a photo Chomp can read. Try a JPEG or PNG.") from e
    img = ImageOps.exif_transpose(img).convert("L")  # phones store rotation in EXIF
    scale = LONG_SIDE / max(img.size)
    if scale < 1 or scale > 1.5:  # shrink big photos; enlarge small crops
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(img, cutoff=1)


def _tesseract(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    try:
        out = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "6"],
            input=buf.getvalue(),
            capture_output=True,
            timeout=OCR_TIMEOUT_S,
            check=True,
        )
    except FileNotFoundError as e:
        raise LabelError("Label reading isn't installed on this server.") from e
    except subprocess.TimeoutExpired as e:
        raise LabelError("Reading the label took too long. Try a closer photo.") from e
    except subprocess.CalledProcessError as e:
        log.warning("tesseract failed: %s", e.stderr.decode(errors="replace")[:200])
        raise LabelError("Couldn't read that photo. Try another one.") from e
    return out.stdout.decode(errors="replace")


def read_photo(data: bytes) -> LabelRead:
    """OCR a label photo and parse it. A photo taken sideways or upside down is retried
    turned, keeping whichever reading finds the most values."""
    img = _prepare(data)
    best = parse(_tesseract(img))
    for angle in (90, 270, 180):
        if _found(best) >= 4:
            break
        turned = parse(_tesseract(img.rotate(angle, expand=True)))
        if _found(turned) > _found(best):
            best = turned
    return best


def _found(r: LabelRead) -> int:
    return len(r.values) + (r.serving_g is not None)


# --- Parsing --------------------------------------------------------------------------------

# Label wording per nutrient, in a lowercased text. Order matters only for readability.
NAMES = {
    "kcal": r"calories(?!\s*from)",
    "fat_g": r"total\s*fat",
    "carbs_g": r"total\s*carb(?:ohydrates?|s|\.)?",
    "fiber_g": r"(?:dietary\s*)?fib(?:er|re)",
    "sugar_g": r"(?<!added\s)(?<!incl\.\s)(?:total\s*)?sugars?(?!\s*alcohol)",
    "protein_g": r"protein",
    "sodium_mg": r"sodium",
    "sat_fat_g": r"sat(?:urated|\.)?\s*fat",
    "trans_fat_g": r"trans\s*fat",
    "poly_fat_g": r"poly\s*unsat(?:urated|\.)?\s*fat",
    "mono_fat_g": r"mono\s*unsat(?:urated|\.)?\s*fat",
    "cholesterol_mg": r"chol(?:esterol|\.)?",
    "added_sugar_g": r"added\s*sugars?",
    "sugar_alcohol_g": r"sugar\s*alcohols?",
    "vitamin_d_mcg": r"vit(?:amin|\.)?\s*d[23]?",
    "calcium_mg": r"calcium",
    "iron_mg": r"[il1|]ron",  # OCR often reads the I as l
    "potassium_mg": r"potassium",
    "vitamin_a_mcg": r"vit(?:amin|\.)?\s*a",
    "vitamin_c_mg": r"vit(?:amin|\.)?\s*c",
    "vitamin_e_mg": r"vit(?:amin|\.)?\s*e",
    "vitamin_k_mcg": r"vit(?:amin|\.)?\s*k",
    "thiamin_mg": r"thiamine?",
    "riboflavin_mg": r"riboflavin",
    "niacin_mg": r"niacin",
    "vitamin_b6_mg": r"vit(?:amin|\.)?\s*b\s*6",
    "folate_mcg": r"folate|folic\s*acid",
    "vitamin_b12_mcg": r"vit(?:amin|\.)?\s*b\s*12",
    "phosphorus_mg": r"phosphorus",
    "magnesium_mg": r"magnesium",
    "zinc_mg": r"zinc",
}
UNIT = r"(mcg|meg|mog|µg|μg|ug|mg|g)\b"
NUMBER = r"(\d+(?:\.\d+)?)"
# After a nutrient name: an optional "less than" and the amount with its unit, then perhaps
# a % DV ("Total Fat 8g 10%"); or only a % DV ("Vitamin A 10%", older labels).
AMOUNT = re.compile(rf"[\s:.,]{{0,3}}(<|less\s*than\s*)?{NUMBER}\s*{UNIT}")
PERCENT = re.compile(rf"[\s:.,]{{0,3}}{NUMBER}\s*%")
CALORIES = re.compile(rf"[^\d\n]{{0,12}}\n?[^\d\n]{{0,6}}{NUMBER}(?![\d.])(?![ \t]*(?:%|m?c?g\b))")
SERVING = re.compile(r"serv(?:ing|\.)\s*size\s*:?\s*(.*)")
SERVING_AMOUNT = re.compile(rf"{NUMBER}\s*(g|ml)\b")
SERVING_SLIP = re.compile(r"\((\d+(?:\.\d+)?)9\)")  # "(55g)" read as "(559)"
ENERGY_KCAL = re.compile(rf"{NUMBER}\s*kcal\b")
# US labels put the amount first: "Includes 10g Added Sugars"
INCLUDES_ADDED = re.compile(rf"incl(?:udes|\.)?\s*{NUMBER}\s*{UNIT}\s*added\s*sugars?")


def _fix_ocr(text: str) -> str:
    """Undo common OCR slips in amounts: 'Og' or 'O g' for 0g, 'lg' or 'Ig' for 1g, 'l0mg'
    for 10mg, and a comma used as a decimal point ('2,5 g')."""
    text = text.lower()

    def digits(m: re.Match) -> str:
        return m.group(0).translate(str.maketrans("oil|", "0111"))

    # letters that look like digits, inside a token that ends in a unit
    text = re.sub(rf"(?<![a-z])[0-9oil|][0-9oil|.]*(?=\s?{UNIT})", digits, text)
    text = re.sub(r"(\d),(\d{1,2})(?=\s?(?:m?c?g|%)\b)", r"\1.\2", text)
    text = re.sub(r"(\d),(\d{3})\b", r"\1\2", text)  # 1,200 -> 1200
    return re.sub(r"\b(meg|mog)\b", "mcg", text)


def _amount(key: str, value: float, unit: str) -> float | None:
    unit = {"meg": "mcg", "mog": "mcg", "μg": "mcg", "µg": "mcg", "ug": "mcg"}.get(unit, unit)
    if key == "kcal":
        return None
    want = logic.unit_of(key)
    if unit == want:
        return value
    if want == "g" and unit != "g":
        return None  # "Protein 5mg" is a misread, not a tiny amount
    return logic.convert_amount(key, value, unit)


def parse(text: str) -> LabelRead:
    """The serving size and per-serving values a label's OCR text gives. Values the text
    doesn't clearly give are left out."""
    clean = _fix_ocr(text)
    read = LabelRead(text=text.strip())
    if m := SERVING.search(clean):
        rest = m.group(1).strip()
        amounts = SERVING_AMOUNT.findall(rest) or [(n, "g") for n in SERVING_SLIP.findall(rest)]
        if amounts:
            number, unit = amounts[-1]  # "2/3 cup (55g)": the grams come last
            read.serving_g = float(number)
            read.serving_ml = unit == "ml"
            rest = SERVING_SLIP.sub(r"(\1g)", rest)
            read.serving_desc = " ".join(rest.split())[:60] or None
    for key, name in NAMES.items():
        value = _first_value(clean, key, name)
        if value is not None and math.isfinite(value):
            read.values[key] = round(value, 3)
    if "kcal" not in read.values and (m := ENERGY_KCAL.search(clean)):
        read.values["kcal"] = float(m.group(1))
    if "added_sugar_g" not in read.values and (m := INCLUDES_ADDED.search(clean)):
        value = _amount("added_sugar_g", float(m.group(1)), m.group(2))
        if value is not None:
            read.values["added_sugar_g"] = round(value, 3)
    return read


def _first_value(text: str, key: str, name: str) -> float | None:
    for m in re.finditer(rf"(?<![a-z]){name}(?![a-z])", text):
        after = text[m.end() :]
        if key == "kcal":
            if c := CALORIES.match(after):
                return float(c.group(1))
            continue
        if a := AMOUNT.match(after):
            amount = _amount(key, float(a.group(2)), a.group(3))
            if amount is not None:
                return amount
            continue
        if key in logic.DAILY_VALUES and (p := PERCENT.match(after)):
            return logic.percent_dv_to_amounts({key: float(p.group(1))})[key]
    return None
