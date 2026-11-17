"""Адаптер промту для Gemini (Nano Banana): той самий готовий промт + вимоги, на які Gemini реагує краще.

Що робить adapt():
  1. прибирає з промту буквальні написи й дату цифрами — щоб моделі не було чого малювати текстом;
  2. додає загальний блок вимог (унікальна композиція, сезон, спокійний верх кадру, без тексту, 16:9);
  3. додає вимоги для категорії картинки (календарна картка, затишок, природа, енергія дня, намір,
     фон афірмації, особлива дата, а також привітання групи й день народження);
  4. додає сезон на сьогодні, варіацію композиції та сюжети останніх генерацій, яких слід уникати;
  5. якщо треба, щоб модель намалювала текст у кадрі (дату для «Омніки», афірмацію для листівки) —
     додає блок, який скасовує заборону тексту саме для цього напису (draw_text).
"""
from __future__ import annotations

import datetime as dt
import re

from . import image_prompts as IP

# Загальний блок: додається до кожного промту для Gemini.
GEMINI_BASE = """Generate one unique high-quality image for a Ukrainian Telegram morning post.

Important requirements:
- Create a fresh, visually distinct composition every time.
- Follow the season appropriate to the date.
- Keep the atmosphere warm, harmonious, spiritual, calm, and uplifting.
- Use a beautiful landscape composition suitable for a Telegram post.
- Preferred aspect ratio: 16:9.
- Leave the upper 20–25% of the image visually calm and uncluttered so a date or greeting can be overlaid later.
- Do not render any text, letters, words, numbers, dates, watermarks, or logos inside the image.
- No people with clear faces in the foreground.
- No branded objects, no copyrighted characters, no UI elements.
- Avoid visual clutter at the edges.
- Use realistic seasonal details, gentle natural lighting, and harmonious colors.
- Make the image feel like a new scene, not a near-duplicate of previous generations.
- The image must be aesthetically suitable for spiritual, morning, gratitude, or daily-energy themed posts."""

# Вимоги за категорією картинки (category_id з prompts/*.json).
CATEGORY_RULES: dict[str, str] = {
    "calendar": ("Calendar card: a collectible seasonal scene that will carry a large date added later in code. "
                 "Keep the upper 25–28% especially smooth and evenly lit (sky, mist or a soft gradient) and place the main "
                 "subject in the lower two thirds. Do not draw calendar pages, grids, clocks or numerals."),
    "cozy": ("Cozy morning: an intimate, lived-in home scene with window light, a warm drink and natural textiles. "
             "No readable book pages, labels or screens, and no people. Warm interior tones against the season outside."),
    "nature": ("Morning nature: an awakening landscape at first light with layered depth and believable local flora "
               "for the season. No fantasy glows, no oversaturated HDR look, no large animals in the foreground."),
    "daily_energy": ("Daily energy: a serene symbolic scene with one clear focal symbol and generous negative space. "
                     "Keep the spirituality subtle: no zodiac wheels, tarot, astrological glyphs, chakra diagrams, portals "
                     "or sacred-geometry overlays."),
    "intention": ("Intention of the day: a quiet, grounded scene that suggests a first step or a morning ritual "
                  "(a path, a threshold, a desk, a seedling). Any journal or paper must be closed or blank. "
                  "Reflective rather than dramatic."),
    "affirmation": ("Affirmation background: an almost empty card background, not a scene. Keep the central 65% smooth, "
                    "uniform and low in contrast for text added later; put ornament only at the corners and edges, "
                    "with no frame lines or objects crossing the centre."),
    "special": ("Special date: a tasteful, quietly celebratory scene built only around the symbol named in the prompt. "
                "Do not invent astronomical events, moon phases, zodiac signs, religious icons or numerals."),
    "heart": ("Warm community greeting: tender and heartfelt, with flowers, soft light and paired natural elements "
              "that suggest connection. No religious iconography, no kitsch, no people."),
    "birthday": ("Birthday greeting background: festive but gentle, for a birthday greeting rather than a morning post. "
                 "Keep the central 50% calm and low in detail for a name and a greeting added later; decorations stay "
                 "at the edges. No numerals on cakes, balloons or candles."),
}
# у цих категоріях текст лягає в центр кадру, тому правило «спокійний центр» важливіше за «спокійний верх»
CENTER_TEXT = {"affirmation", "birthday"}
OVERRIDE_NOTE = ("For this category the calm-centre rule replaces the landscape-scene and calm-upper-area rules above; "
                 "all other general requirements still apply.")

# Варіації композиції: береться та, що найдовше не використовувалась у цій рубриці.
VARIATIONS_SCENE = [
    "a low viewpoint close to the ground with a softly blurred distance",
    "a wide elevated panorama with small details far below",
    "a view framed through foreground branches or leaves on one side",
    "a close detail in the lower third with the landscape dissolving behind it",
    "strong diagonal leading lines starting from a lower corner",
    "the main subject placed on the right third, open space on the left",
    "the main subject placed on the left third, open space on the right",
    "a backlit scene with long soft shadows falling toward the viewer",
]
VARIATIONS_BACKGROUND = [
    "ornament weighted toward the two lower corners",
    "ornament weighted toward the left edge, the right side almost empty",
    "ornament weighted toward the right edge, the left side almost empty",
    "a soft diagonal wash of light from the upper left",
    "a faint radial glow rising from the lower third",
    "a thin asymmetric botanical frame with wide empty margins",
]

# Текст у кадрі: вмикається, коли переданий draw_text. Скасовує заборону напису саме для нього.
DRAW_TEXT_RULES = """Text inside this image (this block overrides the rules above that forbid text, letters and numerals \
— but only for this one inscription; every other requirement still applies):
- Render this text exactly once, spelled character for character: {text}
- It is Ukrainian (Cyrillic). Copy the characters exactly as given: do not translate, transliterate, abbreviate, \
correct, repeat or add anything.
- {style}
- The lettering must be clean, correctly spelled, fully inside the frame, with generous margins, and easy to read \
against the background behind it.
- Add no other text, letters, numbers, captions, signatures, watermarks or logos anywhere in the image."""

# Розмір і колір напису з датою змінюються щодня (довжини списків різні, тож пара не повторюється 42 дні).
DATE_SIZES = [
    "Set it in a large, confident size across the upper area",
    "Set it in a medium size, modest and airy, in the upper left area",
    "Set it in an oversized, bold size that dominates the upper third",
    "Set it in a refined medium-small size in the upper right area",
    "Set it in a large, widely letter-spaced size centred in the upper area",
    "Set it in a tall, slender size along the upper left, with wide empty space beside it",
]
DATE_COLORS = [
    "in warm gold", "in soft ivory white", "in muted terracotta", "in deep teal",
    "in gentle rose", "in dark chocolate brown with a faint light edge", "in pale mint green",
]
DATE_FACES = [
    "an elegant serif face", "a clean light sans-serif face", "a gentle handwritten script",
    "a calm rounded sans-serif face", "a classic high-contrast serif face",
]

_CYR = "А-Яа-яІіЇїЄєҐґ"
_OVERLAY = re.compile(r"with exactly [“\"«][^”\"»]*[”\"»] and [“\"«][^”\"»]*[”\"»]")
_QUOTED_CYR = re.compile(rf"[“\"«][^”\"»]*[{_CYR}][^”\"»]*[”\"»]")
_ISO_DATE = re.compile(r"\bon (\d{4})-(\d{2})-(\d{2})(\s*\([^)]*\))?")
_PAREN_CYR = re.compile(rf"\s*\([^)]*[{_CYR}][^)]*\)")
_MONTHS_EN = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]


def month_part(d: dt.date) -> str:
    """17.11 -> «mid-November»: дата без цифр, щоб модель не намалювала число."""
    part = "early" if d.day <= 10 else "mid" if d.day <= 20 else "late"
    return f"{part}-{_MONTHS_EN[d.month - 1]}"


def strip_literal_text(prompt: str) -> str:
    """Прибирає те, що модель могла б надрукувати на картинці: написи в лапках, день тижня, дату цифрами."""
    def date_words(m: re.Match) -> str:
        try:
            return "on a " + month_part(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))) + " morning"
        except ValueError:
            return "on this morning"
    out = _OVERLAY.sub("with a greeting line and a date line", prompt)
    out = _ISO_DATE.sub(date_words, out)
    out = _QUOTED_CYR.sub("a short caption", out)
    out = _PAREN_CYR.sub("", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def date_draw_style(d: dt.date) -> str:
    """Опис стилю напису з датою: розмір, шрифт і колір, інші щодня."""
    n = d.toordinal()
    return (f"{DATE_SIZES[n % len(DATE_SIZES)]}, in {DATE_FACES[n % len(DATE_FACES)]}, "
            f"{DATE_COLORS[n % len(DATE_COLORS)]}, harmonised with the scene")


def draw_block(text: str, style: str = "") -> str:
    return DRAW_TEXT_RULES.format(text=f"«{text.strip()}»",
                                  style=style or "Place it in the calm upper area of the image")


def variations_for(category: str) -> list[str]:
    return VARIATIONS_BACKGROUND if category in CENTER_TEXT else VARIATIONS_SCENE


def pick_variation(category: str, recent: list[str]) -> str:
    """Варіація, якої немає серед останніх; якщо всі були — та, що траплялась найдавніше."""
    pool = variations_for(category)
    fresh = [v for v in pool if v not in recent]
    if fresh:
        return fresh[0]
    return max(pool, key=lambda v: recent.index(v))      # recent: новіші першими


def adapt(prompt: str, *, category: str = "", date: dt.date | None = None, variation: str = "",
          avoid: list[str] | None = None, draw_text: str = "", draw_style: str = "") -> str:
    """Готовий промт -> промт для Gemini. category — category_id готового промту або '' для власного.

    draw_text — напис, який модель має намалювати в кадрі (інакше текст у зображенні заборонений)."""
    date = date or dt.date.today()
    season, details, palette, *_ = IP.SEASONS[date.month]
    parts = [strip_literal_text(prompt), GEMINI_BASE]
    rule = CATEGORY_RULES.get(category)
    if rule:
        parts.append("Category-specific requirements:\n- " + rule
                     + ("\n- " + OVERRIDE_NOTE if category in CENTER_TEXT else ""))
    extra = [f"Season for this image: {season} in the Northern Hemisphere ({month_part(date)}): {details}. "
             f"Seasonal palette: {palette}."]
    if variation:
        extra.append(f"Composition for this generation: {variation}.")
    if avoid:
        extra.append("Previously generated scenes to stay clearly different from: " + "; ".join(avoid) + ".")
    parts.append("\n".join("- " + e for e in extra))
    if draw_text.strip():
        parts.append(draw_block(draw_text, draw_style))
    return "\n\n".join(parts)
