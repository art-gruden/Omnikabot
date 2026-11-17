"""Генерація зображень: спільний інтерфейс провайдерів, Gemini та OpenAI, повтори, збереження, історія.

Провайдер вибирається в .env:
    IMAGE_PROVIDER=gemini | openai      (якщо не задано: gemini, коли є GEMINI_API_KEY, інакше openai)
    GEMINI_API_KEY=...
    GEMINI_MODEL=nano-banana-2-1k       (назва-псевдонім або справжній ID моделі; «-1k/-2k/-4k» — розмір)

ImageGenerator.generate() приймає промт, отримує рівно одне зображення, зберігає його у
<DATA_DIR>/generated/ і повертає GeneratedImage: провайдер, модель, промт, шлях до файла.
Невдалі спроби повторюються (до IMAGE_ATTEMPTS разів), кожна має тайм-аут; усе пишеться в лог
без тексту промту й без ключа.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from . import gemini_api as GA
from . import gemini_prompt as GP
from . import image_prompts as IP
from . import openai_api as O

log = logging.getLogger("textcard-bot.images")

GEMINI_BASE_URL_DEFAULT = GA.BASE_URL_DEFAULT
GEMINI_ALIASES = GA.ALIASES                 # назви-псевдоніми -> ID моделі (спільні з текстом)
GEMINI_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_num(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


class ImageGenError(RuntimeError):
    """Помилка генерації з текстом для користувача.

    retryable — чи має сенс повторити спробу; fallback_ok — чи можна підставити запасну картинку."""

    def __init__(self, message: str, *, retryable: bool = False, fallback_ok: bool = True):
        super().__init__(message)
        self.retryable = retryable
        self.fallback_ok = fallback_ok


@dataclass
class GeneratedImage:
    provider: str                 # gemini | openai | backup
    model: str
    prompt: str                   # промт, який пішов провайдеру (після адаптера)
    source_prompt: str            # промт до адаптера
    path: str                     # де файл збережено локально
    mime: str
    width: int
    height: int
    attempts: int
    kind: str = ""                # рубрика
    prompt_id: str = ""           # готовий промт: calendar_2…
    category: str = ""
    variation: str = ""
    draw_text: str = ""           # текст, який мала намалювати модель (дата, афірмація)
    fallback: bool = False        # True — це запасна картинка, а не нова генерація
    error: str = ""               # чому знадобилась запасна
    created: float = field(default_factory=time.time)
    data: bytes = field(default=b"", repr=False)

    def meta(self) -> dict:
        d = asdict(self)
        d.pop("data")
        return d


# ------------------------------------------------------------------ провайдери

class ImageProvider:
    """Один виклик = одна спроба = одне зображення. Повтори робить ImageGenerator."""
    name = ""
    label = ""                    # як назвати користувачеві: «ChatGPT», «Gemini»
    attempts = 3                  # скільки спроб робить генератор
    timeout: float | None = None  # тайм-аут однієї спроби, с (None — провайдер стежить сам)
    post_size = (1536, 1024)      # розмір ілюстрації до посту, який просимо в моделі

    @property
    def model(self) -> str:
        raise NotImplementedError

    def ready(self) -> bool:
        raise NotImplementedError

    def not_ready_hint(self) -> str:
        raise NotImplementedError

    def adapt(self, prompt: str, **ctx) -> str:
        """Підготовка промту під модель. Типово — без змін."""
        return prompt

    async def generate(self, prompt: str, width: int, height: int, quality: str = "") -> tuple[bytes, str]:
        """Повертає (байти зображення, mime). Помилки — ImageGenError."""
        raise NotImplementedError


class OpenAIImageProvider(ImageProvider):
    """Попередня поведінка без змін: клієнт OpenAI сам повторює запит при 429/5xx."""
    name, label = "openai", "ChatGPT"
    attempts = 1                  # повтори вже є всередині OpenAIClient._post

    def __init__(self, cfg: Callable[[], dict]):
        self._cfg = cfg           # ключ і модель задаються в меню бота, тож читаємо їх на кожен виклик

    @property
    def model(self) -> str:
        return self._cfg()["image_model"]

    def ready(self) -> bool:
        return bool(self._cfg()["key"])

    def not_ready_hint(self) -> str:
        return "Потрібен ключ OpenAI: меню → Адміністрування → OpenAI"

    async def generate(self, prompt: str, width: int, height: int, quality: str = "") -> tuple[bytes, str]:
        c = self._cfg()
        client = O.OpenAIClient(c["key"], c["base_url"], c["text_model"], c["image_model"])
        try:
            data = await client.image(prompt, O.pick_size(width, height), quality=quality or c.get("quality", "high"))
        except O.OpenAIError as e:
            raise ImageGenError(str(e)) from None
        return data, "image/png"


def parse_gemini_model(value: str, default_size: str = "1K") -> tuple[str, str]:
    """«nano-banana-2-1k» -> («gemini-3.1-flash-image», «1K»). Справжній ID моделі лишається як є."""
    return GA.parse_model(value, default_size)


def nearest_ratio(width: int, height: int) -> str:
    import math
    r = width / max(1, height)
    return min(GEMINI_RATIOS, key=lambda s: abs(math.log(r / (int(s.split(":")[0]) / int(s.split(":")[1])))))


class GeminiImageProvider(ImageProvider):
    name, label = "gemini", "Gemini"
    post_size = (1376, 768)       # 16:9

    def __init__(self):
        self.api_key = GA.api_key()
        self.base_url = GA.base_url()
        self._model, self.image_size = GA.parse_model(_env("GEMINI_MODEL"), _env("GEMINI_IMAGE_SIZE") or "1K")
        self.api = GA.api_mode()
        self.timeout = _env_num("GEMINI_TIMEOUT", 120)
        self.attempts = max(1, int(_env_num("IMAGE_ATTEMPTS", 3)))

    @property
    def model(self) -> str:
        return self._model

    def ready(self) -> bool:
        return bool(self.api_key)

    def not_ready_hint(self) -> str:
        return "Потрібен ключ Gemini: GEMINI_API_KEY у .env (./deploy.sh gemini)"

    def adapt(self, prompt: str, **ctx) -> str:
        return GP.adapt(prompt, **ctx)

    def request(self, prompt: str, width: int, height: int) -> tuple[str, dict]:
        ratio = nearest_ratio(width, height)
        if self.api == "interactions":
            return f"{self.base_url}/interactions", {
                "model": self._model,
                "input": [{"type": "text", "text": prompt}],
                "response_format": {"type": "image", "mime_type": "image/png",
                                    "aspect_ratio": ratio, "image_size": self.image_size},
            }
        return f"{self.base_url}/models/{self._model}:generateContent", {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": ratio, "imageSize": self.image_size}},
        }

    async def generate(self, prompt: str, width: int, height: int, quality: str = "") -> tuple[bytes, str]:
        if not self.ready():
            raise ImageGenError("Ключ Gemini не заданий: додайте GEMINI_API_KEY у .env.", fallback_ok=False)
        url, payload = self.request(prompt, width, height)
        try:
            body = await GA.post(url, payload, timeout=self.timeout, model=self._model,
                                 interactions=self.api == "interactions")
        except GA.GeminiError as e:
            raise ImageGenError(str(e), retryable=e.retryable) from None
        found = GA.find_images(body)
        if not found:
            reason = GA.finish_reason(body)
            raise ImageGenError("Gemini не повернув зображення" + (f" ({reason})." if reason else "."), retryable=True)
        data64, mime = found[-1]                               # рівно одне: останнє, остаточне зображення
        try:
            return base64.b64decode(data64), mime
        except Exception:  # noqa: BLE001
            raise ImageGenError("Не вдалося прочитати зображення від Gemini.", retryable=True) from None


def provider_name() -> str:
    name = _env("IMAGE_PROVIDER").lower()
    if name in ("gemini", "openai"):
        return name
    if name:
        log.warning("невідомий IMAGE_PROVIDER=%s — використовую openai", name)
        return "openai"
    return "gemini" if _env("GEMINI_API_KEY") else "openai"


def make_provider(openai_cfg: Callable[[], dict]) -> ImageProvider:
    return GeminiImageProvider() if provider_name() == "gemini" else OpenAIImageProvider(openai_cfg)


# ------------------------------------------------------------------ генератор

class ImageGenerator:
    """Промт -> одне збережене зображення з метаданими. Повтори, тайм-аути, лог, історія."""

    def __init__(self, db, data_dir: Path, openai_cfg: Callable[[], dict]):
        self.db = db
        self.dir = Path(_env("GENERATED_DIR") or Path(data_dir) / "generated")
        self.keep = int(_env_num("GENERATED_IMAGES_KEEP", 200))
        self.retry_delay = _env_num("IMAGE_RETRY_DELAY", 2.0)
        self.provider = make_provider(openai_cfg)
        log.info("зображення малює %s (%s)", self.provider.name, self.provider.model)

    # ---------- історія: що вже малювали в цій рубриці
    def _history(self, kind: str) -> tuple[list[str], list[str]]:
        rows = self.db.image_recent(kind, 12) if kind else []
        variations = [r["variation"] for r in rows if r["variation"]]
        scenes = []
        for r in rows[:5]:
            tag = IP.VISUAL_TAGS.get(r["prompt_id"], "")
            if tag:
                scenes.append(tag + (f" ({r['variation']})" if r["variation"] else ""))
        return variations, list(dict.fromkeys(scenes))

    async def generate(self, prompt: str, *, width: int, height: int, uid: int = 0, kind: str = "",
                       prompt_id: str = "", category: str = "", quality: str = "",
                       draw_text: str = "", draw_style: str = "") -> GeneratedImage:
        p = self.provider
        if not p.ready():
            raise ImageGenError(p.not_ready_hint() + ".", fallback_ok=False)
        used, scenes = self._history(kind)
        variation = GP.pick_variation(category, used) if p.name == "gemini" else ""
        final = p.adapt(prompt, category=category, date=dt.date.today(), variation=variation, avoid=scenes,
                        draw_text=draw_text, draw_style=draw_style)
        sha = hashlib.sha256(final.encode()).hexdigest()[:12]
        last: ImageGenError | None = None
        for attempt in range(1, p.attempts + 1):
            t0 = time.time()
            try:
                data, mime = await asyncio.wait_for(p.generate(final, width, height, quality),
                                                    timeout=p.timeout + 15 if p.timeout else None)
                w, h = await asyncio.to_thread(self._check, data)
            except asyncio.TimeoutError:
                last = ImageGenError(f"{p.label} не відповів вчасно.", retryable=True)
            except ImageGenError as e:
                last = e
            else:
                path = await asyncio.to_thread(self._save, data, mime, kind, prompt_id)
                log.info("user %s: %s/%s намалював %dx%d (%d КБ) за %.1f с, спроба %d, промт %s (%d симв.) -> %s",
                         uid, p.name, p.model, w, h, len(data) // 1024, time.time() - t0, attempt, sha, len(final),
                         path.name)
                img = GeneratedImage(provider=p.name, model=p.model, prompt=final, source_prompt=prompt,
                                     path=str(path), mime=mime, width=w, height=h, attempts=attempt, kind=kind,
                                     prompt_id=prompt_id, category=category, variation=variation,
                                     draw_text=draw_text, data=data)
                self.db.add_image_log(uid, kind, prompt_id, category, p.name, p.model, variation, "ok", attempt,
                                      str(path), sha)
                return img
            log.warning("user %s: %s/%s, спроба %d з %d не вдалася за %.1f с: %s", uid, p.name, p.model, attempt,
                        p.attempts, time.time() - t0, last)
            if not last.retryable or attempt == p.attempts:
                break
            await asyncio.sleep(self.retry_delay * attempt)
        self.db.add_image_log(uid, kind, prompt_id, category, p.name, p.model, variation, "failed", attempt, "", sha)
        raise last

    def backup(self, source: Path, *, uid: int, kind: str, prompt: str, error: str) -> GeneratedImage:
        """Запасна картинка замість невдалої генерації (файл уже лежить локально)."""
        data = source.read_bytes()
        w, h = self._check(data)
        self.db.add_image_log(uid, kind, "", "", "backup", "", "", "backup", 0, str(source), "")
        log.info("user %s: генерація не вдалася, запасна картинка %s", uid, source.name)
        return GeneratedImage(provider="backup", model="", prompt=prompt, source_prompt=prompt, path=str(source),
                              mime="image/jpeg", width=w, height=h, attempts=0, kind=kind, fallback=True,
                              error=error, data=data)

    @staticmethod
    def _check(data: bytes) -> tuple[int, int]:
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                return im.size
        except Exception:  # noqa: BLE001
            raise ImageGenError("Провайдер повернув файл, який не є зображенням.", retryable=True) from None

    def _save(self, data: bytes, mime: str, kind: str, prompt_id: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        stem = "_".join(x for x in (dt.date.today().isoformat(), kind, prompt_id) if x)
        path = self.dir / f"{stem}_{time.strftime('%H%M%S')}{EXT.get(mime, '.png')}"
        n = 1
        while path.exists():
            n += 1
            path = path.with_name(f"{stem}_{time.strftime('%H%M%S')}-{n}{EXT.get(mime, '.png')}")
        path.write_bytes(data)
        files = sorted((f for f in self.dir.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
        for old in files[:-self.keep] if self.keep > 0 and len(files) > self.keep else []:
            try:
                old.unlink()
            except OSError:
                pass
        return path


# ------------------------------------------------------------------ перевірка з командного рядка

def _cli() -> None:
    """python -m bot.image_gen [--category nature] [--size 1376x768] "A calm misty lake at dawn"

    Малює одне зображення поточним провайдером із .env, зберігає його й друкує метадані.
    У Docker: docker compose exec bot python -m bot.image_gen "…"
    """
    import argparse

    from .db import DB

    ap = argparse.ArgumentParser(description="Пробна генерація одного зображення провайдером із .env")
    ap.add_argument("prompt")
    ap.add_argument("--category", default="", help="calendar, cozy, nature, daily_energy, intention, affirmation, "
                                                   "special, heart, birthday")
    ap.add_argument("--size", default="1376x768")
    ap.add_argument("--draw-text", default="", help="текст, який модель має намалювати в кадрі")
    ap.add_argument("--show-prompt", action="store_true", help="надрукувати промт після адаптера й не малювати")
    a = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data_dir = Path(_env("DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    cfg = {"key": _env("OPENAI_API_KEY"), "base_url": O.DEFAULT_BASE_URL, "text_model": O.DEFAULT_TEXT_MODEL,
           "image_model": O.DEFAULT_IMAGE_MODEL, "quality": "high"}
    gen = ImageGenerator(DB(data_dir / "bot.sqlite3"), data_dir, lambda: cfg)
    if a.show_prompt:
        print(gen.provider.adapt(a.prompt, category=a.category, date=dt.date.today(), variation="", avoid=[],
                                 draw_text=a.draw_text))
        return
    w, h = (int(x) for x in a.size.lower().split("x"))
    try:
        img = asyncio.run(gen.generate(a.prompt, width=w, height=h, kind="cli", category=a.category,
                                       draw_text=a.draw_text))
    except ImageGenError as e:
        raise SystemExit(f"Не вдалося: {e}")
    print(json.dumps(img.meta(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
