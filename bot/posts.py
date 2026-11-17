"""Рубрики постів у боті: оформлення тексту, промти й генерація зображень.

Провайдери задаються в .env: тексти — TEXT_PROVIDER (bot/text_gen.py), зображення — IMAGE_PROVIDER
(bot/image_gen.py); типово обидва Gemini.

Особливості рубрик:
    omnika — текст оформлюється простими засобами, модель підключається кнопкою «Оформити з Gemini»;
             у картинці модель домальовує сьогоднішню дату (розмір, шрифт і колір інші щодня);
    morning — картка малюється внутрішнім рендером, а кнопка «Картинка з текстом» віддає її Gemini;
    unity  — промт картинки складається з тексту посту, картинка малюється одразу.

Підмішується до CardBot (bot/main.py): екрани меню, стани введення, обробники кнопок.
Префікси callback_data:
    pm:  — меню рубрик і налаштування рубрики
    p:   — дії над чернеткою поста: p:prompt, p:newp, p:copy, p:editp, p:gen (картинка),
           p:fmt (оформити текст моделлю), p:gentext (картинка з текстом усередині)
    o:   — налаштування OpenAI
    sm:  — зразки постів (аналіз стилю)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import time
from pathlib import Path

from telegram import InputFile
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from textcard import render as R

from . import content as C
from . import gemini_prompt as GP
from . import image_gen as IG
from . import image_prompts as IP
from . import openai_api as O
from . import text_gen as TX

log = logging.getLogger("textcard-bot.posts")

IMAGE_LIMIT = int(os.environ.get("IMAGE_LIMIT_PER_DAY", "20"))
GENERATED_KEEP = int(os.environ.get("GENERATED_TEMPLATES_KEEP", "20"))
MAX_POST_CHARS = int(os.environ.get("MAX_POST_CHARS", "3000"))
AUTO_IMAGE = os.environ.get("AUTO_IMAGE", "1").strip().lower() not in ("0", "off", "no", "")


def esc(s) -> str:
    import html
    return html.escape(str(s), quote=False)


class PostsMixin:
    # ------------------------------------------------------------ OpenAI: налаштування

    def oai_cfg(self) -> dict:
        base = {"key": os.environ.get("OPENAI_API_KEY", ""), "text_model": O.DEFAULT_TEXT_MODEL,
                "image_model": O.DEFAULT_IMAGE_MODEL, "base_url": O.DEFAULT_BASE_URL,
                "quality": "high", "limit": IMAGE_LIMIT, "autosample": True}
        return {**base, **(self.db.kv_get("openai", {}) or {})}

    def oai_set(self, **kw) -> dict:
        cfg = self.oai_cfg()
        cfg.update(kw)
        self.db.kv_set("openai", cfg)
        return cfg

    def oai(self) -> O.OpenAIClient:
        c = self.oai_cfg()
        return O.OpenAIClient(c["key"], c["base_url"], c["text_model"], c["image_model"])

    def oai_ready(self) -> bool:
        return bool(self.oai_cfg()["key"])

    @property
    def text(self) -> TX.TextProvider:
        """Текстова модель: оформлення постів, промти, опис стилю (TEXT_PROVIDER у .env)."""
        if getattr(self, "_text", None) is None:
            self._text = TX.make_provider(self.oai_cfg)
        return self._text

    def text_ready(self) -> bool:
        return self.text.ready()

    def text_label(self) -> str:
        return self.text.label

    def text_status(self) -> str:
        p = self.text
        return f"✍️ Тексти оформлює: {p.label} ({p.model})" + ("" if p.ready() else " — ключ не заданий")

    @property
    def images(self) -> IG.ImageGenerator:
        """Генератор зображень; провайдер вибирається в .env під час першого звернення."""
        if getattr(self, "_images", None) is None:
            self._images = IG.ImageGenerator(self.db, self.data_dir, self.oai_cfg)
        return self._images

    def image_ready(self) -> bool:
        return self.images.provider.ready()

    def image_label(self) -> str:
        return self.images.provider.label

    def image_status(self) -> str:
        p = self.images.provider
        size = f", {p.image_size}" if getattr(p, "image_size", "") else ""
        state = "" if p.ready() else " — ключ не заданий"
        return f"🖼 Зображення малює: {p.label} ({p.model}{size}){state}"

    def image_quota_left(self, uid: int) -> int:
        limit = int(self.oai_cfg().get("limit") or 0)
        if limit <= 0:
            return 10 ** 6
        return max(0, limit - self.db.usage_count(uid, "image", 24))

    # ------------------------------------------------------------ стиль рубрики

    def style_of(self, kind_id: str) -> dict:
        return self.db.kv_get(f"style:{kind_id}", {}) or {}

    async def style_profile(self, kind_id: str, *, rebuild: bool = False) -> str:
        """Опис стилю рубрики, виведений із зразків. Перебудовується, коли зразків побільшало."""
        kind = C.KINDS[kind_id]
        rows = self.db.samples(kind_id, 12)
        saved = self.style_of(kind_id)
        n = self.db.samples_count(kind_id)
        if not rebuild and saved.get("profile") and saved.get("n") == n:
            return saved["profile"]
        if n < 2 or not self.text_ready():
            return saved.get("profile", "") if not rebuild else ""
        sys, user = C.style_profile_messages(kind, [r["text"] for r in rows])
        try:
            profile = C.strip_markup(await self.text.chat(sys, user, max_tokens=700))
        except TX.TextError as e:
            log.info("не вдалося описати стиль %s: %s", kind_id, e)
            return saved.get("profile", "")
        self.db.kv_set(f"style:{kind_id}", {"profile": profile, "n": n, "updated": time.time()})
        return profile

    # ------------------------------------------------------------ побудова тексту й промту

    async def build_text(self, uid: int, kind_id: str, source: str, *, compose: bool = False,
                         extra: str = "", marks: bool = False) -> tuple[str, bool]:
        """Повертає (оформлений текст, чи використано модель).

        Рубрики з local_first оформлюються простими засобами; модель вмикає лише marks=True
        (кнопка «Оформити з Gemini») або compose=True."""
        kind = C.KINDS[kind_id]
        if kind.local_first and not compose and not marks:
            return C.local_format(kind, source, self.post_cfg(kind_id).get("hashtags", "")), False
        if not self.text_ready():
            if compose:
                raise TX.TextError(f"Щоб складати тексти, потрібен ключ: {self.text.not_ready_hint()}. "
                                   "Поки що надішліть свій текст — я його оформлю.")
            return C.local_format(kind, source, self.post_cfg(kind_id).get("hashtags", "")), False
        profile = await self.style_profile(kind_id)
        samples = [r["text"] for r in self.db.samples(kind_id, 3)]
        if compose:
            sys, user = C.compose_messages(kind, source, profile, samples, extra)
        else:
            sys, user = C.format_messages(kind, source, profile, samples, extra, marks=marks)
        out = C.strip_markup(await self.text.chat(sys, user, max_tokens=1500), keep_bold=marks)
        if kind.output == "post":
            tags = self.post_cfg(kind_id).get("hashtags", "").strip()
            if tags and tags.lower() not in out.lower():
                out = out.rstrip() + "\n\n" + tags
        self.db.add_usage(uid, "text")
        return out.strip(), True

    async def build_prompt(self, uid: int, kind_id: str, text: str, extra: str = "") -> tuple[str, bool]:
        """Запасний шлях: промт складає GPT (або локальний шаблон). Працює, якщо бібліотека промтів недоступна."""
        kind = C.KINDS[kind_id]
        if not self.text_ready():
            return C.local_image_prompt(kind, text), False
        sys, user = C.image_prompt_messages(kind, text, extra)
        try:
            p = C.strip_markup(await self.text.chat(sys, user, max_tokens=600))
        except TX.TextError as e:
            log.info("промт через GPT не вийшов: %s", e)
            return C.local_image_prompt(kind, text), False
        self.db.add_usage(uid, "text")
        if "no text" not in p.lower():
            p += " No text, no letters, no captions, no watermark."
        return p.strip(), True

    async def image_theme(self, uid: int, kind_id: str, text: str) -> tuple[str, str]:
        """(тема, символ) англійською для готового промту. Без ключа OpenAI або при помилці — порожні."""
        if not self.text_ready() or not text.strip():
            return "", ""
        try:
            reply = await self.text.chat(*C.image_theme_messages(C.KINDS[kind_id], text), max_tokens=300)
        except TX.TextError as e:
            log.info("тему картинки через GPT не визначено: %s", e)
            return "", ""
        self.db.add_usage(uid, "text")
        return IP.parse_theme_reply(reply)

    async def prompt_from_text(self, uid: int, d, previous: str = "") -> str:
        """Промт картинки, складений моделлю за текстом посту: послання й настрій дня -> сцена.

        previous — попередній промт, щоб нова сцена була відчутно іншою. Порожньо, якщо не вийшло."""
        text = d["text"] or d["source"]
        if not self.text_ready() or not text.strip():
            return ""
        sys, user = C.image_from_text_messages(C.KINDS[d["kind"]], text, C.season_uk(), previous)
        try:
            reply = await self.text.chat(sys, user, max_tokens=500)
        except TX.TextError as e:
            log.info("промт із тексту посту не вийшов: %s", e)
            return ""
        self.db.add_usage(uid, "text")
        return C.strip_markup(reply).strip()[:1800]

    async def ensure_prompt(self, uid: int, did: str, *, renew: bool = False, from_text: bool = False) -> str:
        """Промт картинки для чернетки. from_text=True — модель складає промт за текстом посту,
        інакше береться готовий промт рубрики з prompts/*.json із підстановкою дати, сезону й теми.
        renew=True — наступний варіант. Обраний промт зберігається в чернетці."""
        d = self.db.draft(did)
        if d["prompt"] and not renew and not from_text:
            return d["prompt"]
        if from_text:
            fresh_prompt = await self.prompt_from_text(uid, d, previous=d["prompt"] if renew else "")
            if fresh_prompt:
                fresh = json.loads(self.db.draft(did)["extra"] or "{}")
                fresh.pop("own", None)
                fresh["ip"] = {"id": "", "title": f"Промт за текстом посту ({self.text_label()})"}
                self.db.update_draft(did, prompt=fresh_prompt, extra=fresh)
                return fresh_prompt
        extra = json.loads(d["extra"] or "{}")
        meta = extra.get("ip") or {}
        kind_id = d["kind"]
        try:
            IP.load_library()
            if "theme" not in meta:                       # тему визначаємо один раз на чернетку
                meta["theme"], meta["symbol"] = await self.image_theme(uid, kind_id, d["text"] or d["source"])
            jubilee = kind_id == "birthday" and re.search(r"ювіле", f"{d['source']} {d['text']}", re.I)
            ip = IP.build(kind_id, dt.date.today(), theme=meta["theme"], symbol=meta["symbol"],
                          last_used=self.db.prompt_last_used(kind_id), recent=self.db.prompt_recent(kind_id),
                          exclude=(meta["id"],) if renew and meta.get("id") else (),
                          variant=3 if jubilee and not renew else None, allow_explicit=renew)
        except IP.PromptError as e:
            log.warning("готові промти недоступні (%s) — складаю промт по-старому", e)
            prompt, _ = await self.build_prompt(uid, kind_id, d["text"])
            fresh = json.loads(self.db.draft(did)["extra"] or "{}")
            fresh.pop("ip", None)
            self.db.update_draft(did, prompt=prompt, extra=fresh)
            return prompt
        self.db.add_prompt_log(kind_id, ip.prompt_id, ip.anchor, uid)
        fresh = json.loads(self.db.draft(did)["extra"] or "{}")     # за час запиту чернетку могли змінити
        fresh.pop("own", None)
        fresh["ip"] = {"id": ip.prompt_id, "title": ip.title, "theme": meta["theme"], "symbol": meta["symbol"]}
        self.db.update_draft(did, prompt=ip.prompt, extra=fresh)
        return ip.prompt

    def chatgpt_url_limit(self) -> int:
        return int(self.db.kv_get("chatgpt_url_limit", 0) or IP.MAX_BUTTON_URL)

    def prompt_kb(self, did: str, url: str | None):
        from .main import kb
        rows = [([("🌐 Відкрити в ChatGPT", url)] if url else []) + [("📋 Скопіювати промт", f"p:copy:{did}")]]
        if self.image_ready():
            rows.append([("🖌 Згенерувати картинку", f"p:gen:{did}")])
        rows.append([("🔁 Інший промт", f"p:newp:{did}"), ("✏️ Змінити промт", f"p:editp:{did}")])
        return kb(rows)

    async def send_prompt(self, ctx, chat_id: int, did: str, head: str = "🎨 Промт для генератора зображень"):
        """Повідомлення з промтом і кнопками. Якщо Telegram не приймає довге посилання —
        повторює з коротшим, потім без кнопки-посилання; знайдений поріг запам'ятовує."""
        d = self.db.draft(did)
        prompt = d["prompt"] or ""
        title = (json.loads(d["extra"] or "{}").get("ip") or {}).get("title", "")
        limit = self.chatgpt_url_limit()
        for lim in dict.fromkeys((limit, min(limit, IP.SAFE_BUTTON_URL), 0)):
            url, shortened = IP.button_url(prompt, lim) if lim else (None, True)
            note = ("" if url and not shortened else
                    "\n\nℹ️ У посиланні промт трохи скорочено; повний текст дає «Скопіювати промт»." if url else
                    "\n\nℹ️ Промт задовгий для посилання — скористайтеся кнопкою «Скопіювати промт».")
            text = head + (f"\n{title}" if title else "") + "\n\n" + prompt + note
            try:
                return await ctx.bot.send_message(chat_id, text[:C.TG_MESSAGE], reply_markup=self.prompt_kb(did, url))
            except BadRequest as e:
                if not url:
                    raise
                log.info("Telegram не прийняв посилання на ChatGPT (%d символів): %s", len(url), e)
                if lim > IP.SAFE_BUTTON_URL:
                    self.db.kv_set("chatgpt_url_limit", IP.SAFE_BUTTON_URL)

    async def gen_image(self, uid: int, prompt: str, width: int, height: int, *, kind: str = "",
                        prompt_id: str = "", category: str = "", draw_text: str = "",
                        draw_style: str = "") -> IG.GeneratedImage:
        """Одне зображення за промтом через вибраний провайдер: збережений файл + метадані."""
        cfg = self.oai_cfg()
        if self.image_quota_left(uid) <= 0:
            raise IG.ImageGenError(f"Вичерпано денний ліміт генерацій зображень ({cfg['limit']} на добу). "
                                   "Ліміт змінюється в 🛠 Адміністрування → 🤖 OpenAI.", fallback_ok=False)
        img = await self.images.generate(prompt, width=width, height=height, uid=uid, kind=kind,
                                         prompt_id=prompt_id, category=category, quality=cfg.get("quality", "high"),
                                         draw_text=draw_text, draw_style=draw_style)
        self.db.add_usage(uid, "image")
        return img

    def backup_image(self) -> Path | None:
        """Запасна картинка, якщо генерація не вдалася: фото дня ddmm.jpg, далі — папка IMAGE_BACKUP_DIR."""
        photo = C.day_photo()
        if photo:
            return photo
        folder = Path(os.environ.get("IMAGE_BACKUP_DIR") or self.data_dir / "backup-images")
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in C.PHOTO_EXT)
        except OSError:
            return None
        if not files:
            return None
        used = set(self.db.image_paths("backup"))
        fresh = [p for p in files if str(p) not in used]
        return fresh[0] if fresh else files[dt.date.today().toordinal() % len(files)]

    async def image_fallback(self, ctx, chat_id: int, uid: int, d, prompt: str, err: IG.ImageGenError):
        """Після невдалих спроб: запасна картинка для посту або безпечна зупинка для листівки."""
        if not err.fallback_ok:
            return
        k = C.KINDS[d["kind"]]
        if k.output == "card":
            return await ctx.bot.send_message(
                chat_id, "ℹ️ Листівка лишається на поточному фоні. Спробуйте ще раз пізніше або відкрийте промт "
                         "у ChatGPT кнопкою «🎨 Промт картинки».", reply_markup=self.draft_kb(d))
        if k.use_day_photo and C.day_photo():
            return await ctx.bot.send_message(chat_id, "ℹ️ Пост уже має фото дня — воно лишається ілюстрацією.",
                                              reply_markup=self.draft_kb(d))
        src = self.backup_image()
        if not src:
            return await ctx.bot.send_message(
                chat_id, "ℹ️ Запасної картинки немає: покладіть фото на сьогодні (ddmm.jpg) у папку фото. "
                         "Пост лишається без нової ілюстрації.", reply_markup=self.draft_kb(d))
        try:
            img = await asyncio.to_thread(self.images.backup, src, uid=uid, kind=d["kind"], prompt=prompt,
                                          error=str(err))
        except (OSError, IG.ImageGenError) as e:
            log.info("запасну картинку %s не вдалося прочитати: %s", src.name, e)
            return await ctx.bot.send_message(chat_id, "ℹ️ Запасну картинку не вдалося прочитати. "
                                                       "Пост лишається без нової ілюстрації.")
        await ctx.bot.send_photo(chat_id, InputFile(io.BytesIO(img.data), filename=src.name),
                                 caption=f"🖼 Запасна картинка замість нової: {src.name}",
                                 reply_markup=self.draft_kb(d))

    # ------------------------------------------------------------ налаштування рубрики

    def post_cfg(self, kind_id: str) -> dict:
        kind = C.KINDS[kind_id]
        base = {"template": kind.template, "hashtags": kind.hashtags, "title": kind.title or "",
                "gpt": True, "extra": ""}
        return {**base, **(self.db.kv_get(f"kind:{kind_id}", {}) or {})}

    def post_cfg_set(self, kind_id: str, **kw) -> dict:
        cfg = self.post_cfg(kind_id)
        cfg.update(kw)
        self.db.kv_set(f"kind:{kind_id}", cfg)
        return cfg

    # ------------------------------------------------------------ екрани

    def posts_screen(self, uid: int, name: str, arg: str = ""):
        from .main import kb  # локальний імпорт, щоб не було циклічного на рівні модуля
        back = [("⬅️ Назад", "m:main")]

        if name == "posts":
            rows = [[(f"{k.emoji} {k.name}", f"pm:go:{k.id}")] for k in (C.KINDS[i] for i in C.ORDER)]
            rows.append([("⚙️ Налаштування рубрик", "m:kinds")])
            rows.append(back)
            gpt = "✅ підключений" if self.text_ready() else "⚠️ не підключений (оформлення буде простим)"
            gpt = f"{gpt} — {esc(self.text.label)} ({esc(self.text.model)})"
            return ("📝 <b>Пости</b>\nВиберіть рубрику — я попрошу текст і оформлю його.\n"
                    f"Текстова модель: {gpt}\n{esc(self.image_status())}"), kb(rows)

        if name == "kinds":
            rows = [[(f"{C.KINDS[i].emoji} {C.KINDS[i].name}", f"pm:cfg:{i}")] for i in C.ORDER]
            rows.append(back)
            return "⚙️ <b>Налаштування рубрик</b>\nШаблон листівки, заголовок, хештеги, зразки для стилю.", kb(rows)

        if name == "kindcfg":
            k = C.KINDS[arg]
            cfg = self.post_cfg(arg)
            n = self.db.samples_count(arg)
            prof = self.style_of(arg).get("profile", "")
            text = (f"{k.emoji} <b>{esc(k.name)}</b>\n\n"
                    f"🖼 Шаблон листівки: <b>{esc(self.t_name(cfg['template']))}</b>\n"
                    + (f"🏷 Заголовок: <b>{esc(cfg['title']) or '—'}</b>\n" if k.output == "card" else "")
                    + (f"#️⃣ Хештеги: {esc(cfg['hashtags']) or '—'}\n" if k.output == "post" else "")
                    + f"🧩 Зразків для стилю: <b>{n}</b>"
                    + (" · стиль описано ✅" if prof else "")
                    + (f"\n💬 Побажання до оформлення: {esc(cfg['extra'])}" if cfg["extra"] else "")
                    + (f"\n📅 Фото дня: папка <code>{esc(C.PHOTOS_DIR)}</code>, файл <code>ddmm.jpg</code>"
                       if k.use_day_photo else ""))
            rows = [[("🖼 Шаблон листівки", f"pm:tpl:{arg}")],
                    [("🏷 Заголовок", f"pm:title:{arg}")] if k.output == "card" else
                    [("#️⃣ Хештеги", f"pm:tags:{arg}")],
                    [(f"🧩 Зразки стилю ({n})", f"sm:list:{arg}")],
                    [("💬 Побажання до оформлення", f"pm:extra:{arg}")],
                    [(f"{k.emoji} Створити пост", f"pm:go:{arg}")],
                    [("⬅️ Назад", "m:kinds")]]
            return text, kb(rows)

        if name == "samples":
            k = C.KINDS[arg]
            rows = [[("➕ Додати зразки", f"sm:add:{arg}")]]
            items = self.db.samples(arg, 8)
            for r in items:
                rows.append([(("🗑 " + r["text"].replace("\n", " ")[:38]), f"sm:del:{r['id']}:{arg}")])
            if items:
                rows.append([("🧠 Описати стиль заново", f"sm:style:{arg}"), ("🧹 Очистити всі", f"sm:clear:{arg}")])
            rows.append([("⬅️ Назад", f"pm:cfg:{arg}")])
            n = self.db.samples_count(arg)
            prof = self.style_of(arg).get("profile", "")
            text = (f"🧩 <b>Зразки стилю: {esc(k.name)}</b>\n\n"
                    f"Збережено постів: <b>{n}</b> (показано {len(items)}).\n"
                    "Надішліть або перешліть у бота кілька своїх попередніх постів цієї рубрики — "
                    "я аналізуватиму їхній тон, довжину й ритм, і нові тексти звучатимуть так само.\n"
                    + (f"\n<b>Опис стилю:</b>\n{esc(prof[:600])}" if prof else
                       "\nОпис стилю з'явиться після 2+ зразків (потрібен ключ OpenAI)."))
            return text, kb(rows)

        if name == "oai" and self.is_admin(uid):
            c = self.oai_cfg()
            text = ("🤖 <b>OpenAI (ChatGPT)</b>\n\n"
                    f"🔑 Ключ: <b>{esc(O.mask_key(c['key']))}</b>\n"
                    f"💬 Модель тексту: <code>{esc(c['text_model'])}</code>\n"
                    f"🖼 Модель зображень: <code>{esc(c['image_model'])}</code>\n"
                    f"✨ Якість зображень: {esc(c['quality'])}\n"
                    f"📊 Ліміт зображень: {c['limit']} на добу на людину\n"
                    f"{esc(self.image_status())}"
                    + ("\nℹ️ Модель і якість зображень вище стосуються лише OpenAI; провайдер задається "
                       "в .env (IMAGE_PROVIDER)." if self.images.provider.name != "openai" else "")
                    + (f"\n🌐 Адреса API: <code>{esc(c['base_url'])}</code>" if c["base_url"] != O.DEFAULT_BASE_URL else "")
                    + "\n\nКлюч беруть на platform.openai.com → API keys. Генерація тексту й зображень "
                      "оплачується з вашого рахунку OpenAI.")
            rows = [[("🔑 Ввести ключ", "i:oai_key")] + ([("🗑 Прибрати ключ", "o:clearkey")] if c["key"] else []),
                    [("💬 Модель тексту", "o:models:text"), ("🖼 Модель зображень", "o:models:image")],
                    [("✨ Якість", "o:quality"), ("📊 Ліміт", "i:oai_limit")],
                    [("🌐 Адреса API", "i:oai_url")],
                    [("✅ Перевірити ключ", "o:check")],
                    [("⬅️ Назад", "m:admin")]]
            return text, kb(rows)

        if name == "models" and self.is_admin(uid):
            which = "image" if arg.startswith("image") else "text"
            c = self.oai_cfg()
            cur = c[f"{which}_model"]
            cached = self.db.kv_get("models", {}) or {}
            avail = cached.get(which) or []
            if not avail:
                avail = ([O.DEFAULT_IMAGE_MODEL, "gpt-image-1", "gpt-image-1-mini"] if which == "image"
                         else [O.DEFAULT_TEXT_MODEL, "gpt-5.4", "gpt-5.4-nano"])
            btns = [(("✅ " if m == cur else "") + m, f"o:setmodel:{which}:{m}") for m in avail[:24]]
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
            rows.append([("🔄 Оновити список з OpenAI", f"o:fetch:{which}"), ("✏️ Ввести назву", f"i:oai_model_{which}")])
            rows.append([("⬅️ Назад", "m:oai")])
            label = "зображень" if which == "image" else "тексту"
            note = "" if cached.get(which) else "\nСписок типовий; «Оновити» підтягне моделі, доступні вашому ключу."
            return f"🖼 <b>Модель {label}</b>\nЗараз: <code>{esc(cur)}</code>{note}", kb(rows)

        if name == "quality" and self.is_admin(uid):
            c = self.oai_cfg()
            rows = [[(("✅ " if c["quality"] == q else "") + n, f"o:quality:{q}")]
                    for q, n in (("low", "низька (найдешевше)"), ("medium", "середня"),
                                 ("high", "висока (типово)"), ("auto", "хай вирішує модель"))]
            rows.append([("⬅️ Назад", "m:oai")])
            return "✨ <b>Якість зображень</b>\nВища якість — довше й дорожче.", kb(rows)

        if name == "kindtpl":
            ts = self.store.all()
            cur = self.post_cfg(arg)["template"]
            btns = [((("✅ " if t.id == cur else "") + t.config.get("name", t.id)), f"pm:settpl:{arg}:{t.id}")
                    for t in ts[:24]]
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
            rows.append([("⬅️ Назад", f"pm:cfg:{arg}")])
            return f"🖼 <b>Шаблон для рубрики «{esc(C.KINDS[arg].name)}»</b>", kb(rows)

        return self.screen(uid, "main")

    # ------------------------------------------------------------ потік рубрики

    async def start_kind(self, ctx, chat_id: int, uid: int, kind_id: str):
        k = C.KINDS[kind_id]
        self.db.set_state(uid, f"post:{kind_id}", {})
        hint = ""
        if k.use_day_photo:
            p = C.day_photo()
            hint = ("\n\n📅 Фото на сьогодні: " + (f"знайдено ({p.name})" if p else
                    "немає у папці — надішліть його після тексту або згенеруємо картинку."))
        if k.local_first:
            hint += ("\n\nℹ️ Текст оформлю простими засобами. Кнопка «Оформити з "
                     f"{self.text_label()}» під чернеткою зробить це розумніше.") if self.text_ready() else ""
        if not self.text_ready():
            hint += f"\n\n⚠️ {self.text.not_ready_hint()}: оформлю текст простими засобами, без аналізу стилю."
        await ctx.bot.send_message(chat_id, f"{k.emoji} <b>{esc(k.name)}</b>\n\n{esc(k.ask)}{hint}"
                                            "\n\n/cancel — скасувати.", parse_mode=ParseMode.HTML)

    def draft_kb(self, draft, extra_note: str = ""):
        from .main import kb
        k = C.KINDS[draft["kind"]]
        did = draft["id"]
        if k.output == "card":      # листівку малює внутрішній рендер: «інший варіант» — інший фон і стиль
            first = [("🔄 Інший варіант", f"p:look:{did}")]
            if k.compose:
                first.append(("✍️ Інший текст", f"p:redo:{did}"))
            first.append(("📋 Мій текст", f"p:src:{did}"))
            rows = [first]
        else:
            rows = [[("🔄 Інший варіант", f"p:redo:{did}"), ("📋 Мій текст", f"p:src:{did}")]]
        if self.image_ready():
            rows.append([("🎨 Промт картинки", f"p:prompt:{did}"), ("🖌 Згенерувати картинку", f"p:gen:{did}")])
        else:
            rows.append([("🎨 Промт картинки", f"p:prompt:{did}")])
        if k.output == "post" and self.text_ready():
            rows.append([(f"✨ Оформити з {self.text_label()}", f"p:fmt:{did}")])
        if k.output == "card" and self.image_ready():
            rows.append([(f"🎨 Картинка з текстом у {self.image_label()}", f"p:gentext:{did}")])
        line = []
        if k.output == "card":
            line.append(("✂️ Скоротити текст", f"p:short:{did}"))
        if k.output == "post":
            line.append(("🖼 Зробити листівку", f"p:card:{did}"))
        if k.use_day_photo:
            line.append(("📅 Фото дня", f"p:photo:{did}"))
        if line:
            rows.append(line)
        rows.append([("💾 У зразки стилю", f"p:sample:{did}"), ("✖️ Закрити", "x")])
        return kb(rows)

    async def send_draft(self, ctx, chat_id: int, uid: int, did: str, note: str = "",
                         photo: tuple[bytes, str] | None = None):
        """Показує чернетку: пост текстом (+ картинка) або листівку.

        photo — готове зображення (data, ім'я файла): згенерована картинка або фото дня.
        Якщо текст оформлювала модель, **фрагменти** стають жирними (extra.html)."""
        d = self.db.draft(did)
        if not d:
            return await ctx.bot.send_message(chat_id, "Чернетку не знайдено — почніть рубрику заново.")
        k = C.KINDS[d["kind"]]
        if k.output == "card":
            return await self.send_draft_card(ctx, chat_id, uid, d, note)
        text = d["text"]
        html_mode = bool(json.loads(d["extra"] or "{}").get("html"))
        day = None
        if photo is None and k.use_day_photo:
            day = C.day_photo()
            if day:
                try:
                    photo = (day.read_bytes(), day.name)
                except OSError as e:
                    log.info("фото дня %s не прочиталося: %s", day.name, e)
                    photo, day = None, None
        parts = C.split_for_telegram(text, bool(photo))
        kw = {"parse_mode": ParseMode.HTML} if html_mode else {}

        def body(part: str) -> str:
            return C.post_html(part) if html_mode else part

        def markup(last: bool):
            return self.draft_kb(d) if last else None

        try:
            if photo:
                await ctx.bot.send_photo(chat_id, InputFile(io.BytesIO(photo[0]), filename=photo[1]),
                                         caption=body(parts[0]), reply_markup=markup(len(parts) == 1), **kw)
            else:
                await ctx.bot.send_message(chat_id, body(parts[0]), reply_markup=markup(len(parts) == 1), **kw)
        except BadRequest as e:
            log.info("не вдалося надіслати пост: %s", e)
            await ctx.bot.send_message(chat_id, parts[0])
        for i, part in enumerate(parts[1:], 1):
            await ctx.bot.send_message(chat_id, body(part), reply_markup=markup(i == len(parts) - 1), **kw)
        tail = note or ("📅 Фото на сьогодні додано." if day else
                        ("📅 Фото на сьогодні у папці немає — надішліть його кнопкою «Фото дня» або згенеруйте картинку."
                         if k.use_day_photo and photo is None else ""))
        if tail:
            await ctx.bot.send_message(chat_id, tail, parse_mode=ParseMode.HTML)

    async def send_draft_card(self, ctx, chat_id: int, uid: int, d, note: str = ""):
        k = C.KINDS[d["kind"]]
        cfg = self.post_cfg(d["kind"])
        extra = json.loads(d["extra"] or "{}")
        tpl = extra.get("template") or cfg["template"]
        title = extra.get("title", cfg.get("title") or "")
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        try:
            t, res, photo, _ = await self.render(uid, d["text"], title, template_id=tpl,
                                                 s={**self.st(uid), "send_as": "photo"})
        except (R.RenderError, KeyError) as e:
            return await ctx.bot.send_message(
                chat_id, f"⚠️ {esc(e)}\n\nТекст можна скоротити кнопкою «Інший варіант» або надіслати свій.",
                parse_mode=ParseMode.HTML, reply_markup=self.draft_kb(d))
        caption = note or (("⚠️ " + "; ".join(res.warnings)) if res.warnings else None)
        m = await ctx.bot.send_photo(chat_id, InputFile(io.BytesIO(photo), filename=f"card-{d['kind']}.jpg"),
                                     caption=caption, reply_markup=self.draft_kb(d))
        self.db.add_render(chat_id, m.message_id, uid, d["text"], title)

    async def make_draft(self, ctx, chat_id: int, uid: int, kind_id: str, source: str, *, compose: bool = False,
                         marks: bool = False):
        """marks=True — оформлення моделлю з виділенням головного послання жирним."""
        k = C.KINDS[kind_id]
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        wait = None
        if self.text_ready() and (marks or compose or not k.local_first):
            wait = await ctx.bot.send_message(chat_id, f"⏳ Оформлюю текст ({self.text_label()})…")
        try:
            text, used = await self.build_text(uid, kind_id, source, compose=compose, marks=marks,
                                               extra=self.post_cfg(kind_id).get("extra", ""))
        except TX.TextError as e:
            if wait:
                await self._del(ctx, chat_id, wait.message_id)
            text = C.local_format(k, source, self.post_cfg(kind_id).get("hashtags", "")) if not compose else ""
            if not text:
                return await ctx.bot.send_message(chat_id, f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
            did = self.db.add_draft(uid, kind_id, source, text)
            await ctx.bot.send_message(chat_id, f"⚠️ {esc(e)}\nОформив простими засобами:", parse_mode=ParseMode.HTML)
            return await self.send_draft(ctx, chat_id, uid, did)
        if wait:
            await self._del(ctx, chat_id, wait.message_id)
        if len(text) > MAX_POST_CHARS:
            text = text[:MAX_POST_CHARS].rstrip()
        did = self.db.add_draft(uid, kind_id, source, text, None, {"html": True} if marks and used else None)
        if used and marks:
            note = f"✨ Оформлено з {self.text_label()}: головне виділено жирним."
        elif used:
            note = ""
        elif k.local_first:
            note = (f"ℹ️ Оформлено простими засобами. Кнопка «Оформити з {self.text_label()}» зробить це розумніше."
                    if self.text_ready() else "ℹ️ Оформлено простими засобами.")
        else:
            note = "ℹ️ Оформлено простими засобами."
        if self.oai_cfg().get("autosample", True) and used and k.output == "post":
            self.db.add_sample(kind_id, C.plain(text), uid)
        auto = k.auto_image and AUTO_IMAGE and self.image_ready() and self.image_quota_left(uid) > 0
        if not auto:
            await self.send_draft(ctx, chat_id, uid, did, note)
            return did
        if note:                                   # спершу коротка примітка, далі промт із тексту й картинка
            await ctx.bot.send_message(chat_id, note, parse_mode=ParseMode.HTML)
        if not await self.do_generate(ctx, chat_id, uid, did, from_text=True, quiet_fallback=True):
            await self.send_draft(ctx, chat_id, uid, did)
        return did

    async def next_look(self, ctx, chat_id: int, uid: int, did: str) -> None:
        """«Інший варіант» для листівки: наступний шаблон — інше фонове зображення, шрифти й кольори."""
        d = self.db.draft(did)
        if not d:
            return await ctx.bot.send_message(chat_id, "Чернетку не знайдено.")
        extra = json.loads(d["extra"] or "{}")
        ids = [t.id for t in self.store.all()]
        if len(ids) < 2:
            return await ctx.bot.send_message(chat_id, "Інших шаблонів немає — додайте фото як новий шаблон.")
        current = extra.get("template") or self.post_cfg(d["kind"])["template"]
        nxt = ids[(ids.index(current) + 1) % len(ids)] if current in ids else ids[0]
        extra["template"] = nxt
        self.db.update_draft(did, extra=extra)
        self.set_st(uid, template=nxt)
        name = self.store.get(nxt).config.get("name", nxt)
        await self.send_draft(ctx, chat_id, uid, did, note=f"🎨 Фон і стиль: {esc(name)}.")

    async def _del(self, ctx, chat_id: int, mid: int):
        try:
            await ctx.bot.delete_message(chat_id, mid)
        except BadRequest:
            pass

    # ------------------------------------------------------------ генерація фону / картинки

    async def do_generate(self, ctx, chat_id: int, uid: int, did: str, *, with_text: bool = False,
                          from_text: bool = False, quiet_fallback: bool = False) -> bool:
        """Малює картинку до чернетки. Повертає True, якщо показав результат користувачеві.

        with_text=True  — модель домальовує в кадрі текст листівки (кнопка «Картинка з текстом»);
        from_text=True  — промт складає модель за текстом посту (послання й настрій дня);
        друге й дальші натискання «Згенерувати картинку» спершу оновлюють промт у моделі,
        і лише тоді малюється нова картинка."""
        d = self.db.draft(did)
        if not d:
            await ctx.bot.send_message(chat_id, "Чернетку не знайдено.")
            return False
        k = C.KINDS[d["kind"]]
        started = json.loads(d["extra"] or "{}")
        done = int(started.get("gen", 0))
        # друге й дальші натискання оновлюють промт у моделі — крім випадку, коли промт написав сам користувач
        again = done >= 1 and not with_text and self.text_ready() and not started.get("own")
        wait = None
        if (from_text or again) and self.text_ready():
            wait = await ctx.bot.send_message(
                chat_id, f"⏳ {'Оновлюю' if again else 'Складаю'} промт картинки ({self.text_label()})…")
        try:
            prompt = await self.ensure_prompt(uid, did, renew=again, from_text=from_text or again)
        finally:
            if wait:
                await self._del(ctx, chat_id, wait.message_id)
        d = self.db.draft(did)                       # ensure_prompt міг оновити чернетку
        extra = json.loads(d["extra"] or "{}")
        s = self.st(uid)
        try:
            w, h = R.parse_size(s["size"], "1280x782")
        except R.RenderError:
            w, h = 1280, 782
        if k.output == "post":
            w, h = self.images.provider.post_size
        meta = extra.get("ip") or {}
        prompt_id = meta.get("id", "") if meta.get("title") else ""      # свій промт — без категорії
        try:
            category = IP.load_library().get(prompt_id, {}).get("category_id", "") if prompt_id else ""
        except IP.PromptError:
            category = ""
        draw_text, draw_style = self.draw_text_for(d, k, with_text)
        label = self.image_label()
        wait = await ctx.bot.send_message(chat_id, f"🖌 Малюю зображення через {label}… це може зайняти до хвилини.")
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        try:
            img = await self.gen_image(uid, prompt, w, h, kind=d["kind"], prompt_id=prompt_id, category=category,
                                       draw_text=draw_text, draw_style=draw_style)
        except IG.ImageGenError as e:
            await self._del(ctx, chat_id, wait.message_id)
            await ctx.bot.send_message(chat_id, f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
            if quiet_fallback:
                return False
            await self.image_fallback(ctx, chat_id, uid, d, prompt, e)
            return True
        data = img.data
        await self._del(ctx, chat_id, wait.message_id)
        extra = json.loads(self.db.draft(did)["extra"] or "{}")
        extra["gen"] = done + 1
        self.db.update_draft(did, extra=extra)
        left = self.image_quota_left(uid)
        tail = f" Залишилось сьогодні: {left}." if left < 10 ** 5 else ""
        note = f"🖼 Картинка від {label}.{tail}"
        if img.draw_text:
            note += f"\nНапис у кадрі: {esc(img.draw_text.replace(chr(10), ' / '))}"
        if with_text:                                # текст уже всередині картинки — показуємо саму картинку
            await ctx.bot.send_photo(
                chat_id, InputFile(io.BytesIO(data), filename=f"card-{d['kind']}{Path(img.path).suffix}"),
                caption=note, reply_markup=self.draft_kb(self.db.draft(did)), parse_mode=ParseMode.HTML)
            return True
        if k.output == "post":                       # картинка + оформлений текст посту під нею
            await self.send_draft(ctx, chat_id, uid, did, note=note,
                                  photo=(data, "illustration" + Path(img.path).suffix))
            return True
        # для листівки: фон стає шаблоном, одразу малюємо листівку з текстом внутрішнім рендером
        name = f"{k.emoji} {dt.date.today():%d.%m} {time.strftime('%H:%M')}"
        try:
            t = await asyncio.to_thread(self.store.create, name, data, self.post_cfg(d["kind"])["template"])
            cfg = dict(t.config)
            cfg.update(owner=uid, generated=True, prompt=prompt[:900])
            t = self.store.update(t.id, cfg)
        except (R.RenderError, KeyError) as e:
            await ctx.bot.send_message(chat_id, f"⚠️ Зображення не вдалося зберегти: {esc(e)}",
                                       parse_mode=ParseMode.HTML)
            return False
        self.cleanup_generated(uid)
        extra = json.loads(self.db.draft(did)["extra"] or "{}")
        extra["template"] = t.id
        self.db.update_draft(did, extra=extra)
        self.set_st(uid, template=t.id)
        await self.send_draft(ctx, chat_id, uid, did, note=f"🎨 Новий фон від {label}.{tail}")
        return True

    def draw_text_for(self, d, k, with_text: bool) -> tuple[str, str]:
        """Що модель має намалювати в кадрі: текст листівки або сьогоднішня дата (стиль інший щодня)."""
        if with_text:
            extra = json.loads(d["extra"] or "{}")
            title = str(extra.get("title") if extra.get("title") is not None
                        else self.post_cfg(d["kind"]).get("title") or "").strip()
            body = C.plain(d["text"]).replace("*", "").strip()
            return (f"{title}\n{body}" if title else body), GP.card_draw_style(bool(title))
        if k.date_in_image and k.output == "post":
            return C.date_uk(), GP.date_draw_style(dt.date.today())
        return "", ""

    def cleanup_generated(self, uid: int) -> None:
        mine = [t for t in self.store.all() if t.custom and t.config.get("generated") and t.config.get("owner") == uid]
        for t in mine[:-GENERATED_KEEP] if len(mine) > GENERATED_KEEP else []:
            try:
                self.store.delete(t.id)
            except (KeyError, PermissionError):
                pass

    async def regenerate_card_background(self, ctx, chat_id: int, uid: int, text: str, title):
        """Кнопка «🎨 Новий фон» під звичайною листівкою (поза рубриками)."""
        did = self.db.add_draft(uid, "morning", text, text, None, {"title": title if title is not None else None})
        if title is not None:
            self.db.update_draft(did, extra={"title": title})
        await self.do_generate(ctx, chat_id, uid, did)

    # ------------------------------------------------------------ стани введення тексту

    POST_PROMPTS = {
        "oai_key": "🔑 Надішліть ключ OpenAI (починається з sk-…). Повідомлення з ключем я одразу видалю. /cancel — скасувати.",
        "oai_limit": "📊 Скільки зображень на добу дозволити одній людині? Число (0 — без ліміту). /cancel — скасувати.",
        "oai_url": "🌐 Надішліть адресу API (напр. https://api.openai.com/v1). /cancel — скасувати.",
        "oai_model_text": "✏️ Надішліть назву моделі для тексту, напр. gpt-5.4-mini. /cancel — скасувати.",
        "oai_model_image": "✏️ Надішліть назву моделі для зображень, напр. gpt-image-1.5. /cancel — скасувати.",
    }

    async def posts_text(self, ctx, msg, uid: int, state: str, data: dict, text: str) -> bool:
        """Обробляє текст, якщо він належить до рубрик/налаштувань. True — текст оброблено."""
        from .main import kb
        chat_id = msg.chat_id

        if state and state.startswith("post:"):
            kind_id = state.split(":", 1)[1]
            if kind_id not in C.KINDS:
                self.db.set_state(uid, None)
                return False
            src = text.strip()
            if len(src) > MAX_POST_CHARS:
                await msg.reply_text(f"⚠️ Задовгий текст: {len(src)} символів, максимум {MAX_POST_CHARS}.")
                return True
            self.db.set_state(uid, None)
            if C.KINDS[kind_id].compose:
                did = self.db.add_draft(uid, kind_id, src, "")
                await msg.reply_text(
                    "Що зробити з цим текстом?",
                    reply_markup=kb([[("✍️ Скласти привітання", f"p:compose:{did}")],
                                     [("🎨 Оформити мій текст", f"p:format:{did}")]]))
                return True
            await self.make_draft(ctx, chat_id, uid, kind_id, src)
            return True

        if state and state.startswith("sample:"):
            kind_id = state.split(":", 1)[1]
            ok = self.db.add_sample(kind_id, text.strip(), uid)
            n = self.db.samples_count(kind_id)
            await msg.reply_text(("✅ Зразок додано." if ok else "ℹ️ Такий зразок уже є або він надто короткий.")
                                 + f" Усього: {n}.\nНадішліть ще або /cancel, щоб завершити.")
            return True

        if state and state.startswith("editprompt:"):
            did = state.split(":", 1)[1]
            d = self.db.draft(did)
            if not d:
                self.db.set_state(uid, None)
                await msg.reply_text("Чернетка застаріла — почніть рубрику заново.")
                return True
            extra = json.loads(d["extra"] or "{}")
            extra["own"] = True                       # свій промт модель більше не переписує
            if "ip" in extra:                         # назву готового промту більше не показуємо
                extra["ip"] = {k: v for k, v in extra["ip"].items() if k in ("id", "theme", "symbol")}
            self.db.update_draft(did, prompt=text.strip()[:3000], extra=extra)
            self.db.set_state(uid, None)
            await self.send_prompt(ctx, chat_id, did, head="✅ Промт збережено.")
            return True

        if state and state.startswith("kindtags:"):
            kind_id = state.split(":", 1)[1]
            self.post_cfg_set(kind_id, hashtags=" ".join(text.split())[:200])
            self.db.set_state(uid, None)
            t, m = self.posts_screen(uid, "kindcfg", kind_id)
            await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
            return True

        if state and state.startswith("kindtitle:"):
            kind_id = state.split(":", 1)[1]
            val = " ".join(text.split())[:80]
            self.post_cfg_set(kind_id, title="" if val in ("-", "—") else val)
            self.db.set_state(uid, None)
            t, m = self.posts_screen(uid, "kindcfg", kind_id)
            await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
            return True

        if state and state.startswith("kindextra:"):
            kind_id = state.split(":", 1)[1]
            val = " ".join(text.split())[:300]
            self.post_cfg_set(kind_id, extra="" if val in ("-", "—") else val)
            self.db.set_state(uid, None)
            t, m = self.posts_screen(uid, "kindcfg", kind_id)
            await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
            return True

        if state in ("oai_key", "oai_limit", "oai_url", "oai_model_text", "oai_model_image"):
            if not self.is_admin(uid):
                self.db.set_state(uid, None)
                return True
            val = text.strip()
            if state == "oai_key":
                await self._del(ctx, chat_id, msg.message_id)   # ключ не має лишатися в чаті
                if not O.looks_like_key(val):
                    await ctx.bot.send_message(chat_id, "⚠️ Це не схоже на ключ OpenAI. Спробуйте ще раз або /cancel.")
                    return True
                self.oai_set(key=val)
                self.db.set_state(uid, None)
                await ctx.bot.send_message(chat_id, f"🔑 Ключ збережено ({O.mask_key(val)}). Повідомлення з ключем видалено.")
                t, m = self.posts_screen(uid, "oai")
                await ctx.bot.send_message(chat_id, t, parse_mode=ParseMode.HTML, reply_markup=m)
                return True
            if state == "oai_limit":
                if not val.isdigit():
                    await msg.reply_text("Потрібне число. /cancel — скасувати.")
                    return True
                self.oai_set(limit=int(val))
            elif state == "oai_url":
                if not val.startswith(("http://", "https://")):
                    await msg.reply_text("Адреса має починатися з http:// або https://. /cancel — скасувати.")
                    return True
                self.oai_set(base_url=val.rstrip("/"))
            else:
                which = "text" if state.endswith("text") else "image"
                self.oai_set(**{f"{which}_model": val[:80]})
            self.db.set_state(uid, None)
            t, m = self.posts_screen(uid, "oai")
            await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)
            return True

        return False

    async def posts_image(self, ctx, msg, uid: int, state: str, data: dict) -> bool:
        """Фото для «фото дня». True — оброблено тут."""
        if state != "dayphoto":
            return False
        f = msg.photo[-1] if msg.photo else msg.document
        if msg.document and not (msg.document.mime_type or "").startswith("image/"):
            await msg.reply_text("Це не зображення.")
            return True
        tg = await f.get_file()
        raw = bytes(await tg.download_as_bytearray())
        date = dt.date.today()
        target = C.photo_target(date)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            img = await asyncio.to_thread(self._to_jpeg, raw)
            target.write_bytes(img)
        except (OSError, ValueError) as e:
            await msg.reply_text(f"⚠️ Не вдалося зберегти фото: {e}")
            return True
        self.db.set_state(uid, None)
        await msg.reply_text(f"✅ Фото збережено як {target.name} у папці фото — воно підставлятиметься сьогодні.")
        did = (data or {}).get("draft")
        if did and self.db.draft(did):
            await self.send_draft(ctx, msg.chat_id, uid, did)
        return True

    @staticmethod
    def _to_jpeg(raw: bytes) -> bytes:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = ImageOps.exif_transpose(im).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92, optimize=True)
        return buf.getvalue()

    # ------------------------------------------------------------ кнопки

    async def posts_callback(self, q, ctx, uid: int, parts: list[str]) -> bool:
        from .main import kb
        chat_id = q.message.chat_id
        kind = parts[0]

        async def show(name: str, arg: str = ""):
            text, markup = self.posts_screen(uid, name, arg)
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)

        if kind == "pm":
            act = parts[1]
            if act == "go":
                await q.answer()
                return await self.start_kind(ctx, chat_id, uid, parts[2]) or True
            if act == "cfg":
                await q.answer()
                await show("kindcfg", parts[2])
                return True
            if act == "tpl":
                await q.answer()
                await show("kindtpl", parts[2])
                return True
            if act == "settpl":
                self.post_cfg_set(parts[2], template=parts[3])
                await q.answer("Збережено")
                await show("kindcfg", parts[2])
                return True
            if act in ("tags", "title", "extra"):
                await q.answer()
                state = {"tags": "kindtags", "title": "kindtitle", "extra": "kindextra"}[act]
                self.db.set_state(uid, f"{state}:{parts[2]}")
                hint = {"tags": "#️⃣ Надішліть хештеги одним рядком (або «-», щоб прибрати).",
                        "title": "🏷 Надішліть заголовок листівки для цієї рубрики (або «-», щоб без заголовка).",
                        "extra": "💬 Надішліть побажання до оформлення, напр. «коротше, без емодзі» (або «-»)."}[act]
                await ctx.bot.send_message(chat_id, hint + " /cancel — скасувати.")
                return True

        if kind == "sm":
            act = parts[1]
            if act == "list":
                await q.answer()
                await show("samples", parts[2])
                return True
            if act == "add":
                self.db.set_state(uid, f"sample:{parts[2]}")
                await q.answer()
                await ctx.bot.send_message(
                    chat_id, f"🧩 Надсилайте або пересилайте сюди попередні пости рубрики "
                             f"«{C.KINDS[parts[2]].name}» — один пост = одне повідомлення.\n"
                             "Коли закінчите — /cancel.")
                return True
            if act == "del":
                self.db.delete_sample(int(parts[2]))
                await q.answer("Видалено")
                await show("samples", parts[3])
                return True
            if act == "clear":
                n = self.db.clear_samples(parts[2])
                self.db.kv_set(f"style:{parts[2]}", {})
                await q.answer(f"Видалено зразків: {n}")
                await show("samples", parts[2])
                return True
            if act == "style":
                if not self.text_ready():
                    await q.answer("Потрібен ключ OpenAI", show_alert=True)
                    return True
                await q.answer("Аналізую стиль…")
                p = await self.style_profile(parts[2], rebuild=True)
                if not p:
                    await ctx.bot.send_message(chat_id, "Не вдалося описати стиль: потрібно хоча б 2 зразки.")
                await show("samples", parts[2])
                return True

        if kind == "o":
            if not self.is_admin(uid):
                await q.answer("Лише для адміністраторів", show_alert=True)
                return True
            act = parts[1]
            if act == "clearkey":
                self.oai_set(key="")
                await q.answer("Ключ прибрано")
                await show("oai")
                return True
            if act == "models":
                await q.answer()
                await show("models", parts[2])
                return True
            if act == "setmodel":
                self.oai_set(**{f"{parts[2]}_model": parts[3]})
                await q.answer("Збережено")
                await show("models", parts[2])
                return True
            if act == "fetch":
                await q.answer("Питаю OpenAI…")
                try:
                    ms = await self.oai().models()
                except TX.TextError as e:
                    await ctx.bot.send_message(chat_id, f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
                    return True
                cache = {"text": [m for m in ms if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")) and "image" not in m],
                         "image": [m for m in ms if "image" in m]}
                self.db.kv_set("models", cache)
                await show("models", parts[2])
                return True
            if act == "quality":
                if len(parts) > 2:
                    self.oai_set(quality=parts[2])
                    await q.answer("Збережено")
                    await show("oai")
                else:
                    await q.answer()
                    await show("quality")
                return True
            if act == "check":
                await q.answer("Перевіряю…")
                try:
                    res = await self.oai().check()
                except TX.TextError as e:
                    res = f"⚠️ {e}"
                await ctx.bot.send_message(chat_id, esc(res), parse_mode=ParseMode.HTML)
                return True

        if kind == "p":
            act, did = parts[1], parts[2]
            d = self.db.draft(did)
            if not d:
                await q.answer("Чернетка застаріла — почніть рубрику заново.", show_alert=True)
                return True
            if act in ("compose", "format"):
                await q.answer("Готую текст…")
                try:
                    await q.edit_message_reply_markup(None)
                except BadRequest:
                    pass
                await self.make_draft(ctx, chat_id, uid, d["kind"], d["source"], compose=(act == "compose"))
                return True
            if act == "look":
                await q.answer("Міняю фон і стиль…")
                await self.next_look(ctx, chat_id, uid, did)
                return True
            if act == "redo":
                await q.answer("Роблю інший варіант…")
                await self.make_draft(ctx, chat_id, uid, d["kind"], d["source"],
                                      compose=bool(C.KINDS[d["kind"]].compose and not d["source"].strip().endswith(".")))
                return True
            if act == "fmt":
                if not self.text_ready():
                    await q.answer(self.text.not_ready_hint(), show_alert=True)
                    return True
                await q.answer(f"Оформлюю з {self.text_label()}…")
                try:
                    await q.edit_message_reply_markup(None)
                except BadRequest:
                    pass
                await self.make_draft(ctx, chat_id, uid, d["kind"], d["source"], marks=True)
                return True
            if act == "gentext":
                if not self.image_ready():
                    await q.answer(self.images.provider.not_ready_hint(), show_alert=True)
                    return True
                await q.answer("Малюю картинку з текстом…")
                await self.do_generate(ctx, chat_id, uid, did, with_text=True)
                return True
            if act == "src":
                await q.answer()
                await ctx.bot.send_message(chat_id, "📋 Ваш вихідний текст:\n\n" + d["source"][:3500])
                return True
            if act == "sample":
                ok = self.db.add_sample(d["kind"], d["text"], uid)
                await q.answer("✅ Додано у зразки стилю" if ok else "Такий зразок уже є", show_alert=not ok)
                return True
            if act == "prompt":
                await q.answer()
                if not d["prompt"]:
                    wait = await ctx.bot.send_message(chat_id, "⏳ Підбираю промт…")
                    try:
                        await self.ensure_prompt(uid, did)
                    finally:
                        await self._del(ctx, chat_id, wait.message_id)
                await self.send_prompt(ctx, chat_id, did)
                return True
            if act == "newp":
                await q.answer("Підбираю інший промт…")
                await self.ensure_prompt(uid, did, renew=True)
                await self.send_prompt(ctx, chat_id, did)
                return True
            if act == "copy":
                # кнопка Telegram «скопіювати текст» вміщує лише 256 символів, а промт довший,
                # тому надсилаємо його моноширинним блоком: натискання на текст копіює його цілком
                await q.answer("Натисніть на текст промту — він скопіюється")
                prompt = await self.ensure_prompt(uid, did)
                await ctx.bot.send_message(chat_id, "📋 Натисніть на текст, і він скопіюється в буфер:\n\n"
                                                    f"<code>{esc(prompt)}</code>", parse_mode=ParseMode.HTML)
                return True
            if act == "editp":
                self.db.set_state(uid, f"editprompt:{did}")
                await q.answer()
                await ctx.bot.send_message(chat_id, "✏️ Надішліть свій промт (англійською — так краще). /cancel — скасувати.")
                return True
            if act == "gen":
                if not self.image_ready():
                    await q.answer(self.images.provider.not_ready_hint(), show_alert=True)
                    return True
                await q.answer("Малюю…")
                await self.do_generate(ctx, chat_id, uid, did)
                return True
            if act == "short":
                await q.answer("Скорочую…")
                text = d["text"]
                if self.text_ready():
                    try:
                        sys_, user = C.shorten_messages(text, 25)
                        text = C.strip_markup(await self.text.chat(sys_, user, max_tokens=400))
                        self.db.add_usage(uid, "text")
                    except TX.TextError as e:
                        await ctx.bot.send_message(chat_id, f"⚠️ {esc(e)} Скоротив простіше.", parse_mode=ParseMode.HTML)
                        text = C.shorten_local(text, 25)
                else:
                    text = C.shorten_local(text, 25)
                self.db.update_draft(did, text=text)
                await self.send_draft(ctx, chat_id, uid, did, note="✂️ Скорочений варіант.")
                return True
            if act == "card":
                await q.answer("Роблю листівку…")
                short = d["text"]
                if R.count_words(short) > 40:
                    short = " ".join(short.split()[:40])
                await self.send_card(ctx, chat_id, uid, short, self.post_cfg(d["kind"]).get("title") or None)
                return True
            if act == "photo":
                self.db.set_state(uid, "dayphoto", {"draft": did})
                await q.answer()
                await ctx.bot.send_message(
                    chat_id, f"📅 Надішліть фото на сьогодні — я збережу його як "
                             f"{C.photo_target(dt.date.today()).name} у папці фото. /cancel — скасувати.")
                return True

        return False
