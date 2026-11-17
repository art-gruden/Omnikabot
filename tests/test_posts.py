"""Наскрізний тест рубрик, оформлення текстів і генерації зображень.

Піднімає імітацію Telegram Bot API (fake_tg) та імітацію OpenAI API (fake_openai), без мережі.

    python3 tests/test_posts.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import io
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DATA = Path(tempfile.mkdtemp(prefix="cardbot-posts-"))
PHOTOS = DATA / "photos"
PHOTOS.mkdir(parents=True)
os.environ["DATA_DIR"] = str(DATA)
os.environ["PHOTOS_DIR"] = str(PHOTOS)
os.environ["AUTO_IMAGE"] = "0"       # автокартинку для «Єдності сердець» перевіряє tests/test_gemini.py
os.environ.pop("ADMIN_IDS", None)
for _v in ("GEMINI_API_KEY", "IMAGE_PROVIDER", "TEXT_PROVIDER"):
    os.environ.pop(_v, None)
os.environ.pop("OPENAI_API_KEY", None)

from PIL import Image  # noqa: E402

from fake_openai import KEY as OAI_KEY, FakeOpenAI  # noqa: E402
from fake_tg import FakeTelegram  # noqa: E402
from bot import main as M  # noqa: E402
from bot import image_prompts as IP  # noqa: E402

TG_PORT, OAI_PORT = 18771, 18772
A, B = 4111, 4222
fake = FakeTelegram(TG_PORT)
oai = FakeOpenAI(OAI_PORT)
passed, failed = 0, []

OMNIKA_TEXT = ("доброго ранку друзі. сьогодні ми починаємо день з тиші та вдячності. "
               "нехай кожен знайде свій спокій. центр омніка з вами")
UNITY_TEXT = "сьогодні налаштовуємось на день у єдності сердець хай тепло всередині веде нас крізь усі справи"
BDAY_BRIEF = "Наталія, 45 років, любить йогу, квіти й гори"


def check(cond, label):
    global passed
    if cond:
        passed += 1
        print("  ok  ", label)
    else:
        failed.append(label)
        print("  FAIL", label)


async def wait(mark, pred, timeout=20.0, label=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for c in fake.since(mark):
            if pred(c):
                return c
        await asyncio.sleep(0.03)
    raise AssertionError(f"не дочекався: {label}")


async def settle(mark, quiet=0.5, timeout=25.0):
    t0, last, n = time.time(), time.time(), fake.mark()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.05)
        if fake.mark() != n:
            n, last = fake.mark(), time.time()
        elif time.time() - last > quiet and n > mark:
            break
    return fake.since(mark)


def is_(method, chat=None, text=None):
    def f(c):
        if c["method"] != method:
            return False
        if chat is not None and int(c["params"].get("chat_id", 0)) != chat:
            return False
        if text is not None and text not in (c["params"].get("text") or c["params"].get("caption") or ""):
            return False
        return True
    return f


def cur(c):
    r = c["result"]
    return fake.messages.get((r["chat"]["id"], r["message_id"]), r)


def buttons(msg):
    return [b for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def btn(msg, part):
    for b in buttons(msg):
        if part in b["text"]:
            return b["callback_data"]
    raise AssertionError(f"нема кнопки «{part}»: {[b['text'] for b in buttons(msg)]}")


def url_btn(msg):
    """Адреса кнопки-посилання «Відкрити в ChatGPT» або None."""
    return next((b["url"] for b in buttons(msg) if b.get("url")), None)


def url_prompt(msg):
    return parse_qs(urlparse(url_btn(msg)).query)["q"][0]


def draft_of(cb, msg):
    did = btn(msg, "Скопіювати промт").split(":")[2]
    d = cb.db.draft(did)
    return d, (json.loads(d["extra"] or "{}").get("ip") or {})


def cat_of(prompt_id):
    return IP.load_library()[prompt_id]["category_id"]


async def press(uid, msg, data, quiet=0.5):
    m = fake.mark()
    fake.press(uid, msg, data)
    await wait(m, is_("answerCallbackQuery"), label=f"answer {data}")
    return await settle(m, quiet)


async def menu(uid):
    m = fake.mark()
    fake.send_text(uid, "/menu")
    return cur(await wait(m, is_("sendMessage", uid, "Налаштування"), label="меню"))


def img_size(c):
    key = "photo" if c["method"] == "sendPhoto" else "document"
    return Image.open(io.BytesIO(c["files"][key][1])).size


async def scenario(cb):
    print("1. Старт, адмін, ключ OpenAI ще не заданий")
    m = fake.mark()
    fake.send_text(A, "/start")
    await settle(m)
    check(cb.is_admin(A), "A — адмін")
    check(not cb.oai_ready(), "ключа OpenAI немає")

    print("2. Рубрики доступні без ключа, оформлення локальне")
    m = fake.mark()
    fake.send_text(A, "/post")
    posts = cur(await wait(m, is_("sendMessage", A, "Пости")))
    check("не підключений" in posts["text"], "попередження про відсутній ChatGPT")
    m = fake.mark()
    fake.press(A, posts, btn(posts, "Омніка"))
    ask = cur(await wait(m, is_("sendMessage", A, "Надішліть текст")))
    check("Потрібен ключ OpenAI" in ask["text"], "підказка про просте оформлення")
    m = fake.mark()
    fake.send_text(A, OMNIKA_TEXT)
    calls = await settle(m)
    post = [c for c in calls if c["method"] == "sendMessage" and "ранку" in c["params"].get("text", "")]
    check(post, "пост оформлено без GPT")
    if post:
        txt = post[0]["params"]["text"]
        check("\n\n" in txt and txt[0] in "☀️🌿💛🙏✨", f"абзаци й емодзі: {txt[:40]!r}")
    if post:
        pmsg = cur(post[0])
        check("Згенерувати картинку" not in str(buttons(pmsg)), "без ключа кнопки генерації немає")
        m = fake.mark()
        fake.press(A, pmsg, btn(pmsg, "Промт картинки"))
        pm0 = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
        d0, ip0 = draft_of(cb, pm0)
        check(cat_of(ip0["id"]) in IP.KIND_CATEGORIES["omnika"], f"без ключа: готовий промт рубрики ({ip0['id']})")
        check("{{" not in pm0["text"] and IP.date_uk(dt.date.today()) in pm0["text"], "поля промту заповнено, дата сьогоднішня")
        check(url_btn(pm0) and url_btn(pm0).startswith("https://chatgpt.com/?q=") and url_prompt(pm0) == d0["prompt"],
              "кнопка «Відкрити в ChatGPT» несе той самий промт")
        check("Згенерувати" not in str(buttons(pm0)) and "Інший промт" in str(buttons(pm0)), "кнопки під промтом без ключа")
    check(len(oai.calls) == 0, "до OpenAI не ходили")

    print("3. Адмін вводить ключ OpenAI; повідомлення з ключем видаляється")
    mn = await menu(A)
    await press(A, mn, "m:admin")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, btn(mn, "OpenAI"))
    mn = fake.messages[(A, mn["message_id"])]
    check("не задано" in mn["text"], "екран OpenAI")
    m = fake.mark()
    fake.press(A, mn, "i:oai_key")
    await wait(m, is_("sendMessage", A, "ключ OpenAI"))
    m = fake.mark()
    fake.send_text(A, "короткий")
    await wait(m, is_("sendMessage", A, "не схоже на ключ"))
    m = fake.mark()
    fake.send_text(A, OAI_KEY)
    await wait(m, is_("sendMessage", A, "Ключ збережено"))
    check(any(c["method"] == "deleteMessage" for c in fake.since(m)), "повідомлення з ключем видалено")
    check(cb.oai_ready() and OAI_KEY not in str(fake.calls[-1]["params"].get("text", "")), "ключ збережено й замаскований")
    m = fake.mark()
    fake.press(A, mn, "o:check")
    c = await wait(m, is_("sendMessage", A, "Ключ працює"))
    check(True, "перевірка ключа")

    print("4. Вибір моделей зі списку OpenAI")
    await press(A, mn, "o:models:image")
    mm = fake.messages[(A, mn["message_id"])]
    m = fake.mark()
    fake.press(A, mm, "o:fetch:image")
    await settle(m)
    mm = fake.messages[(A, mn["message_id"])]
    check("gpt-image-1-mini" in str(buttons(mm)), "список моделей підтягнуто")
    await press(A, mm, "o:setmodel:image:gpt-image-1-mini")
    check(cb.oai_cfg()["image_model"] == "gpt-image-1-mini", "модель зображень збережено")
    await press(A, mm, "o:setmodel:image:gpt-image-1.5")

    print("5. «Омніка»: текст простими засобами, далі кнопка «Оформити з ChatGPT»")
    cb.db.add_sample("omnika", "Доброго ранку, рідні! ☀️ Сьогодні день тиші. Бережіть себе.", A)
    cb.db.add_sample("omnika", "Доброго ранку! 🌿 Нехай справи йдуть легко. Центр «Омніка» з вами.", A)
    n0 = oai.mark()
    m = fake.mark()
    fake.send_text(A, "/omnika " + OMNIKA_TEXT)
    calls = await settle(m)
    local = [c for c in calls if is_("sendMessage", A)(c) and "ранку" in c["params"].get("text", "")]
    check(local and not [c for c in oai.since(n0) if c["what"] == "chat"], "текст оформлено локально, без GPT")
    lmsg = cur(local[0]) if local else None
    check(lmsg and "Оформити з ChatGPT" in str(buttons(lmsg)), "під чернеткою є кнопка оформлення моделлю")
    check(any(is_("sendMessage", A, "простими засобами")(c) for c in calls), "бот пояснює, як оформив")

    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, lmsg, btn(lmsg, "Оформити з ChatGPT"))
    calls = await settle(m)
    chat_calls = [c for c in oai.since(n0) if c["what"] == "chat"]
    check(len(chat_calls) >= 2, f"виклики GPT: стиль + оформлення ({len(chat_calls)})")
    sys_prompts = " ".join(c["body"]["messages"][0]["content"] for c in chat_calls)
    check("Приклади попередніх постів" in sys_prompts, "зразки передано в промт")
    check("Опис стилю цієї рубрики" in sys_prompts, "профіль стилю передано")
    check("двома зірочками" in sys_prompts, "правило виділяти головне жирним")
    check(all("max_tokens" not in c["body"] for c in chat_calls), "надіслано max_completion_tokens")
    post = [c for c in calls if is_("sendMessage", A)(c) and "Нехай цей день" in c["params"].get("text", "")]
    check(post, "пост від GPT надіслано")
    draft_msg = cur(post[0]) if post else None
    check(draft_msg and "Згенерувати картинку" in str(buttons(draft_msg)), "кнопки чернетки")

    print("6. Готовий промт рубрики: тема від GPT, кнопки ChatGPT і копіювання")
    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, draft_msg, btn(draft_msg, "Промт картинки"))
    pm = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    d, ip = draft_of(cb, pm)
    check(cat_of(ip["id"]) in IP.KIND_CATEGORIES["omnika"], f"промт із категорій рубрики «Омніка» ({ip['id']})")
    check(ip["title"] in pm["text"], "у повідомленні назва категорії й варіанта")
    check("gratitude and quiet morning light" in d["prompt"], "тему посту від GPT підставлено в промт")
    themed = [c for c in oai.since(n0) if c["what"] == "chat" and "THEME:" in c["body"]["messages"][0]["content"]]
    check(len(themed) == 1 and "Нехай цей день" in themed[0]["body"]["messages"][1]["content"],
          "один запит теми до GPT, у ньому текст посту")
    check("{{" not in d["prompt"] and IP.date_uk(dt.date.today()) in d["prompt"], "поля заповнено, дата сьогоднішня")
    check("watercolor sunrise" not in pm["text"], "промт більше не складається з нуля")
    check(url_btn(pm).startswith("https://chatgpt.com/?q=") and url_prompt(pm) == d["prompt"],
          "кнопка «Відкрити в ChatGPT»: посилання з повним промтом")
    check([b["text"] for b in buttons(pm)][:2] == ["🌐 Відкрити в ChatGPT", "📋 Скопіювати промт"], "дві кнопки в першому рядку")

    m = fake.mark()
    fake.press(A, pm, btn(pm, "Скопіювати промт"))
    cp = await wait(m, is_("sendMessage", A, "скопіюється"))
    body = cp["params"]["text"]
    check(cp["params"].get("parse_mode") == "HTML" and "<code>" in body, "промт надіслано моноширинним блоком")
    check(html.unescape(body.split("<code>", 1)[1].rsplit("</code>", 1)[0]) == d["prompt"], "у блоці — повний промт без змін")

    n1 = oai.mark()
    m = fake.mark()
    fake.press(A, pm, btn(pm, "Інший промт"))
    pm2 = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    d2, ip2 = draft_of(cb, pm2)
    check(ip2["id"] != ip["id"] and cat_of(ip2["id"]) in IP.KIND_CATEGORIES["omnika"], f"інший промт: {ip['id']} → {ip2['id']}")
    check(not [c for c in oai.since(n1) if c["what"] == "chat"], "тему вдруге в GPT не питаємо")
    check(IP.VISUAL_TAGS[ip["id"]] in d2["prompt"], "попередній сюжет передано як «уникати»")
    seen = {ip["id"], ip2["id"]}
    for _ in range(4):
        m = fake.mark()
        fake.press(A, pm2, btn(pm2, "Інший промт"))
        pm2 = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
        seen.add(draft_of(cb, pm2)[1]["id"])
    check(len(seen) == 6, f"шість натискань — шість різних промтів ({len(seen)})")
    d, ip = draft_of(cb, pm2)
    pm = pm2

    m = fake.mark()
    fake.press(A, pm, btn(pm, "Згенерувати картинку"))
    c = await wait(m, is_("sendPhoto", A), timeout=30)
    check(img_size(c) == (1536, 1024), f"картинка до посту {img_size(c)}")
    icall = oai.of("image")[-1]
    check(icall["body"]["model"] == "gpt-image-1.5" and icall["body"]["size"] == "1536x1024", "параметри запиту зображення")
    check(icall["body"]["prompt"] == d["prompt"], "у генератор пішов той самий готовий промт")
    check("no text" in icall["body"]["prompt"].lower(), "промт забороняє текст на картинці")
    check(cb.db.usage_count(A, "image") == 1, "генерацію враховано в ліміті")

    print("6b. Telegram не приймає довге посилання: коротше, потім без кнопки-посилання")
    fake.max_button_url = IP.SAFE_BUTTON_URL
    m = fake.mark()
    fake.press(A, pm, btn(pm, "Інший промт"))
    pm3 = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    d3, _ = draft_of(cb, pm3)
    check(len(fake.rejected) == 1, "перше, повне посилання відхилено")
    check(url_btn(pm3) and len(url_btn(pm3)) <= IP.SAFE_BUTTON_URL and "трохи скорочено" in pm3["text"],
          f"надіслано коротше посилання ({len(url_btn(pm3) or '')})")
    check("Produce a single 16:9 landscape image" in url_prompt(pm3) and url_prompt(pm3) != d3["prompt"],
          "у короткому посиланні лишилась суть промту")
    check(cb.chatgpt_url_limit() == IP.SAFE_BUTTON_URL, "поріг довжини запам'ятовано")
    m = fake.mark()
    fake.press(A, pm3, btn(pm3, "Скопіювати промт"))
    cp = await wait(m, is_("sendMessage", A, "скопіюється"))
    check(html.unescape(cp["params"]["text"].split("<code>", 1)[1].rsplit("</code>", 1)[0]) == d3["prompt"],
          "копіювання й далі дає повний промт")
    fake.max_button_url = 10
    m = fake.mark()
    fake.press(A, pm3, btn(pm3, "Інший промт"))
    pm4 = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    check(url_btn(pm4) is None and "Скопіювати промт" in str(buttons(pm4)) and "задовгий для посилання" in pm4["text"],
          "без посилання лишається копіювання")
    fake.max_button_url = None

    print("7. Свій промт і повторна генерація")
    m = fake.mark()
    fake.press(A, pm, btn(pm, "Змінити промт"))
    await wait(m, is_("sendMessage", A, "Надішліть свій промт"))
    m = fake.mark()
    fake.send_text(A, "my own calm prompt without text")
    ok = cur(await wait(m, is_("sendMessage", A, "Промт збережено")))
    check(url_prompt(ok) == "my own calm prompt without text" and "Скопіювати промт" in str(buttons(ok)),
          "свій промт теж відкривається в ChatGPT і копіюється")
    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, ok, btn(ok, "Згенерувати"))
    await wait(m, is_("sendPhoto", A), timeout=30)
    check(oai.of("image")[-1]["body"]["prompt"] == "my own calm prompt without text", "використано свій промт")

    print("8. «Єдність сердець»: фото дня ddmm.jpg")
    today = dt.date.today()
    img = Image.new("RGB", (1200, 800), (120, 60, 140))
    img.save(PHOTOS / f"{today.day:02d}{today.month:02d}.jpg", "JPEG")
    m = fake.mark()
    fake.send_text(A, "/unity " + UNITY_TEXT)
    calls = await settle(m, quiet=0.8)
    ph = [c for c in calls if c["method"] == "sendPhoto" and int(c["params"]["chat_id"]) == A]
    check(ph, "пост надіслано з фото дня")
    if ph:
        check(len(ph[0]["params"].get("caption", "")) > 40, "текст посту — підписом до фото")
        check("Згенерувати картинку" in str(buttons(ph[0]["result"])), "кнопки під постом із фото")
    (PHOTOS / f"{today.day:02d}{today.month:02d}.jpg").unlink()
    m = fake.mark()
    fake.send_text(A, "/unity " + UNITY_TEXT)
    calls = await settle(m, quiet=0.8)
    check(any(is_("sendMessage", A, "Фото на сьогодні у папці немає")(c) for c in calls), "повідомлення про брак фото")
    post = [c for c in calls if c["method"] == "sendMessage" and "Нехай цей день" in c["params"].get("text", "")]
    draft_msg = cur(post[0]) if post else None
    check(draft_msg and "Фото дня" in str(buttons(draft_msg)), "кнопка «Фото дня»")
    m = fake.mark()
    fake.press(A, draft_msg, btn(draft_msg, "Промт картинки"))
    upm = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    _, uip = draft_of(cb, upm)
    check(cat_of(uip["id"]) in ("heart", "intention"), f"«Єдність сердець»: категорії heart / intention ({uip['id']})")

    print("9. Завантаження фото дня через бота")
    m = fake.mark()
    fake.press(A, draft_msg, btn(draft_msg, "Фото дня"))
    await wait(m, is_("sendMessage", A, "Надішліть фото на сьогодні"))
    buf = io.BytesIO()
    Image.new("RGB", (1000, 700), (30, 120, 90)).save(buf, "JPEG")
    m = fake.mark()
    fake.send_photo(A, buf.getvalue())
    calls = await settle(m, quiet=0.8)
    saved = PHOTOS / f"{today.day:02d}{today.month:02d}.jpg"
    check(saved.is_file(), "фото збережено як ddmm.jpg")
    check(any(is_("sendMessage", A, "Фото збережено")(c) for c in calls), "підтвердження")
    check(any(c["method"] == "sendPhoto" for c in calls), "пост показано повторно з фото")
    check(cb.db.state(A)[0] is None, "стан скинуто")

    print("10. День народження: скласти привітання + листівка")
    m = fake.mark()
    fake.send_text(A, "/birthday")
    await wait(m, is_("sendMessage", A, "ім'я іменинника"))
    m = fake.mark()
    fake.send_text(A, BDAY_BRIEF)
    ask = cur(await wait(m, is_("sendMessage", A, "Що зробити з цим текстом")))
    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, ask, btn(ask, "Скласти привітання"))
    c = await wait(m, is_("sendPhoto", A), timeout=30)
    check(img_size(c) == (1280, 782), f"листівка {img_size(c)}")
    sysmsg = [x["body"]["messages"][0]["content"] for x in oai.since(n0) if x["what"] == "chat"]
    check(any("привітання з днем народження" in s for s in sysmsg), "промт складання привітання")
    bday = [x for x in oai.since(n0) if x["what"] == "chat"][-1]["body"]["messages"][1]["content"]
    check(BDAY_BRIEF in bday, "дані іменинника передано")
    card = c["result"]
    check("Згенерувати картинку" in str(buttons(card)), "кнопки чернетки під листівкою")

    print("11. Генерація фону для листівки → новий шаблон")
    tpl_before = len(cb.store.all())
    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, card, btn(card, "Згенерувати картинку"))
    c = await wait(m, is_("sendPhoto", A), timeout=40)
    check(img_size(c) == (1280, 782), f"листівка на новому фоні {img_size(c)}")
    check(oai.of("image")[-1]["body"]["size"] == "1536x1024", "розмір під формат листівки")
    check("birthday greeting background" in oai.of("image")[-1]["body"]["prompt"], "ДН: готовий промт дня народження")
    gen = [t for t in cb.store.all() if t.config.get("generated")]
    check(len(cb.store.all()) == tpl_before + 1 and gen, "фон збережено як шаблон")
    if gen:
        check(gen[-1].config.get("owner") == A and gen[-1].config.get("prompt"), "у шаблоні записано автора й промт")
    check(any(is_("sendPhoto", A, "Новий фон")(c) for c in fake.since(m)), "підпис про новий фон")

    print("12. «Оформити мій текст» для ДР")
    m = fake.mark()
    fake.send_text(A, "/birthday Вітаю тебе Наталю з днем народження бажаю світла")
    ask = cur(await wait(m, is_("sendMessage", A, "Що зробити")))
    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, ask, btn(ask, "Оформити мій текст"))
    await wait(m, is_("sendPhoto", A), timeout=30)
    sysmsg = [x["body"]["messages"][0]["content"] for x in oai.since(n0) if x["what"] == "chat"]
    check(any("Оформи цей текст" in x["body"]["messages"][1]["content"] for x in oai.since(n0) if x["what"] == "chat"),
          "оформлення, а не складання")

    print("12b. Скорочення задовгого тексту листівки")
    oai.text_reply = ("Наталю, вітаю Вас із днем народження! Нехай гори дарують натхнення, йога — рівновагу, "
                      "а квіти цвітуть у серці цілий рік. Світла Вам, радості, тепла й міцного здоров'я "
                      "на довгі-довгі роки щасливого життя поруч із рідними людьми!")
    m = fake.mark()
    fake.send_text(A, "/birthday Наталія, 45, любить гори")
    ask = cur(await wait(m, is_("sendMessage", A, "Що зробити")))
    m = fake.mark()
    fake.press(A, ask, btn(ask, "Скласти привітання"))
    c = await wait(m, is_("sendPhoto", A), timeout=30)
    card2 = c["result"]
    check("Скоротити текст" in str(buttons(card2)), "кнопка скорочення під листівкою")
    oai.text_reply = "Наталю, вітаю з днем народження! Нехай гори дарують натхнення, а серце — спокій."
    m = fake.mark()
    fake.press(A, card2, btn(card2, "Скоротити"))
    c = await wait(m, is_("sendPhoto", A, "Скорочений"), timeout=30)
    check(True, "скорочений варіант надіслано")
    oai.text_reply = "Доброго ранку, друзі! ☀️\n\nНехай цей день буде світлим.\n\n💛 Усмішки вам і тепла."

    print("13. Кнопка «Новий фон через ChatGPT» під звичайною листівкою")
    m = fake.mark()
    fake.send_text(A, "Я відкриваю своє серце присутності Творця і світлу нового дня.")
    c = await wait(m, is_("sendPhoto", A))
    plain = c["result"]
    check("Новий фон через ChatGPT" in str(buttons(plain)), "кнопка є, коли ключ заданий")
    m = fake.mark()
    fake.press(A, plain, "r:bg")
    c = await wait(m, is_("sendPhoto", A), timeout=40)
    check(img_size(c) == (1280, 782), "листівка перемальована на згенерованому фоні")
    check("affirmation" in oai.of("image")[-1]["body"]["prompt"].lower(), "фон листівки: промт категорії affirmation")

    print("13b. Ювілей і запасний шлях без бібліотеки промтів")
    m = fake.mark()
    fake.send_text(A, "/birthday Вітаю з ювілеєм, дорога Олено! Світла тобі й радості.")
    ask = cur(await wait(m, is_("sendMessage", A, "Що зробити")))
    m = fake.mark()
    fake.press(A, ask, btn(ask, "Оформити мій текст"))
    c = await wait(m, is_("sendPhoto", A), timeout=30)
    m = fake.mark()
    fake.press(A, c["result"], btn(c["result"], "Промт картинки"))
    jp = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    check(draft_of(cb, jp)[1]["id"] == "birthday_3", "слово «ювілей» у тексті → ювілейний фон")
    saved_dir, IP.PROMPTS_DIR = IP.PROMPTS_DIR, DATA / "no-such-prompts"
    m = fake.mark()
    fake.send_text(A, "/omnika " + OMNIKA_TEXT)
    calls = await settle(m)
    post = [c for c in calls if is_("sendMessage", A)(c) and "ранку" in c["params"].get("text", "")]
    m = fake.mark()
    fake.press(A, cur(post[0]), btn(cur(post[0]), "Промт картинки"))
    fb = cur(await wait(m, is_("sendMessage", A, "Промт для генератора")))
    check("watercolor sunrise" in fb["text"] and url_btn(fb), "без файлів промтів бот складає промт через GPT, кнопки на місці")
    IP.PROMPTS_DIR = saved_dir

    print("14. Зразки стилю через меню")
    mn = await menu(A)
    await press(A, mn, "m:posts")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, btn(mn, "Налаштування рубрик"))
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, btn(mn, "Омніка"))
    mn = fake.messages[(A, mn["message_id"])]
    check("Зразків для стилю" in mn["text"], "екран рубрики")
    await press(A, mn, btn(mn, "Зразки стилю"))
    mn = fake.messages[(A, mn["message_id"])]
    m = fake.mark()
    fake.press(A, mn, btn(mn, "Додати зразки"))
    await wait(m, is_("sendMessage", A, "пересилайте"))
    n_before = cb.db.samples_count("omnika")
    m = fake.mark()
    fake.send_text(A, "Доброго ранку! 🌞 Сьогодні день нових починань. Бережіть тепло в серці.")
    await wait(m, is_("sendMessage", A, "Зразок додано"))
    m = fake.mark()
    fake.send_text(A, "Доброго ранку! 🌞 Сьогодні день нових починань. Бережіть тепло в серці.")
    await wait(m, is_("sendMessage", A, "уже є"))
    check(cb.db.samples_count("omnika") == n_before + 1, "дублікат не додано")
    fake.send_text(A, "/cancel")
    await asyncio.sleep(0.4)
    m = fake.mark()
    fake.press(A, mn, f"sm:style:omnika")
    await settle(m)
    check(cb.style_of("omnika").get("profile"), "стиль описано заново")

    print("15. Налаштування рубрики: шаблон, хештеги, побажання")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, "pm:cfg:omnika")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, "pm:tpl:omnika")
    mn = fake.messages[(A, mn["message_id"])]
    await press(A, mn, "pm:settpl:omnika:night_sky")
    check(cb.post_cfg("omnika")["template"] == "night_sky", "шаблон рубрики")
    m = fake.mark()
    fake.press(A, mn, "pm:tags:omnika")
    await wait(m, is_("sendMessage", A, "хештеги"))
    m = fake.mark()
    fake.send_text(A, "#омніка #ранок")
    await wait(m, is_("sendMessage", A, "Хештеги"))
    check(cb.post_cfg("omnika")["hashtags"] == "#омніка #ранок", "хештеги збережено")
    m = fake.mark()
    fake.press(A, mn, "pm:extra:omnika")
    await wait(m, is_("sendMessage", A, "побажання"))
    m = fake.mark()
    fake.send_text(A, "коротше, без води")
    await settle(m)
    check(cb.post_cfg("omnika")["extra"] == "коротше, без води", "побажання збережено")
    m = fake.mark()
    fake.send_text(A, "/omnika " + OMNIKA_TEXT)
    calls = await settle(m)
    post = [c for c in calls if c["method"] == "sendMessage" and "ранку" in c["params"].get("text", "")]
    check(post and "#омніка" in post[0]["params"]["text"], "хештеги додано до посту й при локальному оформленні")
    n0 = oai.mark()
    m = fake.mark()
    fake.press(A, cur(post[0]), btn(cur(post[0]), "Оформити з ChatGPT"))
    calls = await settle(m)
    sysmsg = [x["body"]["messages"][0]["content"] for x in oai.since(n0) if x["what"] == "chat"]
    check(any("коротше, без води" in s for s in sysmsg), "побажання передано в GPT")
    gpt_post = [c for c in calls if c["method"] == "sendMessage" and "Нехай цей день" in c["params"].get("text", "")]
    check(gpt_post and "#омніка" in gpt_post[0]["params"]["text"], "хештеги додано і при оформленні моделлю")

    print("16. Помилки OpenAI показуються зрозуміло")
    oai.fail_next["chat"] = (429, "Rate limit reached", 9)
    m = fake.mark()
    fake.send_text(A, "/morning Нехай світло наповнює мої думки, слова та вчинки щодня.")
    await wait(m, lambda c: c["method"] == "sendMessage" and "ліміт" in (c["params"].get("text") or ""),
               timeout=40, label="повідомлення 429")
    check(True, "зрозуміле повідомлення про 429")
    await wait(m, is_("sendPhoto", A), timeout=30, label="листівка після помилки")
    check(True, "листівку все одно зроблено (локальне оформлення)")
    oai.fail_next.clear()

    oai.fail_next["image"] = (401, "Incorrect API key provided", 9)
    m = fake.mark()
    fake.send_text(A, "/morning Нехай світло наповнює мої думки і слова.")
    card = await wait(m, is_("sendPhoto", A), timeout=30, label="листівка")
    m = fake.mark()
    fake.press(A, card["result"], btn(card["result"], "Згенерувати картинку"))
    await wait(m, lambda c: c["method"] == "sendMessage" and "не прийняв ключ" in (c["params"].get("text") or ""),
               timeout=40, label="повідомлення 401")
    check(True, "зрозуміле повідомлення про 401")
    oai.fail_next.clear()

    print("17. Ліміт генерацій на добу")
    cb.oai_set(limit=1)
    used = cb.db.usage_count(A, "image")
    check(cb.image_quota_left(A) == 0, f"ліміт вичерпано ({used} генерацій)")
    m = fake.mark()
    fake.send_text(A, "/morning Світло в серці веде мене крізь день.")
    calls = await settle(m, timeout=30)
    card = [c for c in calls if c["method"] == "sendPhoto"][-1]
    m = fake.mark()
    fake.press(A, card["result"], btn(card["result"], "Згенерувати картинку"))
    calls = await settle(m, timeout=30)
    check(any("ліміт генерацій" in (c["params"].get("text") or "") for c in calls if c["method"] == "sendMessage"),
          "повідомлення про ліміт")
    cb.oai_set(limit=50)

    print("18. Звичайний користувач: рубрики доступні, налаштування OpenAI — ні")
    m = fake.mark()
    fake.send_text(B, "/post")
    pm2 = cur(await wait(m, is_("sendMessage", B, "Пости")))
    check("Налаштування рубрик" in str(buttons(pm2)), "B бачить рубрики")
    m = fake.mark()
    fake.press(B, pm2, "o:clearkey")
    c = await wait(m, is_("answerCallbackQuery"))
    check(c["params"].get("show_alert") in (True, "true", "True") and cb.oai_ready(), "B не може прибрати ключ")
    m = fake.mark()
    fake.send_text(B, "/omnika " + OMNIKA_TEXT)
    calls = await settle(m)
    check(any(c["method"] == "sendMessage" and "ранку" in c["params"].get("text", "") for c in calls),
          "B отримав оформлений пост")

    print("19. Усе переживає перезапуск")
    cb2 = M.CardBot(DATA)
    check(cb2.oai_ready() and cb2.post_cfg("omnika")["hashtags"] == "#омніка #ранок"
          and cb2.db.samples_count("omnika") >= 3, "ключ, налаштування рубрик і зразки збережено")


async def amain():
    fake.start()
    oai.start()
    os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{OAI_PORT}/v1"
    import importlib
    from bot import openai_api, content, posts
    importlib.reload(openai_api)
    importlib.reload(content)
    importlib.reload(posts)
    importlib.reload(M)
    app, cb = M.build_app("123:TEST", DATA, f"http://127.0.0.1:{TG_PORT}/bot", f"http://127.0.0.1:{TG_PORT}/file/bot")
    async with app:
        await M._post_init(app)
        await app.start()
        await app.updater.start_polling(poll_interval=0.0, timeout=1)
        try:
            await scenario(cb)
        except Exception:
            traceback.print_exc()
            failed.append("виняток у сценарії")
        finally:
            await app.updater.stop()
            await app.stop()
    fake.stop()
    oai.stop()
    errs = [c for c in fake.calls if c["method"] == "sendMessage" and "Щось пішло не так" in c["params"].get("text", "")]
    check(not errs, f"жодних необроблених помилок ({len(errs)})")
    leaked = [c for c in fake.calls if OAI_KEY in str(c["params"].get("text", ""))]
    check(not leaked, "ключ OpenAI не світиться в повідомленнях бота")


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(amain())
    print(f"\n{passed} ok, {len(failed)} fail за {time.time() - t0:.1f} с")
    if failed:
        print("Провалено:", *failed, sep="\n  - ")
        sys.exit(1)
