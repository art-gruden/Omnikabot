"""Рубрики постів: правила оформлення, промти для GPT, локальне форматування, фото дня.

Чотири рубрики:
  birthday — привітання з днем народження (текст + листівка);
  morning  — ранкове завантаження (листівка, фон можна згенерувати);
  omnika   — ранкове привітання центру «Омніка» (пост);
  unity    — налаштування на день «Єдність сердець» (пост + фото дня ddmm.jpg).

Оформлення працює двома шляхами: локально (типографіка, абзаци, емодзі, хештеги)
і через модель (враховує стиль попередніх постів). Рубрики з local_first=True
оформлюються локально завжди, а модель підключається кнопкою «Оформити з Gemini».
"""
from __future__ import annotations

import datetime as dt
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from textcard import render as R

PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", "/data/photos"))
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")
TG_CAPTION = 1024
TG_MESSAGE = 4096

# спільні правила для будь-якого оформлення тексту
COMMON_RULES = """Ти редактор україномовних постів для духовного центру. Твоє завдання — оформити наданий текст.
Залізні правила:
— Пиши лише українською, грамотно, без росіянізмів.
— НЕ вигадуй фактів, імен, дат, подій, посилань і цін, яких немає у вхідному тексті.
— Зберігай зміст і всі конкретні дані (імена, час, місце) без змін.
— Можна виправляти помилки, розставляти абзаци, додавати доречні емодзі та підсилювати ритм фраз.
— Без Markdown і HTML: ні зірочок, ні решіток, ні підкреслень для розмітки. Лише звичайний текст, емодзі та переноси рядків.
— У відповідь дай ТІЛЬКИ готовий текст посту, без пояснень, без лапок навколо і без заголовка «Ось текст»."""

IMAGE_COMMON = """Склади промт англійською для генератора зображень (gpt-image).
Залізні правила:
— У зображенні НЕ має бути жодного тексту, літер, цифр, написів, підписів і логотипів: текст накладається окремо.
— Опиши сцену, світло, кольори, настрій, стиль, композицію.
— Композиція має лишати спокійну, малодеталізовану зону в центрі кадру під текст.
— Горизонтальний кадр, фотографічна або живописна якість, без людей великим планом, без облич.
— У відповідь дай ТІЛЬКИ сам промт одним абзацом англійською, без пояснень і без лапок."""


@dataclass
class Kind:
    id: str
    name: str
    emoji: str
    output: str                      # card | post
    ask: str                         # що просимо надіслати
    rules: str                       # правила оформлення тексту
    compose_rules: str = ""          # правила написання тексту з нуля (якщо підтримується)
    image_style: str = ""            # підказка для промту зображення (українською, для GPT)
    image_style_en: str = ""         # те саме англійською — для запасного промту без GPT
    template: str = "pink_glow"      # типовий шаблон листівки
    title: str | None = None         # типовий заголовок листівки
    emojis: tuple[str, ...] = ("✨",)  # для локального форматування
    hashtags: str = ""
    use_day_photo: bool = False
    compose: bool = False            # чи можна згенерувати текст із нуля
    local_first: bool = False        # оформлювати локально, а модель — лише кнопкою
    auto_image: bool = False         # одразу після тексту скласти промт і намалювати картинку
    date_in_image: bool = False      # модель домальовує в кадрі сьогоднішню дату


KINDS: dict[str, Kind] = {
    "birthday": Kind(
        id="birthday", name="День народження", emoji="🎂", output="card", compose=True,
        ask=("Надішліть ім'я іменинника і кілька слів про нього — я складу привітання.\n"
             "Або надішліть свій готовий текст привітання — я його оформлю."),
        rules=(COMMON_RULES + "\n\nЦе привітання з днем народження. Тепло, щиро, світло, на «ви» або «ти» — "
               "як у вхідному тексті. Звертання на ім'я. 3–6 коротких рядків, доречні емодзі. "
               "Наприкінці — коротке побажання."),
        compose_rules=(COMMON_RULES + "\n\nНапиши привітання з днем народження українською за наданими даними про людину. "
                       "Тепло, щиро, без пафосу й штампів на кшталт «міцного здоров'я та успіхів у праці». "
                       "Звертання на ім'я. Текст піде на картинку, тому разом НЕ БІЛЬШЕ 30 слів: "
                       "2–4 короткі рядки й світле побажання наприкінці. БЕЗ емодзі й хештегів. "
                       "Якщо про людину відомо мало — пиши загальніше, але не вигадуй фактів."),
        image_style="Святковий настрій: квіти, тепле світло, ніжні кольори, святкова атмосфера.",
        image_style_en="Festive gentle mood: soft flowers, warm celebratory light, delicate pink and gold tones, floating light particles.",
        template="pink_glow", title="З ДНЕМ НАРОДЖЕННЯ", emojis=("🎂", "🌸", "✨", "💐", "🎉"),
    ),
    "morning": Kind(
        id="morning", name="Ранкове завантаження", emoji="🌅", output="card",
        ask="Надішліть текст ранкового завантаження (10–20 слів).",
        rules=(COMMON_RULES + "\n\nЦе короткий текст-афірмація для листівки «Енергозавантаження». "
               "10–20 слів, 1–3 речення від першої особи, теперішній час, ствердно. "
               "БЕЗ емодзі й без хештегів — текст піде на картинку. "
               "Одне ключове слово можеш виділити зірочками з обох боків, напр. *світло*."),
        image_style="Світанок, м'яке ранкове світло, енергія, духовний спокій, абстрактне сяйво або природа.",
        image_style_en="Sunrise light, soft morning glow, calm spiritual energy, airy gradients of rose, amber and gold.",
        template="pink_glow", title="ЕНЕРГОЗАВАНТАЖЕННЯ", emojis=("✨",),
    ),
    "omnika": Kind(
        id="omnika", name="Ранкове привітання «Омніка»", emoji="☀️", output="post",
        ask="Надішліть текст ранкового привітання центру «Омніка» — я його оформлю.",
        rules=(COMMON_RULES + "\n\nЦе ранкове привітання центру «Омніка» для Telegram. "
               "Структура: тепле звітання в першому рядку, далі 2–4 короткі абзаци по 1–3 речення, "
               "наприкінці світле побажання дня. Доречні емодзі на початку абзаців, не більше одного на абзац. "
               "Назву центру «Омніка» зберігай як у вхідному тексті."),
        local_first=True, date_in_image=True,
        image_style="Ранкове сонце, світло крізь листя або хмари, тепла золотава гама, умиротворення.",
        image_style_en="Morning sun, light through leaves or clouds, warm golden palette, peaceful quiet landscape.",
        template="sunset", emojis=("☀️", "🌿", "💛", "🙏", "✨"),
    ),
    "unity": Kind(
        id="unity", name="Єдність сердець — налаштування на день", emoji="💗", output="post",
        use_day_photo=True, auto_image=True,
        ask="Надішліть текст налаштування на день — я оформлю пост і додам фото на сьогодні.",
        rules=(COMMON_RULES + "\n\nЦе ранкове налаштування на день для спільноти «Єдність сердець». "
               "Структура: короткий заголовок-звертання, далі 2–4 абзаци по 1–3 речення — налаштування на день, "
               "наприкінці одне речення-побажання або коротка практика на день. "
               "Доречні емодзі, спокійний сердечний тон, звертання до спільноти."),
        image_style="Світло в серці, єдність, теплі рожево-золоті відтінки, м'яке сяйво, духовна атмосфера.",
        image_style_en="Inner heart light, unity and warmth, soft rose and gold glow, gentle luminous spiritual atmosphere.",
        template="lavender", emojis=("💗", "🤍", "🕊", "🌸", "✨"),
    ),
}

ORDER = ["birthday", "morning", "omnika", "unity"]

SHORTEN_RULES = ("Ти скорочуєш україномовний текст для картинки. Збережи зміст, ім'я й головне побажання, "
                 "прибери все зайве. Максимум {n} слів. Без емодзі, без розмітки, без пояснень — "
                 "у відповідь лише скорочений текст.")


def shorten_messages(text: str, n: int = 25) -> tuple[str, str]:
    return SHORTEN_RULES.format(n=n), text.strip()


def shorten_local(text: str, n: int = 25) -> str:
    """Запасне скорочення без GPT: лишаємо перші речення, що вкладаються в ліміт слів."""
    words, out, total = 0, [], 0
    for sent in _SENT.split(strip_markup(text)):
        w = len(sent.split())
        if out and total + w > n:
            break
        out.append(sent.strip())
        total += w
    if not out:
        return " ".join(strip_markup(text).split()[:n])
    return " ".join(out)


# ------------------------------------------------------------------ локальне форматування

_SENT = re.compile(r"(?<=[.!?…])\s+")


def strip_markup(text: str, keep_bold: bool = False) -> str:
    """Прибирає розмітку, яку могла додати модель (**жирний**, ## заголовки, — списки).

    keep_bold=True лишає **зірочки**: з них робиться жирний текст Telegram (див. post_html)."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)
    if not keep_bold:
        t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = re.sub(r"(?m)^\s*[-*]\s+", "— ", t)
    t = re.sub(r"^\s*[«\"']|[»\"']\s*$", "", t.strip())
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def local_format(kind: Kind, text: str, hashtags: str = "") -> str:
    """Оформлення без GPT: типографіка, абзаци, емодзі на початку абзаців, хештеги."""
    t = R.typography(strip_markup(text))
    if not t:
        return ""
    if kind.output == "card":
        return t
    paragraphs = [p.strip() for p in t.split("\n") if p.strip()]
    if len(paragraphs) == 1:                       # суцільний текст → ділимо по реченнях
        sent = [s.strip() for s in _SENT.split(paragraphs[0]) if s.strip()]
        paragraphs, buf = [], []
        for s in sent:
            buf.append(s)
            if len(buf) >= 2 or len(" ".join(buf)) > 180:
                paragraphs.append(" ".join(buf))
                buf = []
        if buf:
            paragraphs.append(" ".join(buf))
    out = []
    emojis = kind.emojis
    for i, p in enumerate(paragraphs):
        if unicodedata.category(p[0]) not in ("So", "Sk"):   # емодзі користувача не чіпаємо
            p = f"{emojis[i % len(emojis)]} {p}"
        out.append(p)
    body = "\n\n".join(out)
    tags = (hashtags or kind.hashtags).strip()
    if tags:
        body += "\n\n" + tags
    return body


def post_html(text: str) -> str:
    """Текст посту -> HTML для Telegram: **фрагмент** стає жирним, решта екранується.

    Кольору тексту в повідомленнях Telegram немає — колір може нести лише картинка."""
    import html
    out = html.escape(text, quote=False)
    if out.count("**") % 2:                      # непарна кількість — розмітка побилася, прибираємо
        out = out.replace("**", "")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)


def plain(text: str) -> str:
    """Текст без службових зірочок — для зразків стилю й для показу користувачеві."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)


SEASONS_UK = {12: "зима", 1: "зима", 2: "зима", 3: "весна", 4: "весна", 5: "весна",
              6: "літо", 7: "літо", 8: "літо", 9: "осінь", 10: "осінь", 11: "осінь"}


def season_uk(d: dt.date | None = None) -> str:
    """«17 листопада, пізня осінь» — для промтів, щоб сцена відповідала сезону."""
    d = d or dt.date.today()
    part = "початок" if d.day <= 10 else "середина" if d.day <= 20 else "кінець"
    return f"{date_uk(d)}, {part} сезону «{SEASONS_UK[d.month]}»"


def date_uk(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня",
              "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


def local_image_prompt(kind: Kind, text: str) -> str:
    """Запасний промт для зображення, коли GPT недоступний (англійською, без тексту в кадрі)."""
    style = kind.image_style_en or "Soft spiritual light, calm atmosphere."
    return (f"Soft symbolic background illustration. {style} "
            "Calm, low-detail area in the center of the frame for overlaid text. "
            "Horizontal composition, gentle gradients, warm diffused light, dreamy bokeh, high quality, "
            "no text, no letters, no numbers, no captions, no watermark, no logo, no people's faces.")


# ------------------------------------------------------------------ промти для GPT

def style_block(profile: str, samples: list[str], limit: int = 3) -> str:
    parts = []
    if profile:
        parts.append("Опис стилю цієї рубрики (виведений із попередніх постів автора):\n" + profile.strip())
    if samples:
        ex = "\n\n---\n\n".join(s.strip()[:900] for s in samples[:limit])
        parts.append("Приклади попередніх постів автора — наслідуй їхній тон, довжину й ритм "
                     "(але НЕ копіюй їхній зміст):\n\n" + ex)
    return "\n\n".join(parts)


MARKS_RULES = ("\n\nДодатково до правил вище: познач жирним головне послання посту. Обрами **двома зірочками** "
               "з обох боків 1–3 короткі фрагменти (кілька слів, не цілий абзац) — саме ті, що несуть головну думку. "
               "У Telegram вони стануть жирними. Це єдиний дозволений знак розмітки, інших не став.")


def format_messages(kind: Kind, text: str, profile: str = "", samples: list[str] | None = None,
                    extra: str = "", marks: bool = False) -> tuple[str, str]:
    sys = kind.rules + (MARKS_RULES if marks else "")
    block = style_block(profile, samples or [])
    if block:
        sys += "\n\n" + block
    if extra:
        sys += "\n\nДодаткові побажання автора: " + extra
    return sys, "Оформи цей текст:\n\n" + text.strip()


def compose_messages(kind: Kind, brief: str, profile: str = "", samples: list[str] | None = None,
                     extra: str = "") -> tuple[str, str]:
    sys = kind.compose_rules or kind.rules
    block = style_block(profile, samples or [])
    if block:
        sys += "\n\n" + block
    if extra:
        sys += "\n\nДодаткові побажання автора: " + extra
    return sys, "Дані для привітання:\n\n" + brief.strip()


def image_prompt_messages(kind: Kind, text: str, extra: str = "") -> tuple[str, str]:
    sys = IMAGE_COMMON + ("\n\nБажаний настрій і стиль: " + kind.image_style if kind.image_style else "")
    if extra:
        sys += "\n\nДодаткові побажання автора: " + extra
    return sys, ("Склади промт для фонового зображення до такого україномовного посту "
                 "(перекажи його настрій образами, не словами):\n\n" + text.strip()[:1500])


IMAGE_FROM_TEXT = """Ти складаєш промт англійською для генератора зображень Gemini до україномовного посту.
Спершу визнач для себе послання (головну думку) і настрій дня з тексту, а тоді опиши сцену, яка передає їх образами, а не словами.
Залізні правила:
— Опиши конкретну сцену: місце, час доби, світло, кольори, предмети, композицію, настрій.
— Пора року має відповідати сьогоднішній даті. {season}
— Горизонтальний кадр 16:9; верхня частина кадру — спокійна й малодеталізована (туди ляже напис).
— Без людей великим планом і без облич, без брендів і логотипів.
— НЕ пиши в промті жодних написів, цитат, імен і дат: написи додаються окремо.
— 60–110 слів, один абзац, англійською.
У відповідь дай ТІЛЬКИ промт англійською, без пояснень і без лапок."""


def image_from_text_messages(kind: Kind, text: str, season: str = "", previous: str = "") -> tuple[str, str]:
    """Запит до моделі: за текстом посту скласти (або оновити) промт для генератора зображень."""
    sys = IMAGE_FROM_TEXT.format(season=f"Сьогодні {season}." if season else "")
    if kind.image_style:
        sys += "\n\nБажаний настрій і стиль рубрики: " + kind.image_style
    user = "Текст посту:\n\n" + plain(text).strip()[:1500]
    if previous:
        user += ("\n\nПопередній промт був таким — склади відчутно іншу сцену (інше місце, інша композиція, "
                 "інший головний предмет), але з тим самим посланням:\n\n" + previous.strip()[:900])
    return sys, user


IMAGE_THEME = """Ти допомагаєш підібрати ілюстрацію до україномовного посту. Прочитай текст і дай рівно два рядки англійською:
THEME: настрій і зміст посту, 3–8 слів (наприклад: gratitude, quiet confidence, mindful choices)
SYMBOL: один простий предмет-символ цього настрою, з артиклем (наприклад: a single white feather)
Без імен людей, без дат і без назв свят. Лише ці два рядки, без пояснень і лапок."""


def image_theme_messages(kind: Kind, text: str) -> tuple[str, str]:
    """Запит до GPT: короткий англійський опис настрою посту для готового промту картинки."""
    return IMAGE_THEME, f"Рубрика: {kind.name}\n\nТекст:\n\n" + text.strip()[:1500]


def style_profile_messages(kind: Kind, samples: list[str]) -> tuple[str, str]:
    sys = ("Ти аналізуєш стиль автора україномовних постів. За наданими постами опиши стиль стисло й конкретно: "
           "тон і звертання, типова довжина й структура, характерні слова та звороти, уживання емодзі та хештегів, "
           "як починаються й закінчуються пости. 5–10 рядків, без води. "
           "У відповідь дай ТІЛЬКИ опис стилю.")
    joined = "\n\n---\n\n".join(s.strip()[:1200] for s in samples[:12])
    return sys, f"Пости рубрики «{kind.name}»:\n\n{joined}"


# ------------------------------------------------------------------ фото дня

def photo_names(date: dt.date) -> list[str]:
    d, m = f"{date.day:02d}", f"{date.month:02d}"
    stems = [f"{d}{m}", f"{d}-{m}", f"{d}.{m}", f"{d}_{m}", f"{m}{d}", f"{date.strftime('%Y-%m-%d')}"]
    return [s + e for s in stems for e in PHOTO_EXT]


def day_photo(date: dt.date | None = None, folder: Path | None = None) -> Path | None:
    """Фото на дату: файл ddmm.jpg (а також dd-mm, dd.mm, mmdd, YYYY-MM-DD) у папці фото."""
    folder = folder or PHOTOS_DIR
    date = date or dt.date.today()
    if not folder.is_dir():
        return None
    try:
        existing = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in photo_names(date):
        p = existing.get(name.lower())
        if p:
            return p
    return None


def photo_target(date: dt.date, folder: Path | None = None) -> Path:
    """Куди зберігати завантажене через бота фото на дату."""
    folder = folder or PHOTOS_DIR
    return folder / f"{date.day:02d}{date.month:02d}.jpg"


def split_for_telegram(text: str, with_photo: bool) -> list[str]:
    """Ріже текст під ліміти Telegram: підпис до фото 1024, повідомлення 4096."""
    limit = TG_CAPTION if with_photo else TG_MESSAGE
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
        limit = TG_MESSAGE
    return [p for p in parts if p]
