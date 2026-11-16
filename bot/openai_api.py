"""Клієнт OpenAI API: текст (chat/completions) і зображення (images/generations).

Ключ і моделі задаються в меню бота й зберігаються в SQLite, тому клієнт створюється
на кожен виклик із поточними значеннями. Працює і з сумісними API (свій OPENAI_BASE_URL).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re

import httpx

log = logging.getLogger("textcard-bot.openai")

DEFAULT_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEFAULT_TEXT_MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-5.4-mini")
DEFAULT_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1.5")

# розміри, які приймає images/generations; під потрібне співвідношення вибирається найближчий,
# далі картинка обрізається під формат листівки
IMAGE_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
TEXT_TIMEOUT = float(os.environ.get("OPENAI_TEXT_TIMEOUT", "90"))
IMAGE_TIMEOUT = float(os.environ.get("OPENAI_IMAGE_TIMEOUT", "300"))


class OpenAIError(RuntimeError):
    """Помилка з текстом, готовим для показу користувачеві."""


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "не задано"
    return f"{key[:6]}…{key[-4:]}" if len(key) > 14 else "задано"


def looks_like_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-.]{20,200}", (key or "").strip()))


def pick_size(width: int, height: int) -> str:
    r = width / max(1, height)
    if r > 1.2:
        return IMAGE_SIZES["landscape"]
    if r < 0.83:
        return IMAGE_SIZES["portrait"]
    return IMAGE_SIZES["square"]


def _explain(status: int, body: str) -> str:
    msg = ""
    try:
        import json
        err = json.loads(body).get("error") or {}
        msg = err.get("message") or ""
    except Exception:  # noqa: BLE001
        msg = (body or "")[:300]
    if status == 401:
        return "OpenAI не прийняв ключ (401). Перевірте ключ у меню: 🛠 Адміністрування → 🤖 OpenAI."
    if status == 403:
        return f"OpenAI відмовив у доступі (403). Можливо, модель недоступна для цього ключа. {msg}".strip()
    if status == 429:
        return ("OpenAI: перевищено ліміт запитів або вичерпано кошти на рахунку (429). "
                "Спробуйте за хвилину або поповніть баланс.")
    if status == 400 and ("content_policy" in body or "safety" in body.lower()):
        return f"OpenAI відхилив запит через правила вмісту: {msg}"
    if status >= 500:
        return f"OpenAI тимчасово недоступний ({status}). Спробуйте ще раз."
    return f"OpenAI повернув помилку {status}: {msg}"


class OpenAIClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 text_model: str = DEFAULT_TEXT_MODEL, image_model: str = DEFAULT_IMAGE_MODEL):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.text_model = text_model or DEFAULT_TEXT_MODEL
        self.image_model = image_model or DEFAULT_IMAGE_MODEL

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    async def _post(self, path: str, payload: dict, timeout: float, retries: int = 2) -> dict:
        if not self.ready:
            raise OpenAIError("Ключ OpenAI не заданий: 🛠 Адміністрування → 🤖 OpenAI → Ключ API.")
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = ""
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20)) as cli:
            for attempt in range(retries + 1):
                try:
                    r = await cli.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    last = "OpenAI не відповів вчасно. Спробуйте ще раз."
                except httpx.HTTPError as e:
                    last = f"Не вдалося звернутися до OpenAI: {type(e).__name__}."
                else:
                    if r.status_code < 300:
                        return r.json()
                    body = r.text
                    # невідомий параметр — приберемо й повторимо без нього
                    m = re.search(r"Unsupported parameter: '([a-z_]+)'|Unrecognized request argument.*?'([a-z_]+)'", body)
                    if r.status_code == 400 and m:
                        bad = m.group(1) or m.group(2)
                        if bad in payload:
                            payload.pop(bad, None)
                            log.info("OpenAI: прибрано параметр %s і повтор", bad)
                            continue
                    if r.status_code == 400 and "max_tokens" in body and "max_completion_tokens" not in payload and "max_tokens" in payload:
                        payload["max_completion_tokens"] = payload.pop("max_tokens")
                        continue
                    last = _explain(r.status_code, body)
                    if r.status_code not in (429, 500, 502, 503, 504):
                        raise OpenAIError(last)
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise OpenAIError(last or "OpenAI недоступний.")

    async def chat(self, system: str, user: str, *, max_tokens: int = 1200, model: str | None = None) -> str:
        data = await self._post("/chat/completions", {
            "model": model or self.text_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens,
        }, TEXT_TIMEOUT)
        try:
            choice = data["choices"][0]
            text = (choice["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            raise OpenAIError("OpenAI повернув несподівану відповідь.")
        if not text:
            raise OpenAIError("OpenAI повернув порожню відповідь. Спробуйте ще раз або виберіть іншу модель.")
        return text

    async def image(self, prompt: str, size: str = "1536x1024", *, model: str | None = None,
                    quality: str = "high") -> bytes:
        payload = {"model": model or self.image_model, "prompt": prompt[:32000], "size": size, "n": 1}
        if quality and quality != "auto":
            payload["quality"] = quality
        data = await self._post("/images/generations", payload, IMAGE_TIMEOUT, retries=1)
        try:
            item = data["data"][0]
        except (KeyError, IndexError, TypeError):
            raise OpenAIError("OpenAI не повернув зображення.")
        if item.get("b64_json"):
            try:
                return base64.b64decode(item["b64_json"])
            except Exception:  # noqa: BLE001
                raise OpenAIError("Не вдалося прочитати зображення від OpenAI.")
        if item.get("url"):  # старі моделі
            async with httpx.AsyncClient(timeout=60) as cli:
                r = await cli.get(item["url"])
                if r.status_code < 300:
                    return r.content
        raise OpenAIError("OpenAI не повернув зображення.")

    async def models(self) -> list[str]:
        if not self.ready:
            raise OpenAIError("Ключ OpenAI не заданий.")
        url = f"{self.base_url}/models"
        async with httpx.AsyncClient(timeout=30) as cli:
            try:
                r = await cli.get(url, headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError:
                raise OpenAIError("Не вдалося отримати список моделей.")
        if r.status_code >= 300:
            raise OpenAIError(_explain(r.status_code, r.text))
        try:
            return sorted({m["id"] for m in r.json().get("data", []) if isinstance(m.get("id"), str)})
        except (AttributeError, TypeError):
            raise OpenAIError("Несподіваний список моделей.")

    async def check(self) -> str:
        """Коротка перевірка ключа: повертає рядок для показу користувачеві."""
        ms = await self.models()
        txt = [m for m in ms if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")) and "image" not in m]
        img = [m for m in ms if "image" in m]
        return (f"✅ Ключ працює. Моделей доступно: {len(ms)}"
                f"\nТекстових: {len(txt)} · для зображень: {len(img)}")
