"""Генерація зображень через Gemini: адаптер промту, провайдер, повтори, запасна картинка, бот наскрізно.

Піднімає імітації Telegram Bot API (fake_tg) і Gemini API (fake_gemini), без мережі.

    python3 tests/test_gemini.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_gemini import KEY as GKEY, FakeGemini  # noqa: E402

TG_PORT, G_PORT = 18781, 18782
DATA = Path(tempfile.mkdtemp(prefix="cardbot-gemini-"))
PHOTOS = DATA / "photos"
PHOTOS.mkdir(parents=True)
os.environ.update({
    "DATA_DIR": str(DATA), "PHOTOS_DIR": str(PHOTOS),
    "IMAGE_PROVIDER": "gemini", "GEMINI_API_KEY": GKEY, "GEMINI_MODEL": "nano-banana-2-1k",
    "GEMINI_BASE_URL": f"http://127.0.0.1:{G_PORT}/v1beta",
    "GEMINI_TIMEOUT": "1.5", "IMAGE_RETRY_DELAY": "0.05", "GENERATED_IMAGES_KEEP": "6",
})
for k in ("ADMIN_IDS", "OPENAI_API_KEY", "GEMINI_API", "GEMINI_IMAGE_SIZE", "IMAGE_ATTEMPTS", "IMAGE_BACKUP_DIR"):
    os.environ.pop(k, None)

from PIL import Image  # noqa: E402

from fake_tg import FakeTelegram  # noqa: E402
from bot import gemini_prompt as GP  # noqa: E402
from bot import image_gen as IG  # noqa: E402
from bot import image_prompts as IP  # noqa: E402
from bot import main as M  # noqa: E402
from bot.db import DB  # noqa: E402

A = 5111
fake = FakeTelegram(TG_PORT)
gem = FakeGemini(G_PORT)
passed, failed = 0, []
CYR = re.compile("[А-Яа-яІіЇїЄєҐґ]")
OMNIKA_TEXT = ("доброго ранку друзі. сьогодні ми починаємо день з тиші та вдячності. "
               "нехай кожен знайде свій спокій. центр омніка з вами")


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


tap = LogTap()
logging.getLogger("textcard-bot").addHandler(tap)
logging.getLogger("textcard-bot").setLevel(logging.INFO)


def check(cond, label):
    global passed
    if cond:
        passed += 1
        print("  ok  ", label)
    else:
        failed.append(label)
        print("  FAIL", label)


def prompt_of(call) -> str:
    b = call["body"]
    return b["contents"][0]["parts"][0]["text"] if "contents" in b else b["input"][0]["text"]


# ---------------------------------------------------------------- 1. без мережі

def unit():
    print("1. Назва моделі, співвідношення сторін, вибір провайдера")
    check(IG.parse_gemini_model("nano-banana-2-1k") == ("gemini-3.1-flash-image", "1K"), "nano-banana-2-1k → gemini-3.1-flash-image, 1K")
    check(IG.parse_gemini_model("Nano-Banana-2.1-2K") == ("gemini-nano-banana-2.1", "2K"), "nano-banana-2.1-2k")
    check(IG.parse_gemini_model("nano-banana-pro-4k") == ("gemini-3-pro-image", "4K"), "nano-banana-pro-4k")
    check(IG.parse_gemini_model("gemini-3.1-flash-image") == ("gemini-3.1-flash-image", "1K"), "справжній ID лишається, розмір типовий")
    check(IG.parse_gemini_model("", "2k") == ("gemini-3.1-flash-image", "2K"), "порожньо → Nano Banana 2, розмір з GEMINI_IMAGE_SIZE")
    check([IG.nearest_ratio(*s) for s in ((1280, 782), (1376, 768), (1080, 1080), (1080, 1350), (1080, 1920))]
          == ["16:9", "16:9", "1:1", "4:5", "9:16"], "найближче співвідношення сторін")
    saved = {k: os.environ.get(k) for k in ("IMAGE_PROVIDER", "GEMINI_API_KEY")}
    os.environ.pop("IMAGE_PROVIDER")
    check(IG.provider_name() == "gemini", "без IMAGE_PROVIDER, але з ключем Gemini → gemini")
    os.environ.pop("GEMINI_API_KEY")
    check(IG.provider_name() == "openai", "без обох → openai (попередня поведінка)")
    os.environ["IMAGE_PROVIDER"] = "openai"
    os.environ["GEMINI_API_KEY"] = GKEY
    check(IG.provider_name() == "openai" and IG.make_provider(lambda: {}).name == "openai", "IMAGE_PROVIDER=openai перемикає назад")
    os.environ.update({k: v for k, v in saved.items() if v})

    print("2. Адаптер промту для Gemini")
    lib = IP.load_library()
    day = dt.date(2026, 11, 17)
    seen_rules = set()
    ok_all = True
    for kind in IP.KIND_CATEGORIES:
        for p in IP.candidates(lib, kind):
            ip = IP.build(kind, day, lib={p["id"]: p}, allow_explicit=True, theme="gratitude and calm")
            out = GP.adapt(ip.prompt, category=ip.category_id, date=day, variation="a low viewpoint",
                           avoid=["misty forest path"])
            rule = GP.CATEGORY_RULES[ip.category_id]
            seen_rules.add(ip.category_id)
            good = (GP.GEMINI_BASE in out and rule in out and not CYR.search(out) and "2026-11-17" not in out
                    and "mid-November" in out and "late autumn" in out and "{{" not in out
                    and "Composition for this generation: a low viewpoint." in out and "misty forest path" in out
                    and (GP.OVERRIDE_NOTE in out) == (ip.category_id in GP.CENTER_TEXT))
            if not good:
                ok_all = False
                print("       не пройшов:", p["id"])
    check(ok_all, "усі 27 промтів: загальний блок, правило категорії, сезон, без кирилиці й дати цифрами")
    check(seen_rules == set(GP.CATEGORY_RULES) - {"special"} and "special" in GP.CATEGORY_RULES,
          "правила є для всіх категорій, зокрема special")
    for needle in ("Do not render any text, letters, words, numbers, dates, watermarks, or logos",
                   "Leave the upper 20–25% of the image visually calm", "Preferred aspect ratio: 16:9",
                   "fresh, visually distinct composition", "Follow the season appropriate to the date"):
        check(needle in GP.GEMINI_BASE, f"у загальному блоці: «{needle[:44]}…»")
    own = GP.adapt('Soft sunrise, a card with “Доброго ранку!” and «17 листопада» (вівторок)', date=day)
    check(not CYR.search(own) and "Category-specific" not in own and GP.GEMINI_BASE in own,
          "власний промт: написи кирилицею прибрано, блок категорії не додається")
    check(GP.adapt("x", category="calendar", date=dt.date(2026, 1, 5)).count("midwinter") == 1
          and "early-January" in GP.adapt("x", date=dt.date(2026, 1, 5)), "сезон береться з дати")

    print("3. Варіації композиції не повторюються")
    used: list[str] = []
    for _ in range(len(GP.VARIATIONS_SCENE)):
        used.insert(0, GP.pick_variation("nature", used))
    check(len(set(used)) == len(GP.VARIATIONS_SCENE), "коло варіацій без повторів")
    check(GP.pick_variation("nature", used) == used[-1], "далі — та, що була найдавніше")
    check(GP.pick_variation("affirmation", []) in GP.VARIATIONS_BACKGROUND, "для фонів — свій набір варіацій")


# ---------------------------------------------------------------- 2. провайдер і генератор

async def provider_tests():
    db = DB(DATA / "unit.sqlite3")
    gen = IG.ImageGenerator(db, DATA / "unit", lambda: {})
    p = gen.provider
    check(p.name == "gemini" and p.model == "gemini-3.1-flash-image" and p.image_size == "1K" and p.attempts == 3,
          "провайдер із .env: gemini, gemini-3.1-flash-image, 1K, 3 спроби")

    print("4. Вдала генерація: запит, один файл, метадані, історія")
    n = gem.mark()
    img = await gen.generate("A calm misty lake at dawn.", width=1376, height=768, uid=7, kind="omnika",
                             prompt_id="nature_2", category="nature")
    call = gem.since(n)[0]
    check(len(gem.since(n)) == 1 and call["api"] == "generateContent" and call["model"] == "gemini-3.1-flash-image",
          "один запит до models/gemini-3.1-flash-image:generateContent")
    check(call["key"] == GKEY and GKEY not in call["url"], "ключ у заголовку x-goog-api-key, не в адресі")
    cfg = call["body"]["generationConfig"]
    check(cfg["responseModalities"] == ["IMAGE"] and cfg["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "1K"},
          "просимо лише зображення, 16:9, 1K")
    sent = prompt_of(call)
    check(sent.startswith("A calm misty lake at dawn.") and GP.GEMINI_BASE in sent and GP.CATEGORY_RULES["nature"] in sent,
          "у промті: вихідний текст + загальний блок + правило категорії")
    f = Path(img.path)
    check(f.is_file() and f.parent == DATA / "unit" / "generated" and f.read_bytes() == img.data
          and Image.open(f).size == (1376, 768), "зображення збережено локально")
    check(f.name.startswith(f"{dt.date.today().isoformat()}_omnika_nature_2_") and f.suffix == ".png", f"назва файла: {f.name}")
    meta = img.meta()
    check((meta["provider"], meta["model"], meta["path"], meta["prompt"], meta["source_prompt"]) ==
          ("gemini", "gemini-3.1-flash-image", str(f), sent, "A calm misty lake at dawn.") and "data" not in meta,
          "метадані: провайдер, модель, промт, шлях")
    check((img.width, img.height, img.attempts, img.fallback, img.variation) == (1376, 768, 1, False, GP.VARIATIONS_SCENE[0]),
          "метадані: розмір, спроби, варіація")
    row = db.image_last()
    check((row["status"], row["provider"], row["prompt_id"], row["variation"], row["path"]) ==
          ("ok", "gemini", "nature_2", img.variation, str(f)), "запис в історії генерацій")

    print("5. Історія зменшує повтори")
    n = gem.mark()
    img2 = await gen.generate("A calm misty lake at dawn.", width=1376, height=768, uid=7, kind="omnika",
                              prompt_id="nature_2", category="nature")
    sent2 = prompt_of(gem.since(n)[0])
    check(img2.variation == GP.VARIATIONS_SCENE[1] and img2.variation != img.variation, "друга генерація — інша композиція")
    check("Previously generated scenes to stay clearly different from: near-water view of a river or lake" in sent2
          and img.variation in sent2, "попередню сцену передано як «не повторювати»")
    check(sent2 != sent and img2.path != img.path, "той самий вихідний промт дає інший запит і окремий файл")
    n = gem.mark()
    await gen.generate("x", width=1376, height=768, uid=7, kind="unity", category="heart")
    check("Previously generated" not in prompt_of(gem.since(n)[0]), "історія ведеться окремо для кожної рубрики")

    print("6. Повтори, тайм-аут і відмови")
    gem.script = [("status", 503, "The model is overloaded"), ("status", 429, "quota")]
    n = gem.mark()
    tap.lines.clear()
    img = await gen.generate("retry me", width=1376, height=768, uid=7, kind="omnika")
    check(img.attempts == 3 and len(gem.since(n)) == 3, "503, 429, далі успіх — три спроби")
    check(sum("не вдалася" in ln for ln in tap.lines) == 2 and any("намалював" in ln for ln in tap.lines),
          "кожна спроба записана в лог")
    check(not any("retry me" in ln or GKEY in ln for ln in tap.lines), "у лозі немає ні тексту промту, ні ключа")

    gem.script = [("status", 503, "x")] * 3
    n = gem.mark()
    try:
        await gen.generate("fail", width=1376, height=768, uid=7, kind="omnika")
        check(False, "три невдачі мають дати помилку")
    except IG.ImageGenError as e:
        check(len(gem.since(n)) == 3 and e.retryable and e.fallback_ok and "503" in str(e), f"три спроби й помилка: {e}")
    check(db.image_last()["status"] == "failed" and db.image_last()["attempts"] == 3, "невдачу записано в історію")
    check(not gem.script, "четвертого запиту не було")

    gem.script = [("slow", 3.0)]
    n = gem.mark()
    t0 = time.time()
    img = await gen.generate("slow", width=1376, height=768, uid=7, kind="omnika")
    check(img.attempts == 2 and len(gem.since(n)) == 2 and time.time() - t0 < 3.0,
          f"тайм-аут спроби ({p.timeout} с) → повтор ({time.time() - t0:.1f} с)")

    gem.script = [("noimage", "NO_IMAGE")]
    img = await gen.generate("noimage", width=1376, height=768, uid=7, kind="omnika")
    check(img.attempts == 2, "відповідь без зображення → повтор")

    gem.script = [("thought",)]
    img = await gen.generate("thought", width=1376, height=768, uid=7, kind="omnika")
    check((img.width, img.height) == (1376, 768), "рівно одне зображення: чернетку-«думку» пропущено")

    gem.script = [("block", "PROHIBITED_CONTENT")]
    n = gem.mark()
    try:
        await gen.generate("blocked", width=1376, height=768, uid=7, kind="omnika")
        check(False, "блокування промту має дати помилку")
    except IG.ImageGenError as e:
        check(len(gem.since(n)) == 1 and not e.retryable and "правила вмісту" in str(e), "заблокований промт не повторюється")

    os.environ["GEMINI_API_KEY"] = "wrong-key"
    bad = IG.ImageGenerator(db, DATA / "unit", lambda: {})
    n = gem.mark()
    try:
        await bad.generate("x", width=1376, height=768)
        check(False, "неправильний ключ має дати помилку")
    except IG.ImageGenError as e:
        check(len(gem.since(n)) == 1 and "GEMINI_API_KEY" in str(e), "неправильний ключ: одна спроба, зрозуміле повідомлення")
    os.environ["GEMINI_MODEL"] = "no-such-model"
    os.environ["GEMINI_API_KEY"] = GKEY
    try:
        await IG.ImageGenerator(db, DATA / "unit", lambda: {}).generate("x", width=1376, height=768)
        check(False, "невідома модель має дати помилку")
    except IG.ImageGenError as e:
        check("GEMINI_MODEL" in str(e) and "404" in str(e), "невідома модель: підказка про GEMINI_MODEL")
    os.environ["GEMINI_MODEL"] = "nano-banana-2-1k"
    os.environ.pop("GEMINI_API_KEY")
    os.environ["IMAGE_PROVIDER"] = "gemini"
    nokey = IG.ImageGenerator(db, DATA / "unit", lambda: {})
    n = gem.mark()
    try:
        await nokey.generate("x", width=1376, height=768)
        check(False, "без ключа має бути помилка")
    except IG.ImageGenError as e:
        check(not nokey.provider.ready() and len(gem.since(n)) == 0 and not e.fallback_ok, "без ключа до Gemini не ходимо")
    os.environ["GEMINI_API_KEY"] = GKEY

    print("7. Розмір 2K, режим interactions, прибирання старих файлів")
    os.environ.update({"GEMINI_MODEL": "nano-banana-2.1-2k", "GEMINI_API": "interactions"})
    g2 = IG.ImageGenerator(db, DATA / "unit", lambda: {})
    n = gem.mark()
    img = await g2.generate("square", width=1080, height=1080, uid=7)
    call = gem.since(n)[0]
    check(call["api"] == "interactions" and call["body"]["model"] == "gemini-nano-banana-2.1"
          and call["body"]["response_format"] == {"type": "image", "mime_type": "image/png", "aspect_ratio": "1:1",
                                                  "image_size": "2K"}, "interactions: модель і response_format")
    check((img.width, img.height) == (2048, 2048), "зображення з відповіді interactions прочитано")
    os.environ.pop("GEMINI_API")
    os.environ["GEMINI_MODEL"] = "nano-banana-2-1k"
    files = [f for f in (DATA / "unit" / "generated").iterdir()]
    check(len(files) == 6, f"зберігаються лише останні GENERATED_IMAGES_KEEP файлів ({len(files)})")


# ---------------------------------------------------------------- 3. бот наскрізно

async def wait(mark, pred, timeout=25.0, label=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for c in fake.since(mark):
            if pred(c):
                return c
        await asyncio.sleep(0.03)
    raise AssertionError(f"не дочекався: {label}")


async def settle(mark, quiet=0.6, timeout=25.0):
    t0, last, n = time.time(), time.time(), fake.mark()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.05)
        if fake.mark() != n:
            n, last = fake.mark(), time.time()
        elif time.time() - last > quiet and n > mark:
            break
    return fake.since(mark)


def is_(method, text=None):
    def f(c):
        if c["method"] != method or int(c["params"].get("chat_id", 0)) != A:
            return False
        return text is None or text in (c["params"].get("text") or c["params"].get("caption") or "")
    return f


def cur(c):
    r = c["result"]
    return fake.messages.get((r["chat"]["id"], r["message_id"]), r)


def buttons(msg):
    return [b for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def btn(msg, part):
    for b in buttons(msg):
        if part in b["text"] and b.get("callback_data"):
            return b["callback_data"]
    raise AssertionError(f"нема кнопки «{part}»: {[b['text'] for b in buttons(msg)]}")


def img_size(c):
    return Image.open(io.BytesIO(c["files"]["photo"][1])).size


async def scenario(cb):
    print("8. Бот із IMAGE_PROVIDER=gemini і без ключа OpenAI")
    m = fake.mark()
    fake.send_text(A, "/start")
    await settle(m)
    check(cb.image_ready() and not cb.oai_ready() and cb.image_label() == "Gemini", "картинки готові без ключа OpenAI")
    m = fake.mark()
    fake.send_text(A, "/menu")
    mn = cur(await wait(m, is_("sendMessage", "Налаштування")))
    fake.press(A, mn, "m:admin")
    await asyncio.sleep(0.6)
    check("Зображення малює: Gemini (gemini-3.1-flash-image, 1K)" in fake.messages[(A, mn["message_id"])]["text"],
          "в адмін-меню видно провайдера й модель")

    print("9. Пост «Омніка»: текст оформлено локально, картинку малює Gemini")
    m = fake.mark()
    fake.send_text(A, "/omnika " + OMNIKA_TEXT)
    calls = await settle(m)
    post = cur([c for c in calls if is_("sendMessage")(c) and "ранку" in c["params"]["text"]][0])
    check("Згенерувати картинку" in str(buttons(post)), "кнопка генерації є")
    n = gem.mark()
    m = fake.mark()
    fake.press(A, post, btn(post, "Згенерувати картинку"))
    c = await wait(m, is_("sendPhoto"), label="ілюстрація")
    check(img_size(c) == (1376, 768) and "Картинка до посту" in c["params"].get("caption", ""), f"ілюстрація 16:9 {img_size(c)}")
    check(any(is_("sendMessage", "через Gemini")(x) for x in fake.since(m)), "повідомлення «Малюю через Gemini»")
    call = gem.since(n)[0]
    sent = prompt_of(call)
    did = btn(post, "Промт картинки").split(":")[2]
    d = cb.db.draft(did)
    meta = json.loads(d["extra"])["ip"]
    cat = IP.load_library()[meta["id"]]["category_id"]
    check(cat in IP.KIND_CATEGORIES["omnika"] and GP.CATEGORY_RULES[cat] in sent and GP.GEMINI_BASE in sent,
          f"готовий промт рубрики + блоки Gemini (категорія {cat})")
    check(not CYR.search(sent) and dt.date.today().isoformat() not in sent, "у запиті до Gemini немає кирилиці й дати цифрами")
    check(IP.date_uk(dt.date.today()) in d["prompt"], "сам промт у чернетці не змінено (для ChatGPT і копіювання)")
    check(call["body"]["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "1K"}, "16:9, 1K")
    row = cb.db.image_last(A)
    check(row["status"] == "ok" and row["kind"] == "omnika" and row["prompt_id"] == meta["id"] and Path(row["path"]).is_file()
          and Path(row["path"]).parent == DATA / "generated", "файл збережено в /data/generated, історію записано")
    check(cb.db.usage_count(A, "image") == 1, "генерацію враховано в денному ліміті")

    print("10. Листівка «Ранкове завантаження»: фон від Gemini, текст поверх")
    m = fake.mark()
    fake.send_text(A, "/morning Нехай світло наповнює мої думки, слова та вчинки щодня.")
    card = (await wait(m, is_("sendPhoto"), label="листівка"))["result"]
    tpl_before = len(cb.store.all())
    n = gem.mark()
    m = fake.mark()
    fake.press(A, card, btn(card, "Згенерувати картинку"))
    c = await wait(m, is_("sendPhoto", "Новий фон від Gemini"), timeout=40, label="листівка на новому фоні")
    check(img_size(c) == (1280, 782), f"розмір листівки не змінився {img_size(c)}")
    sent = prompt_of(gem.since(n)[0])
    check(GP.CATEGORY_RULES["affirmation"] in sent and GP.OVERRIDE_NOTE in sent, "фон афірмації: правило спокійного центру")
    check(gem.since(n)[0]["body"]["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9", "для 1280×782 просимо 16:9")
    check(len(cb.store.all()) == tpl_before + 1, "фон збережено як шаблон (як і раніше)")

    print("11. Звичайна листівка: кнопка нового фону")
    m = fake.mark()
    fake.send_text(A, "Я відкриваю своє серце присутності Творця і світлу нового дня.")
    plain = (await wait(m, is_("sendPhoto")))["result"]
    check("Новий фон через Gemini" in str(buttons(plain)), "підпис кнопки називає Gemini")
    m = fake.mark()
    fake.press(A, plain, "r:bg")
    c = await wait(m, is_("sendPhoto"), timeout=40)
    check(img_size(c) == (1280, 782), "листівку перемальовано на фоні від Gemini")

    print("12. Три невдалі спроби: запасна картинка або безпечна зупинка")
    gem.script = [("status", 503, "The model is overloaded")] * 3
    n = gem.mark()
    m = fake.mark()
    fake.press(A, post, btn(post, "Згенерувати картинку"))
    await wait(m, is_("sendMessage", "Запасної картинки немає"), label="немає запасної")
    check(len(gem.since(n)) == 3, "рівно три спроби")
    check(any(is_("sendMessage", "тимчасово недоступний (503)")(x) for x in fake.since(m)), "причину показано користувачеві")
    check(not any(x["method"] == "sendPhoto" for x in fake.since(m)), "без запасної картинки нічого не надсилаємо")

    today = dt.date.today()
    Image.new("RGB", (1200, 800), (120, 60, 140)).save(PHOTOS / f"{today.day:02d}{today.month:02d}.jpg", "JPEG")
    gem.script = [("status", 503, "The model is overloaded")] * 3
    used_before = cb.db.usage_count(A, "image")
    m = fake.mark()
    fake.press(A, post, btn(post, "Згенерувати картинку"))
    c = await wait(m, is_("sendPhoto", "Запасна картинка"), label="запасна картинка")
    check(f"{today.day:02d}{today.month:02d}.jpg" in c["params"]["caption"] and img_size(c) == (1200, 800),
          "надіслано фото дня як запасну картинку")
    check(cb.db.image_last(A)["status"] == "backup" and cb.db.usage_count(A, "image") == used_before,
          "запасну записано в історію, ліміт не витрачено")

    gem.script = [("status", 503, "x")] * 3
    m = fake.mark()
    fake.press(A, card, btn(card, "Згенерувати картинку"))
    await wait(m, is_("sendMessage", "Листівка лишається на поточному фоні"), label="листівка без змін")
    check(not any(x["method"] == "sendPhoto" for x in fake.since(m)), "листівку не чіпаємо")
    check(not gem.script, "сценарій помилок вичерпано рівно трьома спробами")

    print("13. Після збою наступна генерація працює")
    m = fake.mark()
    fake.press(A, post, btn(post, "Згенерувати картинку"))
    c = await wait(m, is_("sendPhoto", "Картинка до посту"), label="знову працює")
    check(img_size(c) == (1376, 768), "нова ілюстрація після збою")


async def amain():
    fake.start()
    gem.start()
    unit()
    try:
        await provider_tests()
    except Exception:
        traceback.print_exc()
        failed.append("виняток у тестах провайдера")
    app, cb = M.build_app("123:TEST", DATA, f"http://127.0.0.1:{TG_PORT}/bot", f"http://127.0.0.1:{TG_PORT}/file/bot")
    async with app:
        await M._post_init(app)
        await app.start()
        await app.updater.start_polling(poll_interval=0.0, timeout=1)
        try:
            await scenario(cb)
        except Exception:
            traceback.print_exc()
            failed.append("виняток у сценарії бота")
        finally:
            await app.updater.stop()
            await app.stop()
    fake.stop()
    gem.stop()
    errs = [c for c in fake.calls if c["method"] == "sendMessage" and "Щось пішло не так" in c["params"].get("text", "")]
    check(not errs, f"жодних необроблених помилок ({len(errs)})")
    leaked = [c for c in fake.calls if GKEY in json.dumps(c["params"], ensure_ascii=False, default=str)]
    check(not leaked and not any(GKEY in ln for ln in tap.lines), "ключ Gemini не світиться ні в повідомленнях, ні в лозі")


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(amain())
    print(f"\n{passed} ok, {len(failed)} fail за {time.time() - t0:.1f} с")
    if failed:
        print("Провалено:", *failed, sep="\n  - ")
        sys.exit(1)
