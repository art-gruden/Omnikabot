"""Готові промти картинок: вибір під рубрику й дату, заповнення полів, посилання на ChatGPT.

Бібліотека лежить у prompts/*.json (8 категорій по 3 варіанти + день народження). Кожна рубрика
бере свої категорії (KIND_CATEGORIES). Дату, сезон і деталі сезону підставляє код; тему дня дає
GPT за текстом посту або береться типова для категорії.

    python -m bot.image_prompts omnika 2026-11-17 gratitude and calm
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

PROMPTS_DIR = Path(os.environ.get("IMAGE_PROMPTS_DIR", Path(__file__).resolve().parent.parent / "prompts"))
PROMPT_FILES = ("morning-image-prompts-24.json", "birthday-image-prompts.json")
CHATGPT_URL = os.environ.get("CHATGPT_URL", "https://chatgpt.com/")
# Telegram не публікує ліміт довжини URL у кнопці. Спершу пробуємо повне посилання; якщо Telegram
# його відхиляє, бот повторює із SAFE_BUTTON_URL (з посилання зникають необов'язкові речення).
MAX_BUTTON_URL = int(os.environ.get("MAX_BUTTON_URL", "4096"))
SAFE_BUTTON_URL = int(os.environ.get("SAFE_BUTTON_URL", "2048"))
HISTORY_DAYS = 60          # скільки пам'ятаємо видані промти
RECENT_LIMIT = 4           # скільки останніх сюжетів потрапляє в {{RECENT_VISUALS}}

# рубрика бота -> категорії промтів
KIND_CATEGORIES: dict[str, tuple[str, ...]] = {
    "birthday": ("birthday",),                                  # листівка: спокійний центр під текст
    "morning": ("affirmation",),                                # «Енергозавантаження»: порожній фон картки
    "omnika": ("calendar", "daily_energy", "nature", "cozy"),   # ранковий пост каналу
    "unity": ("heart", "intention"),                            # налаштування на день для групи
}
EXPLICIT_ONLY = {"birthday_3"}   # ювілейний фон: лише коли в тексті є «ювілей» або натиснуто «Інший промт»

MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня",
              "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]

# місяць: (SEASON, SEASON_DETAILS, SEASON_PALETTE, SEASONAL_BOTANICALS, SEASONAL_FRUIT, група якорів)
SEASONS = {
    1: ("midwinter", "deep snow on branches, frost patterns on glass, pale low sun, blue shadows",
        "ice blue, silver white, soft gold, warm amber",
        "spruce and pine branches, pine cones, snow-dusted rowan berries, dried seed heads",
        "apples, mandarins, walnuts", "winter"),
    2: ("late winter", "melting snow patches, icicles catching sunlight, lengthening daylight, crisp air",
        "pale blue, pearl white, soft peach, light gold",
        "pussy willow buds, first snowdrops, bare birch twigs, evergreen sprigs",
        "apples, pears, dried apricots", "winter"),
    3: ("early spring", "last snow patches, swelling buds, wet dark earth, cool clear morning air",
        "fresh light green, soft yellow, sky blue, pearl white",
        "snowdrops, crocuses, pussy willow, primroses",
        "apples, citrus, dried fruit", "spring"),
    4: ("mid-spring", "young bright leaves, blossoming orchards, dew on fresh grass, mild light",
        "blossom pink, tender green, cream white, sunny yellow",
        "cherry and apricot blossom, tulips, daffodils, young birch leaves",
        "apples, rhubarb, fresh herbs", "spring"),
    5: ("late spring", "lush fresh greenery, flowering chestnuts and lilac, warm dawn, morning dew",
        "lilac, fresh green, soft white, warm gold",
        "lilac, lily of the valley, peonies, chestnut blossom",
        "first strawberries, early cherries", "spring"),
    6: ("early summer", "tall green grass, early warm sunrise, light haze over meadows, dew",
        "meadow green, poppy red, sky blue, honey gold",
        "poppies, daisies, linden blossom, wild roses",
        "strawberries, cherries, sweet cherries", "summer"),
    7: ("midsummer", "ripening wheat fields, warm golden haze, long sunrise shadows, clear sky",
        "wheat gold, sunflower yellow, deep green, azure",
        "sunflowers, cornflowers, lavender, hollyhocks",
        "apricots, raspberries, currants", "summer"),
    8: ("late summer", "harvested fields, warm dry grass, soft amber light, light mist over water",
        "amber, warm ochre, olive green, soft blue",
        "sunflowers, marigolds, asters, ripe wheat ears",
        "apples, plums, pears, grapes", "summer"),
    9: ("early autumn", "first yellow leaves among green, cool dewy mornings, gossamer threads, light mist",
        "golden yellow, soft green, warm orange, pale blue",
        "asters, chrysanthemums, viburnum berries, first yellow birch leaves",
        "apples, pears, grapes, plums", "autumn"),
    10: ("mid-autumn", "golden and crimson foliage, leaf-covered paths, morning fog, low warm sun",
         "gold, crimson, burnt orange, deep brown",
         "maple leaves, chrysanthemums, rowan berries, rose hips",
         "apples, pears, quince, walnuts", "autumn"),
    11: ("late autumn", "copper leaves, partly bare branches, chilly dawn mist, subtle frost where appropriate",
         "copper, amber, muted burgundy, mist blue, warm gold",
         "maple leaves, rose hips, rowan berries, dried grasses",
         "apples, pears, persimmons", "autumn"),
    12: ("early winter", "first snow on bare branches, hoarfrost, short pale dawn, quiet still air",
         "snow white, frosty blue, warm candle gold, deep evergreen",
         "fir branches, pine cones, frosted rowan berries, dried grasses",
         "apples, mandarins, walnuts", "winter"),
}
ANCHORS = {
    "winter": ["a small brass lantern", "a hand-knitted woollen mitten", "a bullfinch", "a clay candle holder",
               "a carved wooden star", "a great tit on a twig", "a frosted glass bottle", "a woven wool blanket corner"],
    "spring": ["a tiny bird's nest", "a ceramic watering jug", "a swallow", "a woven willow basket",
               "a small clay bell", "a white butterfly", "a hand-blown glass vase", "a wooden garden trowel"],
    "summer": ["a straw hat", "a glass jar of wildflower honey", "a dragonfly", "a clay jug",
               "a woven flower wreath", "a small wooden boat", "a ladybird", "an embroidered linen cloth"],
    "autumn": ["an amber glass prism", "a copper kettle", "a robin", "a small brass bell",
               "a hand-carved wooden bird", "a woven straw basket", "a ceramic owl figurine", "a glass jar of honey"],
}
# символ теми: перший збіг за коренем слова (укр. або англ.), інакше — із запасного списку за датою
SYMBOL_RULES = [
    (r"вдячн|gratitude|thank", "a single white feather"),
    (r"спок|тиш|мир|calm|peace|still", "a smooth white river stone"),
    (r"очищ|відпус|заверш|звільн|release|cleans|letting go", "a drop of clear water on a leaf"),
    (r"вибір|рішен|choice|decision", "a small golden key"),
    (r"люб|серц|love|heart|kindness", "a single rose petal"),
    (r"достат|гроші|фінанс|добробут|abundance|prosper|money", "a ripe ear of wheat"),
    (r"сил|впевнен|strength|confiden", "an oak leaf with an acorn"),
    (r"нов|почат|new|beginning|renew", "a sprouting seed"),
    (r"світл|натхн|радіст|light|inspir|joy", "a ray of light through a clear crystal"),
    (r"рівноваг|баланс|гармон|balance|harmony", "two balanced pebbles"),
]
SYMBOLS_FALLBACK = ["a single white feather", "a smooth white river stone", "a small golden key",
                    "a drop of clear water on a leaf", "a sprouting seed", "a ray of light through a clear crystal"]
DEFAULT_THEME = {
    "calendar": "a calm, bright start of the day",
    "cozy": "warmth, comfort and unhurried calm",
    "nature": "awakening, freshness and quiet joy",
    "heart": "kindness, gratitude and unity of hearts",
    "daily_energy": "clarity and inner balance",
    "intention": "a clear, gentle intention for the day",
    "affirmation": "trust, inner light and calm confidence",
    "special": "meaning and renewal",
    "birthday": "joy, warmth and celebration",
}
# короткий опис сюжету кожного промту — для {{RECENT_VISUALS}}
VISUAL_TAGS = {
    "calendar_1": "river and mountain lake from an overlook", "calendar_2": "botanical vase still life by a window",
    "calendar_3": "small spring cascade over stones", "cozy_1": "cup and book by a window",
    "cozy_2": "breakfast table with fruit", "cozy_3": "porch with a woven chair",
    "nature_1": "misty forest path", "nature_2": "near-water view of a river or lake",
    "nature_3": "open hills under a wide sky", "heart_1": "wreath or bouquet with a feather",
    "heart_2": "winding garden path to a glowing clearing", "heart_3": "candle still life with leaves",
    "daily_energy_1": "symbol beside a reflective pond", "daily_energy_2": "fork in a woodland path",
    "daily_energy_3": "two balanced stones at the water", "intention_1": "journal and candle on a desk",
    "intention_2": "seedling in a clay pot on a balcony", "intention_3": "path through a tree arch to light",
    "affirmation_1": "ivory parchment card", "affirmation_2": "pastel watercolor card",
    "affirmation_3": "dark violet starry card", "special_1": "arch of light over a lake",
    "special_2": "light and shadow landscape", "special_3": "festive tabletop arrangement",
    "birthday_1": "peony bouquet with a candle", "birthday_2": "cake by a window",
    "birthday_3": "golden ribbons and confetti",
}
# речення, які прибираються ЛИШЕ з посилання, якщо воно задовге (у тексті для копіювання лишаються)
_OPTIONAL_FOR_URL = (
    re.compile(r" Avoid these recent visual combinations: [^.]*\."),
    re.compile(r" Follow the Northern Hemisphere season [^.]*\."),
    re.compile(r" No deformed anatomy, [^.]*\."),
)


class PromptError(ValueError):
    pass


@dataclass
class ImagePrompt:
    prompt_id: str           # напр. calendar_2
    category_id: str
    category: str            # назва категорії українською
    name: str                # назва варіанта українською
    variant: int
    anchor: str
    theme: str
    symbol: str
    prompt: str              # повний заповнений текст

    @property
    def title(self) -> str:
        return f"{self.category} · {self.name}"


# ---------- бібліотека

_cache: dict[str, tuple[tuple, dict[str, dict]]] = {}


def load_library(folder: Path | str | None = None) -> dict[str, dict]:
    """Читає prompts/*.json. Перечитує, якщо файли змінилися; без основного файла — PromptError."""
    folder = Path(folder or PROMPTS_DIR)
    paths = [folder / n for n in PROMPT_FILES]
    try:
        stamp = tuple((str(p), p.stat().st_mtime_ns) for p in paths if p.exists())
    except OSError as e:
        raise PromptError(f"Не вдалося прочитати промти: {e}") from None
    if not paths[0].exists():
        raise PromptError(f"Не знайдено файл промтів: {paths[0]}")
    hit = _cache.get(str(folder))
    if hit and hit[0] == stamp:
        return hit[1]
    lib: dict[str, dict] = {}
    try:
        for p in paths:
            if p.exists():
                for item in json.loads(p.read_text(encoding="utf-8"))["prompts"]:
                    lib[item["id"]] = item
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise PromptError(f"Файл промтів пошкоджений: {e}") from None
    _cache[str(folder)] = (stamp, lib)
    return lib


# ---------- допоміжне

def date_uk(d: dt.date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def clean(value: str, limit: int = 160) -> str:
    """Тема приходить від користувача чи GPT: один рядок, без дужок шаблону, обмежена довжина."""
    value = re.sub(r"\s+", " ", (value or "").replace("{", "").replace("}", "")).strip(" .\"'«»")
    return value[:limit].rstrip()


def theme_symbol(theme: str, d: dt.date) -> str:
    low = theme.lower()
    for pattern, symbol in SYMBOL_RULES:
        if re.search(pattern, low):
            return symbol
    return SYMBOLS_FALLBACK[_seed(d, "symbol") % len(SYMBOLS_FALLBACK)]


def parse_theme_reply(reply: str) -> tuple[str, str]:
    """Відповідь GPT із рядками «THEME: …» і «SYMBOL: …» -> (тема, символ); чого немає — порожньо."""
    def grab(tag: str) -> str:
        m = re.search(rf"(?im)^\W*{tag}\W*[:\-–—]\s*(.+)$", reply or "")
        return clean(m.group(1), 120) if m else ""
    return grab("theme"), grab("symbol")


def fill(template: str, values: dict[str, str]) -> str:
    out = re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)
    left = sorted(set(re.findall(r"\{\{(\w+)\}\}", out)))
    if left:
        raise PromptError("Не заповнено поля промту: " + ", ".join(left))
    return out


def chatgpt_url(prompt: str) -> str:
    """Посилання, яке відкриває ChatGPT із промтом у полі введення (параметр ?q=)."""
    return f"{CHATGPT_URL}?q={quote(prompt, safe='')}"


def button_url(prompt: str, max_len: int | None = None) -> tuple[str | None, bool]:
    """(url, shortened). Якщо посилання задовге — по одному прибирає необов'язкові речення; не влізло — None."""
    max_len = max_len or MAX_BUTTON_URL
    url = chatgpt_url(prompt)
    if len(url) <= max_len:
        return url, False
    short = prompt
    for rx in _OPTIONAL_FOR_URL:
        short = rx.sub("", short)
        url = chatgpt_url(short)
        if len(url) <= max_len:
            return url, True
    return None, True


def candidates(lib: dict[str, dict], kind_id: str) -> list[dict]:
    cats = KIND_CATEGORIES.get(kind_id)
    if not cats:
        raise PromptError(f"Для рубрики «{kind_id}» не задано категорій промтів")
    order = {c: i for i, c in enumerate(cats)}
    found = [p for p in lib.values() if p["category_id"] in order]
    if not found:
        raise PromptError(f"У бібліотеці немає категорій: {', '.join(cats)}")
    return sorted(found, key=lambda p: (order[p["category_id"]], p["variant"]))


def pick(cands: list[dict], post_date: dt.date, last_used: dict[str, float], exclude: tuple[str, ...] = ()) -> dict:
    """Спершу ті, що ще не видавались (порядок прив'язано до дати), далі — виданий найдавніше."""
    pool = [p for p in cands if p["id"] not in exclude] or cands
    fresh = [p for p in pool if p["id"] not in last_used]
    if fresh:
        return min(fresh, key=lambda p: _seed(post_date, p["id"]))
    return min(pool, key=lambda p: last_used[p["id"]])


# ---------- головна функція

def build(kind_id: str, post_date: dt.date, *, theme: str = "", symbol: str = "",
          last_used: dict[str, float] | None = None, recent: list[tuple[str, str]] | None = None,
          exclude: tuple[str, ...] = (), variant: int | None = None, allow_explicit: bool = False,
          lib: dict[str, dict] | None = None) -> ImagePrompt:
    """Підбирає промт під рубрику й дату та заповнює всі поля.

    last_used: {prompt_id: час останньої видачі}; recent: останні видані (prompt_id, anchor), новіші першими.
    variant: 1–3, якщо варіант задано явно; allow_explicit дозволяє й промти з EXPLICIT_ONLY.
    """
    lib = lib if lib is not None else load_library()
    cands = candidates(lib, kind_id)
    if variant:
        cands = [p for p in cands if p["variant"] == variant] or cands
    elif not allow_explicit:
        cands = [p for p in cands if p["id"] not in EXPLICIT_ONLY] or cands
    chosen = pick(cands, post_date, last_used or {}, exclude)

    season, details, palette, botanicals, fruit, group = SEASONS[post_date.month]
    recent = recent or []
    used_anchors = {a for _, a in recent}
    pool = [a for a in ANCHORS[group] if a not in used_anchors] or ANCHORS[group]
    anchor = pool[_seed(post_date, chosen["id"], "anchor") % len(pool)]
    theme = clean(theme) or DEFAULT_THEME.get(chosen["category_id"], "a calm, bright start of the day")
    symbol = clean(symbol) or theme_symbol(theme, post_date)
    tags = list(dict.fromkeys(VISUAL_TAGS.get(pid, pid) for pid, _ in recent[:RECENT_LIMIT] if pid != chosen["id"]))

    values = {
        "DATE_ISO": post_date.isoformat(), "DATE_UK": date_uk(post_date), "WEEKDAY_UK": WEEKDAYS[post_date.weekday()],
        "SEASON": season, "SEASON_DETAILS": details, "SEASON_PALETTE": palette,
        "SEASONAL_BOTANICALS": botanicals, "SEASONAL_FRUIT": fruit,
        "DAY_THEME": theme, "THEME_SYMBOL": symbol, "CREATIVE_ANCHOR": anchor,
        "RECENT_VISUALS": ", ".join(tags) if tags else "none yet",
    }
    return ImagePrompt(prompt_id=chosen["id"], category_id=chosen["category_id"], category=chosen["category"],
                       name=chosen["name"], variant=chosen["variant"], anchor=anchor, theme=theme, symbol=symbol,
                       prompt=fill(chosen["prompt"], values))


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args or args[0] not in KIND_CATEGORIES:
        sys.exit("Приклад: python -m bot.image_prompts omnika 2026-11-17 [тема]\nРубрики: " + ", ".join(KIND_CATEGORIES))
    day = dt.date.fromisoformat(args[1]) if len(args) > 1 else dt.date.today()
    ip = build(args[0], day, theme=" ".join(args[2:]))
    url, short = button_url(ip.prompt)
    print(f"{ip.title} ({ip.prompt_id})", ip.prompt, url or "(посилання задовге — лише копіювання)", sep="\n\n")
